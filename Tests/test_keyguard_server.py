import http.client
import importlib.util
import subprocess
import threading
import time
from http.server import HTTPServer, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

_spec = importlib.util.spec_from_file_location(
    "keyguard_server",
    Path(__file__).parent.parent / "src" / "keyguard-server.py",
)
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)

is_allowed = _module.is_allowed
KeyguardHandler = _module.KeyguardHandler
_cache = _module._cache
_cache_lock = _module._cache_lock
_cache_clear = _module._cache_clear
_cache_get = _module._cache_get
_cache_put = _module._cache_put
_cache_get_shared = _module._cache_get_shared
_parse_timeout = _module._parse_timeout
_parse_share = _module._parse_share
_format_response = _module._format_response
_resolve_source = _module._resolve_source
_resolve_hostname = _module._resolve_hostname
_resolve_container_name = _module._resolve_container_name
_resolve_container_by_hint = _module._resolve_container_by_hint
_escape_osascript = _module._escape_osascript


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


@pytest.fixture(autouse=True)
def clear_cache():
    _cache_clear()
    yield
    _cache_clear()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def http_get(srv: HTTPServer, path: str, extra_headers: dict[str, str] | None = None) -> tuple[int, str]:
    conn = http.client.HTTPConnection(f"127.0.0.1:{srv.server_address[1]}")
    headers = {"Connection": "close"}
    if extra_headers:
        headers.update(extra_headers)
    conn.request("GET", path, headers=headers)
    resp = conn.getresponse()
    return resp.status, resp.read().decode()


def http_post(srv: HTTPServer, path: str, body: str = "") -> tuple[int, str]:
    conn = http.client.HTTPConnection(f"127.0.0.1:{srv.server_address[1]}")
    encoded = body.encode()
    conn.request("POST", path, body=encoded, headers={"Content-Length": str(len(encoded)), "Connection": "close"})
    resp = conn.getresponse()
    return resp.status, resp.read().decode()


