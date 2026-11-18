"""Relay Claude session pings to Telegram, each headed `Claude on <origin>`, off unless a bot is set.

The origin is the label of how the caller authenticated: the request token or the identity header.
Unlock requests send nothing here.
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
from dataclasses import dataclass
from enum import Enum
from typing import Final, Protocol, Union
from uuid import UUID

API_BASE: Final = "https://api.telegram.org"
SEND_TIMEOUT_SECONDS: Final = 10
SAFE_HOST: Final = re.compile(r"[A-Za-z0-9-]{1,63}")
NOTICE_TEXT_LIMIT: Final = 300
NOTICE_INPUT_LIMIT: Final = 4000
NOTICE_TITLE_LIMIT: Final = 60
ELLIPSIS: Final = "\u2026"
SEPARATOR: Final = " \u00b7 "
PARSE_MODE: Final = "HTML"


class NoticeKind(Enum):
    FINISHED = "finished"
    PROMPT = "prompt"


NOTICE_KINDS: Final = {kind.value: kind for kind in NoticeKind}
NOTICE_MARKS: Final = {NoticeKind.FINISHED: "\U0001f4ac", NoticeKind.PROMPT: "\u23f3"}

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


class NoticeError(ValueError):
    pass


def parse_notice(payload: object, origin: str) -> ClaudeNotice:
    if not isinstance(payload, dict):
        raise NoticeError("a notice is a JSON object")
    session, container, text = payload.get("session"), payload.get("container"), payload.get("text")
    if not isinstance(session, str):
        raise NoticeError("session must be a UUID")
    if container is not None and not (isinstance(container, str) and SAFE_HOST.fullmatch(container)):
        raise NoticeError("container must be a host label")
    if not isinstance(text, str) or len(text) > NOTICE_INPUT_LIMIT or not plain(text):
        raise NoticeError(f"text must be 1 to {NOTICE_INPUT_LIMIT} characters")
    kind = parse_kind(payload.get("kind", NoticeKind.FINISHED.value))
    return ClaudeNotice(UUID(session), container, text, origin, kind, parse_title(payload.get("title")))


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


def notice_message(notice: ClaudeNotice) -> str:
    heading = f"<b>Claude on {html.escape(notice.origin, quote=False)}</b>"
    text = html.escape(capped(plain(notice.text), NOTICE_TEXT_LIMIT), quote=False)
    mark = NOTICE_MARKS[notice.kind]
    handle = f"<code>{notice.session}</code>"
    title = html.escape(capped(plain(notice.title or ""), NOTICE_TITLE_LIMIT), quote=False)
    footer = SEPARATOR.join(part for part in (title, notice.container, handle) if part)
    return f"{heading}\n\n{mark} {text}\n\n{footer}"


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
        self._spawn(self._deliver, notice_message(notice))

    def _deliver(self, text: str) -> None:
        body = json.dumps({"chat_id": self._chat_id, "text": text, "parse_mode": PARSE_MODE,
                           "link_preview_options": {"is_disabled": True}}).encode()
        request = urllib.request.Request(f"{API_BASE}/bot{self._token}/sendMessage", data=body,
                                         headers={"Content-Type": "application/json"})
        try:
            self._send(request)
        except (OSError, ValueError) as error:
            print(f"telegram: message not sent ({_describe(error)})", file=sys.stderr, flush=True)


class DisabledNotifier:
    def claude_notice(self, notice: ClaudeNotice) -> None:
        pass


AnyNotifier = Union[TelegramNotifier, DisabledNotifier]


def telegram_notifier(token: str, chat_id: str) -> AnyNotifier:
    return TelegramNotifier(token, chat_id) if token and chat_id else DisabledNotifier()
