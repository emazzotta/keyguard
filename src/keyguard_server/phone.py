"""The phone fallback for a secret read, raced against the Touch ID prompt.

Touch ID shows at once. Unless someone answers it within `Timing.delay`, the unlock client that
`make install` copies from personal-infra (apps/devbox/unlock) also asks the unlock service, where the
web terminal and the phone can approve the read. The first answer wins and the other side is stopped;
the client withdraws its request on SIGTERM. A Touch ID cancel is a no and ends the read, while a
failing client never ends a prompt someone may still answer.
"""
from __future__ import annotations

import queue
import shlex
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from . import encoding, keyguard_cli
from .caller import Caller
from .config import (
    PHONE_CLIENT,
    PHONE_DELAY,
    PHONE_HOST,
    PHONE_PATH,
    PHONE_SIGNING_KEY,
    PHONE_STOP_GRACE,
    PHONE_WAIT,
    SUBPROCESS_TIMEOUT,
)
from .keyguard_cli import CliResult

_EXPORT = "export"
_PANE_ENV = "TMUX_PANE"


class Side(Enum):
    TOUCH_ID = "Touch ID"
    PHONE = "phone client"


@dataclass(frozen=True)
class Timing:
    delay: float = PHONE_DELAY
    deadline: float = PHONE_DELAY + PHONE_WAIT
    touch_id_timeout: float = SUBPROCESS_TIMEOUT
    stop_grace: float = PHONE_STOP_GRACE


DEFAULT_TIMING = Timing()


@dataclass(frozen=True)
class PhoneClient:
    client: Path
    signing_key: Path

    def argv(self, keys: list[str], caller: Caller) -> list[str]:
        """An empty session and title stop the client from guessing a caller of its own."""
        return [str(self.client), "--host", PHONE_HOST, "--signing-key", str(self.signing_key),
                "--hold-seconds", "0", "--session", caller.session or "", "--title", caller.title or "",
                "--", *keys]


def configured(client: Path | None = PHONE_CLIENT, signing_key: Path | None = PHONE_SIGNING_KEY) -> PhoneClient | None:
    if client is None or signing_key is None:
        return None
    if not (client.is_file() and signing_key.is_file()):
        _log(f"phone fallback off: {client} or {signing_key} is missing")
        return None
    return PhoneClient(client, signing_key)


def client_env(pane: str | None = None) -> dict[str, str]:
    """launchd hands the server a bare environment; the client needs Homebrew's age and openssl@3, and
    records the asking pane from TMUX_PANE, as it does when it runs in that pane."""
    env = {"HOME": str(Path.home()), "PATH": PHONE_PATH, "PYTHONIOENCODING": "utf-8", "LANG": "en_US.UTF-8"}
    return env if pane is None else {**env, _PANE_ENV: pane}


def parse_exports(stdout: str, keys: list[str]) -> dict[str, str] | None:
    """The values in the client's `export NAME='value'` lines, or None unless they name exactly `keys`."""
    try:
        tokens = shlex.split(stdout)
    except ValueError:
        return None
    words, assignments = tokens[::2], tokens[1::2]
    if not words or len(words) != len(assignments) or any(word != _EXPORT for word in words):
        return None
    pairs = [assignment.partition("=") for assignment in assignments]
    if not all(separator for _, separator, _ in pairs):
        return None
    values = {name: value for name, _, value in pairs}
    return values if len(values) == len(pairs) and set(values) == set(keys) else None


def ends_read(touch_id: CliResult) -> bool:
    """Touch ID settles the read when it gave the values or the owner cancelled the prompt."""
    return touch_id.ok or touch_id.touch_id_cancelled


def race(keys: list[str], caller: Caller, client: PhoneClient, cache_duration: int | None = None,
         timing: Timing = DEFAULT_TIMING) -> CliResult:
    return _Race(keys, caller, client, cache_duration, timing).run()


@dataclass(frozen=True)
class _Exit:
    side: Side
    result: CliResult


