"""Tests for GET /<key> and GET /_keys (no caching)."""
import subprocess
from unittest.mock import patch

from conftest import cli_run_result, http_get


# ---- Single key ----


def test_single_key_returns_raw_value(server):
    with patch("subprocess.run", return_value=cli_run_result(0, stdout="secret123")) as mock_run:
        status, body = http_get(server, "/MY_TOKEN")

    assert status == 200
    assert body == "secret123"
    mock_run.assert_any_call(
        ["/usr/local/bin/keyguard", "get", "MY_TOKEN"],
        capture_output=True, text=True, errors="replace", timeout=60, input=None,
    )


def test_multiple_keys_returns_key_value_lines(server):
    with patch("subprocess.run", return_value=cli_run_result(0, stdout="A=1\nB=2\n")):
        status, body = http_get(server, "/A,B")

    assert status == 200
    assert body == "A=1\nB=2\n"


def test_missing_path_returns_400(server):
    status, _ = http_get(server, "/")
    assert status == 400


def test_touch_id_cancelled_returns_403(server):
    with patch("subprocess.run", return_value=cli_run_result(2)):
        status, body = http_get(server, "/MY_TOKEN")

    assert status == 403
    assert "Touch ID" in body


def test_keyguard_error_returns_500(server):
    with patch("subprocess.run", return_value=cli_run_result(1, stderr="key not found")):
        status, body = http_get(server, "/MISSING_KEY")

    assert status == 500
    assert "key not found" in body


def test_missing_encryption_key_returns_500_with_clear_message(server):
    with patch("subprocess.run", return_value=cli_run_result(1, stderr="No encryption key found in Keychain")):
        status, body = http_get(server, "/MY_TOKEN")

    assert status == 500
    assert "encryption key" in body


def test_subprocess_timeout_returns_500(server):
    with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="keyguard", timeout=60)):
        status, _ = http_get(server, "/MY_TOKEN")

    assert status == 500


# ---- _keys list ----


def test_keys_list_calls_list_command(server):
    with patch("subprocess.run", return_value=cli_run_result(0, stdout="FOO\nBAR\n")) as mock_run:
        status, body = http_get(server, "/_keys")

    assert status == 200
    assert "FOO" in body
    mock_run.assert_called_once_with(
        ["/usr/local/bin/keyguard", "list"],
        capture_output=True, text=True, errors="replace", timeout=60, input=None,
    )


def test_keys_list_touch_id_cancelled_returns_403(server):
    with patch("subprocess.run", return_value=cli_run_result(2)):
        status, body = http_get(server, "/_keys?timeout=30")

    assert status == 403
    assert "Touch ID" in body
