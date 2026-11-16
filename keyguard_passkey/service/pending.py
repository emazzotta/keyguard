"""The open service requests of this machine, so another tool (a web terminal, a status bar) can offer them.

Each waiting request leaves one file, then its outcome once the wait ends. `pending --watch` streams the open
requests as JSON lines and `pending --cancel ID` makes the waiting process give up. A request of a session that expired
unanswered stays listed until a later request of that session gets its answer, at most a day, so a watcher can mark
the session. Files hold the request id, names, requester, link, session and title, never a value.
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import sys
import time
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path
from typing import Final, TextIO

PRIVATE_DIR_MODE: Final = 0o700
PRIVATE_FILE_MODE: Final = 0o600
REQUEST_TTL_SECONDS: Final = 300
OUTCOME_GRACE_SECONDS: Final = 30
ENDED_LINGER_SECONDS: Final = 15
EXPIRED_LINGER_SECONDS: Final = 24 * 60 * 60
WATCH_INTERVAL_SECONDS: Final = 1.0
HEARTBEAT_SECONDS: Final = 30.0
REQUEST_ID: Final = re.compile(r"[A-Za-z0-9_-]{1,64}")
CANCEL_SUFFIX: Final = ".cancel"
PUBLIC_FIELDS: Final = ("id", "vars", "op", "state", "host", "link", "session", "title", "pane", "created")

Record = dict[str, object]


class State(Enum):
    WAITING = "waiting"
    DONE = "done"
    FAILED = "failed"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class Request:
    id: str
    vars: tuple[str, ...]
    op: str
    host: str
    link: str
    session: str | None = None
    title: str | None = None
    pane: str | None = None

    def __post_init__(self) -> None:
        if not REQUEST_ID.fullmatch(self.id):
            raise ValueError(f"not a request id: {self.id!r}")


def _write(path: Path, record: Record) -> None:
    path.parent.mkdir(mode=PRIVATE_DIR_MODE, parents=True, exist_ok=True)
    staged = path.with_suffix(".tmp")
    fd = os.open(staged, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, PRIVATE_FILE_MODE)
    with os.fdopen(fd, "w") as handle:
        json.dump(record, handle)
    os.replace(staged, path)


def _record(request: Request, state: State, now: float, ended: float | None = None) -> Record:
    return {**asdict(request), "vars": list(request.vars), "state": state.value,
            "created": int(now), "ended": ended, "pid": os.getpid()}


def _try_write(path: Path, record: Record, stderr: TextIO) -> None:
    try:
        _write(path, record)
    except OSError as error:
        print(f"keyguard-passkey: the pending list will not show this request: {error}", file=stderr)


def _cancel_mark(directory: Path, request_id: str) -> Path:
    return directory / f"{request_id}{CANCEL_SUFFIX}"


@contextlib.contextmanager
def announced(request: Request, directory: Path,
              now: Callable[[], float] = time.time,
              expired: tuple[type[BaseException], ...] = (),
              cancelled: tuple[type[BaseException], ...] = (),
              stderr: TextIO = sys.stderr) -> Iterator[Callable[[], bool]]:
    """Records the request while the body waits, and hands the body a check for a `cancel` of it. An exception of an
    `expired` or a `cancelled` type ends the request so."""
    path = directory / f"{request.id}.json"
    mark = _cancel_mark(directory, request.id)
    created = now()
    prune(directory, created)
    _try_write(path, _record(request, State.WAITING, created), stderr)
    outcome = State.FAILED
    try:
        yield mark.exists
        outcome = State.DONE
    except expired:
        outcome = State.EXPIRED
        raise
    except cancelled:
        outcome = State.CANCELLED
        raise
    finally:
        with contextlib.suppress(OSError):
            mark.unlink()
        _try_write(path, _record(request, outcome, created, ended=now()), stderr)
        if outcome is State.DONE and request.session:
            clear_expired(directory, request.session)


def clear_expired(directory: Path, session: str) -> None:
    """Forgets the expired requests of a session once a later one got its answer."""
    for path in directory.glob("*.json"):
        record = _read(path)
        if record is not None and record.get("state") == State.EXPIRED.value and record.get("session") == session:
            path.unlink(missing_ok=True)


def _alive(pid: object) -> bool:
    if not isinstance(pid, int):
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _linger_seconds(record: Record) -> int:
    marks_a_session = record.get("state") == State.EXPIRED.value and bool(record.get("session"))
    return EXPIRED_LINGER_SECONDS if marks_a_session else ENDED_LINGER_SECONDS


def _stale(record: Record, now: float) -> bool:
    ended = record.get("ended")
    if isinstance(ended, (int, float)):
        return now - ended > _linger_seconds(record)
    created = record.get("created")
    if not isinstance(created, (int, float)):
        return True
    return now - created > REQUEST_TTL_SECONDS + OUTCOME_GRACE_SECONDS or not _alive(record.get("pid"))


def _read(path: Path) -> Record | None:
    try:
        record = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return record if isinstance(record, dict) else None


def prune(directory: Path, now: float) -> None:
    for path in directory.glob("*.json"):
        record = _read(path)
        if record is None or _stale(record, now):
            path.unlink(missing_ok=True)
    for mark in directory.glob(f"*{CANCEL_SUFFIX}"):
        if not mark.with_suffix(".json").exists():
            mark.unlink(missing_ok=True)


def _waits(record: Record | None, now: float) -> bool:
    return record is not None and record.get("state") == State.WAITING.value and not _stale(record, now)


def cancel(directory: Path, request_id: str, now: float) -> bool:
    """Asks the process that waits on the request to give up; False when no live process waits on it."""
    if not REQUEST_ID.fullmatch(request_id) or not _waits(_read(directory / f"{request_id}.json"), now):
        return False
    _cancel_mark(directory, request_id).touch(mode=PRIVATE_FILE_MODE)
    return True


def snapshot(directory: Path, now: float) -> list[Record]:
    prune(directory, now)
    records = (_read(path) for path in directory.glob("*.json"))
    public = [{key: record.get(key) for key in PUBLIC_FIELDS} for record in records if record]
    return sorted(public, key=lambda record: (_number(record["created"]), str(record["id"] or "")))


def _number(value: object) -> float:
    return float(value) if isinstance(value, (int, float)) else 0.0


def watch(directory: Path, out: TextIO, now: Callable[[], float] = time.time,
          sleep: Callable[[float], None] = time.sleep, rounds: int | None = None) -> None:
    last: list[Record] | None = None
    sent = 0.0
    while rounds is None or rounds > 0:
        current = snapshot(directory, now())
        if current != last or now() - sent >= HEARTBEAT_SECONDS:
            out.write(json.dumps({"pending": current}) + "\n")
            out.flush()
            last, sent = current, now()
        rounds = None if rounds is None else rounds - 1
        sleep(WATCH_INTERVAL_SECONDS)
