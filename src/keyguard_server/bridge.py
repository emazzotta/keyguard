"""Bridge: whitelisted Mac commands callable over HTTP, gated by a keyguard-stored token.

Token flow:
- The token always lives in keyguard under MAC_BRIDGE_TOKEN (no plaintext fallback).
- Resolved lazily on the first authenticated bridge request - one Touch ID per server lifetime.
- Failed resolutions (Touch ID denied, key missing) are rate-limited so a misconfigured
  client cannot spam Touch ID prompts. SIGHUP forces a reload and clears the rate limit.
- Requests without a Bearer header are rejected before keyguard is ever invoked.
"""
from __future__ import annotations

import hmac
import sys
import threading
import time
from dataclasses import dataclass

from . import keyguard_cli
from .config import (
    BRIDGE_CONFIG_PATH,
    BRIDGE_TOKEN_KEYGUARD_KEY,
    BRIDGE_TOKEN_RETRY_COOLDOWN,
    SUBPROCESS_TIMEOUT,
)


@dataclass(frozen=True)
class Endpoint:
    command: tuple[str, ...]
    allowed_methods: frozenset[str]
    pass_stdin: bool
    timeout: int


_endpoints: dict[str, Endpoint] = {}
_token: str = ""
_token_resolved: bool = False
_token_last_attempt: float = 0.0
_config_dirty: bool = True

_config_lock = threading.Lock()
_token_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def is_configured() -> bool:
    """True if at least one bridge endpoint is defined."""
    return bool(_endpoints)


def get_endpoint(name: str) -> Endpoint | None:
    return _endpoints.get(name)


def list_endpoints() -> list[dict]:
    """Return public metadata for all configured endpoints, sorted by name.

    Command is intentionally omitted - callers need to know what they can call,
    not the underlying implementation.
    """
    return [
        {"name": name, "methods": sorted(ep.allowed_methods), "timeout": ep.timeout}
        for name, ep in sorted(_endpoints.items())
    ]


def ensure_config() -> None:
    """Load the bridge config from disk on first call after each SIGHUP / startup."""
    global _config_dirty
    if not _config_dirty:
        return
    with _config_lock:
        if not _config_dirty:
            return
        _load_config_locked()
        _config_dirty = False


def ensure_token() -> str | None:
    """Resolve the bridge token from keyguard if not already cached.
    Returns None on success (token stored in module state), error message on failure.
    """
    if _token_resolved:
        return None
    with _token_lock:
        return _resolve_token_locked()


def verify_token(auth_header: str | None) -> bool:
    if not auth_header or not auth_header.startswith("Bearer "):
        return False
    provided = auth_header[len("Bearer "):].strip()
    if not _token:
        return False
    return hmac.compare_digest(provided.encode(), _token.encode())


def mark_dirty() -> None:
    """Schedule a config + token reload (called from the SIGHUP handler)."""
    global _config_dirty
    _config_dirty = True
    print("[keyguard] bridge: config reload scheduled (SIGHUP received)", file=sys.stderr)


# ---------------------------------------------------------------------------
# Internal - config loading
# ---------------------------------------------------------------------------


def _reset_state_locked() -> None:
    global _endpoints, _token, _token_resolved, _token_last_attempt
    _endpoints = {}
    _token = ""
    _token_resolved = False
    _token_last_attempt = 0.0


def _load_config_locked() -> None:
    _reset_state_locked()

    if not BRIDGE_CONFIG_PATH.exists():
        return

    try:
        import yaml as _yaml  # type: ignore[import]
    except ImportError:
        print("[keyguard] bridge: PyYAML not installed - run 'pip install pyyaml' to enable bridge",
              file=sys.stderr)
        return

    try:
        raw = _yaml.safe_load(BRIDGE_CONFIG_PATH.read_text())
    except Exception as e:
        print(f"[keyguard] bridge: config parse error: {e}", file=sys.stderr)
        return

    if not isinstance(raw, dict):
        print("[keyguard] bridge: config root must be a YAML mapping", file=sys.stderr)
        return

    endpoints_raw = raw.get("endpoints") or {}
    if not isinstance(endpoints_raw, dict):
        print("[keyguard] bridge: 'endpoints' must be a YAML mapping, ignoring", file=sys.stderr)
        endpoints_raw = {}
    endpoints = _parse_endpoints(endpoints_raw)
    global _endpoints
    _endpoints = endpoints
    print(f"[keyguard] bridge: loaded {len(endpoints)} endpoint(s); "
          f"token will be resolved from keyguard:{BRIDGE_TOKEN_KEYGUARD_KEY} on first use",
          file=sys.stderr)


