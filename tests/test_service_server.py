from __future__ import annotations

import base64
import gzip
import http.client
import json
import threading
import urllib.error
import urllib.request
from http import HTTPStatus
from pathlib import Path

import pytest

from keyguard_passkey.pages import CSS, HTML, JAVASCRIPT, Page
from keyguard_passkey.service import server as service
from keyguard_passkey.service.notify import ClaudeNotice
from keyguard_passkey.service.settings import DEFAULT_IDENTITY_HEADER, Requester, Settings
from keyguard_passkey.store import Store

VAR = "vars/" + "a" * 64 + ".age"
AUTH = {DEFAULT_IDENTITY_HEADER: "owner@example.com"}
REQUEST_TOKEN = "requester-test-token"
BEARER = {"Authorization": f"Bearer {REQUEST_TOKEN}"}
COMMIT_BODY = json.dumps({"files": {"index.age": base64.b64encode(b"x").decode()}}).encode()
NOTICE = json.dumps({"session": "4a015f0f-199b-4270-9c06-fecc5d85b976", "container": "f635a28c3a2e",
                     "text": "Waiting for your OK."}).encode()
TOKEN_LABEL = "dev box"
IDENTITY_LABEL = "laptop"
SWEEP_DEADLINE_SECONDS = 10
LONG_SWEEP_INTERVAL_SECONDS = 3600


class FakePages:
    def __init__(self) -> None:
        self.pages = {
            "/enroll": Page(b"<!doctype html><title>enrol</title>", HTML),
            "/approve": Page(b"<!doctype html><title>approve</title>", HTML),
            "/typage.js": Page(b"export const age = {};", JAVASCRIPT),
            "/approve.js": Page(b"import * as core from './unlock-core.js'", JAVASCRIPT),
            "/unlock.css": Page(b"body { margin: 0 }", CSS),
        }

    def __call__(self, route: str) -> Page | None:
        return self.pages.get(route)


class RecordingNotifier:
    def __init__(self) -> None:
        self.notices: list[ClaudeNotice] = []

    def claude_notice(self, notice: ClaudeNotice) -> None:
        self.notices.append(notice)


class Running:
    def __init__(self, root: Path, settings: dict[str, object] | None = None, **options: object) -> None:
        self.server = service.build_server(Settings(store=root, port=0, **(settings or {})), **options)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.store = Store(root)

    def call(self, path: str, method: str = "GET", headers: dict[str, str] | None = None,
             body: bytes | None = None) -> tuple[int, bytes, dict[str, str]]:
        merged = {"Content-Type": "application/json"} if method == "POST" else {}
        merged.update(headers or {})
        request = urllib.request.Request(self.base + path, method=method, data=body, headers=merged)
        try:
            with urllib.request.urlopen(request) as response:
                return response.status, response.read(), dict(response.headers)
        except urllib.error.HTTPError as error:
            return error.code, error.read(), dict(error.headers)

    def status(self, path: str, method: str = "GET", headers: dict[str, str] | None = None,
               body: bytes | None = None) -> int:
        return self.call(path, method, headers, body)[0]

    def create(self, headers: dict[str, str] = BEARER, **extra: object) -> str:
        _, body, _ = self.call("/requests", "POST", headers, request_body(**extra))
        return str(json.loads(body)["id"])

    def answer(self, request_id: str, answer: dict[str, object]) -> int:
        return self.status(f"/r/{request_id}/response", "POST", AUTH, json.dumps(answer).encode())


def request_body(**extra: object) -> bytes:
    return json.dumps({"vars": ["A", "B"], "host": "devbox", "eph_pubkey": "age1x", "ts": 1, "sig": "s",
                       **extra}).encode()


def commit_body(files: dict[str, bytes]) -> bytes:
    return json.dumps({"files": {path: base64.b64encode(data).decode() for path, data in files.items()}}).encode()


