from __future__ import annotations

import base64
import http.client
import json
import threading
from http import HTTPStatus

import pytest

from keyguard_passkey import pages
from keyguard_passkey.ceremony import (
    TOKEN_HEADER,
    Ceremony,
    CeremonyHandler,
    CeremonyServer,
    Op,
    Request,
    Unanswered,
)
from keyguard_passkey.store import Store

VARIABLE = "vars/" + "cd" * 32 + ".age"
TEST_WAIT_SECONDS = 10.0


class Served:
    def __init__(self, server: CeremonyServer) -> None:
        self.server = server
        self.result: dict[str, object] = {}
        self.headers: dict[str, str] = {}
        self.thread = threading.Thread(target=self._wait, daemon=True)
        self.thread.start()

    def _wait(self) -> None:
        self.result["answer"] = self.server.wait(seconds=TEST_WAIT_SECONDS)

    def call(self, method: str, path: str, body: object = None, token: str | None = "", host: str | None = None,
             headers: dict[str, str] | None = None) -> tuple[int, bytes]:
        sent = dict(headers or {})
        if token is not None:
            sent[TOKEN_HEADER] = token or self.server.ceremony.token
        if host is not None:
            sent["Host"] = host
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        connection.putrequest(method, path, skip_host=True)
        payload = b"" if body is None else json.dumps(body).encode()
        for name, value in {"Host": self.server.authority, "Content-Length": str(len(payload)), **sent}.items():
            connection.putheader(name, value)
        connection.endheaders(payload)
        response = connection.getresponse()
        self.headers = dict(response.getheaders())
        return response.status, response.read()


@pytest.fixture
def served(tmp_path):
    request = Request(op=Op.GET, names=("JIRA_TOKEN",), session="0f5e2a1c", title="Fix the form")
    server = CeremonyServer(Ceremony(request=request, store=Store(tmp_path / "store")))
    running = Served(server)
    yield running
    if server.ceremony.answer is None:
        server.ceremony.answer = {}
    running.thread.join(timeout=5)
    server.server_close()


@pytest.mark.spec("README.md")
def test_should_serve_the_page_before_any_token_is_known(served):
    status, body = served.call("GET", "/approve", token=None)

    assert status == 200
    assert b"approve.js" in body


@pytest.mark.spec("README.md")
@pytest.mark.parametrize("route", [pages.ENROLL_ROUTE, pages.APPROVE_ROUTE])
def test_should_serve_the_pages_under_the_security_headers_of_the_service(served, route):
    status, _ = served.call("GET", route, token=None)

    assert status == 200
    assert pages.SECURITY_HEADERS.items() <= served.headers.items()


@pytest.mark.spec("README.md")
@pytest.mark.parametrize("op, route", [(Op.INIT, "/enroll#token="), (Op.GET, "/approve#token="),
                                       (Op.SET, "/approve#token=")])
def test_should_send_the_browser_to_the_page_for_the_operation(tmp_path, op, route):
    server = CeremonyServer(Ceremony(request=Request(op=op), store=Store(tmp_path / "store")))

    address = server.page_url
    server.server_close()

    assert address == f"http://{server.authority}{route}{server.ceremony.token}"


@pytest.mark.spec("README.md")
def test_should_hand_the_page_its_request(served):
    status, body = served.call("GET", "/request")

    assert status == 200
    assert json.loads(body) == {"op": "get", "names": ["JIRA_TOKEN"], "values": {}, "force": False,
                                "session": "0f5e2a1c", "title": "Fix the form"}


@pytest.mark.spec("README.md")
@pytest.mark.parametrize("token", [None, "not-the-token"])
def test_should_refuse_the_request_without_the_token_of_this_run(served, token):
    status, _ = served.call("GET", "/request", token=token)

    assert status == 403


@pytest.mark.spec("README.md")
def test_should_not_answer_under_another_host_name(served):
    status, _ = served.call("GET", "/request", host=f"127.0.0.1:{served.server.server_port}")

    assert status == 404


@pytest.mark.spec("README.md")
def test_should_commit_files_and_serve_them_back(served):
    files = {"index.age": base64.b64encode(b"index").decode(), VARIABLE: base64.b64encode(b"secret").decode()}

    status, body = served.call("POST", "/store/commit", {"files": files, "delete": []}, headers={"If-Match": "0"})

    assert (status, json.loads(body)) == (200, {"version": 1})
    assert served.call("GET", f"/store/{VARIABLE}") == (200, b"secret")
    assert json.loads(served.call("GET", "/store/meta")[1])["version"] == 1


@pytest.mark.spec("README.md")
def test_should_answer_412_when_the_store_moved_since_the_page_read_it(served):
    files = {"index.age": base64.b64encode(b"index").decode()}
    served.call("POST", "/store/commit", {"files": files}, headers={"If-Match": "0"})

    status, _ = served.call("POST", "/store/commit", {"files": files}, headers={"If-Match": "0"})

    assert status == 412


@pytest.mark.spec("README.md")
@pytest.mark.parametrize("body", [
    {"files": {"../escape.age": "eA=="}},
    {"files": {"index.age": "not base64!"}},
    {"files": ["index.age"]},
])
def test_should_refuse_a_malformed_commit(served, body):
    status, _ = served.call("POST", "/store/commit", body, headers={"If-Match": "0"})

    assert status == 400


@pytest.mark.spec("README.md")
def test_should_not_take_an_answer_without_the_token(served):
    status, _ = served.call("POST", "/answer", {"values": {"JIRA_TOKEN": "stolen"}}, token="guess")

    assert status == 403
    assert served.server.ceremony.answer is None


@pytest.mark.spec("README.md")
def test_should_end_with_the_answer_of_the_page(served):
    status, _ = served.call("POST", "/answer", {"values": {"JIRA_TOKEN": "secret"}})
    served.thread.join(timeout=5)

    assert status == 204
    assert served.result["answer"] == {"values": {"JIRA_TOKEN": "secret"}}


@pytest.mark.spec("README.md")
def test_should_reply_to_the_page_before_the_answer_ends_the_wait(served, monkeypatch):
    answer_at_reply: list[object] = []
    reply = CeremonyHandler._send

    def recorded(handler: CeremonyHandler, status: HTTPStatus, *rest: object) -> None:
        if status == HTTPStatus.NO_CONTENT:
            answer_at_reply.append(handler.server.ceremony.answer)
        reply(handler, status, *rest)

    monkeypatch.setattr(CeremonyHandler, "_send", recorded)

    served.call("POST", "/answer", {"values": {}})

    assert answer_at_reply == [None]


@pytest.mark.spec("README.md")
def test_should_give_up_when_no_answer_comes(tmp_path):
    ceremony = Ceremony(request=Request(op=Op.LIST), store=Store(tmp_path))

    with CeremonyServer(ceremony) as server, pytest.raises(Unanswered):
        server.wait(seconds=0.2)
