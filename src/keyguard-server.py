#!/usr/bin/env python3
from __future__ import annotations

import ipaddress
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
from ipaddress import IPv4Address, IPv4Network
from pathlib import Path
from typing import Final
from urllib.parse import parse_qs, urlparse

KEYGUARD_BIN: Final = Path("/usr/local/bin/keyguard")
HOST: Final = "0.0.0.0"
PORT: Final = 7777
SUBPROCESS_TIMEOUT: Final = 60
MAX_SECRET_BYTES: Final = 65_536
MAX_CACHE_TIMEOUT: Final = 300

ALLOWED_NETWORKS: Final = (
    IPv4Network("127.0.0.0/8"),
    IPv4Network("172.16.0.0/12"),
    IPv4Network("192.168.65.0/24"),
)

_cache: dict[str, tuple[str, float]] = {}
_cache_lock = threading.Lock()


def is_allowed(client_ip: str) -> bool:
    try:
        addr = IPv4Address(client_ip)
        return any(addr in network for network in ALLOWED_NETWORKS)
    except ipaddress.AddressValueError:
        return False


def _cache_get(key: str) -> str | None:
    with _cache_lock:
        entry = _cache.get(key)
        if entry and entry[1] > time.monotonic():
            return entry[0]
        if entry:
            del _cache[key]
        return None


def _cache_put(key: str, value: str, timeout: int) -> None:
    with _cache_lock:
        _cache[key] = (value, time.monotonic() + timeout)


def _cache_clear() -> None:
    with _cache_lock:
        _cache.clear()


def _resolve_hostname(ip: str) -> str | None:
    if ip.startswith("127."):
        return "localhost"
    try:
        import socket
        hostname, _, _ = socket.gethostbyaddr(ip)
        if hostname and hostname != ip:
            return hostname
    except (socket.herror, socket.gaierror, OSError):
        pass
    return None


