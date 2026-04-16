"""Integration tests verifying notifications fire (or don't) for each request type."""
import time
from unittest.mock import patch

from conftest import cli_run_result, http_get, http_post


def _osascript_calls(mock_run):
    return [
        c for c in mock_run.call_args_list
        if len(c[0]) > 0 and isinstance(c[0][0], list) and c[0][0] and c[0][0][0] == "osascript"
    ]


def test_get_sends_notification_async(server):
    with patch("subprocess.run", return_value=cli_run_result(0, stdout="secret123")) as mock_run:
        status, _ = http_get(server, "/MY_TOKEN")

    assert status == 200
    time.sleep(0.2)
    osa = _osascript_calls(mock_run)
    assert len(osa) == 1
    assert "MY_TOKEN" in osa[0][0][0][2]
    assert "keyguard" in osa[0][0][0][2]


def test_cached_get_sends_notification_with_cached_hint(server):
    with patch("subprocess.run", return_value=cli_run_result(0, stdout="secret123")):
        http_get(server, "/MY_TOKEN?timeout=60")

    with patch("subprocess.run") as mock_run:
        http_get(server, "/MY_TOKEN?timeout=60")

    time.sleep(0.2)
    osa = _osascript_calls(mock_run)
    assert len(osa) == 1
    assert "(cached)" in osa[0][0][0][2]


def test_get_with_source_header_includes_hint_in_notification(server):
    def side_effect(cmd, **kwargs):
        if cmd[0] == "/usr/local/bin/keyguard":
            return cli_run_result(0, stdout="secret123")
        if cmd[0] == "docker":
            return cli_run_result(0, stdout="/my-container\n")
        return cli_run_result(0)

    with patch("subprocess.run", side_effect=side_effect) as mock_run:
        status, _ = http_get(server, "/MY_TOKEN", extra_headers={"X-Keyguard-Source": "abc123"})

    assert status == 200
    time.sleep(0.3)
    osa = _osascript_calls(mock_run)
    assert len(osa) == 1
    assert "my-container" in osa[0][0][0][2]


def test_list_does_not_send_notification(server):
    with patch("subprocess.run", return_value=cli_run_result(0, stdout="KEY1\nKEY2\n")) as mock_run:
        http_get(server, "/_keys")

    time.sleep(0.2)
    assert _osascript_calls(mock_run) == []


def test_post_does_not_send_notification(server):
    with patch("subprocess.run", return_value=cli_run_result(0, stdout="Set 'TOKEN'")) as mock_run:
        http_post(server, "/TOKEN", body="value")

    time.sleep(0.2)
    assert _osascript_calls(mock_run) == []


def test_notification_uses_short_dash_not_em_dash(server):
    """Project rule: short dashes everywhere, no em dashes."""
    with patch("subprocess.run", return_value=cli_run_result(0, stdout="secret")) as mock_run:
        http_get(server, "/MY_TOKEN")

    time.sleep(0.2)
    osa = _osascript_calls(mock_run)
    assert len(osa) == 1
    msg = osa[0][0][0][2]
    assert "—" not in msg
    assert " - " in msg
