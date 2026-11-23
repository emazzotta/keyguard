"""Shared test fixtures: server, http helpers, fresh-cache and fresh-bridge state."""
from __future__ import annotations

import http.client
import json
import socket
import sys
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# Make src/keyguard_server importable
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from keyguard_server import bridge, cache, consent, failure_report  # noqa: E402
from keyguard_server.handler import KeyguardHandler  # noqa: E402
from keyguard_server.keyguard_cli import CliResult  # noqa: E402

ABSENT_BRIDGE_CONFIG = Path("/nonexistent/keyguard-tests/bridge.yaml")


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


@pytest.fixture(autouse=True)
def failure_reports_stay_on_this_machine(monkeypatch):
    """KEYGUARD_STORE_URL is set on the Mac that runs the suite: a test's failed read must never post to its service."""
    monkeypatch.setattr(failure_report, "PHONE_SERVICE", "")


def eventually(condition: Callable[[], bool], seconds: float = 3.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.02)
    return condition()


# ---------------------------------------------------------------------------
# Stand-in unlock service
# ---------------------------------------------------------------------------


@dataclass
class Post:
    path: str
    headers: dict[str, str]
    body: dict[str, str]


@dataclass
class UnlockService:
    url: str
    posts: list[Post] = field(default_factory=list)
    status: int = 202
    gate: threading.Event = field(default_factory=threading.Event)

    def arrived(self, count: int = 1) -> bool:
        return eventually(lambda: len(self.posts) >= count)


@pytest.fixture()
def unlock_service() -> Iterator[UnlockService]:
    """Records each POST it gets, answers with `status`, and holds each answer until `gate` is set."""
    service = UnlockService(url="")
    service.gate.set()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            service.posts.append(Post(self.path, {k.lower(): v for k, v in self.headers.items()}, json.loads(raw)))
            service.gate.wait(10)
            self.send_response(service.status)
            self.end_headers()

        def log_message(self, fmt: str, *args: object) -> None:
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    service.url = f"http://127.0.0.1:{srv.server_address[1]}"
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield service
    service.gate.set()
    srv.shutdown()


@pytest.fixture()
def dead_service_url() -> str:
    """An address where nothing listens, so a connection is refused at once."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return f"http://127.0.0.1:{probe.getsockname()[1]}"


# ---------------------------------------------------------------------------
# Bridge state fixtures
# ---------------------------------------------------------------------------


def set_bridge_state(monkeypatch, *, endpoints: dict | None = None) -> None:
    monkeypatch.setattr(bridge, "BRIDGE_CONFIG_PATH", ABSENT_BRIDGE_CONFIG)
    monkeypatch.setattr(bridge, "_endpoints", endpoints or {})
    monkeypatch.setattr(bridge, "_config_dirty", False)


@pytest.fixture()
def approved_prompts(monkeypatch) -> list[str]:
    """Approve every Touch ID prompt without the CLI, recording each reason shown."""
    reasons: list[str] = []

    def approve(reason: str) -> CliResult:
        reasons.append(reason)
        return CliResult(rc=0, stdout="", stderr="")

    monkeypatch.setattr(consent, "ask", approve)
    return reasons


@pytest.fixture()
def configured_bridge(monkeypatch, approved_prompts):
    """Private endpoints whose prompts are approved without the CLI."""
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
    set_bridge_state(monkeypatch, endpoints=endpoints)


@pytest.fixture()
def prompting_bridge(monkeypatch):
    """Endpoints whose prompts go through the real consent gate to the (patched) CLI."""
    endpoints = {
        "echo": bridge.Endpoint(
            command=("/bin/echo", "hello"),
            allowed_methods=frozenset(["POST"]),
            pass_stdin=False,
            timeout=10,
        ),
        "cat": bridge.Endpoint(
            command=("/bin/cat",),
            allowed_methods=frozenset(["POST"]),
            pass_stdin=True,
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
    set_bridge_state(monkeypatch, endpoints=endpoints)


@pytest.fixture()
def mixed_bridge(monkeypatch, approved_prompts):
    """Private and public endpoints side by side, prompts approved without the CLI."""
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
    set_bridge_state(monkeypatch, endpoints=endpoints)


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


def http_bridge_get(srv: HTTPServer, name: str,
                    extra_headers: dict[str, str] | None = None) -> tuple[int, str]:
    headers = {"Connection": "close"}
    if extra_headers:
        headers.update(extra_headers)
    conn = _connection(srv)
    conn.request("GET", f"/_bridge/{name}", headers=headers)
    resp = conn.getresponse()
    return resp.status, resp.read().decode()


def http_bridge_post(srv: HTTPServer, name: str, body: str = "",
                     extra_headers: dict[str, str] | None = None) -> tuple[int, str]:
    headers = {"Connection": "close"}
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
