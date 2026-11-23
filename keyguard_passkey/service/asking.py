"""Service mode for the commands: one signed request to the unlock service, answered on the approving device.

It returns the answers the browser ceremony returns, so every command works the same way in both modes.
"""
from __future__ import annotations

import contextlib
import re
import signal
import sys
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final, TextIO

from ..ceremony import Op, Request
from . import client, pending
from .client import Caller, Signer, UnlockError
from .hint import NotifyHint, clean_container
from .presence import Presence, read_presence

APPROVE_PATH: Final = "/approve"
ENROLL_PATH: Final = "/enroll"
AGE_RECIPIENT_PREFIX: Final = "age1"
PANE_ENV: Final = "TMUX_PANE"
PANE_ID: Final = re.compile(r"%\d{1,6}")


class UsageError(UnlockError):
    """A request the service cannot take as asked."""


def asking_pane(environ: Mapping[str, str]) -> str | None:
    """The tmux pane that asks, outside the signed request: it only tells a watcher where to look."""
    pane = environ.get(PANE_ENV, "")
    return pane if PANE_ID.fullmatch(pane) else None


@contextlib.contextmanager
def sigterm_cancels() -> Iterator[Callable[[], bool]]:
    """While a request waits, SIGTERM withdraws it as `pending --cancel` does, so the process that started this one
    can take its question back."""
    received: list[int] = []
    try:
        previous = signal.signal(signal.SIGTERM, lambda signum, _frame: received.append(signum))
    except ValueError:
        yield lambda: False
        return
    try:
        yield lambda: bool(received)
    finally:
        signal.signal(signal.SIGTERM, previous)


@dataclass(frozen=True)
class ServiceAsk:
    url: str
    signing_key: Path
    requester: str
    token: str
    remote_recipient: str
    pending_dir: Path
    pane: str | None = None
    presence_dir: Path | None = None
    container: str = ""
    now: Callable[[], float] = time.time
    cancelled: Callable[[], bool] = lambda: False
    stderr: TextIO = sys.stderr
    timeout: float = client.DEFAULT_TIMEOUT_SECONDS
    interval: float = client.DEFAULT_POLL_INTERVAL_SECONDS

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    def approve_link(self, request_id: str) -> str:
        return f"{self.url}{APPROVE_PATH}?id={request_id}"

    def __call__(self, request: Request) -> Mapping[str, object]:
        if request.op is Op.INIT:
            raise UsageError(f"through the service, the store is created on the approving device: "
                             f"{self.url}{ENROLL_PATH}")
        if not self.requester:
            raise UsageError("service mode needs --requester or KEYGUARD_PASSKEY_REQUESTER: the id this machine "
                             "signs as, one the service lists")
        asking = client.caller(request.session, request.title)
        if request.op is Op.GET:
            return self._reveal(list(request.names), asking)
        if request.op is Op.LIST:
            return self._list(asking)
        if len(request.names) != 1:
            raise UsageError("the service changes one name per approval")
        if request.op is Op.SET:
            return self._propose(request.names[0], request.values[request.names[0]], asking)
        return self._remove(request.names[0], asking)

    def held(self, request: Request) -> None:
        """A request the hold answered whole settles the session's expired ones, as an approval would."""
        session = client.caller(request.session, request.title).session
        if session:
            pending.clear_expired(self.pending_dir, session)

    def _hint(self) -> NotifyHint:
        """Where the owner sits, for the notices of this request: unknown without a presence folder to read."""
        presence = read_presence(self.presence_dir, self.now()) if self.presence_dir else Presence.UNKNOWN
        return NotifyHint(presence=presence, container=clean_container(self.container))

    @property
    def _signer(self) -> Signer:
        return Signer(key=self.signing_key, host=self.requester)

    def _reveal(self, names: list[str], asking: Caller) -> Mapping[str, object]:
        ephemeral = client.generate_ephemeral()
        response = self._answer(client.request_for(self._signer, names, ephemeral.recipient, asking),
                                names, Op.GET, asking)
        try:
            return {"values": client.decrypt_response(response, ephemeral.identity, names)}
        except client.NotInStore as absent:
            return {"absent": absent.names}

    def _list(self, asking: Caller) -> Mapping[str, object]:
        ephemeral = client.generate_ephemeral()
        response = self._answer(client.list_request_for(self._signer, ephemeral.recipient, asking), [], Op.LIST, asking)
        return {"names": client.decrypt_names(response, ephemeral.identity)}

    def _propose(self, name: str, value: str, asking: Caller) -> Mapping[str, object]:
        if not self.remote_recipient.startswith(AGE_RECIPIENT_PREFIX):
            raise UsageError("setting through the service needs the approving device's age1... recipient: "
                             "--remote-recipient or KEYGUARD_PASSKEY_REMOTE_RECIPIENT")
        ephemeral = client.generate_ephemeral()
        request = client.set_request_for(self._signer, name, value, ephemeral.recipient, self.remote_recipient, asking)
        client.decrypt_ack(self._answer(request, [name], Op.SET, asking), ephemeral.identity, client.SET_OP, name)
        return {"written": [name]}

    def _remove(self, name: str, asking: Caller) -> Mapping[str, object]:
        ephemeral = client.generate_ephemeral()
        response = self._answer(client.rm_request_for(self._signer, name, ephemeral.recipient, asking),
                                [name], Op.RM, asking)
        try:
            client.decrypt_ack(response, ephemeral.identity, client.RM_OP, name)
        except client.NotInStore as absent:
            return {"absent": absent.names}
        return {}

    def _answer(self, request: client.UnlockRequest, names: list[str], op: Op, asking: Caller) -> str:
        with sigterm_cancels() as terminated:
            request_id = client.submit(self.url, self.headers, request, self._hint())
            record = pending.Request(id=request_id, vars=tuple(names), op=op.value, host=self.requester,
                                     link=self.approve_link(request_id), session=asking.session or None,
                                     title=asking.title or None, pane=self.pane)
            return self._wait(record, terminated)

    def _wait(self, record: pending.Request, terminated: Callable[[], bool]) -> str:
        with pending.announced(record, self.pending_dir, expired=(client.UnlockTimeout, client.RequestGone),
                               cancelled=(client.Cancelled, KeyboardInterrupt), stderr=self.stderr) as marked:
            print(f"Approve on your phone: {record.link}", file=self.stderr, flush=True)
            try:
                return client.poll(self.url, self.headers, record.id, timeout=self.timeout, interval=self.interval,
                                   cancelled=lambda: self.cancelled() or terminated() or marked())
            except (client.Cancelled, KeyboardInterrupt):
                self._withdraw(record.id)
                raise

    def _withdraw(self, request_id: str) -> None:
        try:
            client.withdraw(self.url, self.headers, request_id)
        except UnlockError as error:
            print(f"keyguard-passkey: the cancelled request can still be answered until it expires: {error}",
                  file=self.stderr)
