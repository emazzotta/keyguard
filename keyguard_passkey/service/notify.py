"""Relay Claude session pings and keyguard notices to Telegram, off unless a bot is set.

A ping is headed `Claude on <origin>`, the label of how the caller authenticated: the request token or the identity
header. A keyguard notice has no heading and opens with its mark. A message with a session ends with the marker that
the Telegram bot reads from a quoted message. An `answer` carries a whole reply, in up to three messages, with a link to
the session in the web terminal when one is set, and goes on to the spoken answer relay once Telegram took a message.
"""
from __future__ import annotations

import html
import json
import re
import sys
import threading
import unicodedata
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, replace
from enum import Enum
from typing import Final, Protocol, TypeGuard, Union
from uuid import UUID

from .longtext import MESSAGE_LIMIT, multiline, split_parts
from .presence import Presence, presence_of
from .scrub import scrub

API_BASE: Final = "https://api.telegram.org"
SEND_TIMEOUT_SECONDS: Final = 10
SAFE_HOST: Final = re.compile(r"[A-Za-z0-9-]{1,63}")
NOTICE_TEXT_LIMIT: Final = 300
NOTICE_INPUT_LIMIT: Final = 4000
NOTICE_TITLE_LIMIT: Final = 60
NOTICE_ORIGIN_LIMIT: Final = 60
ANSWER_HARD_LIMIT: Final = 100_000
ANSWER_WINDOW: Final = 12_800
ANSWER_MESSAGES: Final = 3
ELLIPSIS: Final = "\u2026"
CUT_NOTE: Final = f"{ELLIPSIS} (cut, full answer in the session)"
NOTE_GAP: Final = "\n\n"
SEPARATOR: Final = " \u00b7 "
SHORT_SESSION_LENGTH: Final = 8
SHORT_SESSION: Final = re.compile(r"[0-9A-Za-z]{8}")
UUID_LENGTH: Final = 36
SESSION_MARKER: Final = "[session {short}]"
PARSE_MODE: Final = "HTML"
CHAT_PATH: Final = "/chat/"
SEEN_LIMIT: Final = 10 ** 12


class NoticeKind(Enum):
    FINISHED = "finished"
    PROMPT = "prompt"
    ANSWER = "answer"
    KEYGUARD_WAIT = "keyguard_wait"
    KEYGUARD_FAILED = "keyguard_failed"


POSTED_KINDS: Final = (NoticeKind.FINISHED, NoticeKind.PROMPT, NoticeKind.ANSWER, NoticeKind.KEYGUARD_FAILED)
NOTICE_KINDS: Final = {kind.value: kind for kind in POSTED_KINDS}
HEADLESS_KINDS: Final = frozenset({NoticeKind.KEYGUARD_WAIT, NoticeKind.KEYGUARD_FAILED})
LINK_LABELS: Final = {NoticeKind.ANSWER: "Open the session in Safari", NoticeKind.KEYGUARD_WAIT: "Approve in Safari"}
NOTICE_MARKS: Final = {NoticeKind.FINISHED: "\U0001f4ac", NoticeKind.PROMPT: "\u23f3", NoticeKind.ANSWER: "\U0001f4ac",
                      NoticeKind.KEYGUARD_WAIT: "\U0001f510", NoticeKind.KEYGUARD_FAILED: "\u274c\U0001f510"}

Send = Callable[[urllib.request.Request], object]
Spawn = Callable[..., None]


def _post(request: urllib.request.Request) -> int | None:
    with urllib.request.urlopen(request, timeout=SEND_TIMEOUT_SECONDS) as response:
        return message_id_of(response.read())


def message_id_of(body: bytes) -> int | None:
    """The id of the message that Telegram's sendMessage answer names, or None."""
    try:
        result = json.loads(body).get("result")
        message_id = result.get("message_id")
    except (ValueError, AttributeError):
        return None
    return message_id if is_whole_number(message_id) else None


def is_whole_number(value: object) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool)


def _in_background(function: Callable[..., None], *args: object) -> None:
    threading.Thread(target=function, args=args, daemon=True).start()


@dataclass(frozen=True)
class ClaudeNotice:
    """What one notice says. The session is a UUID, or the 8 characters that an unlock request carries, or nothing."""
    session: str | None
    container: str | None
    text: str
    origin: str
    kind: NoticeKind = NoticeKind.FINISHED
    title: str | None = None
    link: str | None = None
    cut: bool = False
    presence: Presence = Presence.UNKNOWN
    seen: int | None = None


class NoticeError(ValueError):
    pass


