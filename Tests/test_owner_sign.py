"""Tests for POST /_owner/sign, which hands a message to the keyguard-passkey client for the approve page to sign."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from conftest import http_get, http_post

from keyguard_server import owner_sign, phone
from keyguard_server.caller import Caller

SERVICE = "https://unlock.example.ts.net"
TARGET = "212df38e-7c4b-4a51-9d0e-3f6b2a8c1e57"
TOKEN = "eyJ2IjoxfQ.c2lnbmF0dXJl"
MESSAGE = owner_sign.SignRequest(text="yes, push personal-infra 06be524c", target=TARGET, box="macbook",
                                 title="Mazzotta-bot")
BODY = {"text": MESSAGE.text, "target": TARGET, "box": "macbook", "title": "Mazzotta-bot"}
FAST = 5.0


def script(path: Path, body: str) -> Path:
    path.write_text(f"#!/bin/sh\n{body}\n")
    path.chmod(0o755)
    return path


@pytest.fixture()
def client(tmp_path):
    def install(body: str) -> phone.PhoneClient:
        return phone.PhoneClient(client=script(tmp_path / "keyguard-passkey.pyz", body),
                                 signing_key=tmp_path / "mac-sign.key", service=SERVICE)
    return install


# ---- The body ----


@pytest.mark.parametrize("body", [BODY, {key: value for key, value in BODY.items() if key != "title"}])
def test_should_read_the_text_target_box_and_optional_title(body):
    assert owner_sign.parse(json.dumps(body)) == owner_sign.SignRequest(
        text=body["text"], target=TARGET, box="macbook", title=body.get("title", ""))


@pytest.mark.parametrize("body", [
    "not json",
    json.dumps(["yes"]),
    json.dumps({key: value for key, value in BODY.items() if key != "text"}),
    json.dumps({**BODY, "target": 212}),
    json.dumps({**BODY, "title": None}),
    json.dumps({**BODY, "value": "x"}),
])
def test_should_refuse_a_body_that_is_not_the_message_shape(body):
    assert owner_sign.parse(body) is None


# ---- The client ----


def test_should_ask_the_store_service_as_the_mac_to_sign_for_the_target():
    asking = phone.PhoneClient(client=Path("/c.pyz"), signing_key=Path("/k"), service=SERVICE)

    assert owner_sign.argv(asking, MESSAGE) == [
        "/c.pyz", "--service", SERVICE, "--requester", "mac", "--signing-key", "/k",
        "sign", "--target", TARGET, "--box", "macbook", "--title", "Mazzotta-bot"]


def test_should_leave_out_the_title_when_the_caller_names_none():
    asking = phone.PhoneClient(client=Path("/c.pyz"), signing_key=Path("/k"), service=SERVICE)

    assert "--title" not in owner_sign.argv(asking, owner_sign.SignRequest(text="yes", target=TARGET, box="mac"))


def test_should_answer_the_token_and_send_the_text_on_standard_input(client, tmp_path):
    received = tmp_path / "stdin"

    signed = owner_sign.sign(MESSAGE, Caller(), client(f"cat > {received}; echo {TOKEN}"), timeout=FAST)

    assert signed == owner_sign.Signed(200, TOKEN)
    assert received.read_text() == MESSAGE.text


def test_should_run_the_client_for_the_asking_session(client, tmp_path):
    seen = tmp_path / "env"

    owner_sign.sign(MESSAGE, Caller(session="48f92bcb", title="Fleet master"),
                    client(f'echo "$KEYGUARD_SESSION|$KEYGUARD_TITLE" > {seen}; echo {TOKEN}'), timeout=FAST)

    assert seen.read_text() == "48f92bcb|Fleet master\n"


@pytest.mark.parametrize(("exit_code", "status"), [(2, 403), (4, 404), (64, 400), (3, 504), (1, 502)])
def test_should_answer_the_client_failure_with_its_status_and_reason(client, exit_code, status):
    signed = owner_sign.sign(MESSAGE, Caller(), client(f"echo 'keyguard-passkey: reason' >&2; exit {exit_code}"),
                             timeout=FAST)

    assert signed == owner_sign.Signed(status, "keyguard-passkey: reason")


def test_should_answer_504_when_the_client_outlives_its_time(client):
    signed = owner_sign.sign(MESSAGE, Caller(), client("sleep 5"), timeout=0.2)

    assert signed.status == 504


def test_should_answer_502_when_the_client_prints_no_token(client):
    assert owner_sign.sign(MESSAGE, Caller(), client("echo 'not a token'"), timeout=FAST).status == 502


def test_should_wait_longer_than_the_client_waits_for_the_page():
    assert owner_sign.SIGN_TIMEOUT > phone.PHONE_WAIT


# ---- The handler ----


@pytest.fixture()
def signing(monkeypatch, tmp_path):
    asking = phone.PhoneClient(client=tmp_path / "keyguard-passkey.pyz", signing_key=tmp_path / "mac-sign.key",
                               service=SERVICE)
    seen: dict[str, object] = {}
    monkeypatch.setattr(phone, "configured", lambda: asking)

    def sign(request, caller, client):
        seen.update(request=request, caller=caller, client=client)
        return owner_sign.Signed(200, TOKEN)

    monkeypatch.setattr(owner_sign, "sign", sign)
    return seen


def test_should_answer_the_token_for_the_asking_session(server, signing):
    status, body = http_post(server, "/_owner/sign", json.dumps(BODY), {"X-Keyguard-Session": "48f92bcb"})

    assert (status, body) == (200, TOKEN)
    assert (signing["request"], signing["caller"]) == (MESSAGE, Caller(session="48f92bcb"))


@pytest.mark.parametrize("header", ["Origin", "Sec-Fetch-Site"])
def test_should_refuse_a_sign_request_a_browser_sent(server, signing, header):
    status, _ = http_post(server, "/_owner/sign", json.dumps(BODY), {header: "https://evil.example"})

    assert status == 403
    assert signing == {}


def test_should_refuse_a_malformed_sign_request(server, signing):
    status, _ = http_post(server, "/_owner/sign", json.dumps({"text": "yes"}))

    assert status == 400
    assert signing == {}


def test_should_answer_503_while_the_phone_client_is_not_set_up(server, monkeypatch):
    monkeypatch.setattr(phone, "configured", lambda: None)

    status, body = http_post(server, "/_owner/sign", json.dumps(BODY))

    assert status == 503
    assert "KEYGUARD_STORE_URL" in body


# ---- The owner key ----


@pytest.mark.parametrize("path", ["/KEYGUARD_OWNER_SIGNING_KEY", "/A,KEYGUARD_OWNER_SIGNING_KEY?timeout=60"])
def test_should_refuse_to_read_the_owner_key_before_any_prompt(server, path):
    with patch("subprocess.run") as run, patch("subprocess.Popen") as popen:
        status, body = http_get(server, path)

    assert status == 403
    assert "never released" in body
    assert (run.called, popen.called) == (False, False)


def test_should_refuse_to_replace_the_owner_key(server):
    with patch("subprocess.run") as run:
        status, _ = http_post(server, "/KEYGUARD_OWNER_SIGNING_KEY", "-----BEGIN PRIVATE KEY-----")

    assert status == 403
    assert run.called is False
