"""Tests for the in-memory cache layer applied to GET /<key> and GET /_keys."""
import subprocess
import time
from unittest.mock import patch

from conftest import cli_run_result, http_get, http_post
from keyguard_server import cache


# ---- Single key with timeout ----


def test_with_timeout_passes_cache_duration(server):
    with patch("subprocess.run", return_value=cli_run_result(0, stdout="secret123")) as mock_run:
        status, body = http_get(server, "/MY_TOKEN?timeout=30")

    assert status == 200
    assert body == "secret123"
    mock_run.assert_any_call(
        ["/usr/local/bin/keyguard", "get", "MY_TOKEN", "--cache-duration", "30"],
        capture_output=True, text=True, errors="replace", timeout=60, input=None,
    )


def test_with_timeout_serves_from_cache_on_second_request(server):
    with patch("subprocess.run", return_value=cli_run_result(0, stdout="secret123")):
        http_get(server, "/MY_TOKEN?timeout=30")

    with patch("subprocess.run") as mock_run:
        status, body = http_get(server, "/MY_TOKEN?timeout=30")

    assert status == 200
    assert body == "secret123"
    time.sleep(0.2)
    keyguard_calls = [c for c in mock_run.call_args_list if c[0][0][0] == "/usr/local/bin/keyguard"]
    assert keyguard_calls == []


def test_without_timeout_does_not_cache(server):
    with patch("subprocess.run", return_value=cli_run_result(0, stdout="secret123")):
        http_get(server, "/MY_TOKEN")

    with patch("subprocess.run", return_value=cli_run_result(0, stdout="secret123")) as mock_run:
        http_get(server, "/MY_TOKEN")

    mock_run.assert_any_call(
        ["/usr/local/bin/keyguard", "get", "MY_TOKEN"],
        capture_output=True, text=True, errors="replace", timeout=60, input=None,
    )


def test_with_timeout_caps_at_max(server):
    with patch("subprocess.run", return_value=cli_run_result(0, stdout="secret123")) as mock_run:
        http_get(server, "/MY_TOKEN?timeout=9999")

    mock_run.assert_any_call(
        ["/usr/local/bin/keyguard", "get", "MY_TOKEN", "--cache-duration", "300"],
        capture_output=True, text=True, errors="replace", timeout=60, input=None,
    )


def test_with_timeout_zero_does_not_cache(server):
    with patch("subprocess.run", return_value=cli_run_result(0, stdout="secret123")) as mock_run:
        http_get(server, "/MY_TOKEN?timeout=0")

    mock_run.assert_any_call(
        ["/usr/local/bin/keyguard", "get", "MY_TOKEN"],
        capture_output=True, text=True, errors="replace", timeout=60, input=None,
    )


def test_with_negative_timeout_does_not_cache(server):
    with patch("subprocess.run", return_value=cli_run_result(0, stdout="val")) as mock_run:
        http_get(server, "/MY_TOKEN?timeout=-5")

    mock_run.assert_any_call(
        ["/usr/local/bin/keyguard", "get", "MY_TOKEN"],
        capture_output=True, text=True, errors="replace", timeout=60, input=None,
    )


def test_with_invalid_timeout_does_not_cache(server):
    with patch("subprocess.run", return_value=cli_run_result(0, stdout="val")) as mock_run:
        http_get(server, "/MY_TOKEN?timeout=abc")

    mock_run.assert_any_call(
        ["/usr/local/bin/keyguard", "get", "MY_TOKEN"],
        capture_output=True, text=True, errors="replace", timeout=60, input=None,
    )


# ---- Multi-key with timeout ----


def test_multi_key_caches_individually(server):
    with patch("subprocess.run", return_value=cli_run_result(0, stdout="A=1\nB=2\n")):
        http_get(server, "/A,B?timeout=60")

    with patch("subprocess.run") as mock_run:
        status, body = http_get(server, "/A?timeout=60")

    assert status == 200
    assert body == "1"
    time.sleep(0.2)
    keyguard_calls = [c for c in mock_run.call_args_list if c[0][0][0] == "/usr/local/bin/keyguard"]
    assert keyguard_calls == []


def test_partial_cache_fetches_only_missing_keys(server):
    cache.put("127.0.0.1", "A", "1", 60)

    with patch("subprocess.run", return_value=cli_run_result(0, stdout="secret2")) as mock_run:
        status, body = http_get(server, "/A,B?timeout=60")

    assert status == 200
    assert "A=1" in body
    assert "B=secret2" in body
    mock_run.assert_any_call(
        ["/usr/local/bin/keyguard", "get", "B", "--cache-duration", "60"],
        capture_output=True, text=True, errors="replace", timeout=60, input=None,
    )


def test_cache_from_different_ip_not_shared_by_default(server):
    cache.put("172.17.0.5", "TOKEN", "other-secret", 60)

    with patch("subprocess.run", return_value=cli_run_result(0, stdout="my-secret")) as mock_run:
        status, body = http_get(server, "/TOKEN?timeout=60")

    assert status == 200
    assert body == "my-secret"
    mock_run.assert_any_call(
        ["/usr/local/bin/keyguard", "get", "TOKEN", "--cache-duration", "60"],
        capture_output=True, text=True, errors="replace", timeout=60, input=None,
    )


# ---- Errors with timeout ----


