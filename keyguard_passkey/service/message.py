"""A message the owner signs on the approving device, and the session it is for."""
from __future__ import annotations

import base64
import binascii
import json
import re
import unicodedata
from dataclasses import dataclass
from typing import Final

SIGN_OP: Final = "sign"
OWNER_KEY_NAME: Final = "KEYGUARD_OWNER_SIGNING_KEY"
TOKEN: Final = re.compile(r"([A-Za-z0-9_-]+)\.[A-Za-z0-9_-]+")
TOKEN_VERSION: Final = 1
MAX_MESSAGE_CHARS: Final = 1000
LINE_BREAK: Final = "\n"
HIDDEN_MESSAGE_CATEGORIES: Final = frozenset({"Cc", "Cf", "Co", "Cs", "Zl", "Zp"})
UUID: Final = "[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
TARGET: Final = re.compile(rf"{UUID}|ses_[0-9A-Za-z]{{20,40}}|agy_{UUID}")
BOX: Final = re.compile(r"[0-9A-Za-z-]{1,63}")
MESSAGE_FIELDS: Final = ("text", "target", "target_box", "target_title")
OWNER_KEY_REFUSED: Final = f"{OWNER_KEY_NAME} is never released, written or removed: it only signs a message you approve"


def is_message_text(text: object) -> bool:
    return (isinstance(text, str) and 0 < len(text) <= MAX_MESSAGE_CHARS
            and not any(char != LINE_BREAK and unicodedata.category(char) in HIDDEN_MESSAGE_CATEGORIES
                        for char in text))


def is_target(target: object) -> bool:
    return isinstance(target, str) and TARGET.fullmatch(target) is not None


def is_box(box: object) -> bool:
    return isinstance(box, str) and BOX.fullmatch(box) is not None


@dataclass(frozen=True)
class OwnerMessage:
    text: str
    target: str
    box: str
    title: str = ""

    def fields(self) -> dict[str, str]:
        found = {"text": self.text, "target": self.target, "target_box": self.box, "target_title": self.title}
        return {name: value for name, value in found.items() if value}

    def addressee(self) -> dict[str, str]:
        return {"conversation": self.target, "box": self.box}

    def problem(self) -> str | None:
        if not is_message_text(self.text):
            return (f"the message must be 1 to {MAX_MESSAGE_CHARS} characters, with line breaks but no other "
                    "control, format or separator characters")
        if not is_target(self.target):
            return "the target must be a full conversation id: a Claude Code UUID, an opencode ses_ id or agy_<uuid>"
        if not is_box(self.box):
            return "the box must be 1 to 63 letters, digits or hyphens"
        return None


def token_payload(token: str) -> dict[str, object] | None:
    """The signed payload of a token, unchecked: the signature is for the session that acts on it to check."""
    found = TOKEN.fullmatch(token)
    if found is None:
        return None
    encoded = found.group(1)
    try:
        payload = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
    except (binascii.Error, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def carries(token: str, message: OwnerMessage) -> bool:
    payload = token_payload(token)
    return (payload is not None and payload.get("v") == TOKEN_VERSION and payload.get("text") == message.text
            and payload.get("to") == message.addressee())
