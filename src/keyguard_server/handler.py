"""HTTP request handler - routes requests to secrets API or bridge."""
from __future__ import annotations

import json
import subprocess
import sys
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

from . import bridge, cache, encoding, keyguard_cli, notification
from .config import (
    MAX_BRIDGE_OUTPUT_BYTES,
    MAX_CACHE_TIMEOUT,
    MAX_SECRET_BYTES,
)
from .ip_allowlist import is_allowed
from .keyguard_cli import CliResult


_BRIDGE_PREFIX = "_bridge/"
_BRIDGE_LIST = "list"
_KEYS_PATH = "_keys"
_CACHE_PATH = "_cache"


class KeyguardHandler(BaseHTTPRequestHandler):
    # ---- HTTP entry points ----

    def do_GET(self) -> None:
        if not self._gate_ip():
            return

        parsed = urlparse(self.path)
        path = parsed.path.strip("/")

        if path.startswith(_BRIDGE_PREFIX):
            self._handle_bridge("GET", path[len(_BRIDGE_PREFIX):])
            return

        if not path:
            self._respond(400, b"Missing secret name")
            return

        query = parse_qs(parsed.query)
        if path == _KEYS_PATH:
            self._handle_list(query)
            return

        self._handle_get(path, query)

    def do_POST(self) -> None:
        if not self._gate_ip():
            return

        path = urlparse(self.path).path.strip("/")

        if path.startswith(_BRIDGE_PREFIX):
            body = self._read_body(required=False)
            if body is None:
                return
            self._handle_bridge("POST", path[len(_BRIDGE_PREFIX):], body=body)
            return

        if not path or "," in path or path == _KEYS_PATH:
            self._respond(400, b"Invalid secret name")
            return

        body = self._read_body(required=True)
        if body is None:
            return
        body = body.strip()
        if not body:
            self._respond(400, b"Missing value in request body")
            return

        self._respond_cli(keyguard_cli.set_secret(path, body))

    def do_DELETE(self) -> None:
        if not self._gate_ip():
            return

        path = urlparse(self.path).path.strip("/")
        if path == _CACHE_PATH:
            cache.clear()
            self._respond(200, b"Cache cleared")
        else:
            self._respond(400, b"Unknown endpoint")

    def log_message(self, fmt: str, *args: object) -> None:
        print(f"[keyguard] {self.address_string()} {fmt % args}", file=sys.stderr)

    # ---- Secret GET handlers ----

    def _handle_list(self, query: dict[str, list[str]]) -> None:
        timeout = encoding.parse_timeout(query, MAX_CACHE_TIMEOUT)
        client_ip = self.client_address[0]

        if not timeout:
            self._respond_cli(keyguard_cli.list_keys())
            return

        cached = cache.get(client_ip, _KEYS_PATH)
        if cached is not None:
            self._respond(200, cached.encode())
            return

        result = keyguard_cli.list_keys(cache_duration=timeout)
        if not self._respond_cli_or_handle_error(result):
            return
        cache.put(client_ip, _KEYS_PATH, result.stdout, timeout)

    def _handle_get(self, path: str, query: dict[str, list[str]]) -> None:
        keys = [k.strip() for k in path.split(",") if k.strip()]
        client_ip = self.client_address[0]
        source_hint = self.headers.get("X-Keyguard-Source")
        timeout = encoding.parse_timeout(query, MAX_CACHE_TIMEOUT)

        if not timeout:
            result = keyguard_cli.get(*keys)
            if self._respond_cli_or_handle_error(result):
                notification.notify_async(keys, client_ip, cached=False, source_hint=source_hint)
            return

        share_ips = cache.parse_share(query, client_ip)
        self._handle_get_with_cache(keys, client_ip, timeout, share_ips, source_hint)

    def _handle_get_with_cache(self, keys: list[str], client_ip: str, timeout: int,
                               share_ips: list[str], source_hint: str | None) -> None:
        cached_values = {k: cache.get_shared(share_ips, k) for k in keys}

        if all(v is not None for v in cached_values.values()):
            body = encoding.format_response(keys, cached_values)
            self._respond(200, body.encode())
            notification.notify_async(keys, client_ip, cached=True, source_hint=source_hint)
            return

        missing = [k for k in keys if cached_values[k] is None]
        result = keyguard_cli.get(*missing, cache_duration=timeout)
        if not self._respond_cli_or_handle_error(result, suppress_success=True):
            return

        fresh = {missing[0]: result.stdout} if len(missing) == 1 else encoding.parse_key_value_output(result.stdout)
        for k, v in fresh.items():
            cache.put(client_ip, k, v, timeout)

        all_values = {k: cached_values[k] if cached_values[k] is not None else fresh.get(k) for k in keys}
        body = encoding.format_response(keys, all_values)
        self._respond(200, body.encode())
        notification.notify_async(keys, client_ip, cached=False, source_hint=source_hint)

    # ---- Bridge handler ----

    def _handle_bridge(self, method: str, name: str, body: str = "") -> None:
        bridge.ensure_config()

        if not bridge.is_configured():
            self._respond(501, b"Bridge not configured")
            return

        if name == _BRIDGE_LIST:
            show_all = self._caller_can_see_all_endpoints()
            payload = json.dumps(bridge.list_endpoints(public_only=not show_all)).encode()
            self._respond(200, payload, content_type="application/json")
            return

        endpoint = bridge.get_endpoint(name)
        if endpoint is None:
            self._respond(404, b"Unknown bridge endpoint")
            return

        if not endpoint.public and not self._authorize_bridge():
            return

        if method not in endpoint.allowed_methods:
            self._respond(405, b"Method not allowed")
            return

        self._execute_bridge(name, endpoint, body)

    def _caller_can_see_all_endpoints(self) -> bool:
        """Listing is privilege-aware: a valid bearer token reveals every
        endpoint, anonymous (or wrong-token) callers see only the public ones.

        Callers without a Bearer header never trigger keyguard - the listing
        falls back to public-only without burning a Touch ID prompt. A bearer
        header is treated as a signal that the caller wants the full list and
        is willing to pay the token-resolution cost; if resolution fails
        (rate-limit, Touch ID denied, key missing) the caller still gets a
        useful public-only listing rather than a 503.
        """
        auth = self.headers.get("Authorization")
        if not auth or not auth.startswith("Bearer "):
            return False
        if bridge.ensure_token() is not None:
            return False
        return bridge.verify_token(auth)

    def _authorize_bridge(self) -> bool:
        """Run the bearer-token gate for protected endpoints. Returns True iff the
        caller is authenticated; otherwise emits the response and returns False.

        Reject malformed/missing Authorization headers BEFORE keyguard is touched
        so unauthenticated callers cannot spam Touch ID prompts.
        """
        auth = self.headers.get("Authorization")
        if not auth or not auth.startswith("Bearer "):
            self._respond(401, b"Unauthorized")
            return False

        token_error = bridge.ensure_token()
        if token_error is not None:
            self._respond(503, token_error.encode())
            return False

        if not bridge.verify_token(auth):
            self._respond(401, b"Unauthorized")
            return False

        return True

    def _execute_bridge(self, name: str, endpoint: bridge.Endpoint, body: str) -> None:
        stdin_data = body if endpoint.pass_stdin else None
        client_ip = self.client_address[0]

        try:
            result = subprocess.run(
                list(endpoint.command),
                capture_output=True, text=True, errors="replace",
                timeout=endpoint.timeout, input=stdin_data,
            )
        except subprocess.TimeoutExpired:
            self._respond(504, b"Bridge command timed out")
            return
        except FileNotFoundError:
            self._respond(500, b"Bridge command executable not found")
            return
        except OSError as e:
            self._respond(500, f"Bridge command failed to start: {e}".encode())
            return

        if result.returncode == 0:
            output = result.stdout[:MAX_BRIDGE_OUTPUT_BYTES]
            self._respond(200, output.encode())
            notification.notify_async([f"bridge:{name}"], client_ip, cached=False)
        else:
            error = (result.stderr or result.stdout)[:MAX_BRIDGE_OUTPUT_BYTES]
            self._respond(500, error.encode())

    # ---- Response helpers ----

    def _gate_ip(self) -> bool:
        if is_allowed(self.client_address[0]):
            return True
        self._respond(403, b"Forbidden")
        return False

    def _read_body(self, required: bool) -> str | None:
        try:
            content_length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            self._respond(400, b"Invalid Content-Length")
            return None
        if content_length > MAX_SECRET_BYTES:
            self._respond(400, b"Request body too large")
            return None
        if content_length == 0:
            if required:
                self._respond(400, b"Missing value in request body")
                return None
            return ""
        try:
            return self.rfile.read(content_length).decode("utf-8")
        except UnicodeDecodeError:
            self._respond(400, b"Request body must be valid UTF-8")
            return None

    def _respond_cli(self, result: CliResult) -> None:
        """Respond from a CliResult - 200 stdout / 403 Touch ID / 500 stderr / 500 timeout."""
        self._respond_cli_or_handle_error(result)

    def _respond_cli_or_handle_error(self, result: CliResult, suppress_success: bool = False) -> bool:
        """Returns True if the call succeeded (and the response was sent unless suppressed)."""
        if result.timed_out:
            self._respond(500, b"keyguard timed out")
            return False
        if result.not_found:
            self._respond(500, b"keyguard binary not found")
            return False
        if result.touch_id_cancelled:
            self._respond(403, b"Touch ID cancelled or failed")
            return False
        if not result.ok:
            self._respond(500, result.stderr.encode())
            return False
        if not suppress_success:
            self._respond(200, result.stdout.encode())
        return True

    def _respond(self, code: int, body: bytes, content_type: str = "text/plain") -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.end_headers()
        self.wfile.write(body)