def test_with_timeout_touch_id_cancelled_returns_403(server):
    with patch("subprocess.run", return_value=cli_run_result(2)):
        status, body = http_get(server, "/MY_TOKEN?timeout=30")

    assert status == 403
    assert "Touch ID" in body


def test_with_timeout_keyguard_error_returns_500(server):
    with patch("subprocess.run", return_value=cli_run_result(1, stderr="decrypt failed")):
        status, body = http_get(server, "/MY_TOKEN?timeout=30")

    assert status == 500
    assert "decrypt failed" in body


def test_with_timeout_subprocess_timeout_returns_500(server):
    with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="keyguard", timeout=60)):
        status, _ = http_get(server, "/MY_TOKEN?timeout=30")

    assert status == 500


# ---- _keys list caching ----


def test_keys_list_with_timeout_passes_cache_duration(server):
    with patch("subprocess.run", return_value=cli_run_result(0, stdout="FOO\nBAR\n")) as mock_run:
        status, body = http_get(server, "/_keys?timeout=60")

    assert status == 200
    assert "FOO" in body
    mock_run.assert_called_once_with(
        ["/usr/local/bin/keyguard", "list", "--cache-duration", "60"],
        capture_output=True, text=True, errors="replace", timeout=60, input=None,
    )


def test_keys_list_with_timeout_serves_from_cache(server):
    with patch("subprocess.run", return_value=cli_run_result(0, stdout="FOO\nBAR\n")):
        status1, body1 = http_get(server, "/_keys?timeout=30")

    with patch("subprocess.run") as mock_run:
        status2, body2 = http_get(server, "/_keys?timeout=30")

    assert status1 == 200 and status2 == 200
    assert body1 == body2
    keyguard_calls = [c for c in mock_run.call_args_list if c[0][0][0] == "/usr/local/bin/keyguard"]
    assert keyguard_calls == []


def test_keys_list_without_timeout_does_not_cache(server):
    with patch("subprocess.run", return_value=cli_run_result(0, stdout="FOO\n")):
        http_get(server, "/_keys")

    with patch("subprocess.run", return_value=cli_run_result(0, stdout="FOO\n")) as mock_run:
        http_get(server, "/_keys")

    mock_run.assert_called_once_with(
        ["/usr/local/bin/keyguard", "list"],
        capture_output=True, text=True, errors="replace", timeout=60, input=None,
    )


# ---- Cache isolation across operations ----


def test_cached_token_does_not_serve_list(server):
    cache.put("127.0.0.1", "TOKEN_A", "secret-a", 60)

    with patch("subprocess.run", return_value=cli_run_result(0, stdout="TOKEN_A\nTOKEN_B\n")) as mock_run:
        status, _ = http_get(server, "/_keys?timeout=60")

    assert status == 200
    keyguard_calls = [c for c in mock_run.call_args_list if c[0][0][0] == "/usr/local/bin/keyguard"]
    assert len(keyguard_calls) == 1
    assert keyguard_calls[0][0][0] == ["/usr/local/bin/keyguard", "list", "--cache-duration", "60"]


def test_cached_list_does_not_serve_token_read(server):
    cache.put("127.0.0.1", "_keys", "TOKEN_A\nTOKEN_B\n", 60)

    with patch("subprocess.run", return_value=cli_run_result(0, stdout="secret-a")) as mock_run:
        status, body = http_get(server, "/TOKEN_A?timeout=60")

    assert status == 200
    assert body == "secret-a"
    mock_run.assert_any_call(
        ["/usr/local/bin/keyguard", "get", "TOKEN_A", "--cache-duration", "60"],
        capture_output=True, text=True, errors="replace", timeout=60, input=None,
    )


def test_cached_token_does_not_bypass_touch_id_on_set(server):
    cache.put("127.0.0.1", "TOKEN_A", "old-value", 60)

    with patch("subprocess.run", return_value=cli_run_result(0, stdout="Set 'TOKEN_A'")) as mock_run:
        status, _ = http_post(server, "/TOKEN_A", body="new-value")

    assert status == 200
    keyguard_calls = [c for c in mock_run.call_args_list if c[0][0][0] == "/usr/local/bin/keyguard"]
    assert len(keyguard_calls) == 1
    assert keyguard_calls[0][0][0] == ["/usr/local/bin/keyguard", "set", "TOKEN_A"]
    assert keyguard_calls[0][1]["input"] == "new-value"


def test_list_with_timeout_does_not_populate_per_token_cache(server):
    with patch("subprocess.run", return_value=cli_run_result(0, stdout="TOKEN_A\nTOKEN_B\n")):
        http_get(server, "/_keys?timeout=60")

    assert cache.get("127.0.0.1", "TOKEN_A") is None
    assert cache.get("127.0.0.1", "TOKEN_B") is None


def test_cached_token_a_does_not_serve_token_b(server):
    cache.put("127.0.0.1", "TOKEN_A", "secret-a", 60)

    with patch("subprocess.run", return_value=cli_run_result(0, stdout="secret-b")) as mock_run:
        status, body = http_get(server, "/TOKEN_B?timeout=60")

    assert status == 200
    assert body == "secret-b"
    mock_run.assert_any_call(
        ["/usr/local/bin/keyguard", "get", "TOKEN_B", "--cache-duration", "60"],
        capture_output=True, text=True, errors="replace", timeout=60, input=None,
    )
