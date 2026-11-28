"""The phone fallback for a secret read, raced against the Touch ID prompt.

Touch ID shows at once. The keyguard-passkey client that `make install` copies also asks the unlock service
behind the store (KEYGUARD_STORE_URL), where the web terminal and the phone can approve the read. It asks
at once when nobody sits at a web terminal (presence.py reads that once, when the race starts), else only
once `Timing.delay` passes without an answer. The first answer wins and the other side is stopped;
the client withdraws its request on SIGTERM. A Touch ID approval for a caller with a session also runs the
client's `pending --clear-expired`, as an answer on the phone settles that session's expired requests. A Touch ID
cancel is a no and ends the read, while a failing client never ends a prompt someone may still answer. A read that
fails with no phone request behind it is reported once (failure_report.py).
"""
from __future__ import annotations

import queue
import shlex
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from . import encoding, failure_report, keyguard_cli, presence
from .caller import Caller
from .config import (
    PHONE_CLIENT,
    PHONE_DELAY,
    PHONE_PATH,
    PHONE_REQUESTER,
    PHONE_SERVICE,
    PHONE_SIGNING_KEY,
    PHONE_STOP_GRACE,
    PHONE_WAIT,
    SUBPROCESS_TIMEOUT,
)
from .failure_report import ReadFailure, Reason
from .keyguard_cli import CliResult
from .presence import Presence

_EXPORT = "export"
_PANE_ENV = "TMUX_PANE"
_SESSION_ENV = "KEYGUARD_SESSION"
_TITLE_ENV = "KEYGUARD_TITLE"
_HOLD_ENV = "KEYGUARD_PASSKEY_HOLD_SECONDS"
_REQUEST_SENT = "Approve on your phone:"
_CLEAR_EXPIRED_TIMEOUT = 10


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
class Outside:
    """What a read asks or tells beyond its two processes, so a test can stand in for both."""
    presence_reader: Callable[[], Presence] = presence.current
    report_failure: Callable[[ReadFailure], None] = failure_report.report


@dataclass(frozen=True)
class PhoneClient:
    client: Path
    signing_key: Path
    service: str

    def argv(self, keys: list[str]) -> list[str]:
        return [str(self.client), "--service", self.service, "--requester", PHONE_REQUESTER,
                "--signing-key", str(self.signing_key), "get", "--format", "shell", "--", *keys]

    def clear_expired_argv(self, session: str) -> list[str]:
        return [str(self.client), "pending", f"--clear-expired={session}"]


def configured(client: Path | None = PHONE_CLIENT, signing_key: Path | None = PHONE_SIGNING_KEY,
               service: str = PHONE_SERVICE) -> PhoneClient | None:
    if client is None or signing_key is None or not service:
        return None
    if not (client.is_file() and signing_key.is_file()):
        _log(f"phone fallback off: {client} or {signing_key} is missing")
        return None
    return PhoneClient(client, signing_key, service)


def client_env(caller: Caller) -> dict[str, str]:
    """launchd hands the server a bare environment; the client needs Homebrew's age and openssl@3, starts no
    hold agent, and records the asking session, title and pane (TMUX_PANE, as when it runs in that pane)."""
    env = {"HOME": str(Path.home()), "PATH": PHONE_PATH, "PYTHONIOENCODING": "utf-8", "LANG": "en_US.UTF-8",
           _HOLD_ENV: "0", _SESSION_ENV: caller.session or "", _TITLE_ENV: caller.title or ""}
    return env if caller.pane is None else {**env, _PANE_ENV: caller.pane}


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


def reached_service(phone: CliResult) -> bool:
    """The client prints its approve link once the unlock service holds the request, which its exit code cannot say."""
    return _REQUEST_SENT in phone.stderr


def touch_id_alone(keys: list[str], caller: Caller, cache_duration: int | None = None,
                   outside: Outside = Outside()) -> CliResult:
    """The read while the phone fallback is off: no phone request exists, so a failure is reported from here."""
    result = keyguard_cli.get(*keys, cache_duration=cache_duration, caller=caller.label)
    if not ends_read(result):
        outside.report_failure(ReadFailure(Reason.PHONE_OFF, len(keys), caller.session))
    return result


def race(keys: list[str], caller: Caller, client: PhoneClient, cache_duration: int | None = None,
         timing: Timing = DEFAULT_TIMING, outside: Outside = Outside()) -> CliResult:
    return _Race(keys, caller, client, cache_duration, timing, outside).run()


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
                 timing: Timing, outside: Outside) -> None:
        self._keys = keys
        self._caller = caller
        self._client = client
        self._timing = timing
        self._exits: queue.Queue[_Exit] = queue.Queue()
        self._started = time.monotonic()
        prompt = keyguard_cli.get_args(*keys, cache_duration=cache_duration, caller=caller.label)
        self._touch_id = _Child(Side.TOUCH_ID, keyguard_cli.command(prompt), None, self._exits,
                                timing.touch_id_timeout)
        self._phone_delay = timing.delay if outside.presence_reader().attended else 0.0
        self._report_failure = outside.report_failure
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
        due = self._phone_delay if self._phone is None else self._timing.deadline
        return max(0.0, self._started + due - time.monotonic())

    def _on_due(self) -> CliResult | None:
        if self._phone is None:
            self._ask_phone()
            return None
        return CliResult(rc=-1, stdout="", stderr="neither Touch ID nor the phone answered in time", timed_out=True)

    def _on_touch_id(self, result: CliResult) -> CliResult | None:
        if ends_read(result):
            session = self._caller.session
            if result.ok and session:
                threading.Thread(target=_clear_expired, args=(self._client, self._caller, session), daemon=True).start()
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
        if not reached_service(phone):
            self._report_failure(ReadFailure(Reason.NOT_REACHED, len(self._keys), self._caller.session))
        return CliResult(rc=touch_id.rc, stdout="", stderr=f"{touch_id.stderr.strip()}; phone: {phone.stderr.strip()}",
                         timed_out=touch_id.timed_out)

    def _ask_phone(self) -> None:
        argv = self._client.argv(self._keys)
        self._phone = _Child(Side.PHONE, argv, client_env(self._caller), self._exits, self._timing.deadline)

    def _stop_in_background(self, child: _Child) -> None:
        """Answers the caller at once; the client then has its grace to withdraw its request."""
        threading.Thread(target=child.stop, args=(self._timing.stop_grace,), daemon=True).start()


def _clear_expired(client: PhoneClient, caller: Caller, session: str) -> None:
    """Settles the session's expired phone requests as a phone answer would; a client before the flag refuses."""
    try:
        ended = subprocess.run(client.clear_expired_argv(session), env=client_env(caller), capture_output=True,
                               text=True, timeout=_CLEAR_EXPIRED_TIMEOUT, check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        _log(f"expired phone requests stay listed: {error}")
        return
    if ended.returncode != 0:
        _log(f"expired phone requests stay listed: {ended.stderr.strip()}")


def _log(message: str) -> None:
    print(f"[keyguard] {message}", file=sys.stderr)