def parse_notice(payload: object, origin: str) -> ClaudeNotice:
    if not isinstance(payload, dict):
        raise NoticeError("a notice is a JSON object")
    container = payload.get("container")
    if container is not None and not (isinstance(container, str) and SAFE_HOST.fullmatch(container)):
        raise NoticeError("container must be a host label")
    kind = parse_kind(payload.get("kind", NoticeKind.FINISHED.value))
    session = parse_session(payload.get("session"), kind)
    raw = payload.get("text")
    if kind is not NoticeKind.ANSWER:
        return ClaudeNotice(session, container, parse_text(raw), origin, kind, parse_title(payload.get("title")))
    text, cut = parse_answer(raw)
    return ClaudeNotice(session, container, text, origin, kind, parse_title(payload.get("title")), cut=cut,
                        presence=presence_of(payload.get("presence")), seen=parse_seen(payload.get("seen")))


def parse_seen(seen: object) -> int | None:
    """The last use at a web terminal, in epoch seconds. A wrong value never costs the answer."""
    return seen if is_whole_number(seen) and 0 <= seen < SEEN_LIMIT else None


def parse_session(session: object, kind: NoticeKind) -> str | None:
    """A ping names its session by UUID. A failure line may name it by 8 characters, or by none, as the unlock service
    and the Mac keyguard know a requester that has no Claude session."""
    failure = kind is NoticeKind.KEYGUARD_FAILED
    if failure and session is None:
        return None
    if isinstance(session, str):
        if failure and SHORT_SESSION.fullmatch(session):
            return session
        try:
            return str(UUID(session))
        except ValueError:
            pass
    raise NoticeError("session must be a UUID" + (", 8 letters or digits, or left out" if failure else ""))


def parse_text(text: object) -> str:
    if not isinstance(text, str) or len(text) > NOTICE_INPUT_LIMIT or not plain(text):
        raise NoticeError(f"text must be 1 to {NOTICE_INPUT_LIMIT} characters")
    return text


def parse_answer(text: object) -> tuple[str, bool]:
    """The part of a reply that can be sent, cleaned and redacted, and whether the reply went on past it.

    Any length up to the hard limit is taken, so a long reply is cut and never lost. The redaction scans only the
    window that three messages can show, since its pattern is quadratic on a long run of digits.
    """
    if not isinstance(text, str) or len(text) > ANSWER_HARD_LIMIT:
        raise NoticeError(f"text must be 1 to {ANSWER_HARD_LIMIT} characters")
    cleaned = multiline(text)
    if not cleaned:
        raise NoticeError(f"text must be 1 to {ANSWER_HARD_LIMIT} characters")
    return scrub(cleaned[:ANSWER_WINDOW]).rstrip(), len(cleaned) > ANSWER_WINDOW


def parse_kind(kind: object) -> NoticeKind:
    if not isinstance(kind, str) or kind not in NOTICE_KINDS:
        raise NoticeError(f"kind must be one of {', '.join(NOTICE_KINDS)}")
    return NOTICE_KINDS[kind]


def parse_title(title: object) -> str | None:
    if title is not None and not (isinstance(title, str) and len(title) <= NOTICE_INPUT_LIMIT):
        raise NoticeError(f"title must be at most {NOTICE_INPUT_LIMIT} characters")
    return title


def plain(text: str) -> str:
    visible = "".join(" " if unicodedata.category(char).startswith("C") else char for char in text)
    return " ".join(visible.split())


def capped(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit - 1].rstrip() + ELLIPSIS


def notice_messages(notice: ClaudeNotice) -> list[str]:
    return answer_messages(notice) if notice.kind is NoticeKind.ANSWER else [notice_message(notice)]


def notice_message(notice: ClaudeNotice) -> str:
    return _frame(notice, html.escape(capped(plain(notice.text), NOTICE_TEXT_LIMIT), quote=False), link=_anchor(notice))


def answer_room(notice: ClaudeNotice) -> int:
    """The units of a message that the text may use, with the longest counter, the link, the heading and the footer."""
    return MESSAGE_LIMIT - len(_frame(notice, "", _counter(ANSWER_MESSAGES, ANSWER_MESSAGES), _anchor(notice)))


