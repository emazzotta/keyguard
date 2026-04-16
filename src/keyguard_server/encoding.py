"""Wire-format helpers for multi-key responses (base64 escapes for multi-line values)."""
from __future__ import annotations

import base64

_BASE64_PREFIX = "base64:"


def decode_value(value: str) -> str:
    if value.startswith(_BASE64_PREFIX):
        try:
            return base64.b64decode(value[len(_BASE64_PREFIX):], validate=True).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            pass
    return value


def encode_value(value: str) -> str:
    if "\n" in value:
        return _BASE64_PREFIX + base64.b64encode(value.encode("utf-8")).decode("ascii")
    return value


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