def http_delete(srv: HTTPServer, path: str) -> tuple[int, str]:
    conn = http.client.HTTPConnection(f"127.0.0.1:{srv.server_address[1]}")
    conn.request("DELETE", path, headers={"Connection": "close"})
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
    mock_run.assert_any_call(
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


def test_get_keys_list_with_timeout_passes_cache_duration(server):
    with patch("subprocess.run", return_value=subprocess_result(0, stdout="FOO\nBAR\n")) as mock_run:
        status, body = http_get(server, "/_keys?timeout=60")

    assert status == 200
    assert "FOO" in body
    mock_run.assert_called_once_with(
        ["/usr/local/bin/keyguard", "list", "--cache-duration", "60"],
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_get_keys_list_with_timeout_serves_from_cache(server):
    with patch("subprocess.run", return_value=subprocess_result(0, stdout="FOO\nBAR\n")):
        status1, body1 = http_get(server, "/_keys?timeout=30")

    with patch("subprocess.run") as mock_run:
        status2, body2 = http_get(server, "/_keys?timeout=30")

    assert status1 == 200
    assert status2 == 200
    assert body1 == body2
    keyguard_calls = [
        c for c in mock_run.call_args_list
        if c[0][0][0] == "/usr/local/bin/keyguard"
    ]
    assert len(keyguard_calls) == 0


def test_get_keys_list_without_timeout_does_not_cache(server):
    with patch("subprocess.run", return_value=subprocess_result(0, stdout="FOO\n")):
        http_get(server, "/_keys")

    with patch("subprocess.run", return_value=subprocess_result(0, stdout="FOO\n")) as mock_run:
        http_get(server, "/_keys")

    mock_run.assert_called_once_with(
        ["/usr/local/bin/keyguard", "list"],
        capture_output=True,
        text=True,
        timeout=60,
        input=None,
    )


def test_get_keys_list_touch_id_cancelled_returns_403(server):
    with patch("subprocess.run", return_value=subprocess_result(2)):
        status, body = http_get(server, "/_keys?timeout=30")

    assert status == 403
    assert "Touch ID" in body


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


def test_get_missing_encryption_key_returns_500_with_clear_message(server):
    with patch("subprocess.run", return_value=subprocess_result(1, stderr="No encryption key found in Keychain")):
        status, body = http_get(server, "/MY_TOKEN")

    assert status == 500
    assert "encryption key" in body


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


def test_post_missing_encryption_key_returns_500_with_clear_message(server):
    with patch("subprocess.run", return_value=subprocess_result(1, stderr="No encryption key found in Keychain")):
        status, body = http_post(server, "/MY_TOKEN", body="value")

    assert status == 500
    assert "encryption key" in body


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


# ---------------------------------------------------------------------------
# GET with ?timeout — caching
# ---------------------------------------------------------------------------


def test_get_with_timeout_passes_cache_duration_to_keyguard(server):
    with patch("subprocess.run", return_value=subprocess_result(0, stdout="secret123")) as mock_run:
        status, body = http_get(server, "/MY_TOKEN?timeout=30")

    assert status == 200
    assert body == "secret123"
    mock_run.assert_any_call(
        ["/usr/local/bin/keyguard", "get", "MY_TOKEN", "--cache-duration", "30"],
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_get_with_timeout_serves_from_cache_on_second_request(server):
    with patch("subprocess.run", return_value=subprocess_result(0, stdout="secret123")):
        status1, body1 = http_get(server, "/MY_TOKEN?timeout=30")

    with patch("subprocess.run") as mock_run:
        status2, body2 = http_get(server, "/MY_TOKEN?timeout=30")

    assert status1 == 200
    assert body1 == "secret123"
    assert status2 == 200
    assert body2 == "secret123"
    time.sleep(0.2)
    keyguard_calls = [
        c for c in mock_run.call_args_list
        if c[0][0][0] == "/usr/local/bin/keyguard"
    ]
    assert len(keyguard_calls) == 0


def test_get_without_timeout_does_not_cache(server):
    with patch("subprocess.run", return_value=subprocess_result(0, stdout="secret123")):
        http_get(server, "/MY_TOKEN")

    with patch("subprocess.run", return_value=subprocess_result(0, stdout="secret123")) as mock_run:
        http_get(server, "/MY_TOKEN")

    mock_run.assert_any_call(
        ["/usr/local/bin/keyguard", "get", "MY_TOKEN"],
        capture_output=True,
        text=True,
        timeout=60,
        input=None,
    )


def test_get_with_timeout_caps_at_max(server):
    with patch("subprocess.run", return_value=subprocess_result(0, stdout="secret123")) as mock_run:
        http_get(server, "/MY_TOKEN?timeout=9999")

    mock_run.assert_any_call(
        ["/usr/local/bin/keyguard", "get", "MY_TOKEN", "--cache-duration", "300"],
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_get_with_timeout_zero_does_not_cache(server):
    with patch("subprocess.run", return_value=subprocess_result(0, stdout="secret123")):
        http_get(server, "/MY_TOKEN?timeout=0")

    with patch("subprocess.run", return_value=subprocess_result(0, stdout="secret123")) as mock_run:
        http_get(server, "/MY_TOKEN?timeout=0")

    mock_run.assert_any_call(
        ["/usr/local/bin/keyguard", "get", "MY_TOKEN"],
        capture_output=True,
        text=True,
        timeout=60,
        input=None,
    )


def test_get_with_negative_timeout_does_not_cache(server):
    with patch("subprocess.run", return_value=subprocess_result(0, stdout="val")) as mock_run:
        http_get(server, "/MY_TOKEN?timeout=-5")

    mock_run.assert_any_call(
        ["/usr/local/bin/keyguard", "get", "MY_TOKEN"],
        capture_output=True,
        text=True,
        timeout=60,
        input=None,
    )


def test_get_with_invalid_timeout_does_not_cache(server):
    with patch("subprocess.run", return_value=subprocess_result(0, stdout="val")) as mock_run:
        http_get(server, "/MY_TOKEN?timeout=abc")

    mock_run.assert_any_call(
        ["/usr/local/bin/keyguard", "get", "MY_TOKEN"],
        capture_output=True,
        text=True,
        timeout=60,
        input=None,
    )


def test_get_multi_key_with_timeout_caches_individually(server):
    with patch("subprocess.run", return_value=subprocess_result(0, stdout="A=1\nB=2\n")):
        status1, body1 = http_get(server, "/A,B?timeout=60")

    assert status1 == 200
    assert "A=1" in body1
    assert "B=2" in body1

    with patch("subprocess.run") as mock_run:
        status2, body2 = http_get(server, "/A?timeout=60")

    assert status2 == 200
    assert body2 == "1"
    time.sleep(0.2)
    keyguard_calls = [
        c for c in mock_run.call_args_list
        if c[0][0][0] == "/usr/local/bin/keyguard"
    ]
    assert len(keyguard_calls) == 0


def test_get_with_timeout_touch_id_cancelled_returns_403(server):
    with patch("subprocess.run", return_value=subprocess_result(2)):
        status, body = http_get(server, "/MY_TOKEN?timeout=30")

    assert status == 403
    assert "Touch ID" in body


def test_get_with_timeout_keyguard_error_returns_500(server):
    with patch("subprocess.run", return_value=subprocess_result(1, stderr="decrypt failed")):
        status, body = http_get(server, "/MY_TOKEN?timeout=30")

    assert status == 500
    assert "decrypt failed" in body


def test_get_with_timeout_subprocess_timeout_returns_500(server):
    with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="keyguard", timeout=60)):
        status, _ = http_get(server, "/MY_TOKEN?timeout=30")

    assert status == 500


def test_get_partial_cache_fetches_only_missing_keys(server):
    _cache_put("127.0.0.1", "A", "1", 60)

    with patch("subprocess.run", return_value=subprocess_result(0, stdout="secret2")) as mock_run:
        status, body = http_get(server, "/A,B?timeout=60")

    assert status == 200
    assert "A=1" in body
    assert "B=secret2" in body
    mock_run.assert_any_call(
        ["/usr/local/bin/keyguard", "get", "B", "--cache-duration", "60"],
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_cache_from_different_ip_not_shared_by_default(server):
    _cache_put("172.17.0.5", "TOKEN", "other-secret", 60)

    with patch("subprocess.run", return_value=subprocess_result(0, stdout="my-secret")) as mock_run:
        status, body = http_get(server, "/TOKEN?timeout=60")

    assert status == 200
    assert body == "my-secret"
    mock_run.assert_any_call(
        ["/usr/local/bin/keyguard", "get", "TOKEN", "--cache-duration", "60"],
        capture_output=True,
        text=True,
        timeout=60,
    )


# ---------------------------------------------------------------------------
# DELETE /_cache
# ---------------------------------------------------------------------------


def test_delete_cache_clears_all(server):
    _cache_put("127.0.0.1", "TOKEN", "val", 60)

    status, body = http_delete(server, "/_cache")

    assert status == 200
    assert body == "Cache cleared"
    assert _cache_get("127.0.0.1", "TOKEN") is None


def test_delete_unknown_endpoint_returns_400(server):
    status, body = http_delete(server, "/unknown")

    assert status == 400


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------


def test_get_sends_notification_async(server):
    with patch("subprocess.run", return_value=subprocess_result(0, stdout="secret123")) as mock_run:
        status, _ = http_get(server, "/MY_TOKEN")

    assert status == 200
    time.sleep(0.2)

    notification_calls = [
        c for c in mock_run.call_args_list
        if c[0][0][0] == "osascript"
    ]
    assert len(notification_calls) == 1
    osascript_cmd = notification_calls[0][0][0]
    assert "MY_TOKEN" in osascript_cmd[2]
    assert "keyguard" in osascript_cmd[2]


def test_cached_get_sends_notification_with_cached_hint(server):
    with patch("subprocess.run", return_value=subprocess_result(0, stdout="secret123")):
        http_get(server, "/MY_TOKEN?timeout=60")

    with patch("subprocess.run") as mock_run:
        http_get(server, "/MY_TOKEN?timeout=60")

    time.sleep(0.2)
    notification_calls = [
        c for c in mock_run.call_args_list
        if len(c[0]) > 0 and isinstance(c[0][0], list) and len(c[0][0]) > 0 and c[0][0][0] == "osascript"
    ]
    assert len(notification_calls) == 1
    osascript_cmd = notification_calls[0][0][0]
    assert "(cached)" in osascript_cmd[2]


def test_get_with_source_header_includes_hint_in_notification(server):
    inspect_result = MagicMock()
    inspect_result.returncode = 0
    inspect_result.stdout = "/my-container\n"

    def side_effect(*args, **kwargs):
        cmd = args[0] if args else kwargs.get("args", [])
        if cmd[0] == "/usr/local/bin/keyguard":
            return subprocess_result(0, stdout="secret123")
        if cmd[0] == "docker":
            return inspect_result
        return subprocess_result(0)

    with patch("subprocess.run", side_effect=side_effect) as mock_run:
        status, _ = http_get(server, "/MY_TOKEN",
                             extra_headers={"X-Keyguard-Source": "abc123"})

    assert status == 200
    time.sleep(0.3)

    notification_calls = [
        c for c in mock_run.call_args_list
        if c[0][0][0] == "osascript"
    ]
    assert len(notification_calls) == 1
    assert "my-container" in notification_calls[0][0][0][2]


def test_list_does_not_send_notification(server):
    with patch("subprocess.run", return_value=subprocess_result(0, stdout="KEY1\nKEY2\n")) as mock_run:
        http_get(server, "/_keys")

    time.sleep(0.2)
    notification_calls = [
        c for c in mock_run.call_args_list
        if len(c[0]) > 0 and isinstance(c[0][0], list) and len(c[0][0]) > 0 and c[0][0][0] == "osascript"
    ]
    assert len(notification_calls) == 0


def test_post_does_not_send_notification(server):
    with patch("subprocess.run", return_value=subprocess_result(0, stdout="Set 'TOKEN'")) as mock_run:
        http_post(server, "/TOKEN", body="value")

    time.sleep(0.2)
    notification_calls = [
        c for c in mock_run.call_args_list
        if len(c[0]) > 0 and isinstance(c[0][0], list) and len(c[0][0]) > 0 and c[0][0][0] == "osascript"
    ]
    assert len(notification_calls) == 0


# ---------------------------------------------------------------------------
# Unit tests — helper functions
# ---------------------------------------------------------------------------


def test_parse_timeout_valid():
    assert _parse_timeout({"timeout": ["30"]}) == 30


def test_parse_timeout_caps_at_max():
    assert _parse_timeout({"timeout": ["9999"]}) == 300


def test_parse_timeout_zero_returns_none():
    assert _parse_timeout({"timeout": ["0"]}) is None


def test_parse_timeout_negative_returns_none():
    assert _parse_timeout({"timeout": ["-1"]}) is None


def test_parse_timeout_missing_returns_none():
    assert _parse_timeout({}) is None


def test_parse_timeout_invalid_returns_none():
    assert _parse_timeout({"timeout": ["abc"]}) is None


def test_format_response_single_key():
    assert _format_response(["TOKEN"], {"TOKEN": "secret"}) == "secret"


def test_format_response_multiple_keys():
    result = _format_response(["A", "B"], {"A": "1", "B": "2"})
    assert result == "A=1\nB=2\n"


def test_cache_put_and_get():
    _cache_put("10.0.0.1", "K", "V", 10)
    assert _cache_get("10.0.0.1", "K") == "V"


def test_cache_scoped_to_ip():
    _cache_put("10.0.0.1", "K", "V", 10)
    assert _cache_get("10.0.0.2", "K") is None


def test_cache_expired_returns_none():
    _cache_put("10.0.0.1", "K", "V", 0)
    time.sleep(0.01)
    assert _cache_get("10.0.0.1", "K") is None


def test_cache_clear_removes_all():
    _cache_put("10.0.0.1", "A", "1", 60)
    _cache_put("10.0.0.2", "B", "2", 60)
    _cache_clear()
    assert _cache_get("10.0.0.1", "A") is None
    assert _cache_get("10.0.0.2", "B") is None


def test_cache_get_shared_with_all():
    _cache_put("10.0.0.1", "K", "V", 10)
    assert _cache_get_shared(["*"], "K") == "V"


def test_cache_get_shared_with_specific_ip():
    _cache_put("10.0.0.1", "K", "V", 10)
    assert _cache_get_shared(["10.0.0.1", "10.0.0.2"], "K") == "V"
    assert _cache_get_shared(["10.0.0.3"], "K") is None


def test_parse_share_defaults_to_client_ip():
    assert _parse_share({}, "10.0.0.1") == ["10.0.0.1"]


def test_parse_share_all():
    assert _parse_share({"share": ["all"]}, "10.0.0.1") == ["*"]


def test_parse_share_explicit_ips_includes_client():
    result = _parse_share({"share": ["10.0.0.2,10.0.0.3"]}, "10.0.0.1")
    assert "10.0.0.1" in result
    assert "10.0.0.2" in result
    assert "10.0.0.3" in result


def test_parse_share_does_not_duplicate_client():
    result = _parse_share({"share": ["10.0.0.1,10.0.0.2"]}, "10.0.0.1")
    assert result.count("10.0.0.1") == 1


def test_resolve_source_localhost():
    assert _resolve_source("127.0.0.1") == "localhost"


def test_resolve_source_localhost_other_loopback():
    assert _resolve_source("127.0.0.5") == "localhost"


def test_resolve_source_with_hint_resolves_container():
    inspect_result = MagicMock()
    inspect_result.returncode = 0
    inspect_result.stdout = "/my-app\n"

    with patch("subprocess.run", return_value=inspect_result):
        result = _resolve_source("127.0.0.1", source_hint="abc123")

    assert result == "my-app"


def test_resolve_source_with_hint_unresolvable_uses_raw_hint():
    inspect_result = MagicMock()
    inspect_result.returncode = 1
    inspect_result.stdout = ""

    with patch("subprocess.run", return_value=inspect_result):
        result = _resolve_source("127.0.0.1", source_hint="my-hostname")

    assert result == "my-hostname"


def test_resolve_source_with_hint_non_localhost_includes_ip():
    inspect_result = MagicMock()
    inspect_result.returncode = 0
    inspect_result.stdout = "/my-app\n"

    with patch("subprocess.run", return_value=inspect_result):
        result = _resolve_source("172.17.0.2", source_hint="abc123")

    assert result == "172.17.0.2 (my-app)"


def test_resolve_source_docker_ip_without_docker():
    with patch("subprocess.run", side_effect=FileNotFoundError), \
         patch("socket.gethostbyaddr", side_effect=OSError):
        result = _resolve_source("172.17.0.2")
    assert result == "172.17.0.2"


def test_resolve_source_docker_ip_with_match():
    docker_ps = MagicMock()
    docker_ps.returncode = 0
    docker_ps.stdout = "abc123\n"

    docker_inspect = MagicMock()
    docker_inspect.returncode = 0
    docker_inspect.stdout = "/my-container 172.17.0.2 \n"

    with patch("subprocess.run", side_effect=[docker_ps, docker_inspect]), \
         patch("socket.gethostbyaddr", side_effect=OSError):
        result = _resolve_source("172.17.0.2")

    assert result == "172.17.0.2 (my-container)"


def test_resolve_source_with_hostname_only():
    with patch("socket.gethostbyaddr", return_value=("macbook.local", [], [])), \
         patch("subprocess.run", side_effect=FileNotFoundError):
        result = _resolve_source("192.168.1.50")

    assert result == "192.168.1.50 (macbook.local)"


def test_resolve_source_with_hostname_and_container():
    docker_ps = MagicMock()
    docker_ps.returncode = 0
    docker_ps.stdout = "abc123\n"

    docker_inspect = MagicMock()
    docker_inspect.returncode = 0
    docker_inspect.stdout = "/my-app 172.17.0.2 \n"

    with patch("socket.gethostbyaddr", return_value=("some-host", [], [])), \
         patch("subprocess.run", side_effect=[docker_ps, docker_inspect]):
        result = _resolve_source("172.17.0.2")

    assert result == "172.17.0.2 (some-host, my-app)"


def test_resolve_source_deduplicates_hostname_and_container():
    docker_ps = MagicMock()
    docker_ps.returncode = 0
    docker_ps.stdout = "abc123\n"

    docker_inspect = MagicMock()
    docker_inspect.returncode = 0
    docker_inspect.stdout = "/my-app 172.17.0.2 \n"

    with patch("socket.gethostbyaddr", return_value=("my-app", [], [])), \
         patch("subprocess.run", side_effect=[docker_ps, docker_inspect]):
        result = _resolve_source("172.17.0.2")

    assert result == "172.17.0.2 (my-app)"


def test_resolve_hostname_returns_none_on_failure():
    with patch("socket.gethostbyaddr", side_effect=OSError):
        assert _resolve_hostname("10.0.0.5") is None


def test_resolve_hostname_ignores_ip_echo():
    with patch("socket.gethostbyaddr", return_value=("10.0.0.5", [], [])):
        assert _resolve_hostname("10.0.0.5") is None


def test_escape_osascript_handles_quotes():
    assert _escape_osascript('key "test"') == 'key \\"test\\"'


def test_escape_osascript_handles_backslash():
    assert _escape_osascript("path\\file") == "path\\\\file"