@pytest.fixture
def running(tmp_path):
    served = Running(tmp_path, settings={"request_token": REQUEST_TOKEN}, page_source=FakePages())
    yield served
    served.server.shutdown()


@pytest.fixture
def tokenless(tmp_path):
    served = Running(tmp_path)
    yield served
    served.server.shutdown()


@pytest.fixture
def notified(tmp_path):
    notifier = RecordingNotifier()
    served = Running(tmp_path, notifier=notifier, settings={
        "request_token": REQUEST_TOKEN, "token_label": TOKEN_LABEL, "identity_label": IDENTITY_LABEL})
    yield served, notifier
    served.server.shutdown()


@pytest.mark.spec("docs/service.md")
def test_should_answer_the_health_check_without_an_identity(running):
    status, body, _ = running.call("/healthz")

    assert (status, body) == (HTTPStatus.NO_CONTENT, b"")


@pytest.mark.spec("docs/service.md")
def test_should_refuse_the_store_without_an_identity(running):
    assert running.status("/store/meta") == HTTPStatus.FORBIDDEN
    assert running.status("/store/commit", "POST", body=b"{}") == HTTPStatus.FORBIDDEN


@pytest.mark.spec("docs/service.md")
def test_should_serve_meta_with_its_version_as_etag(running):
    running.store.commit({"index.age": b"x"}, [], expected_version=0)

    status, body, headers = running.call("/store/meta", headers=AUTH)

    assert status == HTTPStatus.OK
    assert json.loads(body) == {"version": 1, "files": running.store.meta().files, "meta_version": 1}
    assert headers["ETag"] == '"1"'


@pytest.mark.spec("docs/service.md")
def test_should_answer_304_when_the_meta_etag_matches(running):
    running.store.commit({"index.age": b"x"}, [], expected_version=0)

    assert running.status("/store/meta", headers={**AUTH, "If-None-Match": '"1"'}) == HTTPStatus.NOT_MODIFIED


@pytest.mark.spec("docs/service.md")
def test_should_round_trip_a_file(running):
    status, body, _ = running.call("/store/commit", "POST", {**AUTH, "If-Match": "0"},
                                   commit_body({"index.age": b"cipher"}))

    assert (status, json.loads(body)) == (HTTPStatus.OK, {"version": 1})
    assert running.call("/store/index.age", headers=AUTH)[:2] == (HTTPStatus.OK, b"cipher")
    assert running.status(f"/store/{VAR}", headers=AUTH) == HTTPStatus.NOT_FOUND


@pytest.mark.spec("docs/service.md")
def test_should_round_trip_a_wrapped_remote_identity(running):
    running.call("/store/commit", "POST", {**AUTH, "If-Match": "0"},
                 commit_body({"identity-remote.phone.age": b"wrapped"}))

    assert running.call("/store/identity-remote.phone.age", headers=AUTH)[:2] == (HTTPStatus.OK, b"wrapped")


@pytest.mark.spec("docs/service.md")
def test_should_answer_412_with_the_current_version_to_a_stale_commit(running):
    running.store.commit({"index.age": b"one"}, [], expected_version=0)

    status, body, _ = running.call("/store/commit", "POST", {**AUTH, "If-Match": "0"},
                                   commit_body({"index.age": b"two"}))

    assert (status, json.loads(body)) == (HTTPStatus.PRECONDITION_FAILED, {"error": "version is 1"})


@pytest.mark.spec("docs/service.md")
def test_should_refuse_a_forged_meta_write(running):
    status = running.status("/store/commit", "POST", {**AUTH, "If-Match": "0"}, commit_body({"meta.json": b"x"}))

    assert status == HTTPStatus.BAD_REQUEST


@pytest.mark.spec("docs/service.md")
@pytest.mark.parametrize("path", ["/store/meta.json", "/store/..%2Fsecrets", "/store/vars/short.age"])
def test_should_refuse_to_read_a_path_that_is_not_a_store_file(running, path):
    assert running.status(path, headers=AUTH) == HTTPStatus.BAD_REQUEST