def _parse_endpoints(raw: dict) -> dict[str, Endpoint]:
    parsed: dict[str, Endpoint] = {}
    for name, spec in raw.items():
        endpoint = _safely_parse_endpoint(name, spec)
        if endpoint is not None:
            parsed[name] = endpoint
    return parsed


def _safely_parse_endpoint(name: str, spec: object) -> Endpoint | None:
    """Returns the parsed endpoint or None on any error. Never raises."""
    try:
        return _parse_endpoint(name, spec)
    except (TypeError, ValueError) as e:
        print(f"[keyguard] bridge: endpoint '{name}' invalid: {e}, skipping", file=sys.stderr)
        return None


_TIMEOUT_MIN = 1
_TIMEOUT_MAX = 600


def _parse_endpoint(name: str, spec: object) -> Endpoint | None:
    if not isinstance(spec, dict):
        print(f"[keyguard] bridge: endpoint '{name}' must be a mapping, skipping", file=sys.stderr)
        return None
    command = spec.get("command")
    if not isinstance(command, list) or not command:
        print(f"[keyguard] bridge: endpoint '{name}' missing valid 'command' list, skipping",
              file=sys.stderr)
        return None
    return Endpoint(
        command=tuple(str(c) for c in command),
        allowed_methods=_parse_methods(spec.get("method", "POST")),
        pass_stdin=bool(spec.get("stdin", False)),
        timeout=_parse_timeout(spec.get("timeout", SUBPROCESS_TIMEOUT)),
    )


def _parse_timeout(value: object) -> int:
    timeout = int(value)  # may raise ValueError/TypeError, caught upstream
    if timeout < _TIMEOUT_MIN:
        return _TIMEOUT_MIN
    if timeout > _TIMEOUT_MAX:
        return _TIMEOUT_MAX
    return timeout


def _parse_methods(spec: object) -> frozenset[str]:
    if isinstance(spec, str):
        return frozenset([spec.upper()])
    if isinstance(spec, list):
        return frozenset(str(m).upper() for m in spec)
    return frozenset(["POST"])


# ---------------------------------------------------------------------------
# Internal - token resolution
# ---------------------------------------------------------------------------


def _resolve_token_locked() -> str | None:
    global _token, _token_resolved, _token_last_attempt

    if _token_resolved:
        return None

    cooldown_error = _check_rate_limit()
    if cooldown_error:
        return cooldown_error
    _token_last_attempt = time.monotonic()

    result = keyguard_cli.get(BRIDGE_TOKEN_KEYGUARD_KEY)
    if result.timed_out:
        return "keyguard timed out while resolving bridge token"
    if result.not_found:
        return "keyguard binary not found"
    if result.touch_id_cancelled:
        return "Touch ID cancelled while resolving bridge token"
    if not result.ok:
        return f"keyguard error resolving bridge token: {result.stderr.strip()}"

    token = result.stdout.strip()
    if not token:
        return f"keyguard:{BRIDGE_TOKEN_KEYGUARD_KEY} resolved to an empty value"

    _token = token
    _token_resolved = True
    return None


def _check_rate_limit() -> str | None:
    if not _token_last_attempt:
        return None
    elapsed = time.monotonic() - _token_last_attempt
    if elapsed >= BRIDGE_TOKEN_RETRY_COOLDOWN:
        return None
    remaining = int(BRIDGE_TOKEN_RETRY_COOLDOWN - elapsed) + 1
    return f"Bridge token resolution rate-limited; retry in {remaining}s or send SIGHUP to reload"
