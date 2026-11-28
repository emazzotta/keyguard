"""Push a heartbeat to a push monitor (Uptime Kuma, Healthchecks) while the service answers.

The service asks its own health endpoint first and only then pushes, so a dead or hung server goes quiet and
the monitor turns red. The push URL carries the monitor's token and is never logged.
"""
from __future__ import annotations

import sys
import threading
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Final

HEALTH_PATH: Final = "/healthz"
INTERVAL_SECONDS: Final = 60
TIMEOUT_SECONDS: Final = 10
LOOPBACK: Final = "127.0.0.1"
ANY_ADDRESS: Final = frozenset({"0.0.0.0", "::", ""})

Fetch = Callable[[str], None]
Sleep = Callable[[float], object]


def _fetch(url: str) -> None:
    with urllib.request.urlopen(url, timeout=TIMEOUT_SECONDS) as response:
        response.read()


def _answers(url: str, fetch: Fetch, what: str) -> bool:
    try:
        fetch(url)
    except (urllib.error.URLError, OSError) as error:
        print(f"heartbeat: {what} failed: {error}", file=sys.stderr, flush=True)
        return False
    return True


def beat(health_url: str, push_url: str, fetch: Fetch = _fetch) -> bool:
    return _answers(health_url, fetch, "service check") and _answers(push_url, fetch, "push")


class Heartbeat:
    def __init__(self, health_url: str, push_url: str, fetch: Fetch = _fetch, sleep: Sleep | None = None,
                 interval: float = INTERVAL_SECONDS, stopped: threading.Event | None = None) -> None:
        self.health_url = health_url
        self.push_url = push_url
        self.fetch = fetch
        self.stopped = threading.Event() if stopped is None else stopped
        self.sleep: Sleep = self.stopped.wait if sleep is None else sleep
        self.interval = interval

    def run(self, beats: int | None = None) -> None:
        count = 0
        while not self.stopped.is_set() and (beats is None or count < beats):
            beat(self.health_url, self.push_url, self.fetch)
            count += 1
            self.sleep(self.interval)


def health_url(bind: str, port: int) -> str:
    host = LOOPBACK if bind in ANY_ADDRESS else bind
    return f"http://{f'[{host}]' if ':' in host else host}:{port}{HEALTH_PATH}"


def heartbeat_for(push_url: str, bind: str, port: int, stopped: threading.Event | None = None) -> Heartbeat | None:
    return Heartbeat(health_url(bind, port), push_url, stopped=stopped) if push_url else None
