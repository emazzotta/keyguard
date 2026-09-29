"""Touch ID consent for the bridge: one prompt per request, naming what will run.

Every reason is built here from the endpoint about to run and the input it will get,
so a caller chooses the input but never the wording.
"""
from __future__ import annotations

import shlex
import threading
import unicodedata
from pathlib import Path
from typing import Final

from . import keyguard_cli
from .bridge import Endpoint
from .keyguard_cli import CliResult

LIST_ALL_REASON: Final = "List all bridge endpoints, private ones included"
MAX_INPUT_LINES: Final = 4
MAX_LINE_CHARS: Final = 100
ELISION: Final = " … "
_ESCAPED_CATEGORIES: Final = frozenset({"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"})
_LAST_BMP_CODE_POINT: Final = 0xFFFF

_prompt_lock = threading.Lock()


def ask(reason: str) -> CliResult | None:
    """Show the Touch ID prompt, or return None while another one is on screen."""
    if not _prompt_lock.acquire(blocking=False):
        return None
    try:
        return keyguard_cli.confirm(reason)
    finally:
        _prompt_lock.release()


def run_reason(name: str, endpoint: Endpoint, body: str) -> str:
    reason = f"Run bridge endpoint {name}: {_command_text(endpoint.command)}"
    lines = body.splitlines() if endpoint.pass_stdin else []
    if lines:
        reason += f", with input {_input_text(lines)}"
    return reason


def _command_text(command: tuple[str, ...]) -> str:
    home = str(Path.home())
    return " ".join(_argument_text(argument, home) for argument in command)


def _argument_text(argument: str, home: str) -> str:
    if argument == home:
        return "~"
    if argument.startswith(home + "/"):
        return "~/" + shlex.quote(argument[len(home) + 1:])
    return shlex.quote(argument)


def _input_text(lines: list[str]) -> str:
    shown = [_line_text(line) for line in lines[:MAX_INPUT_LINES]]
    hidden = len(lines) - len(shown)
    if hidden:
        shown.append(f"and {hidden} more line{'' if hidden == 1 else 's'}")
    return ", ".join(shown)


def _line_text(line: str) -> str:
    if len(line) <= MAX_LINE_CHARS:
        return _quoted(line)
    half = MAX_LINE_CHARS // 2
    return _quoted(line[:half]) + ELISION + _quoted(line[-half:])


def _quoted(text: str) -> str:
    return '"' + "".join(_escaped(char) for char in text) + '"'


def _escaped(char: str) -> str:
    if char in '"\\':
        return "\\" + char
    if unicodedata.category(char) not in _ESCAPED_CATEGORIES:
        return char
    code = ord(char)
    return f"\\u{code:04x}" if code <= _LAST_BMP_CODE_POINT else f"\\U{code:08x}"
