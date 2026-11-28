"""Whether the owner sits at a web terminal, read from the folder that each web-terminal instance writes.

One file per instance, one line per page in view: `<epoch-seconds> <computer|phone>`. A line is fresh while
`now - epoch < 120`. The writer renames a dot-named temporary file over the instance's file, so a dot-named file is
never read. The same rules live in keyguard-passkey (`service/presence.py`) and the dotfiles (`bin/presence.py`):
change all three together.
"""
from __future__ import annotations

import re
import time
from collections.abc import Callable, Iterable
from enum import Enum
from pathlib import Path
from typing import Final

from .config import PRESENCE_DIR

FRESH_SECONDS: Final = 120
MAX_FILE_BYTES: Final = 4096
LINE: Final = re.compile(r"([0-9]{1,12})\s+(computer|phone)")


class Presence(Enum):
    COMPUTER = "computer"
    PHONE = "phone"
    AWAY = "away"
    UNKNOWN = "unknown"

    @property
    def attended(self) -> bool:
        return self in (Presence.COMPUTER, Presence.PHONE)


def classify(lines: Iterable[str], now: float) -> Presence:
    """`computer` when a fresh line says so, else `phone`, else `away`. A line that does not parse is ignored."""
    fresh = set()
    for line in lines:
        found = LINE.fullmatch(line.strip())
        if found and now - int(found.group(1)) < FRESH_SECONDS:
            fresh.add(found.group(2))
    for kind in (Presence.COMPUTER, Presence.PHONE):
        if kind.value in fresh:
            return kind
    return Presence.AWAY


def read_presence(folder: Path, now: float) -> Presence:
    """`unknown` when the folder is missing or cannot be listed, which callers treat as away."""
    try:
        files = sorted(folder.iterdir())
    except OSError:
        return Presence.UNKNOWN
    return classify((line for file in files for line in _lines_of(file)), now)


def current(clock: Callable[[], float] = time.time) -> Presence:
    return read_presence(PRESENCE_DIR, clock())


def _lines_of(file: Path) -> list[str]:
    """A file that vanished since the listing, cannot be read or is dot-named has no lines."""
    try:
        if file.name.startswith(".") or not file.is_file():
            return []
        with file.open(encoding="utf-8", errors="replace") as handle:
            return handle.read(MAX_FILE_BYTES).splitlines()
    except OSError:
        return []