def _resolve_container_name(ip: str) -> str | None:
    try:
        result = subprocess.run(
            ["docker", "ps", "-q"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return None
        container_ids = result.stdout.strip().split("\n")
        result = subprocess.run(
            ["docker", "inspect", "--format",
             "{{.Name}} {{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}"]
            + container_ids,
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            return None
        for line in result.stdout.strip().split("\n"):
            parts = line.strip().split()
            if len(parts) >= 2:
                name = parts[0].lstrip("/")
                ips = parts[1:]
                if ip in ips:
                    return name
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return None


def _resolve_source(ip: str) -> str:
    names: list[str] = []
    hostname = _resolve_hostname(ip)
    if hostname:
        names.append(hostname)
    container = _resolve_container_name(ip)
    if container and container not in names:
        names.append(container)
    if names:
        return f"{ip} ({', '.join(names)})"
    return ip


def _escape_osascript(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _send_notification(keys: list[str], client_ip: str, cached: bool) -> None:
    source = _resolve_source(client_ip)
    cache_hint = " (cached)" if cached else ""
    key_list = ", ".join(keys)
    message = _escape_osascript(f"{key_list} read by {source}{cache_hint}")
    try:
        subprocess.run(
            ["osascript", "-e",
             f'display notification "{message}" with title "keyguard"'],
            capture_output=True, timeout=5,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass


def _notify_async(keys: list[str], client_ip: str, cached: bool) -> None:
    thread = threading.Thread(
        target=_send_notification, args=(keys, client_ip, cached), daemon=True,
    )
    thread.start()


def _parse_timeout(query: dict[str, list[str]]) -> int | None:
    timeout_values = query.get("timeout")
    if not timeout_values:
        return None
    try:
        timeout = int(timeout_values[0])
        if timeout <= 0:
            return None
        return min(timeout, MAX_CACHE_TIMEOUT)
    except (ValueError, IndexError):
        return None


def _format_response(keys: list[str], values: dict[str, str | None]) -> str:
    if len(keys) == 1:
        return values[keys[0]] or ""
    return "\n".join(f"{k}={values[k]}" for k in keys if values[k] is not None) + "\n"


def _parse_key_value_output(stdout: str) -> dict[str, str]:
    values = {}
    for line in stdout.strip().split("\n"):
        if "=" in line:
            k, v = line.split("=", 1)
            values[k] = v
    return values


class KeyguardHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        if not is_allowed(self.client_address[0]):
            self._respond(403, b"Forbidden")
            return

        path = urlparse(self.path).path.strip("/")
        if not path or "," in path or path == "_keys":
            self._respond(400, b"Invalid secret name")
            return

        try:
            content_length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            self._respond(400, b"Invalid Content-Length")
            return

        if content_length > MAX_SECRET_BYTES:
            self._respond(400, b"Request body too large")
            return

        try:
            value = self.rfile.read(content_length).decode("utf-8").strip()
        except UnicodeDecodeError:
            self._respond(400, b"Request body must be valid UTF-8")
            return

        if not value:
            self._respond(400, b"Missing value in request body")
            return

        self._run_keyguard(["set", path], stdin_value=value)

    def do_GET(self) -> None:
        if not is_allowed(self.client_address[0]):
            self._respond(403, b"Forbidden")
            return

        parsed = urlparse(self.path)
        path = parsed.path.strip("/")
        if not path:
            self._respond(400, b"Missing secret name")
            return

        if path == "_keys":
            self._run_keyguard(["list"])
            return

        keys = [k.strip() for k in path.split(",") if k.strip()]
        client_ip = self.client_address[0]
        query = parse_qs(parsed.query)
        timeout = _parse_timeout(query)

        if timeout:
            self._get_with_cache(keys, client_ip, timeout)
        else:
            self._run_keyguard(["get"] + keys, notify_keys=keys, client_ip=client_ip)

    def do_DELETE(self) -> None:
        if not is_allowed(self.client_address[0]):
            self._respond(403, b"Forbidden")
            return

        path = urlparse(self.path).path.strip("/")
        if path == "_cache":
            _cache_clear()
            self._respond(200, b"Cache cleared")
        else:
            self._respond(400, b"Unknown endpoint")

    def _get_with_cache(self, keys: list[str], client_ip: str, timeout: int) -> None:
        cached_values = {k: _cache_get(k) for k in keys}

        if all(v is not None for v in cached_values.values()):
            body = _format_response(keys, cached_values)
            self._respond(200, body.encode(), "text/plain")
            _notify_async(keys, client_ip, cached=True)
            return

        missing_keys = [k for k in keys if cached_values[k] is None]
        fresh_values = self._fetch_keys(missing_keys, timeout)
        if fresh_values is None:
            return

        for k, v in fresh_values.items():
            _cache_put(k, v, timeout)

        all_values = {}
        for k in keys:
            all_values[k] = cached_values[k] if cached_values[k] is not None else fresh_values.get(k)

        body = _format_response(keys, all_values)
        self._respond(200, body.encode(), "text/plain")
        _notify_async(keys, client_ip, cached=False)

    def _fetch_keys(self, keys: list[str], cache_duration: int) -> dict[str, str] | None:
        cmd = ["get"] + keys + ["--cache-duration", str(cache_duration)]
        try:
            result = subprocess.run(
                [str(KEYGUARD_BIN)] + cmd,
                capture_output=True, text=True,
                timeout=SUBPROCESS_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            self._respond(500, b"keyguard timed out")
            return None

        if result.returncode == 2:
            self._respond(403, b"Touch ID cancelled or failed")
            return None
        if result.returncode != 0:
            self._respond(500, result.stderr.encode())
            return None

        if len(keys) == 1:
            return {keys[0]: result.stdout}
        return _parse_key_value_output(result.stdout)

    def _run_keyguard(self, cmd_args: list[str], stdin_value: str | None = None,
                      notify_keys: list[str] | None = None, client_ip: str | None = None) -> None:
        try:
            result = subprocess.run(
                [str(KEYGUARD_BIN)] + cmd_args,
                capture_output=True,
                text=True,
                timeout=SUBPROCESS_TIMEOUT,
                input=stdin_value,
            )
        except subprocess.TimeoutExpired:
            self._respond(500, b"keyguard timed out")
            return

        if result.returncode == 0:
            self._respond(200, result.stdout.encode(), "text/plain")
            if notify_keys and client_ip:
                _notify_async(notify_keys, client_ip, cached=False)
        elif result.returncode == 2:
            self._respond(403, b"Touch ID cancelled or failed")
        else:
            self._respond(500, result.stderr.encode())

    def _respond(self, code: int, body: bytes, content_type: str = "text/plain") -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: object) -> None:
        print(f"[keyguard] {self.address_string()} {fmt % args}", file=sys.stderr)


def main() -> None:
    server = ThreadingHTTPServer((HOST, PORT), KeyguardHandler)
    print(f"[keyguard] listening on {HOST}:{PORT}", file=sys.stderr, flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
