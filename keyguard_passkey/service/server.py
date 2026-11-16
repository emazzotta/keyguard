"""The unlock service: the store's ciphertext, the signed requests and the pages, over HTTP.

It never decrypts anything. Callers prove who they are with the identity header a trusted reverse proxy sets
(and strips from client traffic), or with the request token, which reaches only the request endpoints.
"""
from __future__ import annotations

import base64
import binascii
import gzip
import hashlib
import json
import secrets
import threading
import time
from collections.abc import Callable, Iterable
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import ClassVar, Final

from .. import pages
from ..store import StaleStore, Store, StoreError
from .heartbeat import HEALTH_PATH, heartbeat_for
from .notify import DisabledNotifier, Notifier, parse_notice, telegram_notifier
from .requests import (
    RATE_WINDOW_SECONDS,
    AlreadyAnswered,
    RateLimitError,
    RequestError,
    RequestQueue,
    SlidingWindowLimit,
)
from .settings import Settings

JSON_TYPE: Final = "application/json"
SAME_ORIGIN_FETCH: Final = frozenset({"same-origin", "none"})
STORE_PREFIX: Final = "/store/"
REQUEST_PREFIX: Final = "/r/"
APPROVER_VIEW_SUFFIX: Final = "/request"
RESPONSE_SUFFIX: Final = "/response"
CONFIG_PATH: Final = "/config"
MAX_BODY_BYTES: Final = 16 * 1024 * 1024
SOCKET_TIMEOUT_SECONDS: Final = 30
NOTICE_RATE_LIMIT: Final = 20
SWEEP_INTERVAL_SECONDS: Final = 15
# 151 KB of typage shrinks to 55 KB in about 5 ms; level 9 saves 0.2% more for 15% more time.
PAGE_GZIP_LEVEL: Final = 6
BEARER: Final = "Bearer "


def page_etag(data: bytes) -> str:
    # Weak, so the gzip and the plain body of one page share it.
    return f'W/"{hashlib.sha256(data).hexdigest()}"'


def accepts_gzip(accept_encoding: str) -> bool:
    return "gzip" in {coding.split(";")[0].strip().lower() for coding in accept_encoding.split(",")}


class CommitBodyError(ValueError):
    pass


def decode_files(payload: object) -> dict[str, bytes]:
    files = payload.get("files", {}) if isinstance(payload, dict) else None
    if not isinstance(files, dict):
        raise CommitBodyError("files must be an object")
    try:
        return {path: base64.b64decode(data) for path, data in files.items()}
    except (binascii.Error, TypeError) as error:
        raise CommitBodyError("a file is not base64") from error


def decode_deletes(payload: object) -> list[str]:
    deletes = payload.get("delete", []) if isinstance(payload, dict) else []
    if not isinstance(deletes, Iterable):
        raise CommitBodyError("delete must be a list")
    return [str(path) for path in deletes]


