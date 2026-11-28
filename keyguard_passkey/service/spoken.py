"""Hand each answer that Telegram took to mazzotta-bot, which speaks it when the owner is away or asked by voice.

Off unless both the bot's URL and the token that the two services share are set. The bot decides whether to speak.
"""
from __future__ import annotations

import json
import sys
import urllib.request
from dataclasses import dataclass
from typing import Final

from .notify import ClaudeNotice, Send, describe_failure, shown_title

RELAY_TIMEOUT_SECONDS: Final = 10


def _post(request: urllib.request.Request) -> None:
    with urllib.request.urlopen(request, timeout=RELAY_TIMEOUT_SECONDS) as response:
        response.read()


@dataclass(frozen=True)
class SpokenAnswerRelay:
    url: str
    token: str
    send: Send = _post

    def relay(self, notice: ClaudeNotice, reply_to: int) -> None:
        body = json.dumps({"session": notice.session, "text": notice.text, "presence": notice.presence.value,
                           "seen": notice.seen, "reply_to": reply_to,
                           "title": shown_title(notice.title) or None}).encode()
        request = urllib.request.Request(self.url, data=body, method="POST", headers={
            "Content-Type": "application/json", "Authorization": f"Bearer {self.token}"})
        try:
            self.send(request)
        except (OSError, ValueError) as error:
            print(f"spoken answer: not sent ({describe_failure(error)})", file=sys.stderr, flush=True)


def spoken_answer_relay(url: str, token: str) -> SpokenAnswerRelay | None:
    return SpokenAnswerRelay(url, token) if url and token else None
