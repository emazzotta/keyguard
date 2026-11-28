"""Tests for the SIGHUP signal handler wired in server.main()."""
from unittest.mock import patch

from keyguard_server import bridge, server


def test_on_sighup_marks_bridge_dirty(monkeypatch):
    monkeypatch.setattr(bridge, "_config_dirty", False)

    server._on_sighup(1, None)

    assert bridge._config_dirty is True


def test_on_sighup_calls_bridge_mark_dirty():
    with patch.object(bridge, "mark_dirty") as mark:
        server._on_sighup(1, None)
    mark.assert_called_once()
