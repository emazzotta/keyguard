"""Redacts Telegram bot tokens from text that leaves the service.

The pattern is the one in mazzotta-bot `src/token_scrubber.py`. Change both copies together.
"""
from __future__ import annotations

import re
from typing import Final

REDACTED: Final = "[redacted]"
TOKEN_PATTERN: Final = re.compile(r"bot\d+:[A-Za-z0-9_-]+|\d+:[A-Za-z0-9_-]{35,}")


def scrub(text: str) -> str:
    return TOKEN_PATTERN.sub(REDACTED, text)