def answer_messages(notice: ClaudeNotice) -> list[str]:
    """The answer in at most three messages, each with the heading, the footer and the marker, so that a reply to any
    of them continues the session. The last has the link, and the note when the text did not fit."""
    room = answer_room(notice)
    parts = split_parts(notice.text, room)
    cut = notice.cut or len(parts) > ANSWER_MESSAGES
    if cut:
        parts = split_parts(notice.text, room - len(NOTE_GAP) - len(CUT_NOTE))[:ANSWER_MESSAGES]
    bodies = [html.escape(part, quote=False) for part in parts]
    if cut:
        bodies[-1] += NOTE_GAP + CUT_NOTE
    anchor = _anchor(notice)
    return [_frame(notice, body, _counter(number, len(bodies)) if len(bodies) > 1 else "",
                   anchor if number == len(bodies) else None) for number, body in enumerate(bodies, start=1)]


def with_session_link(notice: ClaudeNotice, terminal_url: str) -> ClaudeNotice:
    if notice.kind is not NoticeKind.ANSWER or not terminal_url:
        return notice
    return replace(notice, link=f"{terminal_url}{CHAT_PATH}{notice.session}")


def _counter(number: int, total: int) -> str:
    return f"({number}/{total}) "


def _anchor(notice: ClaudeNotice) -> str | None:
    if not notice.link:
        return None
    return f'<a href="{html.escape(notice.link)}">{LINK_LABELS[notice.kind]}</a>'


def _frame(notice: ClaudeNotice, body: str, counter: str = "", link: str | None = None) -> str:
    blocks = [] if notice.kind in HEADLESS_KINDS else [_heading(notice)]
    blocks.append(f"{NOTICE_MARKS[notice.kind]} {counter}{body}")
    if link:
        blocks.append(link)
    blocks.append(_footer(notice))
    return "\n\n".join(block for block in blocks if block)


def _heading(notice: ClaudeNotice) -> str:
    return f"<b>Claude on {html.escape(capped(plain(notice.origin), NOTICE_ORIGIN_LIMIT), quote=False)}</b>"


def _footer(notice: ClaudeNotice) -> str:
    """The title, the container and the session's UUID, when each is known, and under them the session marker."""
    title = html.escape(capped(plain(notice.title or ""), NOTICE_TITLE_LIMIT), quote=False)
    session = notice.session or ""
    handle = f"<code>{session}</code>" if len(session) == UUID_LENGTH else ""
    marker = SESSION_MARKER.format(short=session[:SHORT_SESSION_LENGTH]) if session else ""
    line = SEPARATOR.join(part for part in (title, notice.container, handle) if part)
    return "\n".join(part for part in (line, marker) if part)


def describe_failure(error: Exception) -> str:
    if isinstance(error, urllib.error.HTTPError):
        return f"HTTP {error.code}"
    return type(error).__name__


class Notifier(Protocol):
    def claude_notice(self, notice: ClaudeNotice) -> None: ...


class Relay(Protocol):
    def relay(self, notice: ClaudeNotice, reply_to: int) -> None: ...


class TelegramNotifier:
    def __init__(self, token: str, chat_id: str, send: Send = _post, spawn: Spawn = _in_background,
                 spoken: Relay | None = None) -> None:
        self._token = token
        self._chat_id = chat_id
        self._send = send
        self._spawn = spawn
        self._spoken = spoken

    def claude_notice(self, notice: ClaudeNotice) -> None:
        self._spawn(self._deliver_all, notice, notice_messages(notice))

    def _deliver_all(self, notice: ClaudeNotice, messages: list[str]) -> None:
        sent = [self._deliver(text, f" {number} of {len(messages)}" if len(messages) > 1 else "")
                for number, text in enumerate(messages, start=1)]
        first = next((message_id for message_id in sent if message_id is not None), None)
        if self._spoken and notice.kind is NoticeKind.ANSWER and first is not None:
            self._spoken.relay(notice, first)

    def _deliver(self, text: str, position: str) -> int | None:
        body = json.dumps({"chat_id": self._chat_id, "text": text, "parse_mode": PARSE_MODE,
                           "link_preview_options": {"is_disabled": True}}).encode()
        request = urllib.request.Request(f"{API_BASE}/bot{self._token}/sendMessage", data=body,
                                         headers={"Content-Type": "application/json"})
        try:
            message_id = self._send(request)
        except (OSError, ValueError) as error:
            print(f"telegram: message{position} not sent ({describe_failure(error)})", file=sys.stderr, flush=True)
            return None
        return message_id if is_whole_number(message_id) else None


class DisabledNotifier:
    def claude_notice(self, notice: ClaudeNotice) -> None:
        pass


AnyNotifier = Union[TelegramNotifier, DisabledNotifier]


def telegram_notifier(token: str, chat_id: str, spoken: Relay | None = None) -> AnyNotifier:
    return TelegramNotifier(token, chat_id, spoken=spoken) if token and chat_id else DisabledNotifier()
