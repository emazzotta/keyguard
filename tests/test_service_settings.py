from __future__ import annotations

from pathlib import Path

import pytest

from keyguard_passkey.service.settings import (
    DEFAULT_IDENTITY_HEADER,
    Requester,
    Settings,
    SettingsError,
    parse_logins,
    parse_requesters,
    parse_terminal_url,
    settings_from_env,
)

STORE = Path("/srv/store")


@pytest.mark.spec("docs/service.md")
def test_should_default_to_loopback_without_token_bot_or_heartbeat():
    settings = settings_from_env({}, STORE)

    assert settings == Settings(store=STORE)
    assert (settings.bind, settings.identity_header) == ("127.0.0.1", DEFAULT_IDENTITY_HEADER)


@pytest.mark.spec("docs/service.md")
def test_should_read_every_setting_from_its_variable():
    env = {
        "KEYGUARD_PASSKEY_BIND": "0.0.0.0",
        "KEYGUARD_PASSKEY_PORT": "9090",
        "KEYGUARD_PASSKEY_IDENTITY_HEADER": "X-Forwarded-User",
        "KEYGUARD_PASSKEY_ALLOWED_LOGINS": "me@example.com",
        "KEYGUARD_PASSKEY_REQUEST_TOKEN": " token ",
        "KEYGUARD_PASSKEY_REQUEST_RATE_LIMIT": "5",
        "KEYGUARD_PASSKEY_REQUESTERS": "devbox=dev box,mac=Mac",
        "KEYGUARD_PASSKEY_TOKEN_LABEL": "dev box",
        "KEYGUARD_PASSKEY_IDENTITY_LABEL": "Mac",
        "KEYGUARD_PASSKEY_TELEGRAM_BOT_TOKEN": "bot",
        "KEYGUARD_PASSKEY_TELEGRAM_CHAT_ID": "42",
        "KEYGUARD_PASSKEY_HEARTBEAT_URL": "https://monitor.example/push",
        "KEYGUARD_PASSKEY_TERMINAL_URL": "https://terminal.example.ts.net:8443/",
    }

    assert settings_from_env(env, STORE) == Settings(
        store=STORE, bind="0.0.0.0", port=9090, identity_header="X-Forwarded-User",
        allowed_logins=frozenset({"me@example.com"}), request_token="token", request_rate_limit=5,
        requesters=(Requester("devbox", "dev box"), Requester("mac", "Mac")), token_label="dev box",
        identity_label="Mac", telegram_token="bot", telegram_chat="42", heartbeat_url="https://monitor.example/push",
        terminal_url="https://terminal.example.ts.net:8443")


@pytest.mark.spec("docs/service.md")
def test_should_read_the_allowed_logins_from_a_comma_separated_list():
    assert parse_logins(" me@example.com, ,you@example.com ") == frozenset({"me@example.com", "you@example.com"})


@pytest.mark.spec("docs/service.md")
def test_should_publish_only_the_requesters_to_the_pages():
    settings = Settings(store=STORE, request_token="secret", requesters=(Requester("devbox", "dev box"),))

    assert settings.public_config() == {"requesters": {"devbox": {"label": "dev box"}}}


@pytest.mark.spec("docs/service.md")
@pytest.mark.parametrize("text", ["devbox", "devbox=", "=dev box", "Dev Box=x", "a=b,a=c", "box/1=x"])
def test_should_refuse_a_malformed_requester_list(text):
    with pytest.raises(SettingsError):
        parse_requesters(text)


@pytest.mark.spec("docs/service.md")
@pytest.mark.parametrize(("name", "value"), [
    ("PORT", "eighty"), ("REQUEST_RATE_LIMIT", "1.5"), ("IDENTITY_HEADER", "Bad Header"),
])
def test_should_refuse_a_malformed_setting(name, value):
    with pytest.raises(SettingsError, match=f"KEYGUARD_PASSKEY_{name}"):
        settings_from_env({f"KEYGUARD_PASSKEY_{name}": value}, STORE)


@pytest.mark.spec("docs/service.md")
@pytest.mark.parametrize(("text", "origin"), [
    ("", ""),
    ("https://terminal.example", "https://terminal.example"),
    ("https://terminal.example/", "https://terminal.example"),
    ("https://terminal.example:8443", "https://terminal.example:8443"),
])
def test_should_read_the_web_terminal_origin_or_none(text, origin):
    assert parse_terminal_url(text) == origin


@pytest.mark.spec("docs/service.md")
@pytest.mark.parametrize("text", [
    "http://terminal.example", "terminal.example", "https://", "https://terminal.example/app",
    "https://terminal.example?x=1", "https://user@terminal.example", 'https://terminal.example"', "https://a b.example",
    "https://terminal.example:port", "https://" + "a" * 254,
])
def test_should_refuse_a_web_terminal_address_that_is_not_a_bare_https_origin(text):
    with pytest.raises(SettingsError, match="KEYGUARD_PASSKEY_TERMINAL_URL"):
        parse_terminal_url(text)
