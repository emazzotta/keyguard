"""Wire-format helpers for multi-key responses. A value travels on one line as keyguard-passkey encodes it:
line_encoding.py links to keyguard_passkey/encoding.py, and `make install` copies it beside this module."""
from __future__ import annotations

from .line_encoding import from_one_line as decode_value
from .line_encoding import on_one_line as encode_value

__all__ = ["decode_value", "encode_value", "format_response", "parse_key_value_output", "parse_timeout"]


def format_response(keys: list[str], values: dict[str, str | None]) -> str:
    if len(keys) == 1:
        return values[keys[0]] or ""
    return "\n".join(
        f"{k}={encode_value(values[k])}" for k in keys if values[k] is not None
    ) + "\n"


def parse_key_value_output(stdout: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in stdout.strip().split("\n"):
        if "=" in line:
            k, v = line.split("=", 1)
            values[k] = decode_value(v)
    return values


def parse_timeout(query: dict[str, list[str]], cap: int) -> int | None:
    timeout_values = query.get("timeout")
    if not timeout_values:
        return None
    try:
        timeout = int(timeout_values[0])
    except (ValueError, IndexError):
        return None
    if timeout <= 0:
        return None
    return min(timeout, cap)