@pytest.mark.spec("docs/service.md")
def test_should_require_if_match_on_a_commit(running):
    assert running.status("/store/commit", "POST", AUTH, COMMIT_BODY) == HTTPStatus.PRECONDITION_REQUIRED
    assert running.store.meta().version == 0


@pytest.mark.spec("docs/service.md")
@pytest.mark.parametrize("body", [b"[]", b'{"files": []}', b'{"delete": "index.age"}', b"{nope"])
def test_should_refuse_a_malformed_commit(running, body):
    assert running.status("/store/commit", "POST", {**AUTH, "If-Match": "0"}, body) == HTTPStatus.BAD_REQUEST


@pytest.mark.spec("docs/service.md")
@pytest.mark.parametrize("path", ["/store/commit", "/requests", "/r/any/response", "/notify"])
def test_should_refuse_a_post_that_is_not_json(running, path):
    status = running.status(path, "POST", {**AUTH, "If-Match": "0", "Content-Type": "text/plain"}, b"{}")

    assert status == HTTPStatus.UNSUPPORTED_MEDIA_TYPE


@pytest.mark.spec("docs/service.md")
@pytest.mark.parametrize("site", ["cross-site", "same-site"])
def test_should_refuse_a_browser_post_from_another_site(running, site):
    status = running.status("/store/commit", "POST", {**AUTH, "If-Match": "0", "Sec-Fetch-Site": site}, COMMIT_BODY)

    assert status == HTTPStatus.FORBIDDEN
    assert running.store.meta().version == 0


@pytest.mark.spec("docs/service.md")
def test_should_accept_a_browser_post_from_its_own_pages(running):
    status = running.status("/store/commit", "POST", {**AUTH, "If-Match": "0", "Sec-Fetch-Site": "same-origin"},
                            COMMIT_BODY)

    assert status == HTTPStatus.OK


@pytest.mark.spec("docs/service.md")
def test_should_run_the_unlock_request_flow(running):
    status, body, _ = running.call("/requests", "POST", AUTH, request_body())
    request_id = json.loads(body)["id"]
    pending = running.call(f"/r/{request_id}", headers=AUTH)

    answered = running.answer(request_id, {"response": "sealed"})

    assert status == HTTPStatus.OK
    assert (json.loads(pending[1])["vars"], json.loads(pending[1])["response"]) == (["A", "B"], None)
    assert answered == HTTPStatus.OK
    assert json.loads(running.call(f"/r/{request_id}", headers=AUTH)[1])["response"] == "sealed"
    assert running.status(f"/r/{request_id}", headers=AUTH) == HTTPStatus.NOT_FOUND


@pytest.mark.spec("docs/service.md")
def test_should_refuse_the_request_endpoints_without_an_identity_or_token(running):
    assert running.status("/requests", "POST", body=request_body()) == HTTPStatus.FORBIDDEN
    assert running.status("/r/whatever") == HTTPStatus.FORBIDDEN


@pytest.mark.spec("docs/service.md")
def test_should_answer_404_for_an_unknown_request(running):
    assert running.status("/r/missing", headers=AUTH) == HTTPStatus.NOT_FOUND
    assert running.status("/r/missing/request", headers=AUTH) == HTTPStatus.NOT_FOUND
    assert running.answer("missing", {"response": "sealed"}) == HTTPStatus.NOT_FOUND


@pytest.mark.spec("docs/service.md")
@pytest.mark.parametrize("body", [b'{"vars": ["A"]}', request_body(session="nope"), b"{nope"])
def test_should_answer_400_to_a_malformed_request(running, body):
    status, response, _ = running.call("/requests", "POST", BEARER, body)

    assert status == HTTPStatus.BAD_REQUEST
    assert "error" in json.loads(response)


