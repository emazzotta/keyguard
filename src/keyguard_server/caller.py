"""The Claude Code or opencode session a client claims, named in Touch ID prompts.

The client reports the session in two headers and its tmux pane in a third, and nothing here
proves them: the label tells the approver which session to look at, the pane which one the web
terminal shows, never who really asked.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Final
from urllib.parse import unquote

SESSION_HEADER: Final = "X-Keyguard-Session"
TITLE_HEADER: Final = "X-Keyguard-Title"
PANE_HEADER: Final = "X-Keyguard-Pane"
MAX_TITLE_CHARS: Final = 80
CLIPPED: Final = "…"
_SESSION: Final = re.compile(r"[0-9A-Za-z]{8}")
_PANE: Final = re.compile(r"%\d{1,6}")
_DROPPED_CATEGORIES: Final = frozenset({"Cc", "Cf", "Co", "Cs", "Zl", "Zp"})


@dataclass(frozen=True)
class Caller:
    session: str | None = None
    title: str | None = None
    pane: str | None = None

    @classmethod
    def parse(cls, session: str | None, title: str | None, pane: str | None = None) -> Caller:
        """Builds a caller from the raw header values, dropping what does not hold up."""
        return cls(session=_session(session), title=_title(title), pane=_pane(pane))

    @property
    def label(self) -> str | None:
        if self.title and self.session:
            return f"{_quoted(self.title)} ({self.session})"
        if self.title:
            return _quoted(self.title)
        if self.session:
            return f"session {self.session}"
        return None


def _session(value: str | None) -> str | None:
    return value if value and _SESSION.fullmatch(value) else None


def _pane(value: str | None) -> str | None:
    return value if value and _PANE.fullmatch(value) else None


def _title(value: str | None) -> str | None:
    if not value:
        return None
    visible = " ".join("".join(map(_visible, unquote(value))).split())
    if len(visible) > MAX_TITLE_CHARS:
        return visible[:MAX_TITLE_CHARS - len(CLIPPED)] + CLIPPED
    return visible or None


def _visible(char: str) -> str:
    if char.isspace():
        return " "
    return "" if unicodedata.category(char) in _DROPPED_CATEGORIES else char


def _quoted(title: str) -> str:
    """Escapes quotes, so a title cannot close its own and pass text off as the prompt's."""
    return '"' + title.replace("\\", "\\\\").replace('"', '\\"') + '"'
