"""The unlock service's settings, read from KEYGUARD_PASSKEY_* environment variables."""
from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

PREFIX: Final = "KEYGUARD_PASSKEY_"
DEFAULT_BIND: Final = "127.0.0.1"
DEFAULT_PORT: Final = 8080
DEFAULT_IDENTITY_HEADER: Final = "Tailscale-User-Login"
DEFAULT_REQUEST_RATE_LIMIT: Final = 30
DEFAULT_TOKEN_LABEL: Final = "token client"
DEFAULT_IDENTITY_LABEL: Final = "signed-in device"
REQUESTER_ID: Final = re.compile(r"[a-z0-9-]{1,32}")
HEADER_NAME: Final = re.compile(r"[A-Za-z0-9-]{1,64}")
LIST_SEPARATOR: Final = ","
PAIR_SEPARATOR: Final = "="


class SettingsError(ValueError):
    pass


@dataclass(frozen=True)
class Requester:
    """A machine that may ask, checked on the approving device against the key pinned for its id."""
    id: str
    label: str


@dataclass(frozen=True)
class Settings:
    store: Path
    bind: str = DEFAULT_BIND
    port: int = DEFAULT_PORT
    identity_header: str = DEFAULT_IDENTITY_HEADER
    allowed_logins: frozenset[str] = frozenset()
    request_token: str = ""
    request_rate_limit: int = DEFAULT_REQUEST_RATE_LIMIT
    requesters: tuple[Requester, ...] = ()
    token_label: str = DEFAULT_TOKEN_LABEL
    identity_label: str = DEFAULT_IDENTITY_LABEL
    telegram_token: str = ""
    telegram_chat: str = ""
    heartbeat_url: str = ""

    def public_config(self) -> dict[str, object]:
        return {"requesters": {requester.id: {"label": requester.label} for requester in self.requesters}}


def _setting(env: Mapping[str, str], name: str, default: str = "") -> str:
    return env.get(PREFIX + name, default).strip()


def _whole_number(env: Mapping[str, str], name: str, default: int) -> int:
    text = _setting(env, name, str(default))
    try:
        return int(text)
    except ValueError:
        raise SettingsError(f"{PREFIX}{name} is not a whole number: {text!r}") from None


def _items(text: str) -> list[str]:
    return [item.strip() for item in text.split(LIST_SEPARATOR) if item.strip()]


def parse_logins(text: str) -> frozenset[str]:
    return frozenset(_items(text))


def parse_requesters(text: str) -> tuple[Requester, ...]:
    """`devbox=dev box,mac=Mac`: an id for the pinned key, and the name the approving device shows."""
    requesters = []
    for item in _items(text):
        requester_id, separator, label = item.partition(PAIR_SEPARATOR)
        requester_id, label = requester_id.strip(), label.strip()
        if not separator or not REQUESTER_ID.fullmatch(requester_id) or not label:
            raise SettingsError(f"{PREFIX}REQUESTERS needs id=label pairs with ids of a-z, 0-9 and -: {item!r}")
        requesters.append(Requester(requester_id, label))
    if len({requester.id for requester in requesters}) != len(requesters):
        raise SettingsError(f"{PREFIX}REQUESTERS names an id twice")
    return tuple(requesters)


def settings_from_env(env: Mapping[str, str], store: Path) -> Settings:
    header = _setting(env, "IDENTITY_HEADER", DEFAULT_IDENTITY_HEADER)
    if not HEADER_NAME.fullmatch(header):
        raise SettingsError(f"{PREFIX}IDENTITY_HEADER is not a header name: {header!r}")
    return Settings(
        store=store,
        bind=_setting(env, "BIND", DEFAULT_BIND),
        port=_whole_number(env, "PORT", DEFAULT_PORT),
        identity_header=header,
        allowed_logins=parse_logins(_setting(env, "ALLOWED_LOGINS")),
        request_token=_setting(env, "REQUEST_TOKEN"),
        request_rate_limit=_whole_number(env, "REQUEST_RATE_LIMIT", DEFAULT_REQUEST_RATE_LIMIT),
        requesters=parse_requesters(_setting(env, "REQUESTERS")),
        token_label=_setting(env, "TOKEN_LABEL", DEFAULT_TOKEN_LABEL),
        identity_label=_setting(env, "IDENTITY_LABEL", DEFAULT_IDENTITY_LABEL),
        telegram_token=_setting(env, "TELEGRAM_BOT_TOKEN"),
        telegram_chat=_setting(env, "TELEGRAM_CHAT_ID"),
        heartbeat_url=_setting(env, "HEARTBEAT_URL"),
    )
