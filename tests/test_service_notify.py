from __future__ import annotations

import io
import json
import urllib.error
import urllib.request
from contextlib import redirect_stderr
from uuid import UUID

import pytest

from keyguard_passkey.service.notify import (
    NOTICE_INPUT_LIMIT,
    NOTICE_TEXT_LIMIT,
    ClaudeNotice,
    DisabledNotifier,
    TelegramNotifier,
    notice_message,
    parse_notice,
    telegram_notifier,
)

TOKEN = "123456:bot-secret"
CHAT = "4242"
SESSION = "4a015f0f-199b-4270-9c06-fecc5d85b976"
ORIGIN = "dev box"


def run_inline(function, *args):
    function(*args)


def notifier(send) -> TelegramNotifier:
    return TelegramNotifier(TOKEN, CHAT, send=send, spawn=run_inline)


def notice(**over: object) -> dict[str, object]:
    return {"session": SESSION, "container": "devbox-7f7d78d989-tgd9t", "text": "Waiting for your OK.", **over}


def ping(target: TelegramNotifier) -> None:
    target.claude_notice(parse_notice(notice(), ORIGIN))


@pytest.mark.spec("docs/service.md")
def test_should_post_the_ping_to_the_chat_without_a_link_preview():
    sent: list[urllib.request.Request] = []

    ping(notifier(sent.append))

    request, = sent
    body = json.loads(request.data)
    assert request.full_url == f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    assert request.get_header("Content-type") == "application/json"
    assert (body["chat_id"], body["link_preview_options"]) == (CHAT, {"is_disabled": True})


@pytest.mark.spec("docs/service.md")
def test_should_send_the_ping_as_telegram_html():
    sent: list[urllib.request.Request] = []

    ping(notifier(sent.append))

    assert json.loads(sent[0].data)["parse_mode"] == "HTML"


@pytest.mark.spec("docs/service.md")
@pytest.mark.parametrize("failure", [
    urllib.error.HTTPError(f"https://api.telegram.org/bot{TOKEN}/sendMessage", 401, "Unauthorized", {}, io.BytesIO()),
    urllib.error.URLError(f"cannot reach https://api.telegram.org/bot{TOKEN}"),
    TimeoutError("timed out"),
])
def test_should_report_a_failed_send_without_the_token(failure):
    def fail(_: urllib.request.Request) -> None:
        raise failure
    stderr = io.StringIO()

    with redirect_stderr(stderr):
        ping(notifier(fail))

    assert "message not sent" in stderr.getvalue()
    assert TOKEN not in stderr.getvalue()


@pytest.mark.spec("docs/service.md")
def test_should_name_the_http_status_of_a_refused_send():
    def refuse(request: urllib.request.Request) -> None:
        raise urllib.error.HTTPError(request.full_url, 401, "Unauthorized", {}, None)
    stderr = io.StringIO()

    with redirect_stderr(stderr):
        ping(notifier(refuse))

    assert "HTTP 401" in stderr.getvalue()


@pytest.mark.spec("docs/service.md")
@pytest.mark.parametrize(("token", "chat"), [("", ""), (TOKEN, ""), ("", CHAT)])
def test_should_stay_silent_when_the_bot_is_not_set(token, chat):
    assert isinstance(telegram_notifier(token, chat), DisabledNotifier)


@pytest.mark.spec("docs/service.md")
def test_should_relay_pings_when_the_bot_is_set():
    assert isinstance(telegram_notifier(TOKEN, CHAT), TelegramNotifier)


@pytest.mark.spec("docs/service.md")
def test_should_head_the_text_with_the_origin_and_end_with_container_and_session():
    sent: list[urllib.request.Request] = []

    ping(notifier(sent.append))

    assert json.loads(sent[0].data)["text"] == (
        f"<b>Claude on dev box</b>\nWaiting for your OK.\n\ndevbox-7f7d78d989-tgd9t \u00b7 <code>{SESSION}</code>")


@pytest.mark.spec("docs/service.md")
def test_should_leave_out_the_container_when_the_notice_has_none():
    message = notice_message(parse_notice(notice(container=None), "laptop"))

    assert message == f"<b>Claude on laptop</b>\nWaiting for your OK.\n\n<code>{SESSION}</code>"


@pytest.mark.spec("docs/service.md")
def test_should_flatten_control_and_format_characters_out_of_the_text():
    text = "line one\nline two\u202e\tKeyguard\x07"

    message = notice_message(ClaudeNotice(UUID(SESSION), None, text, "laptop"))

    assert message.splitlines()[1] == "line one line two Keyguard"


@pytest.mark.spec("docs/service.md")
def test_should_cap_a_long_text_with_an_ellipsis():
    body = notice_message(ClaudeNotice(UUID(SESSION), None, "x" * 1000, "laptop")).splitlines()[1]

    assert len(body) == NOTICE_TEXT_LIMIT
    assert body.endswith("\u2026")


@pytest.mark.spec("docs/service.md")
def test_should_escape_html_in_the_origin_and_the_text():
    message = notice_message(ClaudeNotice(UUID(SESSION), None, "<b>1 < 2 & 3 > 2</b>", "<i>laptop</i>"))

    assert message.splitlines()[:2] == [
        "<b>Claude on &lt;i&gt;laptop&lt;/i&gt;</b>", "&lt;b&gt;1 &lt; 2 &amp; 3 &gt; 2&lt;/b&gt;"]


@pytest.mark.spec("docs/service.md")
def test_should_cap_the_text_before_escaping_it_so_no_entity_is_cut():
    body = notice_message(ClaudeNotice(UUID(SESSION), None, "&" * 1000, "laptop")).splitlines()[1]

    assert body == "&amp;" * (NOTICE_TEXT_LIMIT - 1) + "\u2026"


@pytest.mark.spec("docs/service.md")
@pytest.mark.parametrize("payload", [
    [],
    notice(session="not-a-uuid"),
    notice(session=None),
    notice(container="evil.example.com"),
    notice(container=""),
    notice(text=""),
    notice(text=" \u200b\n"),
    notice(text=7),
    notice(text="x" * (NOTICE_INPUT_LIMIT + 1)),
])
def test_should_refuse_a_malformed_notice(payload):
    with pytest.raises(ValueError):
        parse_notice(payload, "laptop")


@pytest.mark.spec("docs/service.md")
def test_should_drop_a_notice_when_telegram_is_off():
    DisabledNotifier().claude_notice(parse_notice(notice(), "laptop"))