class Handler(BaseHTTPRequestHandler):
    settings: ClassVar[Settings]
    store: ClassVar[Store]
    queue: ClassVar[RequestQueue]
    notifier: ClassVar[Notifier] = DisabledNotifier()
    notice_limit: ClassVar[SlidingWindowLimit]
    page_source: ClassVar[pages.PageSource] = staticmethod(pages.page)
    timeout = SOCKET_TIMEOUT_SECONDS

    def log_message(self, *_: object) -> None:
        pass

    def do_GET(self) -> None:
        route = self.path.partition("?")[0]
        if route == HEALTH_PATH:
            return self._send(HTTPStatus.NO_CONTENT)
        found = type(self).page_source(route)
        if found is not None:
            return self._serve_page(found)
        if route == CONFIG_PATH:
            return self._guard(self._has_identity(), lambda: self._send_json(self.settings.public_config()))
        if route == STORE_PREFIX + "meta":
            return self._guard(self._has_identity(), self._store_meta)
        if route.startswith(STORE_PREFIX):
            return self._guard(self._has_identity(), lambda: self._store_read(route[len(STORE_PREFIX):]))
        if route.startswith(REQUEST_PREFIX) and route.endswith(APPROVER_VIEW_SUFFIX):
            request_id = route[len(REQUEST_PREFIX):-len(APPROVER_VIEW_SUFFIX)]
            return self._guard(self._has_identity(), lambda: self._send_record(self.queue.peek(request_id)))
        if route.startswith(REQUEST_PREFIX):
            request_id = route[len(REQUEST_PREFIX):]
            return self._guard(self._has_identity() or self._has_request_token(),
                               lambda: self._send_record(self.queue.get(request_id)))
        return self._fail(HTTPStatus.NOT_FOUND, "no such endpoint")

    def do_POST(self) -> None:
        if self._body_too_large():
            self.close_connection = True
            return self._fail(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "body too large")
        if self.path == STORE_PREFIX + "commit":
            return self._guard_post(self._has_identity(), self._store_commit)
        if self.path == "/requests":
            return self._guard_post(self._has_identity() or self._has_request_token(), self._request_create)
        if self.path == "/notify":
            return self._guard_post(self._has_identity() or self._has_request_token(), self._notice_relay)
        if self.path.startswith(REQUEST_PREFIX) and self.path.endswith(RESPONSE_SUFFIX):
            request_id = self.path[len(REQUEST_PREFIX):-len(RESPONSE_SUFFIX)]
            return self._guard_post(self._has_identity(), lambda: self._request_respond(request_id))
        return self._fail(HTTPStatus.NOT_FOUND, "no such endpoint")

    def do_DELETE(self) -> None:
        route = self.path.partition("?")[0]
        if route.startswith(REQUEST_PREFIX):
            return self._guard(self._has_identity() or self._has_request_token(),
                               lambda: self._request_withdraw(route[len(REQUEST_PREFIX):]))
        return self._fail(HTTPStatus.NOT_FOUND, "no such endpoint")

    def _has_identity(self) -> bool:
        login = self.headers.get(self.settings.identity_header, "").strip()
        allowed = self.settings.allowed_logins
        return bool(login) and (not allowed or login in allowed)

    def _has_request_token(self) -> bool:
        header = self.headers.get("Authorization", "")
        token = self.settings.request_token
        if not token or not header.startswith(BEARER):
            return False
        return secrets.compare_digest(header[len(BEARER):].encode(), token.encode())

    def _body_too_large(self) -> bool:
        try:
            return int(self.headers.get("Content-Length", 0)) > MAX_BODY_BYTES
        except ValueError:
            return False

    def _guard(self, authorized: bool, handler: Callable[[], None]) -> None:
        if not authorized:
            return self._fail(HTTPStatus.FORBIDDEN, "no identity")
        return handler()

    def _guard_post(self, authorized: bool, handler: Callable[[], None]) -> None:
        if not authorized:
            return self._fail(HTTPStatus.FORBIDDEN, "no identity")
        if self.headers.get_content_type() != JSON_TYPE:
            return self._fail(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "send application/json")
        if self.headers.get("Sec-Fetch-Site", "none") not in SAME_ORIGIN_FETCH:
            return self._fail(HTTPStatus.FORBIDDEN, "cross-site request")
        return handler()

    def _serve_page(self, page: pages.Page) -> None:
        etag = page_etag(page.body)
        headers = {**pages.SECURITY_HEADERS, "Cache-Control": "no-cache", "ETag": etag, "Vary": "Accept-Encoding"}
        if self.headers.get("If-None-Match") == etag:
            return self._send(HTTPStatus.NOT_MODIFIED, content_type=page.content_type, extra=headers)
        if accepts_gzip(self.headers.get("Accept-Encoding", "")):
            return self._send(HTTPStatus.OK, gzip.compress(page.body, PAGE_GZIP_LEVEL),
                              content_type=page.content_type, extra={**headers, "Content-Encoding": "gzip"})
        return self._send(HTTPStatus.OK, page.body, content_type=page.content_type, extra=headers)

    def _send(self, status: HTTPStatus, body: bytes = b"", content_type: str = JSON_TYPE,
              extra: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        for key, value in (extra or {}).items():
            if key != "X-Content-Type-Options":
                self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, payload: object, extra: dict[str, str] | None = None) -> None:
        self._send(HTTPStatus.OK, json.dumps(payload, sort_keys=True).encode(), extra=extra)

    def _fail(self, status: HTTPStatus, message: str) -> None:
        self._send(status, json.dumps({"error": message}).encode())

    def _body_json(self) -> object:
        length = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(length) or b"{}")

    def _store_meta(self) -> None:
        try:
            meta = self.store.meta()
        except StoreError as error:
            return self._fail(HTTPStatus.INTERNAL_SERVER_ERROR, str(error))
        etag = f'"{meta.version}"'
        if self.headers.get("If-None-Match") == etag:
            return self._send(HTTPStatus.NOT_MODIFIED, extra={"ETag": etag})
        return self._send_json(meta.as_json(), extra={"ETag": etag})

    def _store_read(self, path: str) -> None:
        try:
            data = self.store.read(path)
        except StoreError:
            return self._fail(HTTPStatus.BAD_REQUEST, "bad path")
        except FileNotFoundError:
            return self._fail(HTTPStatus.NOT_FOUND, "no such file")
        return self._send(HTTPStatus.OK, data, content_type="application/octet-stream")

    def _store_commit(self) -> None:
        try:
            payload = self._body_json()
            files = decode_files(payload)
        except (ValueError, OSError):
            return self._fail(HTTPStatus.BAD_REQUEST, "bad commit body")
        if_match = self.headers.get("If-Match")
        if if_match is None:
            return self._fail(HTTPStatus.PRECONDITION_REQUIRED, "If-Match required")
        try:
            meta = self.store.commit(files, decode_deletes(payload), int(if_match))
        except StaleStore as error:
            return self._fail(HTTPStatus.PRECONDITION_FAILED, str(error))
        except (StoreError, ValueError) as error:
            return self._fail(HTTPStatus.BAD_REQUEST, str(error))
        return self._send_json({"version": meta.version}, extra={"ETag": f'"{meta.version}"'})

    def _request_create(self) -> None:
        try:
            request_id = self.queue.create(self._body_json())
        except RateLimitError as error:
            return self._fail(HTTPStatus.TOO_MANY_REQUESTS, str(error))
        except RequestError as error:
            return self._fail(HTTPStatus.BAD_REQUEST, str(error))
        except (ValueError, OSError):
            return self._fail(HTTPStatus.BAD_REQUEST, "bad json")
        return self._send_json({"id": request_id})

    def _notice_relay(self) -> None:
        origin = self.settings.token_label if self._has_request_token() else self.settings.identity_label
        try:
            notice = parse_notice(self._body_json(), origin)
        except (ValueError, OSError) as error:
            return self._fail(HTTPStatus.BAD_REQUEST, f"bad notice: {error}")
        try:
            self.notice_limit.take()
        except RateLimitError as error:
            return self._fail(HTTPStatus.TOO_MANY_REQUESTS, str(error))
        self.notifier.claude_notice(notice)
        return self._send(HTTPStatus.ACCEPTED)

    def _send_record(self, record: dict[str, object] | None) -> None:
        if record is None:
            return self._fail(HTTPStatus.NOT_FOUND, "no such request")
        return self._send(HTTPStatus.OK, json.dumps(record).encode())

    def _request_withdraw(self, request_id: str) -> None:
        if not self.queue.withdraw(request_id):
            return self._fail(HTTPStatus.NOT_FOUND, "no such request")
        return self._send(HTTPStatus.NO_CONTENT)

    def _request_respond(self, request_id: str) -> None:
        try:
            body = self._body_json()
        except (ValueError, OSError):
            return self._fail(HTTPStatus.BAD_REQUEST, "bad json")
        response = body.get("response") if isinstance(body, dict) else None
        if not isinstance(response, str):
            return self._fail(HTTPStatus.BAD_REQUEST, "response must be a string")
        try:
            answered = self.queue.respond(request_id, response)
        except AlreadyAnswered:
            return self._fail(HTTPStatus.CONFLICT, "already answered")
        if not answered:
            return self._fail(HTTPStatus.NOT_FOUND, "no such request")
        return self._send_json({"ok": True})


