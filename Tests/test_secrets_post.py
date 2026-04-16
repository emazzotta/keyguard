"""Tests for POST /<key> (set a secret) and DELETE /_cache."""
import http.client
from unittest.mock import patch

from conftest import cli_run_result, http_delete, http_post
from keyguard_server import cache


def test_valid_key_and_value_stores_secret(server):
    with patch("subprocess.run", return_value=cli_run_result(0, stdout="Set 'MY_TOKEN'")) as mock_run:
        status, body = http_post(server, "/MY_TOKEN", body="secret123")

    assert status == 200
    assert "MY_TOKEN" in body
    mock_run.assert_called_once_with(
        ["/usr/local/bin/keyguard", "set", "MY_TOKEN"],
        capture_output=True, text=True, errors="replace", timeout=60, input="secret123",
    )


def test_strips_trailing_newline_from_body(server):
    with patch("subprocess.run", return_value=cli_run_result(0, stdout="Set 'TOKEN'")) as mock_run:
        http_post(server, "/TOKEN", body="value\n")

    assert mock_run.call_args[0][0] == ["/usr/local/bin/keyguard", "set", "TOKEN"]
    assert mock_run.call_args[1]["input"] == "value"


def test_missing_path_returns_400(server):
    status, _ = http_post(server, "/", body="value")
    assert status == 400


def test_comma_in_name_returns_400(server):
    status, _ = http_post(server, "/KEY1,KEY2", body="value")
    assert status == 400


def test_reserved_keys_name_returns_400(server):
    status, _ = http_post(server, "/_keys", body="value")
    assert status == 400


def test_empty_body_returns_400(server):
    status, _ = http_post(server, "/MY_TOKEN", body="")
    assert status == 400


def test_whitespace_only_body_returns_400(server):
    status, _ = http_post(server, "/MY_TOKEN", body="   \n  ")
    assert status == 400


def test_touch_id_cancelled_returns_403(server):
    with patch("subprocess.run", return_value=cli_run_result(2)):
        status, body = http_post(server, "/MY_TOKEN", body="value")

    assert status == 403
    assert "Touch ID" in body


def test_keyguard_error_returns_500(server):
    with patch("subprocess.run", return_value=cli_run_result(1, stderr="encryption failed")):
        status, body = http_post(server, "/MY_TOKEN", body="value")

    assert status == 500
    assert "encryption failed" in body


def test_missing_encryption_key_returns_500_with_clear_message(server):
    with patch("subprocess.run", return_value=cli_run_result(1, stderr="No encryption key found in Keychain")):
        status, body = http_post(server, "/MY_TOKEN", body="value")

    assert status == 500
    assert "encryption key" in body


def test_oversized_body_returns_400(server):
    conn = http.client.HTTPConnection(f"127.0.0.1:{server.server_address[1]}")
    conn.request("POST", "/MY_TOKEN", body=b"x", headers={"Content-Length": "99999999"})
    assert conn.getresponse().status == 400


def test_invalid_content_length_returns_400(server):
    conn = http.client.HTTPConnection(f"127.0.0.1:{server.server_address[1]}")
    conn.request("POST", "/MY_TOKEN", body=b"value", headers={"Content-Length": "not-a-number"})
    assert conn.getresponse().status == 400


def test_non_utf8_body_returns_400(server):
    conn = http.client.HTTPConnection(f"127.0.0.1:{server.server_address[1]}")
    body = b"\xff\xfe"
    conn.request("POST", "/MY_TOKEN", body=body, headers={"Content-Length": str(len(body))})
    assert conn.getresponse().status == 400


# ---- DELETE /_cache ----


def test_delete_cache_clears_all(server):
    cache.put("127.0.0.1", "TOKEN", "val", 60)

    status, body = http_delete(server, "/_cache")

    assert status == 200
    assert body == "Cache cleared"
    assert cache.get("127.0.0.1", "TOKEN") is None


def test_delete_unknown_endpoint_returns_400(server):
    status, _ = http_delete(server, "/unknown")
    assert status == 400
