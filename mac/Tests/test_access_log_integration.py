"""Integration tests verifying access-log lines are written (or not) for each request type."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from conftest import cli_run_result, http_get, http_post
from keyguard_server import access_log


@pytest.fixture()
def log_path(tmp_path: Path, monkeypatch) -> Path:
    """Redirect the access log to a temp file for each test."""
    path = tmp_path / "access.log"
    monkeypatch.setattr(access_log, "LOG_PATH", path)
    return path


def _read_lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    return [line for line in path.read_text(encoding="utf-8").splitlines() if line]


def test_get_writes_access_log_line(server, log_path):
    with patch("subprocess.run", return_value=cli_run_result(0, stdout="secret123")):
        status, _ = http_get(server, "/MY_TOKEN")

    assert status == 200
    lines = _read_lines(log_path)
    assert len(lines) == 1
    assert "MY_TOKEN" in lines[0]


def test_cached_get_marks_line_as_cached(server, log_path):
    with patch("subprocess.run", return_value=cli_run_result(0, stdout="secret123")):
        http_get(server, "/MY_TOKEN?timeout=60")
        http_get(server, "/MY_TOKEN?timeout=60")

    lines = _read_lines(log_path)
    assert len(lines) == 2
    assert "(cached)" not in lines[0]
    assert "(cached)" in lines[1]


def test_get_with_source_header_includes_hint_in_log(server, log_path):
    def side_effect(cmd, **kwargs):
        if cmd[0] == "/usr/local/bin/keyguard":
            return cli_run_result(0, stdout="secret123")
        if cmd[0] == "docker":
            return cli_run_result(0, stdout="/my-container\n")
        return cli_run_result(0)

    with patch("subprocess.run", side_effect=side_effect):
        status, _ = http_get(server, "/MY_TOKEN", extra_headers={"X-Keyguard-Source": "abc123"})

    assert status == 200
    lines = _read_lines(log_path)
    assert len(lines) == 1
    assert "my-container" in lines[0]


def test_list_does_not_write_log_line(server, log_path):
    with patch("subprocess.run", return_value=cli_run_result(0, stdout="KEY1\nKEY2\n")):
        http_get(server, "/_keys")

    assert _read_lines(log_path) == []


def test_post_does_not_write_log_line(server, log_path):
    with patch("subprocess.run", return_value=cli_run_result(0, stdout="Set 'TOKEN'")):
        http_post(server, "/TOKEN", body="value")

    assert _read_lines(log_path) == []


def test_log_line_uses_short_dash_not_em_dash(server, log_path):
    """Project rule: short dashes everywhere, no em dashes."""
    with patch("subprocess.run", return_value=cli_run_result(0, stdout="secret")):
        http_get(server, "/MY_TOKEN")

    lines = _read_lines(log_path)
    assert len(lines) == 1
    assert "—" not in lines[0]


def test_log_line_format_is_iso_timestamp_source_keys(server, log_path):
    with patch("subprocess.run", return_value=cli_run_result(0, stdout="secret")):
        http_get(server, "/MY_TOKEN")

    lines = _read_lines(log_path)
    assert len(lines) == 1
    parts = lines[0].split(" ", 2)
    assert len(parts) == 3
    timestamp, _, rest = parts
    assert timestamp.endswith("Z")
    assert "T" in timestamp
    assert "MY_TOKEN" in rest
