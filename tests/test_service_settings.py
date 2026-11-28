from __future__ import annotations

from pathlib import Path

import pytest

from keyguard_passkey.service.settings import (
    DEFAULT_IDENTITY_HEADER,
    SPOKEN_ANSWER_TOKEN_VARIABLE,
    Requester,
    Settings,
    SettingsError,
    parse_logins,
    parse_origin,
    parse_requesters,
    parse_service_url,
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
        "KEYGUARD_PASSKEY_PUBLIC_URL": "https://unlock.example.ts.net/",
        "KEYGUARD_PASSKEY_SPOKEN_ANSWER_URL": "http://telegram-bot.telegram-bot:8080/spoken-answer",
        SPOKEN_ANSWER_TOKEN_VARIABLE: " spoken ",
    }

    assert settings_from_env(env, STORE) == Settings(
        store=STORE, bind="0.0.0.0", port=9090, identity_header="X-Forwarded-User",
        allowed_logins=frozenset({"me@example.com"}), request_token="token", request_rate_limit=5,
        requesters=(Requester("devbox", "dev box"), Requester("mac", "Mac")), token_label="dev box",
        identity_label="Mac", telegram_token="bot", telegram_chat="42", heartbeat_url="https://monitor.example/push",
        terminal_url="https://terminal.example.ts.net:8443", public_url="https://unlock.example.ts.net",
        spoken_answer_url="http://telegram-bot.telegram-bot:8080/spoken-answer", spoken_answer_token="spoken")


@pytest.mark.spec("docs/service.md")
def test_should_read_the_spoken_answer_token_under_the_name_the_bot_shares():
    assert SPOKEN_ANSWER_TOKEN_VARIABLE == "UNLOCK_TELEGRAM_BOT_SPOKEN_ANSWER_TOKEN"
    assert settings_from_env({"KEYGUARD_PASSKEY_" + SPOKEN_ANSWER_TOKEN_VARIABLE: "x"}, STORE).spoken_answer_token == ""


@pytest.mark.spec("docs/service.md")
@pytest.mark.parametrize("text", [
    "", "http://telegram-bot:8080/spoken-answer", "https://bot.example/spoken-answer", "http://10.0.0.7:8080/",
    "http://bot.example",
])
def test_should_read_a_service_url_or_none(text):
    assert parse_service_url(text, "SPOKEN_ANSWER_URL") == text


@pytest.mark.spec("docs/service.md")
@pytest.mark.parametrize("text", [
    "telegram-bot:8080/spoken-answer", "ftp://bot.example/x", "http://", "http://bot.example/x?y=1",
    "http://user@bot.example/x", "http://bot example/x", 'http://bot.example/x"', "http://bot.example:port/x",
    "http://bot.example/x#y", "http://" + "a" * 254,
])
def test_should_refuse_a_service_url_with_more_than_scheme_host_port_and_path(text):
    with pytest.raises(SettingsError, match="KEYGUARD_PASSKEY_SPOKEN_ANSWER_URL"):
        settings_from_env({"KEYGUARD_PASSKEY_SPOKEN_ANSWER_URL": text}, STORE)


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
def test_should_read_an_origin_or_none(text, origin):
    assert parse_origin(text, "TERMINAL_URL") == origin


@pytest.mark.spec("docs/service.md")
@pytest.mark.parametrize("text", [
    "http://terminal.example", "terminal.example", "https://", "https://terminal.example/app",
    "https://terminal.example?x=1", "https://user@terminal.example", 'https://terminal.example"', "https://a b.example",
    "https://terminal.example:port", "https://" + "a" * 254,
])
def test_should_refuse_an_address_that_is_not_a_bare_https_origin(text):
    with pytest.raises(SettingsError, match="KEYGUARD_PASSKEY_TERMINAL_URL"):
        parse_origin(text, "TERMINAL_URL")


@pytest.mark.spec("docs/service.md")
@pytest.mark.parametrize("name", ["TERMINAL_URL", "PUBLIC_URL"])
def test_should_refuse_a_malformed_origin_setting_by_its_name(name):
    with pytest.raises(SettingsError, match=f"KEYGUARD_PASSKEY_{name}"):
        settings_from_env({f"KEYGUARD_PASSKEY_{name}": "http://insecure.example"}, STORE)
