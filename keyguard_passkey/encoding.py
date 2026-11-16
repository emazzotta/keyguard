"""How a value travels on one line: as it is, or as base64 after a marker."""
from __future__ import annotations

import base64
import binascii
from typing import Final

BASE64_MARKER: Final = "base64:"
LINE_BREAKS: Final = ("\n", "\r")


def as_env_line(name: str, value: str) -> str:
    return f"{name}={on_one_line(value)}\n"


def as_shell_export(name: str, value: str) -> str:
    quoted = value.replace("'", "'\\''")
    return f"export {name}='{quoted}'\n"


def on_one_line(value: str) -> str:
    if value.startswith(BASE64_MARKER) or any(line_break in value for line_break in LINE_BREAKS):
        return BASE64_MARKER + base64.b64encode(value.encode()).decode()
    return value


def from_one_line(text: str) -> str:
    """Text after the marker that is not base64 of UTF-8 stays as it is, as in the Swift keyguard."""
    if not text.startswith(BASE64_MARKER):
        return text
    try:
        return base64.b64decode(text[len(BASE64_MARKER):], validate=True).decode()
    except (binascii.Error, UnicodeDecodeError):
        return text
