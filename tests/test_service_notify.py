from __future__ import annotations

import html
import io
import json
import re
import urllib.error
import urllib.request
from contextlib import redirect_stderr
from dataclasses import replace as dataclasses_replace

import pytest

from keyguard_passkey.service import notify
from keyguard_passkey.service.longtext import MESSAGE_LIMIT, utf16_length
from keyguard_passkey.service.notify import (
    ANSWER_HARD_LIMIT,
    ANSWER_WINDOW,
    CUT_NOTE,
    NOTICE_INPUT_LIMIT,
    NOTICE_TEXT_LIMIT,
    NOTICE_TITLE_LIMIT,
    ClaudeNotice,
    DisabledNotifier,
    NoticeKind,
    TelegramNotifier,
    answer_room,
    message_id_of,
    notice_message,
    notice_messages,
    parse_notice,
    telegram_notifier,
    with_session_link,
)
from keyguard_passkey.service.presence import Presence
from keyguard_passkey.service.scrub import REDACTED

TOKEN = "123456:bot-secret"
CHAT = "4242"
SESSION = "4a015f0f-199b-4270-9c06-fecc5d85b976"
ORIGIN = "dev box"
SPEECH_BALLOON = "\U0001f4ac"
HOURGLASS = "\u23f3"
PARTY = chr(0x1F389)
FAKE_TOKEN = "987654321:AAFakeScrubberTestSecret01234567-_x"
TERMINAL = "https://terminal.example.test"
BOT_MARKER = re.compile(r"^\[session ([0-9A-Za-z]{8})\]\s*\Z", re.MULTILINE)


def run_inline(function, *args):
    function(*args)


def notifier(send) -> TelegramNotifier:
    return TelegramNotifier(TOKEN, CHAT, send=send, spawn=run_inline)


def notice(**over: object) -> dict[str, object]:
    return {"session": SESSION, "container": "devbox-7f7d78d989-tgd9t", "text": "Waiting for your OK.", **over}


def ping(target: TelegramNotifier) -> None:
    target.claude_notice(parse_notice(notice(), ORIGIN))


def text_line(message: str) -> str:
    return message.splitlines()[2]


def text_of(message: str) -> str:
    return text_line(message).split(" ", 1)[1]


def footer_line(message: str) -> str:
    return message.splitlines()[-2]


def as_telegram_shows(message: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", message))


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
        f"<b>Claude on dev box</b>\n\n{SPEECH_BALLOON} Waiting for your OK.\n\n"
        f"devbox-7f7d78d989-tgd9t \u00b7 <code>{SESSION}</code>\n[session 4a015f0f]")


@pytest.mark.spec("docs/service.md")
def test_should_leave_out_the_container_when_the_notice_has_none():
    message = notice_message(parse_notice(notice(container=None), "laptop"))

    assert message == (f"<b>Claude on laptop</b>\n\n{SPEECH_BALLOON} Waiting for your OK.\n\n<code>{SESSION}</code>"
                       "\n[session 4a015f0f]")


@pytest.mark.spec("docs/service.md")
def test_should_put_the_title_before_container_and_session():
    message = notice_message(parse_notice(notice(title="Telegram alert layout"), ORIGIN))

    assert footer_line(message) == (
        f"Telegram alert layout \u00b7 devbox-7f7d78d989-tgd9t \u00b7 <code>{SESSION}</code>")


@pytest.mark.spec("docs/service.md")
@pytest.mark.parametrize("title", [{}, {"title": None}, {"title": " \u200b\n"}])
def test_should_leave_out_a_missing_or_blank_title(title):
    message = notice_message(parse_notice(notice(**title), ORIGIN))

    assert footer_line(message) == f"devbox-7f7d78d989-tgd9t \u00b7 <code>{SESSION}</code>"


@pytest.mark.spec("docs/service.md")
def test_should_cap_the_title_before_escaping_it():
    message = notice_message(parse_notice(notice(title="<i>" + "x" * 100, container=None), ORIGIN))

    assert footer_line(message) == (
        "&lt;i&gt;" + "x" * (NOTICE_TITLE_LIMIT - 4) + f"\u2026 \u00b7 <code>{SESSION}</code>")


@pytest.mark.spec("docs/service.md")
@pytest.mark.parametrize(("kind", "mark"), [
    ({"kind": "prompt"}, HOURGLASS),
    ({"kind": "finished"}, SPEECH_BALLOON),
    ({}, SPEECH_BALLOON),
])
def test_should_open_the_text_with_the_mark_of_its_kind(kind, mark):
    message = notice_message(parse_notice(notice(**kind), "laptop"))

    assert text_line(message) == f"{mark} Waiting for your OK."


