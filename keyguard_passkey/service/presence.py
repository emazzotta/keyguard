"""Whether the owner sits at a screen, read from the folder that each web-terminal instance writes.

The folder holds one file per instance. A line is `<epoch-seconds> <kind>`, with kind `computer` or `phone`, and counts
while it is younger than 120 seconds.
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Iterator
from enum import Enum
from pathlib import Path
from typing import Final

FRESH_SECONDS: Final = 120
MAX_FILE_BYTES: Final = 4096
LINE: Final = re.compile(r"([0-9]{1,12}(?:\.[0-9]{1,6})?) (computer|phone)")


class Presence(Enum):
    COMPUTER = "computer"
    PHONE = "phone"
    AWAY = "away"
    UNKNOWN = "unknown"


def classify(lines: Iterable[str], now: float) -> Presence:
    """The kind of the fresh lines: `computer` before `phone`, else `away`. A line that does not parse is ignored."""
    fresh = set()
    for line in lines:
        found = LINE.fullmatch(line.strip())
        if found and now - float(found.group(1)) < FRESH_SECONDS:
            fresh.add(found.group(2))
    for kind in (Presence.COMPUTER, Presence.PHONE):
        if kind.value in fresh:
            return kind
    return Presence.AWAY


def read_presence(folder: Path, now: float) -> Presence:
    """`unknown` when the folder is missing or cannot be listed; a file that cannot be read counts as no line."""
    try:
        files = sorted(folder.iterdir())
    except OSError:
        return Presence.UNKNOWN
    return classify(_lines_of(files), now)


def _lines_of(files: list[Path]) -> Iterator[str]:
    for file in files:
        try:
            if file.is_file():
                with file.open(encoding="utf-8", errors="replace") as handle:
                    yield from handle.read(MAX_FILE_BYTES).splitlines()
        except OSError:
            continue