@pytest.mark.spec("docs/service.md")
@pytest.mark.parametrize("answer", [{}, {"response": 7}])
def test_should_answer_400_to_a_response_that_is_not_a_string(running, answer):
    assert running.answer(running.create(), answer) == HTTPStatus.BAD_REQUEST


@pytest.mark.spec("docs/service.md")
def test_should_show_the_approver_the_request_without_taking_the_answer(running):
    request_id = running.create()
    running.answer(request_id, {"response": "sealed"})

    status, body, _ = running.call(f"/r/{request_id}/request", headers=AUTH)

    assert status == HTTPStatus.OK
    assert json.loads(body)["answered"] is True and "response" not in json.loads(body)
    assert json.loads(running.call(f"/r/{request_id}", headers=AUTH)[1])["response"] == "sealed"


@pytest.mark.spec("docs/service.md")
def test_should_accept_a_request_with_the_request_token(running):
    status, body, _ = running.call("/requests", "POST", BEARER, request_body())

    assert status == HTTPStatus.OK and json.loads(body)["id"]


@pytest.mark.spec("docs/service.md")
def test_should_refuse_a_request_with_a_wrong_token(running):
    status = running.status("/requests", "POST", {"Authorization": "Bearer wrong"}, request_body())

    assert status == HTTPStatus.FORBIDDEN


@pytest.mark.spec("docs/service.md")
def test_should_keep_the_store_closed_to_the_request_token(running):
    assert running.status("/store/meta", headers=BEARER) == HTTPStatus.FORBIDDEN
    assert running.status("/config", headers=BEARER) == HTTPStatus.FORBIDDEN


@pytest.mark.spec("docs/service.md")
def test_should_ignore_a_bearer_token_when_none_is_set(tokenless):
    assert tokenless.status("/requests", "POST", BEARER, request_body()) == HTTPStatus.FORBIDDEN


@pytest.mark.spec("docs/service.md")
def test_should_refuse_a_response_posted_with_the_request_token(running):
    request_id = running.create()

    status = running.status(f"/r/{request_id}/response", "POST", BEARER, b'{"response": "forged"}')

    assert status == HTTPStatus.FORBIDDEN


@pytest.mark.spec("docs/service.md")
def test_should_refuse_the_approver_view_to_the_request_token(running):
    assert running.status(f"/r/{running.create()}/request", headers=BEARER) == HTTPStatus.FORBIDDEN


@pytest.mark.spec("docs/service.md")
def test_should_answer_409_to_a_second_response(running):
    request_id = running.create()

    first, second = (running.answer(request_id, {"response": "sealed"}) for _ in range(2))

    assert (first, second) == (HTTPStatus.OK, HTTPStatus.CONFLICT)


@pytest.mark.spec("docs/service.md")
@pytest.mark.parametrize("outcome", ["absent", "released"])
def test_should_accept_a_response_that_still_carries_an_outcome(running, outcome):
    request_id = running.create()

    status = running.answer(request_id, {"response": "sealed", "outcome": outcome})

    assert status == HTTPStatus.OK
    assert json.loads(running.call(f"/r/{request_id}", headers=AUTH)[1])["response"] == "sealed"


@pytest.mark.spec("docs/service.md")
def test_should_let_the_requester_withdraw_its_request(running):
    request_id = running.create()

    status = running.status(f"/r/{request_id}", "DELETE", BEARER)

    assert status == HTTPStatus.NO_CONTENT
    assert running.status(f"/r/{request_id}/request", headers=AUTH) == HTTPStatus.NOT_FOUND


@pytest.mark.spec("docs/service.md")
@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer wrong"}])
def test_should_keep_a_request_that_a_caller_without_identity_or_token_withdraws(running, headers):
    request_id = running.create()

    status = running.status(f"/r/{request_id}", "DELETE", headers)

    assert status == HTTPStatus.FORBIDDEN
    assert running.status(f"/r/{request_id}/request", headers=AUTH) == HTTPStatus.OK