@pytest.mark.spec("docs/service.md")
@pytest.mark.parametrize("kind", ["finished", "prompt"])
def test_should_end_every_ping_with_the_marker_the_bot_reads_from_a_reply(kind):
    message = notice_message(parse_notice(notice(kind=kind, title="<b>Plan</b> & more"), ORIGIN))

    found = BOT_MARKER.search(as_telegram_shows(message))

    assert found is not None
    assert found.group(1) == SESSION[:8]
    assert message.splitlines()[-1] == "[session 4a015f0f]"


@pytest.mark.spec("docs/service.md")
def test_should_flatten_control_and_format_characters_out_of_the_text():
    text = "line one\nline two\u202e\tKeyguard\x07"

    message = notice_message(ClaudeNotice(SESSION, None, text, "laptop"))

    assert text_of(message) == "line one line two Keyguard"


@pytest.mark.spec("docs/service.md")
def test_should_cap_a_long_text_with_an_ellipsis():
    body = text_of(notice_message(ClaudeNotice(SESSION, None, "x" * 1000, "laptop")))

    assert len(body) == NOTICE_TEXT_LIMIT
    assert body.endswith("\u2026")


@pytest.mark.spec("docs/service.md")
def test_should_escape_html_in_the_origin_and_the_text():
    message = notice_message(ClaudeNotice(SESSION, None, "<b>1 < 2 & 3 > 2</b>", "<i>laptop</i>"))

    assert message.splitlines()[0] == "<b>Claude on &lt;i&gt;laptop&lt;/i&gt;</b>"
    assert text_of(message) == "&lt;b&gt;1 &lt; 2 &amp; 3 &gt; 2&lt;/b&gt;"


@pytest.mark.spec("docs/service.md")
def test_should_cap_the_text_before_escaping_it_so_no_entity_is_cut():
    body = text_of(notice_message(ClaudeNotice(SESSION, None, "&" * 1000, "laptop")))

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
    notice(kind="question"),
    notice(kind=None),
    notice(kind=["prompt"]),
    notice(kind="keyguard_wait"),
    notice(kind="finished", session="4a015f0f"),
    notice(kind="answer", session=None),
    notice(kind="keyguard_failed", session="4a015f0"),
    notice(kind="keyguard_failed", session="4a015f0f!"),
    notice(kind="keyguard_failed", session=7),
    notice(kind="keyguard_failed", session="not-a-uuid"),
    notice(title=7),
    notice(title=["x"]),
    notice(title="x" * (NOTICE_INPUT_LIMIT + 1)),
])
def test_should_refuse_a_malformed_notice(payload):
    with pytest.raises(ValueError):
        parse_notice(payload, "laptop")


@pytest.mark.spec("docs/service.md")
def test_should_drop_a_notice_when_telegram_is_off():
    DisabledNotifier().claude_notice(parse_notice(notice(), "laptop"))


def answer(**over: object) -> dict[str, object]:
    return notice(kind="answer", **over)


def messages_of(payload: dict[str, object], origin: str = ORIGIN) -> list[str]:
    return notice_messages(parse_notice(payload, origin))


def shown_units(message: str) -> int:
    return utf16_length(as_telegram_shows(message))


def paragraphs(count: int, length: int = 1480) -> str:
    return "\n\n".join(f"{number:04d} " + "w" * (length - 5) for number in range(count))


def three_parts() -> str:
    """Paragraphs whose blank lines fall in the last quarter of a part, so each cut loses the most room."""
    return "\n\n".join(letter * size for letter, size in (("q", 2990), ("z", 2990), ("j", 1400)))


def counters(messages: list[str]) -> list[str]:
    return [text_line(message).split(" ")[1] for message in messages]


@pytest.mark.spec("docs/service.md")
def test_should_take_an_answer_of_the_hard_limit_and_keep_only_what_three_messages_can_show():
    parsed = parse_notice(answer(text="x" * ANSWER_HARD_LIMIT), ORIGIN)

    assert (parsed.kind, len(parsed.text), parsed.cut) == (NoticeKind.ANSWER, ANSWER_WINDOW, True)


@pytest.mark.spec("docs/service.md")
def test_should_keep_a_short_answer_whole_and_uncut():
    parsed = parse_notice(answer(text="One.\n\nTwo."), ORIGIN)

    assert (parsed.text, parsed.cut) == ("One.\n\nTwo.", False)


