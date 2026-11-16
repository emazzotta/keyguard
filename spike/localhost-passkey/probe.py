#!/usr/bin/env python3
"""Probe whether a browser on this machine can wrap and unwrap an age file with a passkey bound to `localhost`.

Serves the probe page on loopback and appends each report the page posts to a JSON-lines file. It stays up
until Ctrl+C, so several browsers and authenticators can be tried in one run.
"""
from __future__ import annotations

import argparse
import json
import secrets
import webbrowser
from dataclasses import dataclass
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Final

PAGE_DIR: Final = Path(__file__).resolve().parent
RP_HOST: Final = "localhost"
BIND_ADDRESS: Final = "127.0.0.1"
TOKEN_HEADER: Final = "X-Probe-Token"
TOKEN_BYTES: Final = 16
REPORT_PATH: Final = "/report"
MAX_REPORT_BYTES: Final = 64 * 1024
DEFAULT_RESULTS: Final = Path("probe-results.jsonl")
BUNDLED_TYPAGE: Final = PAGE_DIR / "typage.js"
REPOSITORY_TYPAGE: Final = PAGE_DIR.parents[1] / "keyguard_passkey" / "pages" / "typage.js"
STATIC_FILES: Final = {
    "/": (PAGE_DIR / "probe.html", "text/html; charset=utf-8"),
    "/probe.js": (PAGE_DIR / "probe.js", "text/javascript; charset=utf-8"),
    "/typage.js": (BUNDLED_TYPAGE if BUNDLED_TYPAGE.is_file() else REPOSITORY_TYPAGE, "text/javascript; charset=utf-8"),
}


@dataclass(frozen=True)
class Probe:
    token: str
    results: Path

    def accepts(self, token: str) -> bool:
        return secrets.compare_digest(token.encode(), self.token.encode())

    def record(self, report: dict[str, object]) -> None:
        stamped = {"received": datetime.now(timezone.utc).isoformat(timespec="seconds"), **report}
        with self.results.open("a", encoding="utf-8") as out:
            out.write(json.dumps(stamped, ensure_ascii=False) + "\n")


class ProbeServer(HTTPServer):
    def __init__(self, probe: Probe, port: int) -> None:
        super().__init__((BIND_ADDRESS, port), ProbeHandler)
        self.probe = probe

    @property
    def authority(self) -> str:
        return f"{RP_HOST}:{self.server_port}"

    @property
    def page_url(self) -> str:
        return f"http://{self.authority}/#token={self.probe.token}"


class ProbeHandler(BaseHTTPRequestHandler):
    server: ProbeServer

    def do_GET(self) -> None:
        static = STATIC_FILES.get(self.path.partition("?")[0])
        if static is None or not self._asked_by_name():
            return self._answer(HTTPStatus.NOT_FOUND)
        file, content_type = static
        self._answer(HTTPStatus.OK, file.read_bytes(), content_type)

    def do_POST(self) -> None:
        if self.path != REPORT_PATH or not self._asked_by_name():
            return self._answer(HTTPStatus.NOT_FOUND)
        if not self.server.probe.accepts(self.headers.get(TOKEN_HEADER, "")):
            return self._answer(HTTPStatus.FORBIDDEN)
        report = self._read_report()
        if report is None:
            return self._answer(HTTPStatus.BAD_REQUEST)
        self.server.probe.record(report)
        print(summarize(report), flush=True)
        self._answer(HTTPStatus.NO_CONTENT)

    def log_message(self, *_: object) -> None:
        pass

    def _asked_by_name(self) -> bool:
        return self.headers.get("Host", "") == self.server.authority

    def _read_report(self) -> dict[str, object] | None:
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            return None
        if not 0 < length <= MAX_REPORT_BYTES:
            return None
        try:
            report = json.loads(self.rfile.read(length))
        except ValueError:
            return None
        return report if isinstance(report, dict) else None

    def _answer(self, status: HTTPStatus, body: bytes = b"", content_type: str = "text/plain") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def step_outcome(step: object) -> str:
    if not isinstance(step, dict):
        return "unreadable step"
    if step.get("ok") is True:
        return f"{step.get('name')} ok"
    if step.get("pending") is True:
        return f"{step.get('name')} still waiting"
    return f"{step.get('name')} failed ({step.get('error')})"


def summarize(report: dict[str, object]) -> str:
    steps = report.get("steps")
    outcomes = ", ".join(step_outcome(step) for step in steps) if isinstance(steps, list) and steps else "no steps"
    return f"{report.get('label') or 'no label'} | {report.get('browser') or 'unknown browser'} | {outcomes}"


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=0, help="loopback port to serve on (default: any free port)")
    parser.add_argument("--results", type=Path, default=DEFAULT_RESULTS,
                        help=f"JSON-lines file the reports are appended to (default: {DEFAULT_RESULTS})")
    parser.add_argument("--no-browser", action="store_true", help="print the page's address without opening it")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    probe = Probe(token=secrets.token_urlsafe(TOKEN_BYTES), results=args.results)
    with ProbeServer(probe, args.port) as server:
        print(f"Probe page: {server.page_url}", flush=True)
        print(f"Open it in each browser to test. Reports go to {args.results}. Ctrl+C ends.", flush=True)
        if not args.no_browser:
            webbrowser.open(server.page_url)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