@pytest.mark.spec("docs/service.md")
def test_should_answer_404_to_withdrawing_an_unknown_request(running):
    assert running.status("/r/missing", "DELETE", BEARER) == HTTPStatus.NOT_FOUND


@pytest.mark.spec("docs/service.md")
def test_should_answer_429_when_requests_flood_in(tmp_path):
    flooded = Running(tmp_path, settings={"request_rate_limit": 1})
    try:
        first, second = (flooded.status("/requests", "POST", AUTH, request_body()) for _ in range(2))
    finally:
        flooded.server.shutdown()

    assert (first, second) == (HTTPStatus.OK, HTTPStatus.TOO_MANY_REQUESTS)


@pytest.mark.spec("docs/service.md")
def test_should_accept_an_allowed_login(tmp_path):
    allowlisted = Running(tmp_path, settings={"allowed_logins": frozenset({"owner@example.com"})})
    try:
        allowed = allowlisted.status("/store/meta", headers=AUTH)
        stranger = allowlisted.status("/store/meta", headers={DEFAULT_IDENTITY_HEADER: "guest@example.com"})
    finally:
        allowlisted.server.shutdown()

    assert (allowed, stranger) == (HTTPStatus.OK, HTTPStatus.FORBIDDEN)


@pytest.mark.spec("docs/service.md")
def test_should_accept_any_login_when_no_list_is_set(running):
    assert running.status("/store/meta", headers={DEFAULT_IDENTITY_HEADER: "guest@example.com"}) == HTTPStatus.OK


@pytest.mark.spec("docs/service.md")
def test_should_read_the_identity_from_the_configured_header(tmp_path):
    proxied = Running(tmp_path, settings={"identity_header": "X-Forwarded-User"})
    try:
        own = proxied.status("/store/meta", headers={"X-Forwarded-User": "owner"})
        default = proxied.status("/store/meta", headers=AUTH)
    finally:
        proxied.server.shutdown()

    assert (own, default) == (HTTPStatus.OK, HTTPStatus.FORBIDDEN)


@pytest.mark.spec("docs/service.md")
def test_should_refuse_a_blank_identity(running):
    assert running.status("/store/meta", headers={DEFAULT_IDENTITY_HEADER: "  "}) == HTTPStatus.FORBIDDEN


@pytest.mark.spec("docs/service.md")
def test_should_refuse_a_body_over_the_cap_without_reading_it(running):
    host, port = running.base.removeprefix("http://").split(":")
    connection = http.client.HTTPConnection(host, int(port), timeout=5)
    connection.putrequest("POST", "/store/commit")
    for name, value in {**AUTH, "Content-Type": "application/json",
                        "Content-Length": str(service.MAX_BODY_BYTES + 1)}.items():
        connection.putheader(name, value)
    connection.endheaders()

    assert connection.getresponse().status == HTTPStatus.REQUEST_ENTITY_TOO_LARGE


@pytest.mark.spec("docs/service.md")
def test_should_give_up_on_a_silent_connection():
    assert service.Handler.timeout == service.SOCKET_TIMEOUT_SECONDS


@pytest.mark.spec("docs/service.md")
def test_should_answer_404_for_an_unknown_endpoint(running):
    assert running.status("/nope") == HTTPStatus.NOT_FOUND
    assert running.status("/nope", "POST", AUTH, b"{}") == HTTPStatus.NOT_FOUND
    assert running.status("/nope", "DELETE", AUTH) == HTTPStatus.NOT_FOUND


@pytest.mark.spec("docs/service.md")
def test_should_tell_the_pages_which_requesters_to_expect(tmp_path):
    configured = Running(tmp_path, settings={"requesters": (Requester("devbox", "dev box"), Requester("mac", "Mac"))})
    try:
        status, body, _ = configured.call("/config", headers=AUTH)
        anonymous = configured.status("/config")
    finally:
        configured.server.shutdown()

    assert status == HTTPStatus.OK
    assert json.loads(body) == {"requesters": {"devbox": {"label": "dev box"}, "mac": {"label": "Mac"}}}
    assert anonymous == HTTPStatus.FORBIDDEN


