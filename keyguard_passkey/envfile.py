"""Read the secrets of a plaintext .env file."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Final

from .encoding import from_one_line

ASSIGNMENT: Final = re.compile(r"(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)")
INLINE_COMMENT: Final = re.compile(r"\s+#.*")
QUOTES: Final = ("\"", "'")


class EnvFileError(Exception):
    pass


def parse_value(raw: str) -> str:
    text = raw.strip()
    if text[:1] in QUOTES:
        closing = text.find(text[0], 1)
        if closing > 0:
            return text[1:closing]
    return INLINE_COMMENT.sub("", text).strip()


def parse_env(text: str) -> dict[str, str]:
    """Names and values in file order; a problem names its line number, never its content."""
    entries: dict[str, str] = {}
    for number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        assignment = ASSIGNMENT.fullmatch(stripped)
        if assignment is None:
            raise EnvFileError(f"line {number} is not NAME=value")
        entries[assignment.group(1)] = from_one_line(parse_value(assignment.group(2)))
    return entries


def read_env_text(text: str, source: str) -> dict[str, str]:
    entries = parse_env(text)
    if not entries:
        raise EnvFileError(f"{source} holds no NAME=value line")
    return entries


def read_env_file(path: Path) -> dict[str, str]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise EnvFileError(f"cannot read {path}: {error.strerror}") from error
    return read_env_text(text, str(path))
