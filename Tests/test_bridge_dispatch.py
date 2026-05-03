"""Bridge: auth, method, endpoint dispatch, command execution, stdin, notifications."""
import subprocess
import time
from unittest.mock import call, patch

from conftest import (
    BRIDGE_TOKEN,
    cli_run_result,
    http_bridge_get,
    http_bridge_post,
    set_bridge_state,
)


def _bridge_calls(mock_run):
    return [c for c in mock_run.call_args_list if c[0][0][0] != "osascript"]


# ---- Auth ----


def test_missing_auth_header_returns_401(server, configured_bridge):
    status, _ = http_bridge_get(server, "get-status", token=None)
    assert status == 401


def test_wrong_token_returns_401(server, configured_bridge):
    status, _ = http_bridge_get(server, "get-status", token="wrong-token")
    assert status == 401


def test_correct_token_succeeds(server, configured_bridge):
    with patch("subprocess.run", return_value=cli_run_result(0, stdout="ok")):
        status, _ = http_bridge_get(server, "get-status", token=BRIDGE_TOKEN)

    assert status == 200


def test_not_configured_returns_501(server, monkeypatch):
    set_bridge_state(monkeypatch)

    status, body = http_bridge_post(server, "anything", token=BRIDGE_TOKEN)

    assert status == 501
    assert "not configured" in body.lower()


# ---- _bridge/list ----


def test_list_returns_200_with_json(server, configured_bridge):
    status, body = http_bridge_get(server, "list", token=BRIDGE_TOKEN)
    assert status == 200
    endpoints = __import__("json").loads(body)
    names = {e["name"] for e in endpoints}
    assert names == {"echo", "get-status", "stdin-endpoint"}


def test_list_includes_methods_and_timeout(server, configured_bridge):
    _, body = http_bridge_get(server, "list", token=BRIDGE_TOKEN)
    endpoints = __import__("json").loads(body)
    by_name = {e["name"]: e for e in endpoints}
    assert by_name["echo"]["methods"] == ["POST"]
    assert by_name["get-status"]["methods"] == ["GET"]
    assert by_name["echo"]["timeout"] == 10


def test_list_includes_public_flag_for_each_entry(server, configured_bridge):
    _, body = http_bridge_get(server, "list", token=BRIDGE_TOKEN)
    endpoints = __import__("json").loads(body)
    for entry in endpoints:
        assert "public" in entry
        assert entry["public"] is False


def test_list_does_not_expose_command(server, configured_bridge):
    _, body = http_bridge_get(server, "list", token=BRIDGE_TOKEN)
    assert "command" not in body
    assert "/bin/echo" not in body


def test_list_without_auth_returns_only_public_endpoints(server, configured_bridge):
    """Anonymous listing reveals only public endpoints. configured_bridge has none,
    so the list is empty - protected endpoints are not leaked to anonymous callers.
    """
    status, body = http_bridge_get(server, "list", token=None)
    assert status == 200
    assert __import__("json").loads(body) == []


def test_list_with_wrong_token_returns_only_public_endpoints(server, configured_bridge):
    status, body = http_bridge_get(server, "list", token="garbage")
    assert status == 200
    assert __import__("json").loads(body) == []


def test_list_without_bearer_does_not_invoke_keyguard(server, lazy_token_bridge):
    """Anonymous list must never trigger Touch ID, even when a token has not been resolved yet."""
    with patch("subprocess.run") as mock_run:
        status, _ = http_bridge_get(server, "list", token=None)

    assert status == 200
    keyguard_calls = [c for c in mock_run.call_args_list if c[0][0][0] == "/usr/local/bin/keyguard"]
    assert keyguard_calls == []


def test_list_returns_sorted_names(server, configured_bridge):
    _, body = http_bridge_get(server, "list", token=BRIDGE_TOKEN)
    endpoints = __import__("json").loads(body)
    names = [e["name"] for e in endpoints]
    assert names == sorted(names)


# ---- Endpoint dispatch ----


def test_unknown_endpoint_returns_404(server, configured_bridge):
    status, _ = http_bridge_post(server, "nonexistent", token=BRIDGE_TOKEN)
    assert status == 404


def test_wrong_method_returns_405(server, configured_bridge):
    """`echo` is POST-only - calling via GET returns 405."""
    status, _ = http_bridge_get(server, "echo", token=BRIDGE_TOKEN)
    assert status == 405


def test_get_endpoint_via_get_succeeds(server, configured_bridge):
    with patch("subprocess.run", return_value=cli_run_result(0, stdout="ok")):
        status, body = http_bridge_get(server, "get-status", token=BRIDGE_TOKEN)

    assert status == 200
    assert body == "ok"


