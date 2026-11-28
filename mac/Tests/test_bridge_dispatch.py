"""Bridge: browser gate, consent, method, endpoint dispatch, command execution, stdin, access logging."""
import json
import subprocess
import time
from pathlib import Path
from unittest.mock import call, patch

import pytest

from conftest import (
    cli_run_result,
    http_bridge_get,
    http_bridge_post,
    set_bridge_state,
)
from keyguard_server import access_log, consent
from keyguard_server.config import KEYGUARD_BIN

KEYGUARD = str(KEYGUARD_BIN)


def _bridge_calls(mock_run):
    return list(mock_run.call_args_list)


def _keyguard_calls(mock_run):
    return [c for c in mock_run.call_args_list if c[0][0][0] == KEYGUARD]


def _command_calls(mock_run):
    return [c for c in mock_run.call_args_list if c[0][0][0] != KEYGUARD]


def _route(confirm_rc: int = 0, confirm_stderr: str = "", command_stdout: str = "ran"):
    """Answer the keyguard CLI with `confirm_rc` and every bridge command with success."""
    def side_effect(cmd, **kwargs):
        if cmd[0] == KEYGUARD:
            return cli_run_result(confirm_rc, stderr=confirm_stderr)
        return cli_run_result(0, stdout=command_stdout)
    return side_effect


@pytest.fixture()
def log_path(tmp_path: Path, monkeypatch) -> Path:
    path = tmp_path / "access.log"
    monkeypatch.setattr(access_log, "LOG_PATH", path)
    return path


def _read_log_lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    return [line for line in path.read_text(encoding="utf-8").splitlines() if line]


# ---- Browser gate ----


@pytest.mark.parametrize("header", [{"Origin": "https://example.com"}, {"Sec-Fetch-Site": "cross-site"}])
@pytest.mark.parametrize("name", ["echo", "public-echo", "list?all=1"])
def test_should_refuse_a_browser_request_before_anything_runs(server, prompting_bridge, header, name):
    with patch("subprocess.run", side_effect=_route()) as mock_run:
        status, _ = http_bridge_post(server, name, extra_headers=header)

    assert status == 403
    assert mock_run.call_args_list == []


# ---- Consent ----


def test_should_ask_touch_id_before_running_a_private_endpoint(server, prompting_bridge):
    with patch("subprocess.run", side_effect=_route()) as mock_run:
        status, body = http_bridge_post(server, "echo")

    assert (status, body) == (200, "ran")
    assert [c[0][0][0] for c in mock_run.call_args_list] == [KEYGUARD, "/bin/echo"]


def test_should_ask_again_for_every_request(server, prompting_bridge):
    with patch("subprocess.run", side_effect=_route()) as mock_run:
        http_bridge_post(server, "echo")
        http_bridge_post(server, "echo")

    assert len(_keyguard_calls(mock_run)) == 2


def test_should_send_the_reason_on_stdin_rather_than_in_argv(server, prompting_bridge):
    with patch("subprocess.run", side_effect=_route()) as mock_run:
        http_bridge_post(server, "echo")

    confirm_call = _keyguard_calls(mock_run)[0]
    assert confirm_call[0][0] == [KEYGUARD, "confirm"]
    assert confirm_call[1]["input"] == "Run bridge endpoint echo: /bin/echo hello"


def test_should_show_the_input_the_command_will_get(server, prompting_bridge):
    with patch("subprocess.run", side_effect=_route()) as mock_run:
        http_bridge_post(server, "cat", body="/tmp/a.png\n/tmp/b.png\n")

    assert _keyguard_calls(mock_run)[0][1]["input"] == (
        'Run bridge endpoint cat: /bin/cat, with input "/tmp/a.png", "/tmp/b.png"'
    )


def test_should_not_run_the_command_when_touch_id_is_denied(server, prompting_bridge):
    with patch("subprocess.run", side_effect=_route(confirm_rc=2)) as mock_run:
        status, body = http_bridge_post(server, "echo")

    assert status == 403
    assert "touch id" in body.lower()
    assert _command_calls(mock_run) == []


def test_should_not_run_the_command_when_keyguard_predates_confirm(server, prompting_bridge):
    with patch("subprocess.run", side_effect=_route(confirm_rc=1, confirm_stderr="Unknown command 'confirm'")) as mock_run:
        status, body = http_bridge_post(server, "echo")

    assert status == 500
    assert "Unknown command 'confirm'" in body
    assert _command_calls(mock_run) == []


