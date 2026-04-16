"""Bridge config path: env var override and tilde expansion."""
import importlib
import os
import sys
from pathlib import Path


def _reload_config(env: dict[str, str | None]) -> object:
    """Reimport keyguard_server.config under a clean env, returning the fresh module."""
    saved = {k: os.environ.get(k) for k in env}
    try:
        for k, v in env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        # Force a fresh import so the module-level Final is recomputed
        sys.modules.pop("keyguard_server.config", None)
        return importlib.import_module("keyguard_server.config")
    finally:
        for k, original in saved.items():
            if original is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = original
        # Restore the canonical config module so other tests see consistent state
        sys.modules.pop("keyguard_server.config", None)
        importlib.import_module("keyguard_server.config")


def test_defaults_to_home_yaml():
    fresh = _reload_config({"KEYGUARD_BRIDGE_CONFIG_FILE": None})
    assert fresh.BRIDGE_CONFIG_PATH == Path("~/.mac-bridge-endpoints.yaml").expanduser()


def test_honours_env_var(tmp_path: Path):
    custom = tmp_path / "custom-bridge.yaml"
    fresh = _reload_config({"KEYGUARD_BRIDGE_CONFIG_FILE": str(custom)})
    assert fresh.BRIDGE_CONFIG_PATH == custom


def test_expands_tilde():
    fresh = _reload_config({"KEYGUARD_BRIDGE_CONFIG_FILE": "~/somewhere/bridge.yaml"})
    expected = Path("~/somewhere/bridge.yaml").expanduser()
    assert fresh.BRIDGE_CONFIG_PATH == expected
    assert "~" not in str(fresh.BRIDGE_CONFIG_PATH)