@pytest.mark.spec("docs/service.md")
def test_should_redact_no_more_of_an_answer_than_it_can_send(monkeypatch):
    scanned: list[int] = []
    monkeypatch.setattr(notify, "scrub", lambda text: scanned.append(len(text)) or text)

    parse_notice(answer(text="1" * ANSWER_HARD_LIMIT), ORIGIN)

    assert scanned == [ANSWER_WINDOW]


@pytest.mark.spec("docs/service.md")
def test_should_keep_the_old_limit_for_the_other_kinds():
    for kind in ("finished", "prompt"):
        with pytest.raises(ValueError):
            parse_notice(notice(kind=kind, text="x" * (NOTICE_INPUT_LIMIT + 1)), ORIGIN)


@pytest.mark.spec("docs/service.md")
@pytest.mark.parametrize("text", [
    "x" * (ANSWER_HARD_LIMIT + 1),
    PARTY * (ANSWER_HARD_LIMIT + 1),
    "",
    " \n\t" + chr(0x200B) + "\n",
    7,
    None,
])
def test_should_refuse_an_answer_that_is_blank_or_over_the_hard_limit(text):
    with pytest.raises(ValueError):
        parse_notice(answer(text=text), ORIGIN)


@pytest.mark.spec("docs/service.md")
def test_should_keep_the_line_breaks_of_an_answer_and_escape_them_for_telegram():
    message, = messages_of(answer(text="First <line> & more\n\n\n\n- item one\n- item two\r\nlast"))

    assert message.splitlines()[:7] == [
        f"<b>Claude on {ORIGIN}</b>", "", f"{SPEECH_BALLOON} First &lt;line&gt; &amp; more", "",
        "- item one", "- item two", "last"]


@pytest.mark.spec("docs/service.md")
def test_should_drop_control_and_format_characters_from_an_answer_but_not_its_line_breaks():
    message, = messages_of(answer(text="one" + chr(0x202E) + "two\x07\nthree"))

    assert text_line(message) == f"{SPEECH_BALLOON} one two"
    assert message.splitlines()[3] == "three"


@pytest.mark.spec("docs/service.md")
def test_should_send_a_short_answer_as_one_message_that_ends_like_a_ping():
    message, = messages_of(answer(title="Plan"))

    assert message.splitlines()[-2:] == [
        f"Plan \u00b7 devbox-7f7d78d989-tgd9t \u00b7 <code>{SESSION}</code>", "[session 4a015f0f]"]
    assert "(1/1)" not in message


@pytest.mark.spec("docs/service.md")
def test_should_split_a_long_answer_into_numbered_messages_that_each_end_with_the_marker():
    messages = messages_of(answer(text=paragraphs(4)))

    assert counters(messages) == ["(1/2)", "(2/2)"]
    for message in messages:
        assert message.splitlines()[-1] == "[session 4a015f0f]"
        assert BOT_MARKER.search(as_telegram_shows(message)) is not None
        assert shown_units(message) <= MESSAGE_LIMIT


@pytest.mark.spec("docs/service.md")
def test_should_split_the_longest_answer_into_three_messages():
    messages = messages_of(answer(text=three_parts()))

    assert counters(messages) == ["(1/3)", "(2/3)", "(3/3)"]
    assert [message.count(letter) for message, letter in zip(messages, "qzj")] == [2990, 2990, 1400]


@pytest.mark.spec("docs/service.md")
def test_should_lose_no_word_of_a_long_answer_when_it_splits():
    text = paragraphs(4)

    messages = messages_of(answer(text=text))

    shown = "".join(as_telegram_shows(message) for message in messages)
    assert shown.count("w") == text.count("w")
    assert [number for number in range(4) if f"{number:04d} " in shown] == [0, 1, 2, 3]


@pytest.mark.spec("docs/service.md")
def test_should_cut_an_answer_between_paragraphs_when_it_can():
    first, second = messages_of(answer(text=paragraphs(4, 1300)))

    assert "0002 " in first and "0003 " not in first
    assert second.splitlines()[2].split(" ", 2)[2].startswith("0003 ")