def test_should_not_run_the_command_when_the_prompt_times_out(server, prompting_bridge):
    def side_effect(cmd, **kwargs):
        if cmd[0] == KEYGUARD:
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=60)
        return cli_run_result(0, stdout="ran")

    with patch("subprocess.run", side_effect=side_effect) as mock_run:
        status, body = http_bridge_post(server, "echo")

    assert status == 500
    assert "timed out" in body
    assert _command_calls(mock_run) == []


def test_should_answer_429_without_prompting_while_another_prompt_is_open(server, prompting_bridge):
    with patch("subprocess.run", side_effect=_route()) as mock_run, consent._prompt_lock:
        status, body = http_bridge_post(server, "echo")

    assert status == 429
    assert "retry" in body
    assert mock_run.call_args_list == []


def test_should_ignore_an_authorization_header(server, prompting_bridge):
    with patch("subprocess.run", side_effect=_route()) as mock_run:
        status, _ = http_bridge_post(server, "echo", extra_headers={"Authorization": "Bearer stale-token"})

    assert status == 200
    assert len(_keyguard_calls(mock_run)) == 1


def test_should_not_prompt_for_an_endpoint_absent_from_the_config(server, prompting_bridge):
    with patch("subprocess.run", side_effect=_route()) as mock_run:
        status, _ = http_bridge_post(server, "not-configured")

    assert status == 404
    assert mock_run.call_args_list == []


def test_should_not_prompt_for_a_method_the_endpoint_refuses(server, prompting_bridge):
    with patch("subprocess.run", side_effect=_route()) as mock_run:
        status, _ = http_bridge_get(server, "echo")

    assert status == 405
    assert mock_run.call_args_list == []


def test_not_configured_returns_501(server, monkeypatch):
    set_bridge_state(monkeypatch)

    status, body = http_bridge_post(server, "anything")

    assert status == 501
    assert "not configured" in body.lower()


# ---- _bridge/list ----


def test_list_returns_200_with_json(server, configured_bridge):
    status, body = http_bridge_get(server, "list?all=1")
    assert status == 200
    names = {e["name"] for e in json.loads(body)}
    assert names == {"echo", "get-status", "stdin-endpoint"}


def test_list_includes_methods_and_timeout(server, configured_bridge):
    _, body = http_bridge_get(server, "list?all=1")
    by_name = {e["name"]: e for e in json.loads(body)}
    assert by_name["echo"]["methods"] == ["POST"]
    assert by_name["get-status"]["methods"] == ["GET"]
    assert by_name["echo"]["timeout"] == 10


def test_list_includes_public_flag_for_each_entry(server, configured_bridge):
    _, body = http_bridge_get(server, "list?all=1")
    for entry in json.loads(body):
        assert entry["public"] is False


def test_list_does_not_expose_command(server, configured_bridge):
    _, body = http_bridge_get(server, "list?all=1")
    assert "command" not in body
    assert "/bin/echo" not in body


def test_list_returns_sorted_names(server, configured_bridge):
    _, body = http_bridge_get(server, "list?all=1")
    names = [e["name"] for e in json.loads(body)]
    assert names == sorted(names)


def test_should_list_only_public_endpoints_without_the_all_parameter(server, configured_bridge):
    status, body = http_bridge_get(server, "list")
    assert status == 200
    assert json.loads(body) == []


def test_should_list_public_endpoints_without_prompting(server, prompting_bridge):
    with patch("subprocess.run", side_effect=_route()) as mock_run:
        status, body = http_bridge_get(server, "list")

    assert status == 200
    assert {e["name"] for e in json.loads(body)} == {"public-echo"}
    assert mock_run.call_args_list == []


def test_should_ask_touch_id_before_listing_private_endpoints(server, prompting_bridge):
    with patch("subprocess.run", side_effect=_route()) as mock_run:
        status, body = http_bridge_get(server, "list?all=1")

    assert status == 200
    assert {e["name"] for e in json.loads(body)} == {"echo", "cat", "public-echo"}
    assert _keyguard_calls(mock_run)[0][1]["input"] == consent.LIST_ALL_REASON


def test_should_answer_403_when_the_full_listing_is_denied(server, prompting_bridge):
    with patch("subprocess.run", side_effect=_route(confirm_rc=2)):
        status, body = http_bridge_get(server, "list?all=1")

    assert status == 403
    assert "echo" not in body


def test_should_treat_an_all_parameter_without_a_value_as_public_only(server, prompting_bridge):
    with patch("subprocess.run", side_effect=_route()) as mock_run:
        status, body = http_bridge_get(server, "list?all")

    assert status == 200
    assert {e["name"] for e in json.loads(body)} == {"public-echo"}
    assert mock_run.call_args_list == []


# ---- Endpoint dispatch ----


