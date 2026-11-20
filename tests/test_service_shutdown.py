from __future__ import annotations

import json
import os
import re
import select
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from conftest import ROOT

from keyguard_passkey.cli import STATE_ENV
from keyguard_passkey.service import asking
from keyguard_passkey.service.server import build_server
from keyguard_passkey.service.settings import DEFAULT_IDENTITY_HEADER, Settings
from keyguard_passkey.service.shutdown import Background, DrainingServer, shut_down, stop_on_sigterm

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="the service stops on SIGTERM")

LOGIN = "owner@example.com"
REQUEST = {"vars": ["A"], "host": "devbox", "eph_pubkey": "age1x", "ts": 1, "sig": "s"}
LISTENING = re.compile(r"service on 127\.0\.0\.1:(\d+),")
DEADLINE_SECONDS = 10
PROMPT_SECONDS = 10
POLL_SECONDS = 0.01
SLOW_STOP_SECONDS = 0.1
SLOW_WORKER_SECONDS = 1.0


def health_answer(port: int) -> int:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=DEADLINE_SECONDS) as answer:
        status: int = answer.status
    return status


def refuses_connections(port: int) -> bool:
    deadline = time.monotonic() + DEADLINE_SECONDS
    while time.monotonic() < deadline:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=1).close()
        except ConnectionRefusedError:
            return True
        time.sleep(POLL_SECONDS)
    return False


class OpenCall:
    def __init__(self, port: int) -> None:
        body = json.dumps(REQUEST).encode()
        half = len(body) // 2
        head = (f"POST /requests HTTP/1.0\r\nContent-Type: application/json\r\n{DEFAULT_IDENTITY_HEADER}: {LOGIN}\r\n"
                f"Content-Length: {len(body)}\r\n\r\n").encode()
        self._rest = body[half:]
        self._socket = socket.create_connection(("127.0.0.1", port), timeout=DEADLINE_SECONDS)
        self._socket.sendall(head + body[:half])
        assert health_answer(port) == 204

    def finish(self) -> tuple[int, dict[str, object]]:
        self._socket.sendall(self._rest)
        chunks = []
        while chunk := self._socket.recv(4096):
            chunks.append(chunk)
        self._socket.close()
        head, _, body = b"".join(chunks).partition(b"\r\n\r\n")
        return int(head.split()[1]), json.loads(body)


class Service:
    def __init__(self, folder: Path, heartbeat_url: str = "") -> None:
        env = {name: value for name, value in os.environ.items() if not name.startswith("KEYGUARD")}
        env.update({"PYTHONPATH": str(ROOT), STATE_ENV: str(folder / "state"),
                    "KEYGUARD_PASSKEY_STORE": str(folder / "store"), "KEYGUARD_PASSKEY_BIND": "127.0.0.1",
                    "KEYGUARD_PASSKEY_PORT": "0", "KEYGUARD_PASSKEY_HEARTBEAT_URL": heartbeat_url})
        self.process = subprocess.Popen([sys.executable, "-m", "keyguard_passkey", "serve"], stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, text=True, env=env)
        assert self.process.stdout is not None
        first_line = self.process.stdout.readline()
        found = LISTENING.search(first_line)
        assert found, f"serve printed {first_line!r} and exited {self.process.poll()}"
        self.port = int(found.group(1))

    def wait_for_stderr_line(self) -> None:
        assert self.process.stderr is not None
        ready, _, _ = select.select([self.process.stderr], [], [], DEADLINE_SECONDS)
        assert ready, f"serve wrote nothing to stderr within {DEADLINE_SECONDS} s"
        self.process.stderr.readline()

    def exit_code_within(self, seconds: float) -> int | None:
        try:
            return self.process.wait(timeout=seconds)
        except subprocess.TimeoutExpired:
            return None


@contextmanager
def running_service(folder: Path, heartbeat_url: str = "") -> Iterator[Service]:
    service = Service(folder, heartbeat_url)
    try:
        yield service
    finally:
        if service.process.poll() is None:
            service.process.kill()
        service.process.wait(timeout=DEADLINE_SECONDS)
        for pipe in (service.process.stdout, service.process.stderr):
            if pipe:
                pipe.close()


def closed_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
    return port


def stopping_in_background(server: DrainingServer, background: Background | None = None) -> threading.Thread:
    stopping = threading.Thread(target=shut_down, args=(server, background or Background()), daemon=True)
    stopping.start()
    return stopping


@pytest.fixture
def serving(tmp_path: Path) -> Iterator[DrainingServer]:
    server = build_server(Settings(store=tmp_path, bind="127.0.0.1", port=0))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server
    server.shutdown()
    server.server_close()


