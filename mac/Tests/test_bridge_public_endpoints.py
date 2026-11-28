"""Public bridge endpoints - `public: true` in YAML skips the Touch ID prompt for that endpoint only.

Default posture is a prompt per request; public is the exception. These tests
guarantee:
  - public endpoints run without a prompt and never invoke keyguard
  - private endpoints still prompt when public siblings exist
  - method whitelist, stdin, access logging, command execution still apply
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from conftest import (
    cli_run_result,
    http_bridge_get,
    http_bridge_post,
)
from keyguard_server import access_log
from keyguard_server.config import KEYGUARD_BIN


def _bridge_calls(mock_run):
    return list(mock_run.call_args_list)


def _keyguard_calls(mock_run):
    return [c for c in mock_run.call_args_list if c[0][0][0] == str(KEYGUARD_BIN)]


@pytest.fixture()
def log_path(tmp_path: Path, monkeypatch) -> Path:
    path = tmp_path / "access.log"
    monkeypatch.setattr(access_log, "LOG_PATH", path)
    return path


def _read_log_lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    return [line for line in path.read_text(encoding="utf-8").splitlines() if line]


# ---- No prompt for public endpoints ----


def test_public_endpoint_succeeds_without_a_prompt(server, mixed_bridge, approved_prompts):
    with patch("subprocess.run", return_value=cli_run_result(0, stdout="public\n")):
        status, body = http_bridge_post(server, "public-echo")

    assert status == 200
    assert body == "public\n"
    assert approved_prompts == []


def test_public_endpoint_never_reaches_keyguard(server, prompting_bridge):
    def side_effect(cmd, **kwargs):
        if cmd[0] == str(KEYGUARD_BIN):
            raise AssertionError("keyguard must not be invoked for public endpoints")
        return cli_run_result(0, stdout="public\n")

    with patch("subprocess.run", side_effect=side_effect) as mock_run:
        status, body = http_bridge_post(server, "public-echo")

    assert status == 200
    assert body == "public\n"
    assert _keyguard_calls(mock_run) == []


# ---- Private endpoints still prompt ----


def test_private_endpoint_next_to_public_ones_still_prompts(server, mixed_bridge, approved_prompts):
    with patch("subprocess.run", return_value=cli_run_result(0, stdout="private\n")):
        status, body = http_bridge_post(server, "private-echo")

    assert status == 200
    assert body == "private\n"
    assert approved_prompts == ["Run bridge endpoint private-echo: /bin/echo private"]


# ---- Method whitelist still applies to public endpoints ----


def test_public_endpoint_method_whitelist_rejects_wrong_method(server, mixed_bridge):
    """`public-status` is GET-only - POSTing it must still 405."""
    status, _ = http_bridge_post(server, "public-status")
    assert status == 405


def test_public_endpoint_get_succeeds(server, mixed_bridge):
    with patch("subprocess.run", return_value=cli_run_result(0, stdout="ok")):
        status, body = http_bridge_get(server, "public-status")

    assert status == 200
    assert body == "ok"


# ---- Public endpoints honour stdin ----


def test_public_endpoint_passes_stdin(server, mixed_bridge):
    with patch("subprocess.run", return_value=cli_run_result(0, stdout="hi")) as mock_run:
        http_bridge_post(server, "public-stdin", body="hi")

    time.sleep(0.2)
    bridge_calls = _bridge_calls(mock_run)
    assert len(bridge_calls) == 1
    assert bridge_calls[0][1]["input"] == "hi"


# ---- Access log still records public calls ----


def test_public_endpoint_success_writes_access_log_line(server, mixed_bridge, log_path):
    with patch("subprocess.run", return_value=cli_run_result(0, stdout="public\n")):
        http_bridge_post(server, "public-echo")

    lines = _read_log_lines(log_path)
    assert len(lines) == 1
    assert "bridge:public-echo" in lines[0]


# ---- Listing: public by default, everything once approved ----


def test_list_without_all_returns_only_public_endpoints(server, mixed_bridge):
    """Private endpoint names must not leak to a listing the user has not approved."""
    status, body = http_bridge_get(server, "list")
    assert status == 200
    names = {e["name"] for e in json.loads(body)}
    assert names == {"public-echo", "public-status", "public-stdin"}


def test_list_without_all_entries_are_marked_public(server, mixed_bridge):
    _, body = http_bridge_get(server, "list")
    for entry in json.loads(body):
        assert entry["public"] is True


def test_list_approved_with_all_returns_every_endpoint(server, mixed_bridge):
    """The approved full list carries the `public` flag to tell the two kinds apart."""
    status, body = http_bridge_get(server, "list?all=1")
    assert status == 200
    by_name = {e["name"]: e for e in json.loads(body)}
    assert set(by_name) == {"private-echo", "public-echo", "public-status", "public-stdin"}
    assert by_name["private-echo"]["public"] is False
    assert by_name["public-echo"]["public"] is True
    assert by_name["public-status"]["public"] is True
    assert by_name["public-stdin"]["public"] is True


# ---- Unknown endpoint behaviour ----


def test_unknown_endpoint_returns_404(server, mixed_bridge):
    status, _ = http_bridge_post(server, "does-not-exist")
    assert status == 404
