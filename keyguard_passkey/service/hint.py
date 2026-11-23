"""The `notify` field of POST /requests: what the notices of a request need to know about its requester.

It stays outside the signed body and outside both views of a request, and it never costs a request. No field, or a
field that is not an object, means an old client: there is no hint at all. A hint with a wrong value reads as `unknown`
presence and no container.
"""
from __future__ import annotations

from dataclasses import dataclass

from .notify import SAFE_HOST
from .presence import Presence

HINT_FIELD = "notify"


def clean_container(label: object) -> str | None:
    return label if isinstance(label, str) and SAFE_HOST.fullmatch(label) else None


@dataclass(frozen=True)
class NotifyHint:
    presence: Presence = Presence.UNKNOWN
    container: str | None = None

    def wire(self) -> dict[str, str]:
        fields = {"presence": self.presence.value}
        if self.container:
            fields["container"] = self.container
        return fields


def parse_hint(raw: object) -> NotifyHint | None:
    if not isinstance(raw, dict):
        return None
    return NotifyHint(presence=_presence(raw.get("presence")), container=clean_container(raw.get("container")))


def _presence(value: object) -> Presence:
    try:
        return Presence(value)
    except ValueError:
        return Presence.UNKNOWN