def test_post_endpoint_via_post_succeeds(server, configured_bridge):
    with patch("subprocess.run", return_value=cli_run_result(0, stdout="hello\n")):
        status, body = http_bridge_post(server, "echo", token=BRIDGE_TOKEN)

    assert status == 200
    assert body == "hello\n"


# ---- Command execution ----


def test_runs_configured_command(server, configured_bridge):
    with patch("subprocess.run", return_value=cli_run_result(0, stdout="out")) as mock_run:
        http_bridge_post(server, "echo", token=BRIDGE_TOKEN)

    time.sleep(0.2)
    bridge_calls = _bridge_calls(mock_run)
    assert len(bridge_calls) == 1
    assert bridge_calls[0] == call(
        ["/bin/echo", "hello"],
        capture_output=True, text=True, errors="replace", timeout=10, input=None,
    )


def test_command_failure_returns_500(server, configured_bridge):
    with patch("subprocess.run", return_value=cli_run_result(1, stderr="oops")):
        status, body = http_bridge_post(server, "echo", token=BRIDGE_TOKEN)

    assert status == 500
    assert "oops" in body


def test_command_failure_falls_back_to_stdout_when_no_stderr(server, configured_bridge):
    with patch("subprocess.run", return_value=cli_run_result(1, stdout="error detail", stderr="")):
        status, body = http_bridge_post(server, "echo", token=BRIDGE_TOKEN)

    assert status == 500
    assert "error detail" in body


def test_command_timeout_returns_504(server, configured_bridge):
    with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="echo", timeout=10)):
        status, body = http_bridge_post(server, "echo", token=BRIDGE_TOKEN)

    assert status == 504
    assert "timed out" in body.lower()


def test_command_not_found_returns_500(server, configured_bridge):
    with patch("subprocess.run", side_effect=FileNotFoundError):
        status, body = http_bridge_post(server, "echo", token=BRIDGE_TOKEN)

    assert status == 500
    assert "not found" in body.lower()


# ---- stdin ----


def test_stdin_false_does_not_pass_body(server, configured_bridge):
    with patch("subprocess.run", return_value=cli_run_result(0, stdout="out")) as mock_run:
        http_bridge_post(server, "echo", body="ignored", token=BRIDGE_TOKEN)

    time.sleep(0.2)
    bridge_calls = _bridge_calls(mock_run)
    assert len(bridge_calls) == 1
    assert bridge_calls[0][1]["input"] is None


def test_stdin_true_passes_body(server, configured_bridge):
    with patch("subprocess.run", return_value=cli_run_result(0, stdout="piped")) as mock_run:
        http_bridge_post(server, "stdin-endpoint", body="hello stdin", token=BRIDGE_TOKEN)

    time.sleep(0.2)
    bridge_calls = _bridge_calls(mock_run)
    assert len(bridge_calls) == 1
    assert bridge_calls[0][1]["input"] == "hello stdin"


# ---- Notifications ----


def test_success_sends_notification(server, configured_bridge):
    with patch("subprocess.run", return_value=cli_run_result(0, stdout="ok")) as mock_run:
        http_bridge_post(server, "echo", token=BRIDGE_TOKEN)

    time.sleep(0.2)
    osa = [c for c in mock_run.call_args_list if c[0][0][0] == "osascript"]
    assert len(osa) == 1
    assert "bridge:echo" in osa[0][0][0][2]


def test_failure_does_not_send_notification(server, configured_bridge):
    with patch("subprocess.run", return_value=cli_run_result(1, stderr="fail")) as mock_run:
        http_bridge_post(server, "echo", token=BRIDGE_TOKEN)

    time.sleep(0.2)
    osa = [c for c in mock_run.call_args_list if c[0][0][0] == "osascript"]
    assert osa == []


# ---- subprocess kwargs / robustness ----


def test_subprocess_uses_errors_replace_for_non_utf8_safety(server, configured_bridge):
    """Bridge commands with binary stdout must not crash the response handler."""
    with patch("subprocess.run", return_value=cli_run_result(0, stdout="ok")) as mock_run:
        http_bridge_post(server, "echo", token=BRIDGE_TOKEN)

    time.sleep(0.2)
    bridge_calls = _bridge_calls(mock_run)
    assert bridge_calls[0][1].get("errors") == "replace"


def test_oserror_starting_command_returns_500(server, configured_bridge):
    """Permission denied or similar OSErrors when spawning the command return 500, not 5xx crash."""
    with patch("subprocess.run", side_effect=PermissionError("no exec")):
        status, body = http_bridge_post(server, "echo", token=BRIDGE_TOKEN)

    assert status == 500
    assert "no exec" in body
