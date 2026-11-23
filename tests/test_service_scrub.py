from __future__ import annotations

import pytest

from keyguard_passkey.service.scrub import REDACTED, scrub

BOT_ID = "987654321"
FAKE_SECRET = "AAFakeScrubberTestSecret01234567-_x"
FAKE_TOKEN = f"{BOT_ID}:{FAKE_SECRET}"


@pytest.mark.spec("docs/service.md")
def test_should_use_a_fake_secret_with_the_length_of_a_real_one():
    assert len(FAKE_SECRET) == 35


@pytest.mark.spec("docs/service.md")
@pytest.mark.parametrize("text", [
    f"/bot{FAKE_TOKEN}/sendMessage",
    f"/bot{BOT_ID}:short/getMe",
    f"token {FAKE_TOKEN} was rejected",
    f"token={FAKE_TOKEN}",
])
def test_should_replace_a_bot_token_wherever_it_appears(text):
    scrubbed = scrub(text)

    assert FAKE_SECRET not in scrubbed
    assert BOT_ID not in scrubbed
    assert REDACTED in scrubbed


@pytest.mark.spec("docs/service.md")
def test_should_replace_the_whole_bot_segment_of_a_request_path():
    assert scrub(f"url: /bot{FAKE_TOKEN}/sendMessage") == f"url: /{REDACTED}/sendMessage"


@pytest.mark.spec("docs/service.md")
def test_should_replace_each_token_of_a_text():
    assert scrub(f"{FAKE_TOKEN} and {FAKE_TOKEN}") == f"{REDACTED} and {REDACTED}"


@pytest.mark.spec("docs/service.md")
@pytest.mark.parametrize("text", [
    "Sending help to 42",
    "Unknown command in 1234567",
    "started at 12:30:45",
    '{"update_id": 17, "sender_id": 42}',
    "https://api.telegram.org/file/other",
    "line one\nline two",
    "",
])
def test_should_leave_text_without_a_token_alone(text):
    assert scrub(text) == text
