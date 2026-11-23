"""Prepares a long text for Telegram: keeps its line breaks, then splits it into messages.

Telegram counts a message in UTF-16 code units, which this module assumes: a character outside the Basic
Multilingual Plane counts twice, so the bound holds whichever way Telegram counts.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Final

MESSAGE_LIMIT: Final = 4096
MIN_PART_LIMIT: Final = 8
BOUNDARIES: Final = ("\n\n", "\n", " ")
BOUNDARY_WINDOW: Final = 4
BLANK_RUNS: Final = re.compile(r"\n{3,}")


def utf16_length(text: str) -> int:
    return len(text) + sum(1 for char in text if ord(char) > 0xFFFF)


def multiline(text: str) -> str:
    """The text with its line breaks, one blank line at most, and no control or format character."""
    lines = [_visible(line).rstrip() for line in text.splitlines()]
    return BLANK_RUNS.sub("\n\n", "\n".join(lines)).strip()


def _visible(line: str) -> str:
    return "".join(" " if unicodedata.category(char).startswith("C") else char for char in line)


def split_parts(text: str, limit: int) -> list[str]:
    """Parts of at most `limit` UTF-16 units, cut at a blank line, a line break or a space when one lies in the last
    quarter of the part, else in the middle of a word or line."""
    if limit < MIN_PART_LIMIT:
        raise ValueError(f"a part needs room for at least {MIN_PART_LIMIT} units")
    parts: list[str] = []
    rest = text
    while utf16_length(rest) > limit:
        cut = _cut_at(rest, limit)
        parts.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip()
    parts.append(rest)
    return parts


def _cut_at(text: str, limit: int) -> int:
    end = _fit(text, limit)
    start = _fit(text, limit - limit // BOUNDARY_WINDOW)
    for boundary in BOUNDARIES:
        found = text.rfind(boundary, start, end)
        if found > 0:
            return found
    return end


def _fit(text: str, units: int) -> int:
    used = 0
    for index, char in enumerate(text):
        used += 2 if ord(char) > 0xFFFF else 1
        if used > units:
            return index
    return len(text)