def build_server(settings: Settings, notifier: Notifier | None = None,
                 notice_rate_limit: int = NOTICE_RATE_LIMIT,
                 page_source: pages.PageSource = pages.page,
                 queue: RequestQueue | None = None) -> ThreadingHTTPServer:
    settings.store.mkdir(parents=True, exist_ok=True)
    handler = type("BoundHandler", (Handler,), {
        "settings": settings,
        "store": Store(settings.store),
        "queue": queue or RequestQueue(rate_limit=settings.request_rate_limit),
        "notifier": notifier or telegram_notifier(settings.telegram_token, settings.telegram_chat),
        "notice_limit": SlidingWindowLimit(notice_rate_limit, RATE_WINDOW_SECONDS),
        "page_source": staticmethod(page_source),
    })
    server = ThreadingHTTPServer((settings.bind, settings.port), handler)
    server.daemon_threads = True
    return server


def sweep_forever(queue: RequestQueue, interval: float = SWEEP_INTERVAL_SECONDS) -> None:
    while True:
        time.sleep(interval)
        queue.sweep()


def describe(settings: Settings, server: ThreadingHTTPServer) -> str:
    logins = settings.allowed_logins
    identities = f"{len(logins)} allowed login(s)" if logins else f"any {settings.identity_header}"
    return (f"keyguard-passkey service on {settings.bind}:{server.server_port}, store {settings.store}, "
            f"{len(settings.requesters)} requester(s), {identities}, "
            f"request token {'on' if settings.request_token else 'off'}, "
            f"telegram {'on' if settings.telegram_token and settings.telegram_chat else 'off'}, "
            f"heartbeat {'on' if settings.heartbeat_url else 'off'}")


def serve(settings: Settings) -> None:
    queue = RequestQueue(rate_limit=settings.request_rate_limit)
    server = build_server(settings, queue=queue)
    print(describe(settings, server), flush=True)
    threading.Thread(target=sweep_forever, args=(queue,), daemon=True).start()
    heartbeat = heartbeat_for(settings.heartbeat_url, settings.bind, server.server_port)
    if heartbeat:
        threading.Thread(target=heartbeat.run, daemon=True).start()
    with server:
        server.serve_forever()
