"""Server entrypoint - wires the SIGHUP handler and starts the threaded HTTP server."""
from __future__ import annotations

import signal
import sys
from http.server import ThreadingHTTPServer

from . import bridge
from .config import HOST, PORT
from .handler import KeyguardHandler


def _on_sighup(signum: int, frame: object) -> None:
    bridge.mark_dirty()


def main() -> None:
    signal.signal(signal.SIGHUP, _on_sighup)
    server = ThreadingHTTPServer((HOST, PORT), KeyguardHandler)
    print(f"[keyguard] listening on {HOST}:{PORT}", file=sys.stderr, flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