class _Child:
    """One side of the race: a process whose result lands on the shared queue once it ends."""

    def __init__(self, side: Side, argv: list[str], env: dict[str, str] | None,
                 exits: queue.Queue[_Exit], timeout: float) -> None:
        self._side = side
        self._ended = threading.Event()
        try:
            self._process: subprocess.Popen[str] | None = subprocess.Popen(
                argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, errors="replace", env=env)
        except OSError as error:
            self._process = None
            self._ended.set()
            exits.put(_Exit(side, CliResult(rc=-1, stdout="", stderr=f"{side.value} did not start: {error}",
                                            not_found=True)))
            return
        threading.Thread(target=self._collect, args=(self._process, exits, timeout), daemon=True).start()

    def _collect(self, process: subprocess.Popen[str], exits: queue.Queue[_Exit], timeout: float) -> None:
        try:
            stdout, stderr = process.communicate(timeout=timeout)
            result = CliResult(rc=process.returncode, stdout=stdout, stderr=stderr)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
            result = CliResult(rc=-1, stdout="", stderr=f"{self._side.value} timed out", timed_out=True)
        self._ended.set()
        exits.put(_Exit(self._side, result))

    def stop(self, grace: float) -> None:
        """SIGTERM, then SIGKILL once `grace` passes without an exit."""
        if self._process is None or self._ended.is_set():
            return
        self._process.terminate()
        if not self._ended.wait(grace):
            self._process.kill()


class _Race:
    def __init__(self, keys: list[str], caller: Caller, client: PhoneClient, cache_duration: int | None,
                 timing: Timing) -> None:
        self._keys = keys
        self._caller = caller
        self._client = client
        self._timing = timing
        self._exits: queue.Queue[_Exit] = queue.Queue()
        self._started = time.monotonic()
        prompt = keyguard_cli.get_args(*keys, cache_duration=cache_duration, caller=caller.label)
        self._touch_id = _Child(Side.TOUCH_ID, keyguard_cli.command(prompt), None, self._exits,
                                timing.touch_id_timeout)
        self._phone: _Child | None = None
        self._touch_id_failure: CliResult | None = None
        self._phone_failure: CliResult | None = None

    def run(self) -> CliResult:
        try:
            outcome = None
            while outcome is None:
                outcome = self._step()
            return outcome
        finally:
            self._stop_in_background(self._touch_id)
            if self._phone is not None:
                self._stop_in_background(self._phone)

    def _step(self) -> CliResult | None:
        try:
            ended = self._exits.get(timeout=self._until_due())
        except queue.Empty:
            return self._on_due()
        if ended.side is Side.TOUCH_ID:
            return self._on_touch_id(ended.result)
        return self._on_phone(ended.result)

    def _until_due(self) -> float:
        due = self._timing.delay if self._phone is None else self._timing.deadline
        return max(0.0, self._started + due - time.monotonic())

    def _on_due(self) -> CliResult | None:
        if self._phone is None:
            self._ask_phone()
            return None
        return CliResult(rc=-1, stdout="", stderr="neither Touch ID nor the phone answered in time", timed_out=True)

    def _on_touch_id(self, result: CliResult) -> CliResult | None:
        if ends_read(result):
            return result
        self._touch_id_failure = result
        if self._phone is None:
            self._ask_phone()
            return None
        return self._both_failed() if self._phone_failure else None

    def _on_phone(self, result: CliResult) -> CliResult | None:
        values = parse_exports(result.stdout, self._keys) if result.ok else None
        if values is not None:
            answer: dict[str, str | None] = dict(values)
            return CliResult(rc=0, stdout=encoding.format_response(self._keys, answer), stderr="")
        self._phone_failure = result
        _log(f"phone fallback failed: {result.stderr.strip() or 'no readable answer'}")
        return self._both_failed() if self._touch_id_failure else None

    def _both_failed(self) -> CliResult:
        touch_id, phone = self._touch_id_failure, self._phone_failure
        assert touch_id is not None and phone is not None
        return CliResult(rc=touch_id.rc, stdout="", stderr=f"{touch_id.stderr.strip()}; phone: {phone.stderr.strip()}",
                         timed_out=touch_id.timed_out)

    def _ask_phone(self) -> None:
        argv = self._client.argv(self._keys, self._caller)
        self._phone = _Child(Side.PHONE, argv, client_env(self._caller.pane), self._exits, self._timing.deadline)

    def _stop_in_background(self, child: _Child) -> None:
        """Answers the caller at once; the client then has its grace to withdraw its request."""
        threading.Thread(target=child.stop, args=(self._timing.stop_grace,), daemon=True).start()


def _log(message: str) -> None:
    print(f"[keyguard] {message}", file=sys.stderr)