@pytest.mark.spec("docs/service.md")
@pytest.mark.parametrize(("route", "marker", "content_type"), [
    ("/enroll", b"enrol", HTML), ("/approve", b"approve", HTML), ("/approve?id=abc123", b"approve", HTML),
    ("/typage.js", b"age", JAVASCRIPT), ("/approve.js", b"core", JAVASCRIPT), ("/unlock.css", b"margin", CSS),
])
def test_should_serve_the_pages_without_an_identity(running, route, marker, content_type):
    status, body, headers = running.call(route)

    assert status == HTTPStatus.OK
    assert marker in body
    assert headers["Content-Type"] == content_type
    assert headers["Cache-Control"] == "no-cache"


@pytest.mark.spec("docs/service.md")
@pytest.mark.parametrize("route", ["/enroll", "/approve"])
def test_should_serve_pages_with_a_strict_content_security_policy(running, route):
    status, _, headers = running.call(route)
    policy = headers["Content-Security-Policy"]

    assert status == HTTPStatus.OK
    assert "script-src 'self'" in policy and "style-src 'self'" in policy
    assert "'unsafe-inline'" not in policy
    assert "frame-ancestors 'none'" in policy
    assert (headers["X-Content-Type-Options"], headers["Referrer-Policy"]) == ("nosniff", "no-referrer")


@pytest.mark.spec("docs/service.md")
def test_should_answer_304_when_the_browser_has_the_current_page(running):
    _, _, first = running.call("/typage.js")

    status, body, headers = running.call("/typage.js", headers={"If-None-Match": first["ETag"]})

    assert (status, body) == (HTTPStatus.NOT_MODIFIED, b"")
    assert (headers["ETag"], headers["Content-Type"]) == (first["ETag"], JAVASCRIPT)


@pytest.mark.spec("docs/service.md")
def test_should_serve_the_new_page_when_its_content_changed(tmp_path):
    source = FakePages()
    served = Running(tmp_path, page_source=source)
    try:
        _, _, first = served.call("/typage.js")
        source.pages["/typage.js"] = Page(b"export const age = { v: 2 };", JAVASCRIPT)
        status, body, _ = served.call("/typage.js", headers={"If-None-Match": first["ETag"]})
    finally:
        served.server.shutdown()

    assert (status, body) == (HTTPStatus.OK, b"export const age = { v: 2 };")


@pytest.mark.spec("docs/service.md")
def test_should_gzip_a_page_when_the_browser_accepts_gzip(running):
    status, body, headers = running.call("/typage.js", headers={"Accept-Encoding": "gzip, deflate, br"})

    assert status == HTTPStatus.OK
    assert (headers["Content-Encoding"], headers["Vary"]) == ("gzip", "Accept-Encoding")
    assert gzip.decompress(body) == b"export const age = {};"


@pytest.mark.spec("docs/service.md")
def test_should_serve_a_page_plain_when_the_browser_does_not_accept_gzip(running):
    status, body, headers = running.call("/typage.js", headers={"Accept-Encoding": "identity"})

    assert (status, body) == (HTTPStatus.OK, b"export const age = {};")
    assert "Content-Encoding" not in headers


@pytest.mark.spec("docs/service.md")
def test_should_answer_404_for_a_page_the_source_lacks(running):
    assert running.status("/enroll.js") == HTTPStatus.NOT_FOUND


@pytest.mark.spec("docs/service.md")
def test_should_send_no_notice_for_a_request_or_its_answer(notified):
    served, notifier = notified

    status = served.answer(served.create(), {"response": "sealed"})

    assert status == HTTPStatus.OK
    assert notifier.notices == []


