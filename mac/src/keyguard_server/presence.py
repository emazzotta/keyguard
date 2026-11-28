"""Whether the owner sits at a web terminal, read from the folder that each web-terminal instance writes.

The rules are keyguard-passkey's: presence_rules.py links to keyguard_passkey/service/presence.py, and `make install`
copies it beside this module. This module adds the server's folder and clock. The dotfiles keep their own reader, as
bin/presence.py, since they run without this repository: change its rules with these.
"""
from __future__ import annotations

import time
from collections.abc import Callable

from .config import PRESENCE_DIR
from .presence_rules import FRESH_SECONDS, MAX_FILE_BYTES, Presence, classify, read_presence

__all__ = ["FRESH_SECONDS", "MAX_FILE_BYTES", "Presence", "attended", "classify", "current", "read_presence"]


def attended(presence: Presence) -> bool:
    return presence in (Presence.COMPUTER, Presence.PHONE)


def current(clock: Callable[[], float] = time.time) -> Presence:
    return read_presence(PRESENCE_DIR, clock())