@pytest.mark.spec("docs/service.md#stop-it")
def test_should_refuse_a_new_connection_when_the_shutdown_has_begun(serving):
    port = serving.server_address[1]
    open_call = OpenCall(port)
    stopping = stopping_in_background(serving)

    refused = refuses_connections(port)
    open_call.finish()
    stopping.join(DEADLINE_SECONDS)

    assert refused


@pytest.mark.spec("docs/service.md#stop-it")
def test_should_answer_an_open_request_before_the_shutdown_returns(serving):
    port = serving.server_address[1]
    open_call = OpenCall(port)
    stopping = stopping_in_background(serving)
    assert refuses_connections(port)
    waiting_for_the_request = stopping.is_alive()

    status, answer = open_call.finish()
    stopping.join(DEADLINE_SECONDS)

    assert (waiting_for_the_request, status, sorted(answer)) == (True, 200, ["id"])
    assert not stopping.is_alive()


@pytest.mark.spec("docs/service.md#stop-it")
def test_should_return_at_once_when_no_request_is_open(serving):
    stopping = stopping_in_background(serving)

    stopping.join(DEADLINE_SECONDS)

    assert not stopping.is_alive()
    assert refuses_connections(serving.server_address[1])


@pytest.mark.spec("docs/service.md#stop-it")
def test_should_stop_the_background_threads_before_returning(serving):
    background = Background()
    ended: list[bool] = []

    def work() -> None:
        stopped = background.stopped.wait(DEADLINE_SECONDS)
        time.sleep(SLOW_WORKER_SECONDS)
        ended.append(stopped)

    background.start(work)

    shut_down(serving, background)

    assert ended == [True]


@pytest.mark.spec("docs/service.md#stop-it")
def test_should_stop_once_when_sigterm_arrives_twice():
    stops: list[str] = []

    with stop_on_sigterm(lambda: stops.append("stop")):
        signal.raise_signal(signal.SIGTERM)
        signal.raise_signal(signal.SIGTERM)

    assert stops == ["stop"]


@pytest.mark.spec("docs/service.md#stop-it")
def test_should_leave_only_after_the_stop_has_finished():
    stops: list[str] = []

    def slow_stop() -> None:
        time.sleep(SLOW_STOP_SECONDS)
        stops.append("stop")

    with stop_on_sigterm(slow_stop):
        signal.raise_signal(signal.SIGTERM)

    assert stops == ["stop"]


@pytest.mark.spec("docs/service.md#stop-it")
def test_should_not_stop_when_sigterm_never_arrives():
    stops: list[str] = []

    with stop_on_sigterm(lambda: stops.append("stop")):
        pass

    assert stops == []


@pytest.mark.spec("docs/service.md#stop-it")
def test_should_restore_the_previous_sigterm_handler_when_the_service_has_stopped():
    previous = signal.getsignal(signal.SIGTERM)

    with stop_on_sigterm(lambda: None):
        installed = signal.getsignal(signal.SIGTERM)

    assert installed is not previous
    assert signal.getsignal(signal.SIGTERM) is previous


@pytest.mark.spec("docs/service.md#stop-it")
def test_should_leave_sigterm_to_a_waiting_request_when_both_run_in_one_process():
    stops: list[str] = []

    with stop_on_sigterm(lambda: stops.append("stop")), asking.sigterm_cancels() as cancelled:
        signal.raise_signal(signal.SIGTERM)

        withdrawn = cancelled()

    assert (withdrawn, stops) == (True, [])


@pytest.mark.spec("docs/service.md#stop-it")
def test_should_stop_on_sigterm_after_a_waiting_request_in_the_same_process_ends():
    stops: list[str] = []

    with stop_on_sigterm(lambda: stops.append("stop")):
        with asking.sigterm_cancels():
            pass
        signal.raise_signal(signal.SIGTERM)

    assert stops == ["stop"]


@pytest.mark.spec("docs/service.md#stop-it")
def test_should_exit_zero_within_seconds_of_sigterm_when_idle(tmp_path):
    with running_service(tmp_path, heartbeat_url=f"http://127.0.0.1:{closed_port()}/beat") as service:
        service.wait_for_stderr_line()

        service.process.send_signal(signal.SIGTERM)

        assert service.exit_code_within(PROMPT_SECONDS) == 0


@pytest.mark.spec("docs/service.md#stop-it")
def test_should_finish_an_open_request_and_exit_zero_when_sigterm_arrives(tmp_path):
    with running_service(tmp_path) as service:
        open_call = OpenCall(service.port)

        service.process.send_signal(signal.SIGTERM)
        refused = refuses_connections(service.port)
        exited_early = service.process.poll()
        status, answer = open_call.finish()

        assert (refused, exited_early) == (True, None)
        assert (status, sorted(answer)) == (200, ["id"])
        assert service.exit_code_within(PROMPT_SECONDS) == 0
