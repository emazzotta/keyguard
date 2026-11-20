from __future__ import annotations

import io
import threading
import urllib.error
from contextlib import redirect_stderr

import pytest

from keyguard_passkey.service.heartbeat import (
    HEALTH_PATH,
    INTERVAL_SECONDS,
    Heartbeat,
    beat,
    health_url,
    heartbeat_for,
)

HEALTH_URL = f"http://127.0.0.1:8080{HEALTH_PATH}"
PUSH_URL = "http://monitor.example/api/push/secret-token?status=up"
DEADLINE_SECONDS = 10


def failing_on(failing_url: str | None):
    fetched: list[str] = []

    def fetch(url: str) -> None:
        fetched.append(url)
        if url == failing_url:
            raise urllib.error.URLError("connection refused")

    return fetched, fetch


@pytest.mark.spec("docs/service.md")
def test_should_push_after_the_service_answers():
    fetched, fetch = failing_on(None)

    assert beat(HEALTH_URL, PUSH_URL, fetch)

    assert fetched == [HEALTH_URL, PUSH_URL]


@pytest.mark.spec("docs/service.md")
def test_should_stay_quiet_when_the_service_does_not_answer():
    fetched, fetch = failing_on(HEALTH_URL)

    with redirect_stderr(io.StringIO()):
        assert not beat(HEALTH_URL, PUSH_URL, fetch)

    assert fetched == [HEALTH_URL]


@pytest.mark.spec("docs/service.md")
def test_should_report_a_failed_push_without_its_token():
    _, fetch = failing_on(PUSH_URL)
    stderr = io.StringIO()

    with redirect_stderr(stderr):
        assert not beat(HEALTH_URL, PUSH_URL, fetch)

    assert "push failed" in stderr.getvalue()
    assert "secret-token" not in stderr.getvalue()


@pytest.mark.spec("docs/service.md")
def test_should_beat_once_per_interval():
    fetched, fetch = failing_on(None)
    slept: list[float] = []

    Heartbeat(HEALTH_URL, PUSH_URL, fetch=fetch, sleep=slept.append).run(beats=2)

    assert fetched == [HEALTH_URL, PUSH_URL, HEALTH_URL, PUSH_URL]
    assert slept == [INTERVAL_SECONDS, INTERVAL_SECONDS]


@pytest.mark.spec("docs/service.md")
def test_should_keep_beating_after_a_failed_beat():
    fetched, fetch = failing_on(HEALTH_URL)

    with redirect_stderr(io.StringIO()):
        Heartbeat(HEALTH_URL, PUSH_URL, fetch=fetch, sleep=lambda _: None).run(beats=2)

    assert fetched == [HEALTH_URL, HEALTH_URL]


@pytest.mark.spec("docs/service.md")
def test_should_stay_off_without_a_push_url():
    assert heartbeat_for("", "127.0.0.1", 8080) is None


@pytest.mark.spec("docs/service.md")
@pytest.mark.parametrize(("bind", "expected"), [
    ("127.0.0.1", HEALTH_URL), ("0.0.0.0", HEALTH_URL), ("::", HEALTH_URL),
    ("10.0.0.5", f"http://10.0.0.5:8080{HEALTH_PATH}"), ("::1", f"http://[::1]:8080{HEALTH_PATH}"),
])
def test_should_check_the_service_where_it_listens(bind, expected):
    assert health_url(bind, 8080) == expected


@pytest.mark.spec("docs/service.md")
def test_should_push_to_the_configured_url():
    heartbeat = heartbeat_for(PUSH_URL, "0.0.0.0", 8080)

    assert (heartbeat.health_url, heartbeat.push_url) == (HEALTH_URL, PUSH_URL)


@pytest.mark.spec("docs/service.md#stop-it")
def test_should_wake_from_the_wait_and_stop_when_told_to():
    stopped, pushed, fetched = threading.Event(), threading.Event(), []

    def fetch(url: str) -> None:
        fetched.append(url)
        if url == PUSH_URL:
            pushed.set()

    thread = threading.Thread(target=Heartbeat(HEALTH_URL, PUSH_URL, fetch=fetch, stopped=stopped).run, daemon=True)
    thread.start()
    assert pushed.wait(DEADLINE_SECONDS)

    stopped.set()
    thread.join(DEADLINE_SECONDS)

    assert not thread.is_alive()
    assert fetched == [HEALTH_URL, PUSH_URL]


@pytest.mark.spec("docs/service.md#stop-it")
def test_should_not_beat_when_told_to_stop_before_it_starts():
    fetched, fetch = failing_on(None)
    stopped = threading.Event()
    stopped.set()

    Heartbeat(HEALTH_URL, PUSH_URL, fetch=fetch, stopped=stopped).run(beats=1)

    assert fetched == []


@pytest.mark.spec("docs/service.md#stop-it")
def test_should_share_the_stop_it_is_given_with_the_heartbeat_it_builds():
    stopped = threading.Event()

    heartbeat = heartbeat_for(PUSH_URL, "0.0.0.0", 8080, stopped)

    assert heartbeat is not None
    assert heartbeat.stopped is stopped
