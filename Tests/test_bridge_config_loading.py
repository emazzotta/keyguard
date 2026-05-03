"""Tests for bridge.ensure_config / _load_config_locked - real YAML files on disk."""
import os
import textwrap
from pathlib import Path

import pytest

from keyguard_server import bridge


@pytest.fixture()
def yaml_config(tmp_path: Path, monkeypatch):
    """Point bridge at a temp YAML file and return a writer that triggers a fresh load."""
    config_file = tmp_path / "bridge.yaml"
    monkeypatch.setattr(bridge, "BRIDGE_CONFIG_PATH", config_file)
    # Reset bridge state so each test starts clean
    monkeypatch.setattr(bridge, "_endpoints", {})
    monkeypatch.setattr(bridge, "_token", "")
    monkeypatch.setattr(bridge, "_token_resolved", False)
    monkeypatch.setattr(bridge, "_token_last_attempt", 0.0)
    monkeypatch.setattr(bridge, "_config_dirty", True)
    monkeypatch.setattr(bridge, "_config_mtime", 0.0)

    def write(content: str) -> None:
        config_file.write_text(textwrap.dedent(content))
        bridge.mark_dirty()
        bridge.ensure_config()

    return write


# ---- Happy path ----


def test_loads_valid_endpoint_from_disk(yaml_config):
    yaml_config("""
        endpoints:
          uptime:
            command: [/usr/bin/uptime]
            method: GET
            timeout: 5
    """)

    endpoint = bridge.get_endpoint("uptime")
    assert endpoint is not None
    assert endpoint.command == ("/usr/bin/uptime",)
    assert endpoint.allowed_methods == frozenset(["GET"])
    assert endpoint.pass_stdin is False
    assert endpoint.timeout == 5


def test_method_list_supports_get_and_post(yaml_config):
    yaml_config("""
        endpoints:
          mixed:
            command: [/bin/echo]
            method: [GET, POST]
    """)
    assert bridge.get_endpoint("mixed").allowed_methods == frozenset(["GET", "POST"])


def test_method_default_is_post(yaml_config):
    yaml_config("""
        endpoints:
          hello:
            command: [/bin/echo, hi]
    """)
    assert bridge.get_endpoint("hello").allowed_methods == frozenset(["POST"])


def test_stdin_default_is_false(yaml_config):
    yaml_config("""
        endpoints:
          hello:
            command: [/bin/echo, hi]
    """)
    assert bridge.get_endpoint("hello").pass_stdin is False


# ---- public flag (auth bypass for explicitly opted-in endpoints) ----


def test_public_default_is_false(yaml_config):
    yaml_config("""
        endpoints:
          hello:
            command: [/bin/echo, hi]
    """)
    assert bridge.get_endpoint("hello").public is False


def test_public_true_disables_auth(yaml_config):
    yaml_config("""
        endpoints:
          open:
            command: [/bin/echo, hi]
            public: true
    """)
    assert bridge.get_endpoint("open").public is True


def test_public_false_explicit(yaml_config):
    yaml_config("""
        endpoints:
          closed:
            command: [/bin/echo, hi]
            public: false
    """)
    assert bridge.get_endpoint("closed").public is False


def test_public_string_is_treated_as_protected(yaml_config):
    """Defence against the 'public: "true"' footgun. Only a real YAML boolean opens the gate."""
    yaml_config("""
        endpoints:
          ambiguous:
            command: [/bin/echo, hi]
            public: "true"
    """)
    assert bridge.get_endpoint("ambiguous").public is False


def test_public_number_is_treated_as_protected(yaml_config):
    yaml_config("""
        endpoints:
          numeric:
            command: [/bin/echo, hi]
            public: 1
    """)
    assert bridge.get_endpoint("numeric").public is False


def test_mixed_public_and_protected_endpoints_coexist(yaml_config):
    yaml_config("""
        endpoints:
          private-cmd:
            command: [/bin/echo, hi]
          public-cmd:
            command: [/bin/echo, hi]
            public: true
    """)
    assert bridge.get_endpoint("private-cmd").public is False
    assert bridge.get_endpoint("public-cmd").public is True


def test_command_args_coerced_to_strings(yaml_config):
    yaml_config("""
        endpoints:
          numeric:
            command: [/bin/sleep, 5]
    """)
    assert bridge.get_endpoint("numeric").command == ("/bin/sleep", "5")


# ---- Timeout clamping ----


def test_timeout_negative_clamped_to_minimum(yaml_config):
    yaml_config("""
        endpoints:
          neg:
            command: [/bin/echo]
            timeout: -10
    """)
    assert bridge.get_endpoint("neg").timeout == 1


def test_timeout_zero_clamped_to_minimum(yaml_config):
    yaml_config("""
        endpoints:
          zero:
            command: [/bin/echo]
            timeout: 0
    """)
    assert bridge.get_endpoint("zero").timeout == 1


def test_timeout_huge_clamped_to_maximum(yaml_config):
    yaml_config("""
        endpoints:
          huge:
            command: [/bin/echo]
            timeout: 99999
    """)
    assert bridge.get_endpoint("huge").timeout == 600


