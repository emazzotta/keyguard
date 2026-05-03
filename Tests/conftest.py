"""Shared test fixtures: server, http helpers, fresh-cache and fresh-bridge state."""
from __future__ import annotations

import http.client
import sys
import threading
from http.server import HTTPServer, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# Make src/keyguard_server importable
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from keyguard_server import bridge, cache  # noqa: E402
from keyguard_server.handler import KeyguardHandler  # noqa: E402

BRIDGE_TOKEN = "test-bridge-token"


# ---------------------------------------------------------------------------
# Server fixture (one threaded HTTP server per test module)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), KeyguardHandler)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv
    srv.shutdown()


@pytest.fixture(autouse=True)
def reset_state():
    """Wipe shared module state before and after each test."""
    cache.clear()
    yield
    cache.clear()


# ---------------------------------------------------------------------------
# Bridge state fixtures
# ---------------------------------------------------------------------------


def set_bridge_state(monkeypatch, *, endpoints: dict | None = None,
                     token: str = "", token_resolved: bool = False) -> None:
    monkeypatch.setattr(bridge, "_endpoints", endpoints or {})
    monkeypatch.setattr(bridge, "_token", token)
    monkeypatch.setattr(bridge, "_token_resolved", token_resolved)
    monkeypatch.setattr(bridge, "_token_last_attempt", 0.0)
    monkeypatch.setattr(bridge, "_config_dirty", False)


@pytest.fixture()
def configured_bridge(monkeypatch):
    """Bridge configured with a pre-resolved token (skips keyguard subprocess)."""
    endpoints = {
        "echo": bridge.Endpoint(
            command=("/bin/echo", "hello"),
            allowed_methods=frozenset(["POST"]),
            pass_stdin=False,
            timeout=10,
        ),
        "get-status": bridge.Endpoint(
            command=("/bin/echo", "ok"),
            allowed_methods=frozenset(["GET"]),
            pass_stdin=False,
            timeout=10,
        ),
        "stdin-endpoint": bridge.Endpoint(
            command=("/bin/cat",),
            allowed_methods=frozenset(["POST"]),
            pass_stdin=True,
            timeout=10,
        ),
    }
    set_bridge_state(monkeypatch, endpoints=endpoints, token=BRIDGE_TOKEN, token_resolved=True)


@pytest.fixture()
def lazy_token_bridge(monkeypatch):
    """Bridge with endpoints but token not yet resolved (will hit keyguard CLI on first call)."""
    endpoints = {
        "echo": bridge.Endpoint(
            command=("/bin/echo", "hello"),
            allowed_methods=frozenset(["POST"]),
            pass_stdin=False,
            timeout=10,
        ),
        "public-echo": bridge.Endpoint(
            command=("/bin/echo", "public"),
            allowed_methods=frozenset(["POST"]),
            pass_stdin=False,
            timeout=10,
            public=True,
        ),
    }
    set_bridge_state(monkeypatch, endpoints=endpoints, token="", token_resolved=False)


@pytest.fixture()
def mixed_bridge(monkeypatch):
    """Bridge with a mix of protected and public endpoints, token pre-resolved."""
    endpoints = {
        "private-echo": bridge.Endpoint(
            command=("/bin/echo", "private"),
            allowed_methods=frozenset(["POST"]),
            pass_stdin=False,
            timeout=10,
        ),
        "public-echo": bridge.Endpoint(
            command=("/bin/echo", "public"),
            allowed_methods=frozenset(["POST"]),
            pass_stdin=False,
            timeout=10,
            public=True,
        ),
        "public-status": bridge.Endpoint(
            command=("/bin/echo", "ok"),
            allowed_methods=frozenset(["GET"]),
            pass_stdin=False,
            timeout=10,
            public=True,
        ),
        "public-stdin": bridge.Endpoint(
            command=("/bin/cat",),
            allowed_methods=frozenset(["POST"]),
            pass_stdin=True,
            timeout=10,
            public=True,
        ),
    }
    set_bridge_state(monkeypatch, endpoints=endpoints, token=BRIDGE_TOKEN, token_resolved=True)


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


def _connection(srv: HTTPServer) -> http.client.HTTPConnection:
    return http.client.HTTPConnection(f"127.0.0.1:{srv.server_address[1]}")


def http_get(srv: HTTPServer, path: str, extra_headers: dict[str, str] | None = None) -> tuple[int, str]:
    headers = {"Connection": "close"}
    if extra_headers:
        headers.update(extra_headers)
    conn = _connection(srv)
    conn.request("GET", path, headers=headers)
    resp = conn.getresponse()
    return resp.status, resp.read().decode()


def http_post(srv: HTTPServer, path: str, body: str = "",
              extra_headers: dict[str, str] | None = None) -> tuple[int, str]:
    encoded = body.encode()
    headers = {"Content-Length": str(len(encoded)), "Connection": "close"}
    if extra_headers:
        headers.update(extra_headers)
    conn = _connection(srv)
    conn.request("POST", path, body=encoded, headers=headers)
    resp = conn.getresponse()
    return resp.status, resp.read().decode()


def http_delete(srv: HTTPServer, path: str) -> tuple[int, str]:
    conn = _connection(srv)
    conn.request("DELETE", path, headers={"Connection": "close"})
    resp = conn.getresponse()
    return resp.status, resp.read().decode()


def http_bridge_get(srv: HTTPServer, name: str, token: str | None = BRIDGE_TOKEN) -> tuple[int, str]:
    headers = {"Connection": "close"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    conn = _connection(srv)
    conn.request("GET", f"/_bridge/{name}", headers=headers)
    resp = conn.getresponse()
    return resp.status, resp.read().decode()


def http_bridge_post(srv: HTTPServer, name: str, body: str = "",
                     token: str | None = BRIDGE_TOKEN,
                     extra_headers: dict[str, str] | None = None) -> tuple[int, str]:
    headers = {"Connection": "close"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    encoded = body.encode()
    if encoded:
        headers["Content-Length"] = str(len(encoded))
    if extra_headers:
        headers.update(extra_headers)
    conn = _connection(srv)
    conn.request("POST", f"/_bridge/{name}", body=encoded or None, headers=headers)
    resp = conn.getresponse()
    return resp.status, resp.read().decode()


# ---------------------------------------------------------------------------
# Subprocess result helper
# ---------------------------------------------------------------------------


def cli_run_result(returncode: int, stdout: str = "", stderr: str = "") -> MagicMock:
    """Build a MagicMock that quacks like a subprocess.CompletedProcess."""
    r = MagicMock()
    r.returncode = returncode
    r.stdout = stdout
    r.stderr = stderr
    return r
