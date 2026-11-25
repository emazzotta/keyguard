"""Tells the owner's chat about unlock requests: a wait notice while the owner may not see one, and a failure line when
one expires with no answer. A request from an old client sends no hint, so it gets no wait notice: the service cannot
tell whether the owner sits at a screen.

Both go through the notifier of the session pings. The text names the requester by the label in the settings and counts
the variables without naming them. The link comes from the settings and the request's id, never from the request.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Final

from .notify import ClaudeNotice, NoticeKind, Notifier
from .presence import Presence
from .requests import UnlockRequest

SEEN_PRESENCE: Final = frozenset({Presence.COMPUTER, Presence.PHONE})
APPROVE_PATH: Final = "/approve?id="
UNLISTED_REQUESTER: Final = "an unlisted machine"
KEYGUARD_ORIGIN: Final = "unlock service"
ACTIONS: Final = {"set": "set", "rm": "remove"}
WHOLE_ACTIONS: Final = {"list": "list the variable names", "sign": "sign a message"}


class KeyguardAnnouncer:
    """The listener of the request queue."""

    def __init__(self, notifier: Notifier, labels: Mapping[str, str], public_url: str = "") -> None:
        self._notifier = notifier
        self._labels = labels
        self._public_url = public_url

    def created(self, request: UnlockRequest) -> None:
        if request.hint is None or request.hint.presence in SEEN_PRESENCE:
            return
        link = f"{self._public_url}{APPROVE_PATH}{request.id}" if self._public_url else None
        self._post(request, NoticeKind.KEYGUARD_WAIT, f"Waiting for your approval: {self._what(request, 'asks')}.", link)

    def expired(self, request: UnlockRequest) -> None:
        self._post(request, NoticeKind.KEYGUARD_FAILED,
                   f"Request expired with no answer: {self._what(request, 'asked')}.")

    def _post(self, request: UnlockRequest, kind: NoticeKind, text: str, link: str | None = None) -> None:
        container = request.hint.container if request.hint else None
        self._notifier.claude_notice(ClaudeNotice(
            session=request.session, container=container, text=text, origin=KEYGUARD_ORIGIN, kind=kind,
            title=request.title, link=link))

    def _what(self, request: UnlockRequest, verb: str) -> str:
        # The queue keeps host and vars as posted and the phone checks them, so a wrong type must not stop a notice.
        who = self._labels.get(request.host, UNLISTED_REQUESTER) if isinstance(request.host, str) else UNLISTED_REQUESTER
        return f"{who} {verb} to {_action(request)}"


def _action(request: UnlockRequest) -> str:
    if request.op in WHOLE_ACTIONS:
        return WHOLE_ACTIONS[request.op]
    count = len(request.vars) if isinstance(request.vars, list) else 0
    noun = "variable" if count == 1 else "variables"
    return f"{ACTIONS.get(request.op or '', 'read')} {count} {noun}"