# ---- Error tolerance: bad endpoint shapes are skipped, others survive ----


def test_invalid_timeout_string_skips_endpoint_keeps_others(yaml_config):
    yaml_config("""
        endpoints:
          bad:
            command: [/bin/echo]
            timeout: "abc"
          good:
            command: [/bin/echo, hi]
    """)
    assert bridge.get_endpoint("bad") is None
    assert bridge.get_endpoint("good") is not None


def test_endpoint_without_command_is_skipped(yaml_config):
    yaml_config("""
        endpoints:
          missing:
            method: POST
          fine:
            command: [/bin/echo]
    """)
    assert bridge.get_endpoint("missing") is None
    assert bridge.get_endpoint("fine") is not None


def test_endpoint_with_empty_command_is_skipped(yaml_config):
    yaml_config("""
        endpoints:
          empty:
            command: []
    """)
    assert bridge.get_endpoint("empty") is None


def test_endpoint_with_string_command_is_skipped(yaml_config):
    """A string command (vs list) would imply shell expansion - reject explicitly."""
    yaml_config("""
        endpoints:
          shellish:
            command: "rm -rf /"
    """)
    assert bridge.get_endpoint("shellish") is None


def test_endpoint_with_non_dict_value_is_skipped(yaml_config):
    yaml_config("""
        endpoints:
          weird: just-a-string
          fine:
            command: [/bin/echo]
    """)
    assert bridge.get_endpoint("weird") is None
    assert bridge.get_endpoint("fine") is not None


def test_endpoints_as_list_at_root_disables_all(yaml_config):
    yaml_config("""
        endpoints:
          - one
          - two
    """)
    assert bridge.is_configured() is False


def test_yaml_root_must_be_mapping(yaml_config):
    yaml_config("just-a-string")
    assert bridge.is_configured() is False


def test_invalid_yaml_disables_bridge(yaml_config):
    yaml_config("[ unclosed: list")
    assert bridge.is_configured() is False


def test_missing_config_file_disables_bridge(monkeypatch, tmp_path):
    monkeypatch.setattr(bridge, "BRIDGE_CONFIG_PATH", tmp_path / "does-not-exist.yaml")
    monkeypatch.setattr(bridge, "_endpoints", {})
    monkeypatch.setattr(bridge, "_config_dirty", True)

    bridge.ensure_config()

    assert bridge.is_configured() is False


def test_missing_pyyaml_disables_bridge_gracefully(yaml_config, monkeypatch):
    """If PyYAML isn't installed the bridge should disable itself, not crash."""
    yaml_config("""
        endpoints:
          something:
            command: [/bin/echo]
    """)
    assert bridge.is_configured() is True

    import sys
    monkeypatch.setitem(sys.modules, "yaml", None)
    monkeypatch.setattr(bridge, "_config_dirty", True)

    bridge.ensure_config()

    assert bridge.is_configured() is False


# ---- SIGHUP / mark_dirty triggers reload ----


def test_mark_dirty_causes_reload_on_next_ensure_config(yaml_config):
    yaml_config("""
        endpoints:
          first:
            command: [/bin/echo, one]
    """)
    assert bridge.get_endpoint("first") is not None
    assert bridge.get_endpoint("second") is None

    yaml_config("""
        endpoints:
          second:
            command: [/bin/echo, two]
    """)

    assert bridge.get_endpoint("first") is None
    assert bridge.get_endpoint("second") is not None


def test_should_reload_when_file_mtime_changes_without_sighup(yaml_config):
    yaml_config("""
        endpoints:
          first:
            command: [/bin/echo, one]
    """)
    assert bridge.get_endpoint("first") is not None

    config_file = bridge.BRIDGE_CONFIG_PATH
    config_file.write_text(textwrap.dedent("""
        endpoints:
          second:
            command: [/bin/echo, two]
    """))
    mtime = config_file.stat().st_mtime
    os.utime(config_file, (mtime + 1, mtime + 1))

    bridge.ensure_config()

    assert bridge.get_endpoint("first") is None
    assert bridge.get_endpoint("second") is not None


def test_should_not_reload_when_file_mtime_unchanged(yaml_config):
    yaml_config("""
        endpoints:
          stable:
            command: [/bin/echo, hi]
    """)
    config_file = bridge.BRIDGE_CONFIG_PATH
    recorded_mtime = config_file.stat().st_mtime
    os.utime(config_file, (recorded_mtime, recorded_mtime))

    bridge.ensure_config()

    assert bridge.get_endpoint("stable") is not None


def test_mark_dirty_clears_resolved_token(yaml_config, monkeypatch):
    """After SIGHUP, the cached token must be discarded so next request re-resolves."""
    yaml_config("""
        endpoints:
          echo:
            command: [/bin/echo]
    """)
    monkeypatch.setattr(bridge, "_token", "old-token")
    monkeypatch.setattr(bridge, "_token_resolved", True)

    bridge.mark_dirty()
    bridge.ensure_config()

    assert bridge._token == ""
    assert bridge._token_resolved is False
