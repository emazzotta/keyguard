"""Signed unlock requests, held in memory until the approving device answers or they expire.

Deliberately in memory: an answer is already sealed to the request's ephemeral key, so persisting it would guard
nothing, and a lost request just asks again.
"""
from __future__ import annotations

import contextlib
import re
import secrets
import sys
import threading
import time
import unicodedata
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Final, Protocol

from .hint import HINT_FIELD, NotifyHint, parse_hint
from .message import (
    MAX_MESSAGE_CHARS,
    MESSAGE_FIELDS,
    OWNER_KEY_NAME,
    OWNER_KEY_REFUSED,
    SIGN_OP,
    is_box,
    is_message_text,
    is_target,
)

REQUEST_TTL_SECONDS: Final = 300
REQUEST_ID_BYTES: Final = 16
REQUEST_FIELDS: Final = ("vars", "host", "eph_pubkey", "ts", "sig")
CALLER_FIELDS: Final = ("session", "title")
SESSION_ID: Final = re.compile(r"[0-9A-Za-z]{8}")
MAX_TITLE_CHARS: Final = 80
HIDDEN_TITLE_CATEGORIES: Final = frozenset({"Cc", "Cf", "Co", "Cs", "Zl", "Zp"})
REQUEST_OPS: Final = frozenset({"set", "rm", "list", SIGN_OP})
MAX_SEALED_VALUE_CHARS: Final = 64 * 1024
DEFAULT_RATE_LIMIT: Final = 30
RATE_WINDOW_SECONDS: Final = 60.0

Clock = Callable[[], float]


class RequestError(ValueError):
    """A malformed request: a missing field, an unknown op, a value where none belongs."""


class AlreadyAnswered(Exception):
    pass


class RateLimitError(Exception):
    def __init__(self, limit: int, window: float) -> None:
        super().__init__(f"rate limit {limit}/{int(window)}s exceeded")


class SlidingWindowLimit:
    def __init__(self, limit: int, window: float, now: Clock = time.time) -> None:
        self._limit = limit
        self._window = window
        self._now = now
        self._stamps: list[float] = []
        self._lock = threading.Lock()

    def take(self) -> None:
        with self._lock:
            now = self._now()
            cutoff = now - self._window
            self._stamps = [stamp for stamp in self._stamps if stamp >= cutoff]
            if len(self._stamps) >= self._limit:
                raise RateLimitError(self._limit, self._window)
            self._stamps.append(now)


@dataclass
class UnlockRequest:
    id: str
    vars: list[str]
    host: str
    eph_pubkey: str
    ts: int
    sig: str
    created_at: float
    response: str | None = None
    op: str | None = None
    value: str | None = None
    session: str | None = None
    title: str | None = None
    hint: NotifyHint | None = None
    text: str | None = None
    target: str | None = None
    target_box: str | None = None
    target_title: str | None = None

    def _signed_view(self) -> dict[str, object]:
        view: dict[str, object] = {field: getattr(self, field) for field in REQUEST_FIELDS}
        for field in ("op", "value", *CALLER_FIELDS, *MESSAGE_FIELDS):
            if getattr(self, field) is not None:
                view[field] = getattr(self, field)
        return view

    def requester_view(self) -> dict[str, object]:
        return {**self._signed_view(), "response": self.response}

    def approver_view(self) -> dict[str, object]:
        return {**self._signed_view(), "answered": self.response is not None}


def op_fields(payload: dict[str, object]) -> tuple[str | None, str | None]:
    op, value, names = payload.get("op"), payload.get("value"), payload["vars"]
    if isinstance(names, list) and OWNER_KEY_NAME in names:
        raise RequestError(OWNER_KEY_REFUSED)
    if op is None:
        if value is not None:
            raise RequestError("a sealed value needs op set")
        return None, None
    if not isinstance(op, str) or op not in REQUEST_OPS:
        raise RequestError(f"unknown op: {op}")
    if op in ("list", SIGN_OP):
        if names or value is not None:
            raise RequestError(f"{op} names no variable and carries no value")
        return op, None
    if not isinstance(names, list) or len(names) != 1:
        raise RequestError(f"{op} takes exactly one variable")
    if op == "rm":
        if value is not None:
            raise RequestError("rm carries no value")
        return op, None
    if not isinstance(value, str) or not value:
        raise RequestError("set needs a sealed value")
    if len(value) > MAX_SEALED_VALUE_CHARS:
        raise RequestError("sealed value too large")
    return op, value


def caller_fields(payload: dict[str, object]) -> tuple[str | None, str | None]:
    session, title = payload.get("session"), payload.get("title")
    if session is not None and not (isinstance(session, str) and SESSION_ID.fullmatch(session)):
        raise RequestError("session must be 8 letters or digits")
    if title is not None and not (isinstance(title, str) and is_display_title(title)):
        raise RequestError(f"title must be 1 to {MAX_TITLE_CHARS} visible characters")
    return session, title