@pytest.mark.spec("docs/service.md")
@pytest.mark.parametrize(("headers", "origin"), [
    (BEARER, TOKEN_LABEL), (AUTH, IDENTITY_LABEL), ({**AUTH, **BEARER}, TOKEN_LABEL),
])
def test_should_relay_a_notice_named_by_how_its_sender_authenticated(notified, headers, origin):
    served, notifier = notified

    status = served.status("/notify", "POST", headers, NOTICE)

    assert status == HTTPStatus.ACCEPTED
    notice, = notifier.notices
    assert (str(notice.session), notice.container, notice.text, notice.origin) == (
        "4a015f0f-199b-4270-9c06-fecc5d85b976", "f635a28c3a2e", "Waiting for your OK.", origin)


@pytest.mark.spec("docs/service.md")
@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer wrong"}])
def test_should_refuse_a_notice_without_token_or_identity(notified, headers):
    served, notifier = notified

    assert served.status("/notify", "POST", headers, NOTICE) == HTTPStatus.FORBIDDEN
    assert notifier.notices == []


@pytest.mark.spec("docs/service.md")
@pytest.mark.parametrize("body", [b"{not json", b"{}", json.dumps({"session": "x", "text": "hi"}).encode()])
def test_should_refuse_a_malformed_notice(notified, body):
    served, notifier = notified

    assert served.status("/notify", "POST", BEARER, body) == HTTPStatus.BAD_REQUEST
    assert notifier.notices == []


@pytest.mark.spec("docs/service.md")
def test_should_limit_notices_apart_from_requests(tmp_path):
    notifier = RecordingNotifier()
    served = Running(tmp_path, notifier=notifier, notice_rate_limit=1, settings={"request_token": REQUEST_TOKEN})
    try:
        first, second = (served.status("/notify", "POST", BEARER, NOTICE) for _ in range(2))
        request = served.status("/requests", "POST", BEARER, request_body())
    finally:
        served.server.shutdown()

    assert (first, second, request) == (HTTPStatus.ACCEPTED, HTTPStatus.TOO_MANY_REQUESTS, HTTPStatus.OK)
    assert len(notifier.notices) == 1


@pytest.mark.spec("docs/service.md")
def test_should_describe_its_settings_without_their_secrets(tmp_path):
    settings = Settings(store=tmp_path, port=0, request_token=REQUEST_TOKEN, telegram_token="bot-secret",
                        telegram_chat="1", heartbeat_url="https://monitor.example/push/secret")
    served = service.build_server(settings, notifier=RecordingNotifier())
    try:
        line = service.describe(settings, served)
    finally:
        served.server_close()

    assert "request token on, telegram on, heartbeat on" in line
    assert not any(secret in line for secret in (REQUEST_TOKEN, "bot-secret", "push/secret"))


class CountingQueue:
    def __init__(self) -> None:
        self.sweeps = 0
        self.swept_twice = threading.Event()

    def sweep(self) -> None:
        self.sweeps += 1
        if self.sweeps == 2:
            self.swept_twice.set()


@pytest.mark.spec("docs/service.md")
def test_should_sweep_the_queue_each_interval_until_stopped():
    queue, stopped = CountingQueue(), threading.Event()
    thread = threading.Thread(target=service.sweep_until_stopped, args=(queue, stopped, 0.01), daemon=True)

    thread.start()
    swept = queue.swept_twice.wait(SWEEP_DEADLINE_SECONDS)
    stopped.set()
    thread.join(SWEEP_DEADLINE_SECONDS)

    assert swept
    assert not thread.is_alive()


@pytest.mark.spec("docs/service.md#stop-it")
def test_should_wake_the_sweep_from_its_wait_when_told_to_stop():
    queue, stopped = CountingQueue(), threading.Event()
    thread = threading.Thread(target=service.sweep_until_stopped, args=(queue, stopped, LONG_SWEEP_INTERVAL_SECONDS),
                              daemon=True)
    thread.start()

    stopped.set()
    thread.join(SWEEP_DEADLINE_SECONDS)

    assert not thread.is_alive()
    assert queue.sweeps == 0