def test_unknown_endpoint_returns_404(server, configured_bridge):
    status, _ = http_bridge_post(server, "nonexistent")
    assert status == 404


def test_wrong_method_returns_405(server, configured_bridge):
    """`echo` is POST-only - calling via GET returns 405."""
    status, _ = http_bridge_get(server, "echo")
    assert status == 405


def test_get_endpoint_via_get_succeeds(server, configured_bridge):
    with patch("subprocess.run", return_value=cli_run_result(0, stdout="ok")):
        status, body = http_bridge_get(server, "get-status")

    assert status == 200
    assert body == "ok"


def test_post_endpoint_via_post_succeeds(server, configured_bridge):
    with patch("subprocess.run", return_value=cli_run_result(0, stdout="hello\n")):
        status, body = http_bridge_post(server, "echo")

    assert status == 200
    assert body == "hello\n"


# ---- Command execution ----


def test_runs_configured_command(server, configured_bridge):
    with patch("subprocess.run", return_value=cli_run_result(0, stdout="out")) as mock_run:
        http_bridge_post(server, "echo")

    time.sleep(0.2)
    bridge_calls = _bridge_calls(mock_run)
    assert len(bridge_calls) == 1
    assert bridge_calls[0] == call(
        ["/bin/echo", "hello"],
        capture_output=True, text=True, errors="replace", timeout=10, input=None,
    )


def test_command_failure_returns_500(server, configured_bridge):
    with patch("subprocess.run", return_value=cli_run_result(1, stderr="oops")):
        status, body = http_bridge_post(server, "echo")

    assert status == 500
    assert "oops" in body


def test_command_failure_falls_back_to_stdout_when_no_stderr(server, configured_bridge):
    with patch("subprocess.run", return_value=cli_run_result(1, stdout="error detail", stderr="")):
        status, body = http_bridge_post(server, "echo")

    assert status == 500
    assert "error detail" in body


def test_command_timeout_returns_504(server, configured_bridge):
    with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="echo", timeout=10)):
        status, body = http_bridge_post(server, "echo")

    assert status == 504
    assert "timed out" in body.lower()


def test_command_not_found_returns_500(server, configured_bridge):
    with patch("subprocess.run", side_effect=FileNotFoundError):
        status, body = http_bridge_post(server, "echo")

    assert status == 500
    assert "not found" in body.lower()


# ---- stdin ----


def test_stdin_false_does_not_pass_body(server, configured_bridge, approved_prompts):
    with patch("subprocess.run", return_value=cli_run_result(0, stdout="out")) as mock_run:
        http_bridge_post(server, "echo", body="ignored")

    time.sleep(0.2)
    bridge_calls = _bridge_calls(mock_run)
    assert len(bridge_calls) == 1
    assert bridge_calls[0][1]["input"] is None
    assert "ignored" not in approved_prompts[0]


def test_stdin_true_passes_body(server, configured_bridge):
    with patch("subprocess.run", return_value=cli_run_result(0, stdout="piped")) as mock_run:
        http_bridge_post(server, "stdin-endpoint", body="hello stdin")

    time.sleep(0.2)
    bridge_calls = _bridge_calls(mock_run)
    assert len(bridge_calls) == 1
    assert bridge_calls[0][1]["input"] == "hello stdin"


# ---- Access log ----


def test_success_writes_access_log_line(server, configured_bridge, log_path):
    with patch("subprocess.run", return_value=cli_run_result(0, stdout="ok")):
        http_bridge_post(server, "echo")

    lines = _read_log_lines(log_path)
    assert len(lines) == 1
    assert "bridge:echo" in lines[0]


def test_failure_does_not_write_access_log_line(server, configured_bridge, log_path):
    with patch("subprocess.run", return_value=cli_run_result(1, stderr="fail")):
        http_bridge_post(server, "echo")

    assert _read_log_lines(log_path) == []


# ---- subprocess kwargs / robustness ----


def test_subprocess_uses_errors_replace_for_non_utf8_safety(server, configured_bridge):
    """Bridge commands with binary stdout must not crash the response handler."""
    with patch("subprocess.run", return_value=cli_run_result(0, stdout="ok")) as mock_run:
        http_bridge_post(server, "echo")

    time.sleep(0.2)
    bridge_calls = _bridge_calls(mock_run)
    assert bridge_calls[0][1].get("errors") == "replace"


def test_oserror_starting_command_returns_500(server, configured_bridge):
    """Permission denied or similar OSErrors when spawning the command return 500, not 5xx crash."""
    with patch("subprocess.run", side_effect=PermissionError("no exec")):
        status, body = http_bridge_post(server, "echo")

    assert status == 500
    assert "no exec" in body
