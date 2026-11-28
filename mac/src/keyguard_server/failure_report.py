"""One Telegram line when a secret read fails and the unlock service cannot report it.

The unlock service posts the `❌🔐` line itself when a phone request that reached it expires, and it does so before
this server's own deadline. This server posts only for a read with no phone request behind it. The line goes to the
service's `POST /notify` as kind `keyguard_failed`, which adds the mark. It counts variables and never names one,
and a failed post is logged without its text, never retried and never changes the answer to the read.
"""
from __future__ import annotations

import http.client
import json
import sys
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass
from enum import Enum
from typing import Final
from urllib.parse import urlsplit

from .config import PHONE_SERVICE

NOTICE_PATH: Final = "/notify"
NOTICE_KIND: Final = "keyguard_failed"
NOTICE_TITLE: Final = "Mac keyguard"
SEND_TIMEOUT: Final = 10
WEB_SCHEMES: Final = frozenset({"http", "https"})


class Reason(Enum):
    PHONE_OFF = "the phone fallback is off"
    NOT_REACHED = "the phone request never reached the unlock service"


@dataclass(frozen=True)
class ReadFailure:
    reason: Reason
    variables: int
    session: str | None = None

    @property
    def text(self) -> str:
        noun = "variable" if self.variables == 1 else "variables"
        return f"Keyguard read of {self.variables} {noun} failed at the Mac, and {self.reason.value}."

    def notice(self) -> dict[str, str]:
        body = {"kind": NOTICE_KIND, "title": NOTICE_TITLE, "text": self.text}
        return {**body, "session": self.session} if self.session else body


def report(failure: ReadFailure) -> None:
    """Posts from a background thread: the read has ended, and its answer must not wait for the service."""
    service = PHONE_SERVICE
    if not service or failure.variables < 1:
        return
    try:
        threading.Thread(target=send, args=(failure, service), daemon=True).start()
    except RuntimeError as error:
        _log_not_sent(type(error).__name__)


def send(failure: ReadFailure, service: str) -> None:
    """One post to the unlock service, which takes the Mac's tailnet identity in place of a token."""
    try:
        if urlsplit(service).scheme not in WEB_SCHEMES:
            _log_not_sent("KEYGUARD_STORE_URL is not an http or https address")
            return
        request = urllib.request.Request(f"{service.rstrip('/')}{NOTICE_PATH}",
                                         data=json.dumps(failure.notice()).encode(),
                                         headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(request, timeout=SEND_TIMEOUT) as response:
            response.read()
    except (OSError, ValueError, http.client.HTTPException) as error:
        _log_not_sent(_describe(error))


def _describe(error: Exception) -> str:
    return f"HTTP {error.code}" if isinstance(error, urllib.error.HTTPError) else type(error).__name__


def _log_not_sent(why: str) -> None:
    """The reason only: the text names how many variables a read asked for, and the log is not the place for it."""
    print(f"[keyguard] failure report not sent ({why})", file=sys.stderr)
