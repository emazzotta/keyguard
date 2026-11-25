from __future__ import annotations

import io
import json
import urllib.error
import urllib.request
from contextlib import redirect_stderr

import pytest

from keyguard_passkey.service.notify import ClaudeNotice, NoticeKind
from keyguard_passkey.service.presence import Presence
from keyguard_passkey.service.spoken import RELAY_TIMEOUT_SECONDS, SpokenAnswerRelay, spoken_answer_relay

URL = "http://telegram-bot.telegram-bot:8080/spoken-answer"
TOKEN = "spoken-answer-test-token"
SESSION = "4a015f0f-199b-4270-9c06-fecc5d85b976"
TEXT = "ZEBRA-QUILL-7731 is what the answer says."
REPLY_TO = 501
SEEN = 1_764_061_200


def answer(presence: Presence = Presence.AWAY, seen: int | None = SEEN) -> ClaudeNotice:
    return ClaudeNotice(SESSION, "devbox-7f7d78d989-tgd9t", TEXT, "dev box", NoticeKind.ANSWER,
                        presence=presence, seen=seen)


@pytest.mark.spec("docs/service.md")
def test_should_post_the_answer_to_the_bot_with_the_shared_token():
    sent: list[urllib.request.Request] = []

    SpokenAnswerRelay(URL, TOKEN, send=sent.append).relay(answer(), REPLY_TO)

    [request] = sent
    assert (request.full_url, request.get_method()) == (URL, "POST")
    assert request.get_header("Authorization") == f"Bearer {TOKEN}"
    assert request.get_header("Content-type") == "application/json"
    assert json.loads(request.data) == {"session": SESSION, "text": TEXT, "presence": "away", "seen": SEEN,
                                        "reply_to": REPLY_TO}


@pytest.mark.spec("docs/service.md")
def test_should_pass_on_an_unknown_presence_and_a_missing_last_use():
    sent: list[urllib.request.Request] = []

    SpokenAnswerRelay(URL, TOKEN, send=sent.append).relay(answer(Presence.UNKNOWN, None), REPLY_TO)

    body = json.loads(sent[0].data)
    assert (body["presence"], body["seen"]) == ("unknown", None)


@pytest.mark.spec("docs/service.md")
@pytest.mark.parametrize(("failure", "detail"), [
    (urllib.error.HTTPError(URL, 401, "Unauthorized", {}, None), "HTTP 401"),
    (urllib.error.URLError("connection refused"), "URLError"),
    (TimeoutError("timed out"), "TimeoutError"),
])
def test_should_log_one_line_without_the_text_or_token_when_the_bot_cannot_take_it(failure, detail):
    def fail(request: urllib.request.Request) -> None:
        raise failure
    stderr = io.StringIO()

    with redirect_stderr(stderr):
        SpokenAnswerRelay(URL, TOKEN, send=fail).relay(answer(), REPLY_TO)

    assert stderr.getvalue() == f"spoken answer: not sent ({detail})\n"


@pytest.mark.spec("docs/service.md")
def test_should_give_the_bot_ten_seconds():
    assert RELAY_TIMEOUT_SECONDS == 10


@pytest.mark.spec("docs/service.md")
@pytest.mark.parametrize(("url", "token"), [("", ""), (URL, ""), ("", TOKEN)])
def test_should_build_no_relay_without_both_url_and_token(url, token):
    assert spoken_answer_relay(url, token) is None


@pytest.mark.spec("docs/service.md")
def test_should_build_a_relay_from_url_and_token():
    assert spoken_answer_relay(URL, TOKEN) == SpokenAnswerRelay(URL, TOKEN)
