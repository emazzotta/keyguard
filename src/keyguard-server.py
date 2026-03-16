#!/usr/bin/env python3
from __future__ import annotations

import ipaddress
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from ipaddress import IPv4Address, IPv4Network
from pathlib import Path
from typing import Final
from urllib.parse import urlparse

KEYGUARD_BIN: Final = Path("/usr/local/bin/keyguard")
HOST: Final = "0.0.0.0"
PORT: Final = 7777

ALLOWED_NETWORKS: Final = (
    IPv4Network("127.0.0.0/8"),       # loopback
    IPv4Network("172.16.0.0/12"),     # Docker bridge (172.17-172.31.x.x)
    IPv4Network("192.168.65.0/24"),   # Docker Desktop for Mac
)


def is_allowed(client_ip: str) -> bool:
    try:
        addr = IPv4Address(client_ip)
        return any(addr in network for network in ALLOWED_NETWORKS)
    except ipaddress.AddressValueError:
        return False


class KeyguardHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if not is_allowed(self.client_address[0]):
            self._respond(403, b"Forbidden")
            return

        path = urlparse(self.path).path.strip("/")
        if not path:
            self._respond(400, b"Missing secret name")
            return

        keys = [k.strip() for k in path.split(",") if k.strip()]
        result = subprocess.run(
            [str(KEYGUARD_BIN), "get"] + keys,
            capture_output=True,
            text=True,
        )

        if result.returncode == 0:
            self._respond(200, result.stdout.encode(), "text/plain")
        elif result.returncode == 2:
            self._respond(403, b"Touch ID cancelled or failed")
        else:
            self._respond(500, result.stderr.encode())

    def _respond(self, code: int, body: bytes, content_type: str = "text/plain") -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        print(f"[keyguard] {self.address_string()} {format % args}", file=sys.stderr)


def main() -> None:
    server = HTTPServer((HOST, PORT), KeyguardHandler)
    print(f"[keyguard] listening on {HOST}:{PORT}", file=sys.stderr, flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
