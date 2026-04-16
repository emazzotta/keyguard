"""Bridge token resolution: lazy keyguard fetch, caching, rate-limit, no-auth-no-prompt."""
import http.client
from unittest.mock import patch

from conftest import BRIDGE_TOKEN, cli_run_result, http_bridge_post
from keyguard_server import bridge


def _route_keyguard(token: str = BRIDGE_TOKEN, rc: int = 0, command_stdout: str = "ok",
                    stderr: str = ""):
    """Return a side_effect that routes subprocess.run by binary."""
    def side_effect(cmd, **kwargs):
        if cmd[0] == "/usr/local/bin/keyguard":
            return cli_run_result(rc, stdout=token, stderr=stderr)
        return cli_run_result(0, stdout=command_stdout)
    return side_effect


def _keyguard_calls(mock_run):
    return [c for c in mock_run.call_args_list if c[0][0][0] == "/usr/local/bin/keyguard"]


# ---- Lazy resolution ----


def test_token_resolved_from_keyguard_on_first_request(server, lazy_token_bridge):
    with patch("subprocess.run", side_effect=_route_keyguard()) as mock_run:
        status, body = http_bridge_post(server, "echo", token=BRIDGE_TOKEN)

    assert status == 200
    assert body == "ok"
    calls = _keyguard_calls(mock_run)
    assert len(calls) == 1
    assert calls[0][0][0] == ["/usr/local/bin/keyguard", "get", "MAC_BRIDGE_TOKEN"]


def test_token_cached_after_first_resolution(server, lazy_token_bridge):
    with patch("subprocess.run", side_effect=_route_keyguard()) as mock_run:
        http_bridge_post(server, "echo", token=BRIDGE_TOKEN)
        http_bridge_post(server, "echo", token=BRIDGE_TOKEN)
        http_bridge_post(server, "echo", token=BRIDGE_TOKEN)

    assert len(_keyguard_calls(mock_run)) == 1


def test_request_token_compared_against_keyguard_value(server, lazy_token_bridge):
    """A wrong request token returns 401 even after the server token is resolved."""
    with patch("subprocess.run", side_effect=_route_keyguard()):
        status, _ = http_bridge_post(server, "echo", token="wrong-token")
    assert status == 401


# ---- Resolution failures ----


def test_touch_id_cancelled_returns_503(server, lazy_token_bridge):
    with patch("subprocess.run", side_effect=_route_keyguard(rc=2)):
        status, body = http_bridge_post(server, "echo", token=BRIDGE_TOKEN)

    assert status == 503
    assert "touch id cancelled" in body.lower()


def test_keyguard_error_returns_503(server, lazy_token_bridge):
    with patch("subprocess.run", side_effect=_route_keyguard(rc=1, stderr="key not found")):
        status, body = http_bridge_post(server, "echo", token=BRIDGE_TOKEN)

    assert status == 503
    assert "key not found" in body.lower()


def test_empty_token_value_returns_503(server, lazy_token_bridge):
    with patch("subprocess.run", side_effect=_route_keyguard(token="")):
        status, body = http_bridge_post(server, "echo", token=BRIDGE_TOKEN)

    assert status == 503
    assert "empty" in body.lower()


# ---- Touch ID prompt spam protection ----


def test_no_auth_header_does_not_trigger_keyguard(server, lazy_token_bridge):
    """Unauthenticated requests must be rejected before keyguard is invoked."""
    with patch("subprocess.run") as mock_run:
        status, _ = http_bridge_post(server, "echo", token=None)

    assert status == 401
    assert _keyguard_calls(mock_run) == []


def test_non_bearer_auth_scheme_does_not_trigger_keyguard(server, lazy_token_bridge):
    """Same protection: only well-formed Bearer headers can cost a Touch ID prompt."""
    headers = {"Connection": "close", "Authorization": "Basic dXNlcjpwYXNz"}
    conn = http.client.HTTPConnection(f"127.0.0.1:{server.server_address[1]}")
    with patch("subprocess.run") as mock_run:
        conn.request("POST", "/_bridge/echo", headers=headers)
        resp = conn.getresponse()
        resp.read()

    assert resp.status == 401
    assert _keyguard_calls(mock_run) == []


def test_failed_resolution_rate_limits_subsequent_attempts(server, lazy_token_bridge):
    """After a failed resolution, the next attempt within the cooldown is rate-limited."""
    with patch("subprocess.run", side_effect=_route_keyguard(rc=2)):
        status1, _ = http_bridge_post(server, "echo", token=BRIDGE_TOKEN)

    with patch("subprocess.run") as mock_run:
        status2, body2 = http_bridge_post(server, "echo", token=BRIDGE_TOKEN)

    assert status1 == 503
    assert status2 == 503
    assert "rate-limit" in body2.lower()
    assert _keyguard_calls(mock_run) == []


# ---- verify_token helper ----


def test_verify_token_valid(monkeypatch):
    monkeypatch.setattr(bridge, "_token", "my-token")
    assert bridge.verify_token("Bearer my-token") is True


def test_verify_token_wrong_value(monkeypatch):
    monkeypatch.setattr(bridge, "_token", "my-token")
    assert bridge.verify_token("Bearer wrong") is False


def test_verify_token_missing_bearer_prefix(monkeypatch):
    monkeypatch.setattr(bridge, "_token", "my-token")
    assert bridge.verify_token("my-token") is False


def test_verify_token_none_header(monkeypatch):
    monkeypatch.setattr(bridge, "_token", "my-token")
    assert bridge.verify_token(None) is False


def test_verify_token_empty_server_token(monkeypatch):
    monkeypatch.setattr(bridge, "_token", "")
    assert bridge.verify_token("Bearer anything") is False
