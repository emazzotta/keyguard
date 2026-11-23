"""Relay Claude session pings to Telegram, each headed `Claude on <origin>`, off unless a bot is set.

The origin is the label of how the caller authenticated: the request token or the identity header.
Every message ends with the session marker that the Telegram bot reads from a quoted message. An `answer` carries a whole
reply, in up to three messages, with a link to the session in the web terminal when one is set. Unlock requests send
nothing here.
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
from typing import Final, Protocol, Union
from uuid import UUID

from .longtext import MESSAGE_LIMIT, multiline, split_parts, utf16_length
from .scrub import scrub

API_BASE: Final = "https://api.telegram.org"
SEND_TIMEOUT_SECONDS: Final = 10
SAFE_HOST: Final = re.compile(r"[A-Za-z0-9-]{1,63}")
NOTICE_TEXT_LIMIT: Final = 300
NOTICE_INPUT_LIMIT: Final = 4000
NOTICE_TITLE_LIMIT: Final = 60
NOTICE_ORIGIN_LIMIT: Final = 60
ANSWER_TEXT_LIMIT: Final = 7500
ANSWER_MESSAGES: Final = 3
ELLIPSIS: Final = "\u2026"
SEPARATOR: Final = " \u00b7 "
SHORT_SESSION_LENGTH: Final = 8
SESSION_MARKER: Final = "[session {short}]"
PARSE_MODE: Final = "HTML"
CHAT_PATH: Final = "/chat/"


class NoticeKind(Enum):
    FINISHED = "finished"
    PROMPT = "prompt"
    ANSWER = "answer"


NOTICE_KINDS: Final = {kind.value: kind for kind in NoticeKind}
LINK_LABELS: Final = {NoticeKind.ANSWER: "Open the session in Safari"}
NOTICE_MARKS: Final = {NoticeKind.FINISHED: "\U0001f4ac", NoticeKind.PROMPT: "\u23f3",
                      NoticeKind.ANSWER: "\U0001f4ac"}

Send = Callable[[urllib.request.Request], object]
Spawn = Callable[..., None]


def _post(request: urllib.request.Request) -> None:
    with urllib.request.urlopen(request, timeout=SEND_TIMEOUT_SECONDS) as response:
        response.read()


def _in_background(function: Callable[..., None], *args: object) -> None:
    threading.Thread(target=function, args=args, daemon=True).start()


@dataclass(frozen=True)
class ClaudeNotice:
    session: UUID
    container: str | None
    text: str
    origin: str
    kind: NoticeKind = NoticeKind.FINISHED
    title: str | None = None
    link: str | None = None


class NoticeError(ValueError):
    pass


def parse_notice(payload: object, origin: str) -> ClaudeNotice:
    if not isinstance(payload, dict):
        raise NoticeError("a notice is a JSON object")
    session, container = payload.get("session"), payload.get("container")
    if not isinstance(session, str):
        raise NoticeError("session must be a UUID")
    if container is not None and not (isinstance(container, str) and SAFE_HOST.fullmatch(container)):
        raise NoticeError("container must be a host label")
    kind = parse_kind(payload.get("kind", NoticeKind.FINISHED.value))
    text = parse_answer_text(payload.get("text")) if kind is NoticeKind.ANSWER else parse_text(payload.get("text"))
    return ClaudeNotice(UUID(session), container, text, origin, kind, parse_title(payload.get("title")))


def parse_text(text: object) -> str:
    if not isinstance(text, str) or len(text) > NOTICE_INPUT_LIMIT or not plain(text):
        raise NoticeError(f"text must be 1 to {NOTICE_INPUT_LIMIT} characters")
    return text


def parse_answer_text(text: object) -> str:
    """The cleaned and redacted text, held to its limit before and after the redaction, which can lengthen it."""
    problem = NoticeError(f"text must be 1 to {ANSWER_TEXT_LIMIT} characters")
    if not isinstance(text, str) or utf16_length(text) > ANSWER_TEXT_LIMIT:
        raise problem
    cleaned = scrub(multiline(text))
    if not cleaned or utf16_length(cleaned) > ANSWER_TEXT_LIMIT:
        raise problem
    return cleaned


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
    return _frame(notice, html.escape(capped(plain(notice.text), NOTICE_TEXT_LIMIT), quote=False))


def answer_messages(notice: ClaudeNotice) -> list[str]:
    """The answer in at most three messages. Each carries the heading, the footer and the marker, so a reply to any of
    them continues the session, and the last carries the link."""
    link = _anchor(notice) if notice.link else None
    room = MESSAGE_LIMIT - len(_frame(notice, "", _counter(ANSWER_MESSAGES, ANSWER_MESSAGES), link))
    parts = split_parts(notice.text, room)
    if len(parts) > ANSWER_MESSAGES:
        parts = [*parts[:ANSWER_MESSAGES - 1], parts[ANSWER_MESSAGES - 1][:-1].rstrip() + ELLIPSIS]
    total = len(parts)
    return [_frame(notice, html.escape(part, quote=False), _counter(number, total) if total > 1 else "",
                   link if number == total else None) for number, part in enumerate(parts, start=1)]


def with_session_link(notice: ClaudeNotice, terminal_url: str) -> ClaudeNotice:
    if notice.kind is not NoticeKind.ANSWER or not terminal_url:
        return notice
    return replace(notice, link=f"{terminal_url}{CHAT_PATH}{notice.session}")


def _counter(number: int, total: int) -> str:
    return f"({number}/{total}) "


def _anchor(notice: ClaudeNotice) -> str:
    return f'<a href="{html.escape(notice.link or "")}">{LINK_LABELS[notice.kind]}</a>'


def _frame(notice: ClaudeNotice, body: str, counter: str = "", link: str | None = None) -> str:
    origin = html.escape(capped(plain(notice.origin), NOTICE_ORIGIN_LIMIT), quote=False)
    title = html.escape(capped(plain(notice.title or ""), NOTICE_TITLE_LIMIT), quote=False)
    footer = SEPARATOR.join(part for part in (title, notice.container, f"<code>{notice.session}</code>") if part)
    marker = SESSION_MARKER.format(short=str(notice.session)[:SHORT_SESSION_LENGTH])
    blocks = [f"<b>Claude on {origin}</b>", f"{NOTICE_MARKS[notice.kind]} {counter}{body}", *([link] if link else []),
              f"{footer}\n{marker}"]
    return "\n\n".join(blocks)


def _describe(error: Exception) -> str:
    if isinstance(error, urllib.error.HTTPError):
        return f"HTTP {error.code}"
    return type(error).__name__


class Notifier(Protocol):
    def claude_notice(self, notice: ClaudeNotice) -> None: ...


class TelegramNotifier:
    def __init__(self, token: str, chat_id: str, send: Send = _post, spawn: Spawn = _in_background) -> None:
        self._token = token
        self._chat_id = chat_id
        self._send = send
        self._spawn = spawn

    def claude_notice(self, notice: ClaudeNotice) -> None:
        self._spawn(self._deliver_all, notice_messages(notice))

    def _deliver_all(self, messages: list[str]) -> None:
        for number, text in enumerate(messages, start=1):
            self._deliver(text, f" {number} of {len(messages)}" if len(messages) > 1 else "")

    def _deliver(self, text: str, position: str) -> None:
        body = json.dumps({"chat_id": self._chat_id, "text": text, "parse_mode": PARSE_MODE,
                           "link_preview_options": {"is_disabled": True}}).encode()
        request = urllib.request.Request(f"{API_BASE}/bot{self._token}/sendMessage", data=body,
                                         headers={"Content-Type": "application/json"})
        try:
            self._send(request)
        except (OSError, ValueError) as error:
            print(f"telegram: message{position} not sent ({_describe(error)})", file=sys.stderr, flush=True)


class DisabledNotifier:
    def claude_notice(self, notice: ClaudeNotice) -> None:
        pass


AnyNotifier = Union[TelegramNotifier, DisabledNotifier]


def telegram_notifier(token: str, chat_id: str) -> AnyNotifier:
    return TelegramNotifier(token, chat_id) if token and chat_id else DisabledNotifier()
