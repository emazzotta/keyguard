"""consent: the Touch ID reason for a bridge request, and the one-prompt-at-a-time gate."""
from __future__ import annotations

import pytest

from keyguard_server import consent, keyguard_cli
from keyguard_server.bridge import Endpoint
from keyguard_server.keyguard_cli import CliResult

HOME = "/Users/tester"


def _endpoint(*command: str, stdin: bool = False) -> Endpoint:
    return Endpoint(command=command, allowed_methods=frozenset(["POST"]), pass_stdin=stdin, timeout=10)


@pytest.fixture(autouse=True)
def home(monkeypatch):
    monkeypatch.setenv("HOME", HOME)


# ---- Command ----


@pytest.mark.parametrize("command, shown", [
    (("/usr/bin/afplay", "/System/Library/Sounds/Purr.aiff"), "/usr/bin/afplay /System/Library/Sounds/Purr.aiff"),
    ((f"{HOME}/dotfiles/bin/fileserver", "__bridge_serve__"), "~/dotfiles/bin/fileserver __bridge_serve__"),
    (("/usr/bin/find", f"{HOME}/Applications (Parallels)", "-name", "*.app"),
     "/usr/bin/find ~/'Applications (Parallels)' -name '*.app'"),
    (("osascript", "-e", 'tell application "Spotify" to play'),
     "osascript -e 'tell application \"Spotify\" to play'"),
    (("/bin/ls", HOME), "/bin/ls ~"),
    (("/bin/ls", f"{HOME}2/other"), f"/bin/ls {HOME}2/other"),
])
def test_should_show_the_command_as_a_shell_would_run_it(command, shown):
    assert consent.run_reason("x", _endpoint(*command), "") == f"Run bridge endpoint x: {shown}"


# ---- Input ----


def test_should_show_each_input_line_quoted():
    reason = consent.run_reason("mac-trash", _endpoint("/bin/trash", stdin=True), "/a b.png\n/c.png\n")

    assert reason == 'Run bridge endpoint mac-trash: /bin/trash, with input "/a b.png", "/c.png"'


def test_should_leave_out_a_body_the_command_never_reads():
    reason = consent.run_reason("echo", _endpoint("/bin/echo"), "ignored")

    assert reason == "Run bridge endpoint echo: /bin/echo"


def test_should_leave_out_an_empty_body():
    assert consent.run_reason("cat", _endpoint("/bin/cat", stdin=True), "") == "Run bridge endpoint cat: /bin/cat"


def test_should_show_a_blank_line_rather_than_hide_it():
    reason = consent.run_reason("cat", _endpoint("/bin/cat", stdin=True), "a\n\nb")

    assert reason.endswith('with input "a", "", "b"')


@pytest.mark.parametrize("body, count", [("1\n2\n3\n4\n5\n", "and 1 more line"), ("1\n2\n3\n4\n5\n6\n7", "and 3 more lines")])
def test_should_count_the_lines_it_does_not_show(body, count):
    reason = consent.run_reason("cat", _endpoint("/bin/cat", stdin=True), body)

    assert reason.endswith(f'with input "1", "2", "3", "4", {count}')


def test_should_cut_a_long_line_in_the_middle_so_the_file_name_stays_visible():
    line = "/Users/tester/" + "d" * 200 + "/keep-this-name.png"

    reason = consent.run_reason("cat", _endpoint("/bin/cat", stdin=True), line)

    shown = reason.split("with input ", 1)[1]
    assert shown == f'"{line[:50]}"{consent.ELISION}"{line[-50:]}"'
    assert shown.endswith('keep-this-name.png"')


@pytest.mark.parametrize("line, shown", [
    ('say "hi"', r'"say \"hi\""'),
    ("back\\slash", r'"back\\slash"'),
    ("tab\there", r'"tab\u0009here"'),
    ("nul\x00byte", r'"nul\u0000byte"'),
    ("right\u202eto-left", r'"right\u202eto-left"'),
    ("zero\u200bwidth", r'"zero\u200bwidth"'),
    ("Präsentation.pdf", '"Präsentation.pdf"'),
])
def test_should_escape_characters_that_could_disguise_the_input(line, shown):
    reason = consent.run_reason("cat", _endpoint("/bin/cat", stdin=True), line)

    assert reason.endswith(f"with input {shown}")


@pytest.mark.parametrize("separator", ["\r", "\r\n", " ", "\x85"])
def test_should_split_lines_wherever_a_splitlines_reader_would(separator):
    reason = consent.run_reason("cat", _endpoint("/bin/cat", stdin=True), f"/ok.png{separator}/Projects")

    assert reason.endswith('with input "/ok.png", "/Projects"')


# ---- One prompt at a time ----


def test_should_pass_the_reason_to_keyguard_confirm(monkeypatch):
    shown: list[str] = []
    monkeypatch.setattr(keyguard_cli, "confirm", lambda reason: shown.append(reason) or CliResult(0, "", ""))

    result = consent.ask("Run bridge endpoint echo: /bin/echo")

    assert result is not None and result.ok
    assert shown == ["Run bridge endpoint echo: /bin/echo"]


def test_should_refuse_to_open_a_second_prompt(monkeypatch):
    monkeypatch.setattr(keyguard_cli, "confirm", lambda reason: pytest.fail("a second prompt was opened"))

    with consent._prompt_lock:
        assert consent.ask("Run bridge endpoint echo: /bin/echo") is None


def test_should_free_the_gate_once_the_prompt_is_answered(monkeypatch):
    monkeypatch.setattr(keyguard_cli, "confirm", lambda reason: CliResult(2, "", "cancelled"))

    consent.ask("first")

    assert not consent._prompt_lock.locked()
