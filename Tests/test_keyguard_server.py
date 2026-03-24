import http.client
import importlib.util
import subprocess
import threading
from http.server import HTTPServer, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_spec = importlib.util.spec_from_file_location(
    "keyguard_server",
    Path(__file__).parent.parent / "src" / "keyguard-server.py",
)
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)

is_allowed = _module.is_allowed
KeyguardHandler = _module.KeyguardHandler


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), KeyguardHandler)
    thread = threading.Thread(target=srv.serve_forever)
    thread.daemon = True
    thread.start()
    yield srv
    srv.shutdown()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def http_get(srv: HTTPServer, path: str) -> tuple[int, str]:
    conn = http.client.HTTPConnection(f"127.0.0.1:{srv.server_address[1]}")
    conn.request("GET", path, headers={"Connection": "close"})
    resp = conn.getresponse()
    return resp.status, resp.read().decode()


def http_post(srv: HTTPServer, path: str, body: str = "") -> tuple[int, str]:
    conn = http.client.HTTPConnection(f"127.0.0.1:{srv.server_address[1]}")
    encoded = body.encode()
    conn.request("POST", path, body=encoded, headers={"Content-Length": str(len(encoded)), "Connection": "close"})
    resp = conn.getresponse()
    return resp.status, resp.read().decode()


def subprocess_result(returncode: int, stdout: str = "", stderr: str = "") -> MagicMock:
    r = MagicMock()
    r.returncode = returncode
    r.stdout = stdout
    r.stderr = stderr
    return r


# ---------------------------------------------------------------------------
# is_allowed — IP allowlist
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("ip,expected", [
    ("127.0.0.1", True),
    ("127.255.255.255", True),
    ("172.16.0.1", True),
    ("172.17.0.1", True),
    ("172.31.255.255", True),
    ("192.168.65.1", True),
    ("192.168.65.255", True),
    ("172.32.0.1", False),
    ("192.168.1.1", False),
    ("10.0.0.1", False),
    ("8.8.8.8", False),
    ("not-an-ip", False),
])
def test_is_allowed(ip: str, expected: bool) -> None:
    assert is_allowed(ip) == expected


# ---------------------------------------------------------------------------
# GET — single key
# ---------------------------------------------------------------------------


def test_get_single_key_returns_raw_value(server):
    with patch("subprocess.run", return_value=subprocess_result(0, stdout="secret123")) as mock_run:
        status, body = http_get(server, "/MY_TOKEN")

    assert status == 200
    assert body == "secret123"
    mock_run.assert_called_once_with(
        ["/usr/local/bin/keyguard", "get", "MY_TOKEN"],
        capture_output=True,
        text=True,
        timeout=60,
        input=None,
    )


def test_get_multiple_keys_returns_key_value_lines(server):
    with patch("subprocess.run", return_value=subprocess_result(0, stdout="A=1\nB=2\n")):
        status, body = http_get(server, "/A,B")

    assert status == 200
    assert body == "A=1\nB=2\n"


def test_get_keys_list_calls_list_command(server):
    with patch("subprocess.run", return_value=subprocess_result(0, stdout="FOO\nBAR\n")) as mock_run:
        status, body = http_get(server, "/_keys")

    assert status == 200
    assert "FOO" in body
    mock_run.assert_called_once_with(
        ["/usr/local/bin/keyguard", "list"],
        capture_output=True,
        text=True,
        timeout=60,
        input=None,
    )


def test_get_missing_path_returns_400(server):
    status, _ = http_get(server, "/")

    assert status == 400


def test_get_touch_id_cancelled_returns_403(server):
    with patch("subprocess.run", return_value=subprocess_result(2)):
        status, body = http_get(server, "/MY_TOKEN")

    assert status == 403
    assert "Touch ID" in body


def test_get_keyguard_error_returns_500(server):
    with patch("subprocess.run", return_value=subprocess_result(1, stderr="key not found")):
        status, body = http_get(server, "/MISSING_KEY")

    assert status == 500
    assert "key not found" in body


# ---------------------------------------------------------------------------
# POST — store secret
# ---------------------------------------------------------------------------


def test_post_valid_key_and_value_stores_secret(server):
    with patch("subprocess.run", return_value=subprocess_result(0, stdout="Set 'MY_TOKEN'")) as mock_run:
        status, body = http_post(server, "/MY_TOKEN", body="secret123")

    assert status == 200
    assert "MY_TOKEN" in body
    mock_run.assert_called_once_with(
        ["/usr/local/bin/keyguard", "set", "MY_TOKEN"],
        capture_output=True,
        text=True,
        timeout=60,
        input="secret123",
    )


def test_post_strips_trailing_newline_from_body(server):
    with patch("subprocess.run", return_value=subprocess_result(0, stdout="Set 'TOKEN'")) as mock_run:
        http_post(server, "/TOKEN", body="value\n")

    assert mock_run.call_args[0][0] == ["/usr/local/bin/keyguard", "set", "TOKEN"]
    assert mock_run.call_args[1]["input"] == "value"


def test_post_missing_path_returns_400(server):
    status, _ = http_post(server, "/", body="value")

    assert status == 400


def test_post_comma_in_name_returns_400(server):
    status, _ = http_post(server, "/KEY1,KEY2", body="value")

    assert status == 400


def test_post_reserved_keys_name_returns_400(server):
    status, _ = http_post(server, "/_keys", body="value")

    assert status == 400


def test_post_empty_body_returns_400(server):
    status, _ = http_post(server, "/MY_TOKEN", body="")

    assert status == 400


def test_post_whitespace_only_body_returns_400(server):
    status, _ = http_post(server, "/MY_TOKEN", body="   \n  ")

    assert status == 400


def test_post_touch_id_cancelled_returns_403(server):
    with patch("subprocess.run", return_value=subprocess_result(2)):
        status, body = http_post(server, "/MY_TOKEN", body="value")

    assert status == 403
    assert "Touch ID" in body


def test_post_keyguard_error_returns_500(server):
    with patch("subprocess.run", return_value=subprocess_result(1, stderr="encryption failed")):
        status, body = http_post(server, "/MY_TOKEN", body="value")

    assert status == 500
    assert "encryption failed" in body


def test_post_oversized_body_returns_400(server):
    conn = http.client.HTTPConnection(f"127.0.0.1:{server.server_address[1]}")
    conn.request("POST", "/MY_TOKEN", body=b"x", headers={"Content-Length": "99999999"})
    resp = conn.getresponse()

    assert resp.status == 400


def test_post_invalid_content_length_returns_400(server):
    conn = http.client.HTTPConnection(f"127.0.0.1:{server.server_address[1]}")
    conn.request("POST", "/MY_TOKEN", body=b"value", headers={"Content-Length": "not-a-number"})
    resp = conn.getresponse()

    assert resp.status == 400


def test_post_non_utf8_body_returns_400(server):
    conn = http.client.HTTPConnection(f"127.0.0.1:{server.server_address[1]}")
    body = b"\xff\xfe"
    conn.request("POST", "/MY_TOKEN", body=body, headers={"Content-Length": str(len(body))})
    resp = conn.getresponse()

    assert resp.status == 400


def test_get_subprocess_timeout_returns_500(server):
    with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="keyguard", timeout=60)):
        status, _ = http_get(server, "/MY_TOKEN")

    assert status == 500