def message_fields(payload: dict[str, object], op: str | None) -> dict[str, str]:
    found = {field: payload[field] for field in MESSAGE_FIELDS if payload.get(field) is not None}
    if op != SIGN_OP:
        if found:
            raise RequestError(f"only a {SIGN_OP} request carries a message")
        return {}
    if not is_message_text(found.get("text")):
        raise RequestError(f"the message must be 1 to {MAX_MESSAGE_CHARS} characters without hidden characters")
    if not is_target(found.get("target")) or not is_box(found.get("target_box")):
        raise RequestError("a message names its target's full conversation id and box")
    title = found.get("target_title")
    if title is not None and not (isinstance(title, str) and is_display_title(title)):
        raise RequestError(f"target_title must be 1 to {MAX_TITLE_CHARS} visible characters")
    return {field: str(value) for field, value in found.items()}


def is_display_title(title: str) -> bool:
    return (0 < len(title) <= MAX_TITLE_CHARS
            and not any(unicodedata.category(char) in HIDDEN_TITLE_CATEGORIES for char in title))


class RequestEvents(Protocol):
    """What the queue reports, outside its lock: a new request, and one that expired with no answer."""

    def created(self, request: UnlockRequest) -> None: ...

    def expired(self, request: UnlockRequest) -> None: ...


class NoEvents:
    def created(self, request: UnlockRequest) -> None:
        pass

    def expired(self, request: UnlockRequest) -> None:
        pass


class RequestQueue:
    def __init__(self, ttl_seconds: int = REQUEST_TTL_SECONDS, now: Clock = time.time,
                 rate_limit: int = DEFAULT_RATE_LIMIT, rate_window: float = RATE_WINDOW_SECONDS,
                 events: RequestEvents | None = None) -> None:
        self._requests: dict[str, UnlockRequest] = {}
        self._ttl = ttl_seconds
        self._now = now
        self._creations = SlidingWindowLimit(rate_limit, rate_window, now)
        self._events: RequestEvents = events or NoEvents()
        self._lock = threading.Lock()

    def create(self, payload: object) -> str:
        if not isinstance(payload, dict):
            raise RequestError("a request is a JSON object")
        missing = [field for field in REQUEST_FIELDS if field not in payload]
        if missing:
            raise RequestError(f"request missing fields: {', '.join(missing)}")
        op, value = op_fields(payload)
        session, title = caller_fields(payload)
        message = message_fields(payload, op)
        with self._lock:
            self._creations.take()
            request_id = secrets.token_urlsafe(REQUEST_ID_BYTES)
            request = UnlockRequest(
                id=request_id, vars=payload["vars"], host=payload["host"], eph_pubkey=payload["eph_pubkey"],
                ts=payload["ts"], sig=payload["sig"], created_at=self._now(), op=op, value=value,
                session=session, title=title, hint=parse_hint(payload.get(HINT_FIELD)), **message)
            self._requests[request_id] = request
        self._tell(self._events.created, request)
        return request_id

    def get(self, request_id: str) -> dict[str, object] | None:
        """The requester's read: once it carries the answer, the request is gone."""
        with self._exclusive():
            request = self._requests.get(request_id)
            if request is None:
                return None
            if request.response is not None:
                del self._requests[request_id]
            return request.requester_view()

    def peek(self, request_id: str) -> dict[str, object] | None:
        with self._exclusive():
            request = self._requests.get(request_id)
            return None if request is None else request.approver_view()

    def respond(self, request_id: str, response: str) -> bool:
        with self._exclusive():
            request = self._requests.get(request_id)
            if request is None:
                return False
            if request.response is not None:
                raise AlreadyAnswered(request_id)
            request.response = response
            return True

    def withdraw(self, request_id: str) -> bool:
        with self._lock:
            return self._requests.pop(request_id, None) is not None

    def sweep(self) -> None:
        with self._exclusive():
            pass

    @contextlib.contextmanager
    def _exclusive(self) -> Iterator[None]:
        """Holds the lock after it dropped the expired requests, and reports them once the lock is free."""
        expired: list[UnlockRequest] = []
        try:
            with self._lock:
                expired = self._purge_expired()
                yield
        finally:
            for request in expired:
                self._tell(self._events.expired, request)

    def _purge_expired(self) -> list[UnlockRequest]:
        """Drops every expired request and returns those that nobody answered."""
        cutoff = self._now() - self._ttl
        gone = [request for request in self._requests.values() if request.created_at < cutoff]
        for request in gone:
            del self._requests[request.id]
        return [request for request in gone if request.response is None]

    @staticmethod
    def _tell(event: Callable[[UnlockRequest], None], request: UnlockRequest) -> None:
        try:
            event(request)
        except Exception as error:  # noqa: BLE001 - a notice must not cost a request or end the sweeper
            print(f"requests: {event.__name__} notice failed ({type(error).__name__})", file=sys.stderr, flush=True)
