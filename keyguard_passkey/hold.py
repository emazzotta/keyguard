"""Hold released values in memory for a short time, so the next command for them needs no approval.

A per-user agent listens on loopback and answers only a caller that knows the token in its address file,
which only that user can read. It writes values nowhere and exits once it holds nothing.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import secrets
import socket
import socketserver
import subprocess
import sys
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

DEFAULT_HOLD_SECONDS: Final = 600
MAX_HOLD_SECONDS: Final = 600
ADDRESS_FILE: Final = "hold.json"
BIND_ADDRESS: Final = "127.0.0.1"
TOKEN_BYTES: Final = 32
PRIVATE_FILE_MODE: Final = 0o600
PRIVATE_DIR_MODE: Final = 0o700
MAX_MESSAGE_BYTES: Final = 1 << 20
REQUEST_TIMEOUT_SECONDS: Final = 2.0
START_TIMEOUT_SECONDS: Final = 5.0
START_POLL_SECONDS: Final = 0.05
AGENT_TICK_SECONDS: Final = 1.0
EMPTY_GRACE_SECONDS: Final = 10.0
STATE_DIR_FLAG: Final = "--state-dir"
AGENT_MODULE: Final = "keyguard_passkey.hold"


class HoldError(Exception):
    pass


def scope_of(store_root: Path) -> str:
    """Names one store among the values the agent holds, without telling its path."""
    return hashlib.sha256(str(store_root.resolve()).encode()).hexdigest()


def scope_of_service(url: str) -> str:
    """Names the store behind one unlock service, apart from every local store."""
    return hashlib.sha256(f"service {url}".encode()).hexdigest()


def agent_environment(environ: dict[str, str], package_root: Path) -> dict[str, str]:
    """Finds the package on PYTHONPATH, so the agent starts from a folder or from a zip application alike."""
    path = os.pathsep.join(entry for entry in (str(package_root), environ.get("PYTHONPATH", "")) if entry)
    return {**environ, "PYTHONPATH": path}


@dataclass
class Held:
    now: Callable[[], float] = time.monotonic
    entries: dict[tuple[str, str], tuple[str, float]] = field(default_factory=dict)

    def put(self, scope: str, values: dict[str, str], seconds: float) -> None:
        expiry = self.now() + min(seconds, MAX_HOLD_SECONDS)
        self.entries.update({(scope, name): (value, expiry) for name, value in values.items()})

    def values_for(self, scope: str, names: Iterable[str]) -> dict[str, str]:
        self.purge()
        return {name: self.entries[scope, name][0] for name in names if (scope, name) in self.entries}

    def forget(self, scope: str, names: Iterable[str] | None) -> None:
        doomed = [key for key in self.entries if key[0] == scope and (names is None or key[1] in set(names))]
        for key in doomed:
            del self.entries[key]

    def purge(self) -> None:
        now = self.now()
        self.entries = {key: entry for key, entry in self.entries.items() if entry[1] > now}

    @property
    def empty(self) -> bool:
        self.purge()
        return not self.entries


def answer(held: Held, token: str, message: object) -> dict[str, object]:
    if not isinstance(message, dict) or not secrets.compare_digest(str(message.get("token", "")).encode(), token.encode()):
        return {"error": "refused"}
    scope, op = str(message.get("scope", "")), message.get("op")
    if op == "get":
        return {"values": held.values_for(scope, _names(message.get("names")) or [])}
    if op == "put" and isinstance(message.get("values"), dict):
        held.put(scope, {str(name): str(value) for name, value in message["values"].items()},
                 float(message.get("seconds", 0)))
        return {"ok": True}
    if op == "forget":
        held.forget(scope, _names(message.get("names")))
        return {"ok": True}
    return {"error": "unknown request"}


def _names(raw: object) -> list[str] | None:
    return [str(name) for name in raw] if isinstance(raw, list) else None


class AgentServer(socketserver.TCPServer):
    def __init__(self, held: Held, token: str) -> None:
        super().__init__((BIND_ADDRESS, 0), AgentHandler)
        self.held = held
        self.token = token


class AgentHandler(socketserver.StreamRequestHandler):
    server: AgentServer
    timeout = REQUEST_TIMEOUT_SECONDS

    def handle(self) -> None:
        try:
            message = json.loads(self.rfile.readline(MAX_MESSAGE_BYTES))
        except (ValueError, OSError):
            return
        try:
            reply = answer(self.server.held, self.server.token, message)
        except (ValueError, TypeError):
            reply = {"error": "malformed request"}
        with contextlib.suppress(OSError):
            self.wfile.write(json.dumps(reply).encode() + b"\n")


def write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    staged = path.with_name(path.name + ".tmp")
    descriptor = os.open(staged, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, PRIVATE_FILE_MODE)
    with os.fdopen(descriptor, "w", encoding="utf-8") as out:
        out.write(text)
    os.replace(staged, path)


def serve(state_dir: Path, grace: float = EMPTY_GRACE_SECONDS, now: Callable[[], float] = time.monotonic) -> None:
    """The agent: announce the address, answer until nothing has been held for a while, withdraw the address."""
    address_file = state_dir / ADDRESS_FILE
    token = secrets.token_urlsafe(TOKEN_BYTES)
    with AgentServer(Held(now=now), token) as server:
        write_private(address_file, json.dumps({"port": server.server_address[1], "token": token}))
        server.timeout = AGENT_TICK_SECONDS
        last_held = now()
        try:
            while now() - last_held < grace:
                server.handle_request()
                if not server.held.empty:
                    last_held = now()
        finally:
            _withdraw(address_file, token)


def _withdraw(address_file: Path, token: str) -> None:
    with contextlib.suppress(OSError, ValueError):
        if json.loads(address_file.read_text(encoding="utf-8")).get("token") == token:
            address_file.unlink()


@dataclass(frozen=True)
class HoldClient:
    state_dir: Path
    scope: str
    seconds: int = DEFAULT_HOLD_SECONDS

    def lookup(self, names: list[str]) -> dict[str, str]:
        if self.seconds <= 0:
            return {}
        reply = self._ask({"op": "get", "names": names}, start=False)
        values = reply.get("values", {})
        if not isinstance(values, dict):
            raise HoldError("the hold answered without values")
        return {name: value for name, value in values.items() if name in names and isinstance(value, str)}

    def keep(self, values: dict[str, str]) -> None:
        if self.seconds > 0 and values:
            self._ask({"op": "put", "values": values, "seconds": self.seconds}, start=True)

    def forget(self, names: list[str] | None = None) -> None:
        self._ask({"op": "forget", "names": names}, start=False)

    def _ask(self, message: dict[str, object], start: bool) -> dict[str, object]:
        reply = self._send(message)
        if reply is None and start:
            self._start_agent()
            reply = self._send(message)
            if reply is None:
                raise HoldError("the hold did not start")
        if reply is not None and "error" in reply:
            raise HoldError(f"the hold answered: {reply['error']}")
        return reply or {}

    def _send(self, message: dict[str, object]) -> dict[str, object] | None:
        """None when no agent answers: nothing is held then."""
        try:
            address = json.loads((self.state_dir / ADDRESS_FILE).read_text(encoding="utf-8"))
            payload = json.dumps({**message, "scope": self.scope, "token": address["token"]}).encode() + b"\n"
            with socket.create_connection((BIND_ADDRESS, int(address["port"])), timeout=REQUEST_TIMEOUT_SECONDS) as peer:
                peer.sendall(payload)
                with peer.makefile("rb") as replies:
                    reply = json.loads(replies.readline(MAX_MESSAGE_BYTES))
        except (OSError, ValueError, KeyError, TypeError):
            return None
        return reply if isinstance(reply, dict) else None

    def _start_agent(self) -> None:
        with contextlib.suppress(OSError):
            (self.state_dir / ADDRESS_FILE).unlink()
        self.state_dir.mkdir(mode=PRIVATE_DIR_MODE, parents=True, exist_ok=True)
        detached = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        subprocess.Popen(
            [sys.executable, "-m", AGENT_MODULE, STATE_DIR_FLAG, str(self.state_dir)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            env=agent_environment(dict(os.environ), Path(__file__).resolve().parents[1]), cwd=str(self.state_dir),
            start_new_session=os.name != "nt", creationflags=detached)
        deadline = time.monotonic() + START_TIMEOUT_SECONDS
        while time.monotonic() < deadline and not (self.state_dir / ADDRESS_FILE).is_file():
            time.sleep(START_POLL_SECONDS)


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if len(arguments) != 2 or arguments[0] != STATE_DIR_FLAG:
        print(f"usage: python -m keyguard_passkey.hold {STATE_DIR_FLAG} FOLDER", file=sys.stderr)
        return 64
    serve(Path(arguments[1]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
