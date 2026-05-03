"""Public bridge endpoints - `public: true` in YAML disables auth for that endpoint only.

Default posture is hardcore protected; public is the exception. These tests
guarantee:
  - public endpoints succeed without (or with garbage) Authorization
  - public endpoints never invoke keyguard or burn a Touch ID
  - protected endpoints are unaffected by the presence of public siblings
  - method whitelist, stdin, notifications, command execution still apply
"""
from __future__ import annotations

import time
from unittest.mock import patch

from conftest import (
    BRIDGE_TOKEN,
    cli_run_result,
    http_bridge_get,
    http_bridge_post,
)


def _bridge_calls(mock_run):
    return [c for c in mock_run.call_args_list if c[0][0][0] != "osascript"]


def _keyguard_calls(mock_run):
    return [c for c in mock_run.call_args_list if c[0][0][0] == "/usr/local/bin/keyguard"]


# ---- Auth bypass ----


def test_public_endpoint_succeeds_without_auth_header(server, mixed_bridge):
    with patch("subprocess.run", return_value=cli_run_result(0, stdout="public\n")):
        status, body = http_bridge_post(server, "public-echo", token=None)

    assert status == 200
    assert body == "public\n"


def test_public_endpoint_succeeds_with_garbage_auth(server, mixed_bridge):
    with patch("subprocess.run", return_value=cli_run_result(0, stdout="public\n")):
        status, _ = http_bridge_post(server, "public-echo", token="not-the-real-token")

    assert status == 200


def test_public_endpoint_succeeds_with_valid_auth(server, mixed_bridge):
    """A valid token on a public endpoint is accepted and ignored - no regression for existing callers."""
    with patch("subprocess.run", return_value=cli_run_result(0, stdout="public\n")):
        status, _ = http_bridge_post(server, "public-echo", token=BRIDGE_TOKEN)

    assert status == 200


def test_public_endpoint_with_unresolved_token_does_not_burn_touch_id(server, lazy_token_bridge):
    """Public endpoints must short-circuit before keyguard is ever invoked,
    even when the bridge token has never been resolved.
    """
    def side_effect(cmd, **kwargs):
        if cmd[0] == "/usr/local/bin/keyguard":
            raise AssertionError("keyguard must not be invoked for public endpoints")
        return cli_run_result(0, stdout="public\n")

    with patch("subprocess.run", side_effect=side_effect) as mock_run:
        status, body = http_bridge_post(server, "public-echo", token=None)

    assert status == 200
    assert body == "public\n"
    assert _keyguard_calls(mock_run) == []


# ---- Default-protected stays protected ----


def test_protected_endpoint_still_returns_401_without_auth(server, mixed_bridge):
    status, _ = http_bridge_post(server, "private-echo", token=None)
    assert status == 401


def test_protected_endpoint_still_rejects_wrong_token(server, mixed_bridge):
    status, _ = http_bridge_post(server, "private-echo", token="wrong")
    assert status == 401


def test_protected_endpoint_still_succeeds_with_correct_token(server, mixed_bridge):
    with patch("subprocess.run", return_value=cli_run_result(0, stdout="private\n")):
        status, body = http_bridge_post(server, "private-echo", token=BRIDGE_TOKEN)

    assert status == 200
    assert body == "private\n"


# ---- Method whitelist still applies to public endpoints ----


def test_public_endpoint_method_whitelist_rejects_wrong_method(server, mixed_bridge):
    """`public-status` is GET-only - POSTing it must still 405 even without auth."""
    status, _ = http_bridge_post(server, "public-status", token=None)
    assert status == 405


def test_public_endpoint_get_succeeds(server, mixed_bridge):
    with patch("subprocess.run", return_value=cli_run_result(0, stdout="ok")):
        status, body = http_bridge_get(server, "public-status", token=None)

    assert status == 200
    assert body == "ok"


# ---- Public endpoints honour stdin ----


def test_public_endpoint_passes_stdin(server, mixed_bridge):
    with patch("subprocess.run", return_value=cli_run_result(0, stdout="hi")) as mock_run:
        http_bridge_post(server, "public-stdin", body="hi", token=None)

    time.sleep(0.2)
    bridge_calls = _bridge_calls(mock_run)
    assert len(bridge_calls) == 1
    assert bridge_calls[0][1]["input"] == "hi"


