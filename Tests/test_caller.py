"""caller: the session a client names in two headers, and where that name reaches the prompts."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from conftest import cli_run_result, http_bridge_get, http_bridge_post, http_get

from keyguard_server import access_log, consent
from keyguard_server.bridge import Endpoint
from keyguard_server.caller import (
    CLIPPED,
    MAX_TITLE_CHARS,
    SESSION_HEADER,
    TITLE_HEADER,
    Caller,
)
from keyguard_server.config import KEYGUARD_BIN

KEYGUARD = str(KEYGUARD_BIN)
SESSION = "48f92bcb"
TITLE = "Keyguard approval session names"
QUOTED_TITLE = "Keyguard%20approval%20session%20names"
LABEL = f'"{TITLE}" ({SESSION})'
HEADERS = {SESSION_HEADER: SESSION, TITLE_HEADER: QUOTED_TITLE}


# ---- Label ----


@pytest.mark.parametrize("session, title, label", [
    (SESSION, QUOTED_TITLE, LABEL),
    (None, QUOTED_TITLE, f'"{TITLE}"'),
    (SESSION, None, f"session {SESSION}"),
    (None, None, None),
    ("", "", None),
])
def test_should_name_what_the_client_reported(session, title, label):
    assert Caller.parse(session, title).label == label


def test_should_name_an_opencode_session_by_the_end_of_its_id():
    assert Caller.parse("G3yIYwxa", QUOTED_TITLE).label == f'"{TITLE}" (G3yIYwxa)'


@pytest.mark.parametrize("session", ["48f92bc", "48f92bcb0", "48f92bcb-f080", "48f92bc!", "48f92bc_", " 48f92bcb"])
def test_should_drop_a_session_that_is_not_eight_letters_or_digits(session):
    assert Caller.parse(session, None).session is None


def test_should_decode_a_percent_encoded_utf8_title():
    assert Caller.parse(None, "%C3%9Cber%20die%20Br%C3%BCcke").title == "Über die Brücke"


@pytest.mark.parametrize("hidden", [chr(0x202E), chr(0x200B), chr(0x0007), chr(0xE000)])
def test_should_drop_control_format_and_private_use_characters(hidden):
    assert Caller.parse(None, f"Fix{hidden}%20CI").title == "Fix CI"


def test_should_collapse_line_breaks_and_runs_of_space_into_one_space():
    assert Caller.parse(None, "%20Fix%0A%0A%09CI%E2%80%A8now%20%20").title == "Fix CI now"


def test_should_treat_a_title_of_only_hidden_characters_as_absent():
    assert Caller.parse(SESSION, "%E2%80%AE%0A").label == f"session {SESSION}"


def test_should_clip_a_long_title_and_mark_the_cut():
    title = Caller.parse(None, "x" * (MAX_TITLE_CHARS + 20)).title

    assert title == "x" * (MAX_TITLE_CHARS - len(CLIPPED)) + CLIPPED
    assert len(title) == MAX_TITLE_CHARS


def test_should_keep_a_title_that_fits_exactly():
    assert Caller.parse(None, "x" * MAX_TITLE_CHARS).title == "x" * MAX_TITLE_CHARS


def test_should_escape_quotes_so_a_title_cannot_close_its_own():
    label = Caller.parse(SESSION, '%22%3A%20%2Fbin%2Ftrue%20%5C').label

    assert label == f'"\\": /bin/true \\\\" ({SESSION})'


# ---- Bridge consent ----


def test_should_name_the_caller_after_the_command():
    endpoint = Endpoint(command=("/bin/echo", "hi"), allowed_methods=frozenset(["POST"]),
                        pass_stdin=False, timeout=10)

    assert consent.run_reason("echo", endpoint, "", caller=LABEL) == f"Run bridge endpoint echo: /bin/echo hi, for {LABEL}"


def test_should_name_the_caller_after_the_input():
    endpoint = Endpoint(command=("/bin/trash",), allowed_methods=frozenset(["POST"]),
                        pass_stdin=True, timeout=10)

    reason = consent.run_reason("mac-trash", endpoint, "/a.png\n", caller=LABEL)

    assert reason == f'Run bridge endpoint mac-trash: /bin/trash, with input "/a.png", for {LABEL}'


def test_should_name_the_caller_when_listing_every_endpoint():
    assert consent.list_all_reason(LABEL) == f"List all bridge endpoints for {LABEL}, private ones included"
    assert consent.list_all_reason(None) == consent.LIST_ALL_REASON


# ---- Handler ----


def _keyguard_args(mock_run) -> list[list[str]]:
    return [c[0][0] for c in mock_run.call_args_list if c[0][0][0] == KEYGUARD]


def _route(cmd, **kwargs):
    return cli_run_result(0, stdout="secret" if cmd[0] == KEYGUARD else "ran")


def test_should_pass_the_caller_to_a_secret_read(server):
    with patch("subprocess.run", side_effect=_route) as mock_run:
        status, _ = http_get(server, "/MY_TOKEN", extra_headers=HEADERS)

    assert status == 200
    assert _keyguard_args(mock_run) == [[KEYGUARD, "get", "MY_TOKEN", "--caller", LABEL]]


def test_should_pass_the_caller_after_the_cache_duration(server):
    with patch("subprocess.run", side_effect=_route) as mock_run:
        http_get(server, "/MY_TOKEN?timeout=60", extra_headers=HEADERS)

    assert _keyguard_args(mock_run) == [[KEYGUARD, "get", "MY_TOKEN", "--cache-duration", "60", "--caller", LABEL]]


def test_should_pass_the_caller_to_the_key_list(server):
    with patch("subprocess.run", side_effect=_route) as mock_run:
        http_get(server, "/_keys", extra_headers=HEADERS)

    assert _keyguard_args(mock_run) == [[KEYGUARD, "list", "--caller", LABEL]]


def test_should_leave_the_cli_call_unchanged_without_headers(server):
    with patch("subprocess.run", side_effect=_route) as mock_run:
        http_get(server, "/MY_TOKEN", extra_headers={SESSION_HEADER: "not-a-session"})

    assert _keyguard_args(mock_run) == [[KEYGUARD, "get", "MY_TOKEN"]]


def test_should_show_the_caller_in_the_bridge_prompt(server, prompting_bridge):
    with patch("subprocess.run", side_effect=_route) as mock_run:
        status, _ = http_bridge_post(server, "echo", extra_headers=HEADERS)

    assert status == 200
    confirm = [c for c in mock_run.call_args_list if c[0][0] == [KEYGUARD, "confirm"]]
    assert confirm[0][1]["input"] == f"Run bridge endpoint echo: /bin/echo hello, for {LABEL}"


def test_should_show_the_caller_in_the_full_listing_prompt(server, prompting_bridge):
    with patch("subprocess.run", side_effect=_route) as mock_run:
        http_bridge_get(server, "list?all=1", extra_headers=HEADERS)

    assert mock_run.call_args_list[0][1]["input"] == consent.list_all_reason(LABEL)


# ---- Access log ----


@pytest.fixture()
def log_path(tmp_path: Path, monkeypatch) -> Path:
    path = tmp_path / "access.log"
    monkeypatch.setattr(access_log, "LOG_PATH", path)
    return path


def test_should_record_the_session_in_the_access_log(server, log_path):
    with patch("subprocess.run", side_effect=_route):
        http_get(server, "/MY_TOKEN", extra_headers=HEADERS)

    assert f"localhost session {SESSION} MY_TOKEN" in log_path.read_text(encoding="utf-8")


def test_should_record_the_session_of_a_bridge_call(server, log_path, prompting_bridge):
    with patch("subprocess.run", side_effect=_route):
        http_bridge_post(server, "echo", extra_headers=HEADERS)

    assert f"session {SESSION} bridge:echo" in log_path.read_text(encoding="utf-8")
