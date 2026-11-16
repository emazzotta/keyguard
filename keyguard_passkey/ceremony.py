"""One approval in the browser: a loopback server that hands the page its request and the ciphertext,
takes the page's commits, and ends with the page's answer."""
from __future__ import annotations

import base64
import binascii
import json
import secrets
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import Enum
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Final

from . import pages
from .store import StaleStore, Store, StoreError

RP_HOST: Final = "localhost"
BIND_ADDRESS: Final = "127.0.0.1"
TOKEN_HEADER: Final = "X-Keyguard-Token"
TOKEN_BYTES: Final = 16
MAX_BODY_BYTES: Final = 16 * 1024 * 1024
ANSWER_TIMEOUT_SECONDS: Final = 300.0
POLL_SECONDS: Final = 0.5
CONNECTION_TIMEOUT_SECONDS: Final = 10
STORE_PREFIX: Final = "/store/"
JSON_TYPE: Final = "application/json"


class Unanswered(Exception):
    pass


class Op(Enum):
    INIT = "init"
    GET = "get"
    LIST = "list"
    SET = "set"
    RM = "rm"


@dataclass(frozen=True)
class Request:
    op: Op
    names: tuple[str, ...] = ()
    values: dict[str, str] = field(default_factory=dict)
    force: bool = False
    session: str = ""
    title: str = ""

    def as_json(self) -> dict[str, object]:
        return {"op": self.op.value, "names": list(self.names), "values": self.values, "force": self.force,
                "session": self.session, "title": self.title}


@dataclass
class Ceremony:
    request: Request
    store: Store
    token: str = field(default_factory=lambda: secrets.token_urlsafe(TOKEN_BYTES))
    answer: dict[str, object] | None = None

    def accepts(self, token: str) -> bool:
        return secrets.compare_digest(token.encode(), self.token.encode())


class CeremonyServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, ceremony: Ceremony) -> None:
        super().__init__((BIND_ADDRESS, 0), CeremonyHandler)
        self.ceremony = ceremony

    @property
    def authority(self) -> str:
        return f"{RP_HOST}:{self.server_port}"

    @property
    def page_url(self) -> str:
        route = pages.ENROLL_ROUTE if self.ceremony.request.op is Op.INIT else pages.APPROVE_ROUTE
        return f"http://{self.authority}{route}#token={self.ceremony.token}"

    def wait(self, seconds: float = ANSWER_TIMEOUT_SECONDS,
             now: Callable[[], float] = time.monotonic) -> dict[str, object]:
        deadline = now() + seconds
        self.timeout = POLL_SECONDS
        while self.ceremony.answer is None:
            if now() >= deadline:
                raise Unanswered(f"no answer from the browser within {int(seconds)} s")
            self.handle_request()
        return self.ceremony.answer


class CeremonyHandler(BaseHTTPRequestHandler):
    server: CeremonyServer
    timeout = CONNECTION_TIMEOUT_SECONDS

    def do_GET(self) -> None:
        path = self.path.partition("?")[0]
        if not self._asked_by_name():
            return self._send(HTTPStatus.NOT_FOUND)
        found = pages.page(path)
        if found is not None:
            return self._send(HTTPStatus.OK, found.body, found.content_type, pages.SECURITY_HEADERS)
        if not self._has_token():
            return self._send(HTTPStatus.FORBIDDEN)
        if path == "/request":
            return self._send_json(self.server.ceremony.request.as_json())
        if path == "/store/meta":
            return self._send_json(self.server.ceremony.store.meta().as_json())
        if path.startswith(STORE_PREFIX):
            return self._send_store_file(path[len(STORE_PREFIX):])
        self._send(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        if not self._asked_by_name():
            return self._send(HTTPStatus.NOT_FOUND)
        if not self._has_token():
            return self._send(HTTPStatus.FORBIDDEN)
        body = self._read_json()
        if body is None:
            return self._send(HTTPStatus.BAD_REQUEST)
        if self.path == "/store/commit":
            return self._commit(body)
        if self.path == "/answer":
            self._send(HTTPStatus.NO_CONTENT)
            self.server.ceremony.answer = body
            return
        self._send(HTTPStatus.NOT_FOUND)

    def log_message(self, *_: object) -> None:
        pass

    def _asked_by_name(self) -> bool:
        return self.headers.get("Host", "") == self.server.authority

    def _has_token(self) -> bool:
        return self.server.ceremony.accepts(self.headers.get(TOKEN_HEADER, ""))

    def _read_json(self) -> dict[str, object] | None:
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            return None
        if not 0 < length <= MAX_BODY_BYTES:
            return None
        try:
            body = json.loads(self.rfile.read(length))
        except ValueError:
            return None
        return body if isinstance(body, dict) else None

    def _send_store_file(self, path: str) -> None:
        try:
            data = self.server.ceremony.store.read(path)
        except (StoreError, FileNotFoundError):
            return self._send(HTTPStatus.NOT_FOUND)
        self._send(HTTPStatus.OK, data, "application/octet-stream")

    def _commit(self, body: dict[str, object]) -> None:
        try:
            files, deletes = decode_commit(body)
            expected = int(self.headers.get("If-Match", ""))
            meta = self.server.ceremony.store.commit(files, deletes, expected)
        except StaleStore:
            return self._send(HTTPStatus.PRECONDITION_FAILED)
        except (StoreError, ValueError, TypeError):
            return self._send(HTTPStatus.BAD_REQUEST)
        self._send_json({"version": meta.version})

    def _send_json(self, payload: dict[str, object]) -> None:
        self._send(HTTPStatus.OK, json.dumps(payload).encode(), JSON_TYPE)

    def _send(self, status: HTTPStatus, body: bytes = b"", content_type: str = "text/plain",
              headers: Mapping[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def decode_commit(body: dict[str, object]) -> tuple[dict[str, bytes], list[str]]:
    files = body.get("files", {})
    deletes = body.get("delete", [])
    if not isinstance(files, dict) or not isinstance(deletes, list):
        raise TypeError("files must be an object and delete a list")
    if not all(isinstance(path, str) for path in deletes) or not all(isinstance(data, str) for data in files.values()):
        raise TypeError("paths and file contents must be strings")
    try:
        return {path: base64.b64decode(data, validate=True) for path, data in files.items()}, list(deletes)
    except binascii.Error as error:
        raise ValueError("a file is not base64") from error