# ---- Notifications still fire ----


def test_public_endpoint_success_sends_notification(server, mixed_bridge):
    with patch("subprocess.run", return_value=cli_run_result(0, stdout="public\n")) as mock_run:
        http_bridge_post(server, "public-echo", token=None)

    time.sleep(0.2)
    osa = [c for c in mock_run.call_args_list if c[0][0][0] == "osascript"]
    assert len(osa) == 1
    assert "bridge:public-echo" in osa[0][0][0][2]


# ---- Listing is privilege-aware: anonymous sees only public ----


def test_list_anonymous_returns_only_public_endpoints(server, mixed_bridge):
    """Without a bearer header, the listing reveals only public endpoints.
    Protected endpoint names must not leak to anonymous callers.
    """
    status, body = http_bridge_get(server, "list", token=None)
    assert status == 200
    names = {e["name"] for e in __import__("json").loads(body)}
    assert names == {"public-echo", "public-status", "public-stdin"}


def test_list_anonymous_entries_are_marked_public(server, mixed_bridge):
    _, body = http_bridge_get(server, "list", token=None)
    for entry in __import__("json").loads(body):
        assert entry["public"] is True


def test_list_with_wrong_bearer_falls_back_to_public_only(server, mixed_bridge):
    status, body = http_bridge_get(server, "list", token="not-the-real-token")
    assert status == 200
    names = {e["name"] for e in __import__("json").loads(body)}
    assert names == {"public-echo", "public-status", "public-stdin"}


def test_list_with_correct_bearer_returns_every_endpoint(server, mixed_bridge):
    """Authenticated callers see the full list, public + protected, with the
    `public` flag distinguishing them.
    """
    status, body = http_bridge_get(server, "list", token=BRIDGE_TOKEN)
    assert status == 200
    by_name = {e["name"]: e for e in __import__("json").loads(body)}
    assert set(by_name) == {"private-echo", "public-echo", "public-status", "public-stdin"}
    assert by_name["private-echo"]["public"] is False
    assert by_name["public-echo"]["public"] is True
    assert by_name["public-status"]["public"] is True
    assert by_name["public-stdin"]["public"] is True


def test_list_with_bearer_resolves_token_lazily(server, lazy_token_bridge):
    """A bearer header on the listing endpoint signals the caller wants the full
    list and is willing to pay the token-resolution cost - keyguard is invoked.
    """
    def side_effect(cmd, **kwargs):
        if cmd[0] == "/usr/local/bin/keyguard":
            return cli_run_result(0, stdout=BRIDGE_TOKEN)
        return cli_run_result(0, stdout="")

    with patch("subprocess.run", side_effect=side_effect) as mock_run:
        status, body = http_bridge_get(server, "list", token=BRIDGE_TOKEN)

    assert status == 200
    names = {e["name"] for e in __import__("json").loads(body)}
    assert names == {"echo", "public-echo"}  # full list visible to authenticated caller
    assert _keyguard_calls(mock_run) != []


def test_list_token_resolution_failure_falls_back_to_public(server, lazy_token_bridge):
    """If keyguard cannot resolve the bridge token (Touch ID denied, key missing),
    the listing still succeeds with the public-only view rather than 503.
    Callers who need the full list will hit a real protected endpoint and see
    the underlying server error.
    """
    def side_effect(cmd, **kwargs):
        if cmd[0] == "/usr/local/bin/keyguard":
            return cli_run_result(2, stderr="Touch ID cancelled")
        return cli_run_result(0, stdout="")

    with patch("subprocess.run", side_effect=side_effect):
        status, body = http_bridge_get(server, "list", token=BRIDGE_TOKEN)

    assert status == 200
    names = {e["name"] for e in __import__("json").loads(body)}
    assert names == {"public-echo"}  # only public visible since auth could not be verified


# ---- Unknown endpoint behaviour ----


def test_unknown_endpoint_returns_404_without_auth(server, mixed_bridge):
    """Unknown endpoints 404 directly. Names are already discoverable via the public listing,
    so gating 404 vs 401 on auth would only add a useless side-channel.
    """
    status, _ = http_bridge_post(server, "does-not-exist", token=None)
    assert status == 404