@pytest.mark.spec("docs/service.md")
@pytest.mark.parametrize("body", [
    "\n\n".join("p" * 2490 for _ in range(3)),
    "x" * ANSWER_HARD_LIMIT,
    PARTY * (ANSWER_HARD_LIMIT // 2),
    "&" * ANSWER_HARD_LIMIT,
    "line\n" * 20_000,
    paragraphs(60),
    "line\n" * 1500,
    three_parts(),
    paragraphs(5),
    paragraphs(3, 2400) + "\n\n" + "y" * 100,
])
def test_should_fit_the_longest_answer_in_three_messages_whatever_its_shape(body):
    worst_case = {"title": "&" * 80, "container": "c" * 63}

    messages = notice_messages(dataclasses_replace(parse_notice(answer(text=body, **worst_case), "&" * 80),
                                                   link=TERMINAL + "/chat/" + SESSION + "?" + "q" * 80))

    assert 1 <= len(messages) <= 3
    assert all(shown_units(message) <= MESSAGE_LIMIT for message in messages)


def room_of_an_answer() -> int:
    return answer_room(parse_notice(answer(), ORIGIN))


@pytest.mark.spec("docs/service.md")
def test_should_send_an_answer_that_fills_three_messages_exactly_without_a_note():
    messages = messages_of(answer(text="x" * (3 * room_of_an_answer())))

    assert counters(messages) == ["(1/3)", "(2/3)", "(3/3)"]
    assert not any(CUT_NOTE in message for message in messages)
    assert all(shown_units(message) <= MESSAGE_LIMIT for message in messages)


@pytest.mark.spec("docs/service.md")
def test_should_end_an_answer_that_is_one_unit_over_three_messages_with_the_note_and_the_link():
    link = f"{TERMINAL}/chat/{SESSION}"
    cut = with_session_link(parse_notice(answer(text="x" * (3 * room_of_an_answer() + 1), title="Plan"), ORIGIN),
                            TERMINAL)

    first, second, third = notice_messages(cut)

    assert CUT_NOTE not in first and CUT_NOTE not in second
    assert third.splitlines()[-6:] == [
        CUT_NOTE, "", f'<a href="{link}">Open the session in Safari</a>', "",
        f"Plan \u00b7 devbox-7f7d78d989-tgd9t \u00b7 <code>{SESSION}</code>", "[session 4a015f0f]"]
    assert all(shown_units(message) <= MESSAGE_LIMIT for message in (first, second, third))


@pytest.mark.spec("docs/service.md")
def test_should_keep_the_marker_on_the_last_line_of_every_message_of_a_cut_answer():
    messages = messages_of(answer(text=paragraphs(60)))

    assert counters(messages) == ["(1/3)", "(2/3)", "(3/3)"]
    assert [message.splitlines()[-1] for message in messages] == ["[session 4a015f0f]"] * 3
    assert [CUT_NOTE in message for message in messages] == [False, False, True]


@pytest.mark.spec("docs/service.md")
def test_should_cut_a_long_answer_at_a_paragraph_boundary_when_it_can():
    messages = messages_of(answer(text=paragraphs(60)))

    shown = "".join(as_telegram_shows(message) for message in messages)
    sizes = {len(line) for line in shown.splitlines() if line[:4].isdigit() and " w" in line[:7]}
    assert sizes == {1480}


@pytest.mark.spec("docs/service.md")
def test_should_note_the_cut_of_an_answer_that_redaction_shrank_to_one_message():
    messages = messages_of(answer(text=(FAKE_TOKEN + " ") * 600))

    message, = messages
    assert CUT_NOTE in message and "(1/1)" not in message
    assert "AAFakeScrubber" not in message


@pytest.mark.spec("docs/service.md")
def test_should_redact_a_bot_token_in_an_answer_before_it_splits():
    padding = "w" * 2900
    messages = messages_of(answer(text=f"{padding} {FAKE_TOKEN} {padding} {FAKE_TOKEN}"))

    shown = "".join(as_telegram_shows(message) for message in messages)
    assert "AAFakeScrubber" not in shown
    assert shown.count(REDACTED) == 2


@pytest.mark.spec("docs/service.md")
def test_should_link_the_last_message_of_an_answer_to_its_session_in_the_web_terminal():
    linked = with_session_link(parse_notice(answer(text=paragraphs(4)), ORIGIN), TERMINAL)

    first, last = notice_messages(linked)

    anchor = f'<a href="{TERMINAL}/chat/{SESSION}">Open the session in Safari</a>'
    assert anchor not in first
    assert last.splitlines()[-4:-2] == [anchor, ""]


@pytest.mark.spec("docs/service.md")
@pytest.mark.parametrize(("kind", "terminal"), [("answer", ""), ("finished", TERMINAL), ("prompt", TERMINAL)])
def test_should_add_no_link_without_a_web_terminal_address_or_to_another_kind(kind, terminal):
    parsed = parse_notice(notice(kind=kind), ORIGIN)

    assert with_session_link(parsed, terminal).link is None


@pytest.mark.spec("docs/service.md")
def test_should_send_the_parts_of_an_answer_in_order_in_one_background_call():
    sent: list[urllib.request.Request] = []
    calls: list[tuple] = []

    def spawn(function, *args):
        calls.append(args)
        function(*args)

    TelegramNotifier(TOKEN, CHAT, send=sent.append, spawn=spawn).claude_notice(
        parse_notice(answer(text=paragraphs(4)), ORIGIN))

    assert len(calls) == 1
    assert counters([json.loads(request.data)["text"] for request in sent]) == ["(1/2)", "(2/2)"]


@pytest.mark.spec("docs/service.md")
def test_should_report_a_failed_part_without_its_text_and_send_the_rest():
    sent: list[str] = []

    def fail_the_second(request: urllib.request.Request) -> None:
        text = json.loads(request.data)["text"]
        if "(2/3)" in text:
            raise urllib.error.HTTPError(request.full_url, 429, "Too Many Requests", {}, None)
        sent.append(text)
    stderr = io.StringIO()

    with redirect_stderr(stderr):
        notifier(fail_the_second).claude_notice(
            parse_notice(answer(text=three_parts()), ORIGIN))

    assert counters(sent) == ["(1/3)", "(3/3)"]
    assert stderr.getvalue() == "telegram: message 2 of 3 not sent (HTTP 429)\n"


FAILED_MARK = "\u274c\U0001f510"
SHORT = "4a015f0f"


def failure(**over: object) -> dict[str, object]:
    return {"kind": "keyguard_failed", "text": "Request expired with no answer.", **over}


@pytest.mark.spec("docs/service.md")
@pytest.mark.parametrize(("session", "kept"), [(SHORT, SHORT), (SESSION, SESSION), (SESSION.upper(), SESSION), (None, None)])
def test_should_take_a_failure_line_with_a_session_by_uuid_or_by_8_characters_or_with_none(session, kept):
    assert parse_notice(failure(session=session), ORIGIN).session == kept


@pytest.mark.spec("docs/service.md")
def test_should_take_a_failure_line_with_no_session_field_at_all():
    assert parse_notice(failure(), ORIGIN).session is None


@pytest.mark.spec("docs/service.md")
def test_should_open_a_failure_line_with_its_mark_and_no_heading():
    message = notice_message(parse_notice(failure(session=SHORT, container="box-1", title="Plan"), ORIGIN))

    assert message.splitlines() == [f"{FAILED_MARK} Request expired with no answer.", "", "Plan \u00b7 box-1",
                                    "[session 4a015f0f]"]


@pytest.mark.spec("docs/service.md")
def test_should_show_the_session_handle_of_a_failure_line_only_for_a_full_uuid():
    short = notice_message(parse_notice(failure(session=SHORT), ORIGIN))
    full = notice_message(parse_notice(failure(session=SESSION), ORIGIN))

    assert "<code>" not in short
    assert full.splitlines()[-2:] == [f"<code>{SESSION}</code>", "[session 4a015f0f]"]


@pytest.mark.spec("docs/service.md")
@pytest.mark.parametrize(("over", "message"), [
    ({}, f"{FAILED_MARK} Request expired with no answer."),
    ({"container": "box-1"}, f"{FAILED_MARK} Request expired with no answer.\n\nbox-1"),
])
def test_should_end_a_failure_line_without_a_session_where_its_text_or_footer_ends(over, message):
    assert notice_message(parse_notice(failure(**over), ORIGIN)) == message


@pytest.mark.spec("docs/service.md")
def test_should_post_the_failure_line_of_a_caller_to_the_chat():
    sent: list[urllib.request.Request] = []

    notifier(sent.append).claude_notice(parse_notice(failure(session=SHORT), ORIGIN))

    assert json.loads(sent[0].data)["text"] == f"{FAILED_MARK} Request expired with no answer.\n\n[session 4a015f0f]"


class RecordingRelay:
    def __init__(self) -> None:
        self.relayed: list[tuple[ClaudeNotice, int]] = []

    def relay(self, notice: ClaudeNotice, reply_to: int) -> None:
        self.relayed.append((notice, reply_to))


def numbered_send(sent: list[str], first_id: int = 501):
    def send(request: urllib.request.Request) -> int:
        sent.append(json.loads(request.data)["text"])
        return first_id + len(sent) - 1
    return send


@pytest.mark.spec("docs/service.md")
def test_should_keep_the_presence_and_the_last_use_of_an_answer():
    parsed = parse_notice(answer(presence="away", seen=1_764_061_200), ORIGIN)

    assert (parsed.presence, parsed.seen) == (Presence.AWAY, 1_764_061_200)


@pytest.mark.spec("docs/service.md")
@pytest.mark.parametrize(("presence", "seen"), [
    (None, None), ("asleep", "yesterday"), (7, 1.5), (["away"], True), ("AWAY", -1), ("x" * 4000, 10 ** 13),
])
def test_should_read_an_odd_presence_as_unknown_and_an_odd_last_use_as_none_and_still_take_the_answer(
        presence, seen):
    parsed = parse_notice(answer(presence=presence, seen=seen), ORIGIN)

    assert (parsed.presence, parsed.seen) == (Presence.UNKNOWN, None)


@pytest.mark.spec("docs/service.md")
@pytest.mark.parametrize("kind", ["finished", "prompt"])
def test_should_drop_the_presence_and_the_last_use_of_a_ping(kind):
    parsed = parse_notice(notice(kind=kind, presence="away", seen=1_764_061_200), ORIGIN)

    assert (parsed.presence, parsed.seen) == (Presence.UNKNOWN, None)


@pytest.mark.spec("docs/service.md")
def test_should_hand_an_answer_to_the_relay_with_its_first_message_after_all_its_messages():
    sent: list[str] = []
    relay = RecordingRelay()
    target = TelegramNotifier(TOKEN, CHAT, send=numbered_send(sent), spawn=run_inline, spoken=relay)

    target.claude_notice(parse_notice(answer(text=paragraphs(4), presence="phone"), ORIGIN))

    assert len(sent) == 2
    assert [(relayed.session, relayed.presence, reply_to) for relayed, reply_to in relay.relayed] == [
        (SESSION, Presence.PHONE, 501)]


@pytest.mark.spec("docs/service.md")
def test_should_reply_to_the_first_message_that_telegram_took():
    sent: list[str] = []
    relay = RecordingRelay()

    def fail_the_first(request: urllib.request.Request) -> int:
        text = json.loads(request.data)["text"]
        if "(1/2)" in text:
            raise urllib.error.HTTPError(request.full_url, 429, "Too Many Requests", {}, None)
        sent.append(text)
        return 777

    with redirect_stderr(io.StringIO()):
        TelegramNotifier(TOKEN, CHAT, send=fail_the_first, spawn=run_inline, spoken=relay).claude_notice(
            parse_notice(answer(text=paragraphs(4)), ORIGIN))

    assert [reply_to for _, reply_to in relay.relayed] == [777]


@pytest.mark.spec("docs/service.md")
def test_should_relay_nothing_when_telegram_took_no_message_of_the_answer():
    relay = RecordingRelay()

    def refuse(request: urllib.request.Request) -> int:
        raise urllib.error.URLError("down")

    with redirect_stderr(io.StringIO()):
        TelegramNotifier(TOKEN, CHAT, send=refuse, spawn=run_inline, spoken=relay).claude_notice(
            parse_notice(answer(), ORIGIN))

    assert relay.relayed == []


@pytest.mark.spec("docs/service.md")
@pytest.mark.parametrize("kind", ["finished", "prompt"])
def test_should_relay_only_answers(kind):
    relay = RecordingRelay()

    TelegramNotifier(TOKEN, CHAT, send=numbered_send([]), spawn=run_inline, spoken=relay).claude_notice(
        parse_notice(notice(kind=kind), ORIGIN))

    assert relay.relayed == []


@pytest.mark.spec("docs/service.md")
@pytest.mark.parametrize(("body", "message_id"), [
    (b'{"ok": true, "result": {"message_id": 42, "chat": {"id": 1}}}', 42),
    (b'{"ok": true, "result": {"message_id": "42"}}', None),
    (b'{"ok": true, "result": {"message_id": true}}', None),
    (b'{"ok": true, "result": []}', None),
    (b'{"ok": true}', None),
    (b"[]", None),
    (b"not json", None),
    (b"\xff", None),
])
def test_should_read_the_message_id_from_the_answer_of_telegram_or_none(body, message_id):
    assert message_id_of(body) == message_id
