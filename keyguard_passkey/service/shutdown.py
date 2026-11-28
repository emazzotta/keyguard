from __future__ import annotations

import contextlib
import signal
import threading
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import FrameType
from typing import Any


class DrainingServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], handler: type[BaseHTTPRequestHandler]) -> None:
        super().__init__(address, handler)
        self._open_connections = 0
        self._connection_ended = threading.Condition()

    def process_request(self, request: Any, client_address: Any) -> None:
        self._connection_opened()
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._connection_closed()
            raise

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._connection_closed()

    def wait_for_connections(self) -> None:
        with self._connection_ended:
            self._connection_ended.wait_for(lambda: self._open_connections == 0)

    def _connection_opened(self) -> None:
        with self._connection_ended:
            self._open_connections += 1

    def _connection_closed(self) -> None:
        with self._connection_ended:
            self._open_connections -= 1
            self._connection_ended.notify_all()


class Background:
    def __init__(self) -> None:
        self.stopped = threading.Event()
        self._threads: list[threading.Thread] = []

    def start(self, target: Callable[..., None], *args: object) -> None:
        thread = threading.Thread(target=target, args=args, daemon=True)
        self._threads.append(thread)
        thread.start()

    def stop(self) -> None:
        self.stopped.set()

    def join(self) -> None:
        for thread in self._threads:
            thread.join()


def shut_down(server: DrainingServer, background: Background) -> None:
    background.stop()
    server.shutdown()
    server.server_close()
    server.wait_for_connections()
    background.join()


@contextlib.contextmanager
def stop_on_sigterm(stop: Callable[[], None]) -> Iterator[None]:
    stopping = threading.Thread(target=stop)
    first = threading.Lock()

    def begin(_signum: int, _frame: FrameType | None) -> None:
        if first.acquire(blocking=False):
            stopping.start()

    try:
        previous = signal.signal(signal.SIGTERM, begin)
    except ValueError:
        yield
        return
    try:
        yield
        if first.locked():
            stopping.join()
    finally:
        signal.signal(signal.SIGTERM, previous)
