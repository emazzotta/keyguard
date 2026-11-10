"""Tests for the phone fallback raced against the Touch ID prompt, with stand-in processes for both sides."""
from __future__ import annotations

import time
from pathlib import Path

import pytest
from conftest import http_get

from keyguard_server import keyguard_cli, phone
from keyguard_server.caller import Caller
from keyguard_server.keyguard_cli import CliResult

FAST = phone.Timing(delay=0.3, deadline=5.0, touch_id_timeout=5.0, stop_grace=2.0)
NOBODY = Caller()
WAIT_FOR_SIGNAL = "sleep 30 & wait"
EXPORTS_A_B = "printf \"export A='1'\\nexport B='two words'\\n\""


def script(path: Path, body: str) -> Path:
    path.write_text(f"#!/bin/sh\n{body}\n")
    path.chmod(0o755)
    return path


def logged(events: Path, text: str, seconds: float = 3.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if events.exists() and text in events.read_text():
            return True
        time.sleep(0.05)
    return False


@pytest.fixture()
def events(tmp_path) -> Path:
    return tmp_path / "events"


@pytest.fixture()
def touch_id(tmp_path, monkeypatch):
    def install(body: str) -> None:
        monkeypatch.setattr(keyguard_cli, "KEYGUARD_BIN", script(tmp_path / "keyguard", body))
    return install


@pytest.fixture()
def client(tmp_path):
    def install(body: str) -> phone.PhoneClient:
        return phone.PhoneClient(client=script(tmp_path / "unlock.py", body), signing_key=tmp_path / "mac-sign.key")
    return install


# ---- The race ----


def test_should_answer_from_touch_id_without_asking_the_phone_when_touch_id_answers_first(touch_id, client, events):
    touch_id("printf secret")

    result = phone.race(["A"], NOBODY, client(f"echo started >> {events}"), timing=FAST)

    assert (result.ok, result.stdout) == (True, "secret")
    time.sleep(FAST.delay * 2)
    assert not events.exists()


def test_should_end_the_read_and_withdraw_the_phone_request_when_touch_id_is_cancelled(touch_id, client, events):
    touch_id("sleep 1; exit 2")
    asking = client(f"echo started >> {events}; trap 'echo withdrawn >> {events}; exit 1' TERM; {WAIT_FOR_SIGNAL}")

    result = phone.race(["A"], NOBODY, asking, timing=FAST)

    assert result.touch_id_cancelled
    assert logged(events, "started") and logged(events, "withdrawn")


def test_should_answer_from_the_phone_and_close_the_touch_id_prompt(touch_id, client, events):
    touch_id(f"trap 'echo dismissed >> {events}; exit 143' TERM; {WAIT_FOR_SIGNAL}")

    result = phone.race(["A", "B"], NOBODY, client(EXPORTS_A_B), timing=FAST)

    assert (result.ok, result.stdout) == (True, "A=1\nB=two words\n")
    assert logged(events, "dismissed")


def test_should_answer_a_single_key_with_its_bare_value_as_keyguard_does(touch_id, client):
    touch_id(WAIT_FOR_SIGNAL)

    result = phone.race(["A"], NOBODY, client("printf \"export A='multi\\nline'\\n\""), timing=FAST)

    assert (result.ok, result.stdout) == (True, "multi\nline")


def test_should_keep_waiting_on_touch_id_when_the_phone_client_fails(touch_id, client):
    touch_id("sleep 1; printf secret")

    result = phone.race(["A"], NOBODY, client("echo 'unlock: openssl: unknown option -rawin' >&2; exit 1"), timing=FAST)

    assert (result.ok, result.stdout) == (True, "secret")


@pytest.mark.parametrize("ending", [
    "echo 'Biometrics unavailable: no fingerprint sensor' >&2; exit 1",
    "echo 'Authentication failed: Authentication canceled.' >&2; exit 3",
])
def test_should_ask_the_phone_at_once_when_touch_id_ends_without_the_owners_cancel(touch_id, client, ending):
    touch_id(ending)
    started = time.monotonic()

    result = phone.race(["A", "B"], NOBODY, client(EXPORTS_A_B), timing=phone.Timing(delay=30.0, deadline=40.0))

    assert result.ok
    assert time.monotonic() - started < 5


def test_should_keep_waiting_on_the_phone_after_the_touch_id_prompt_runs_out(touch_id, client):
    touch_id(WAIT_FOR_SIGNAL)
    timing = phone.Timing(delay=10.0, deadline=10.0, touch_id_timeout=0.5, stop_grace=2.0)

    result = phone.race(["A", "B"], NOBODY, client(f"sleep 1; {EXPORTS_A_B}"), timing=timing)

    assert (result.ok, result.stdout) == (True, "A=1\nB=two words\n")


def test_should_report_both_errors_when_touch_id_and_the_phone_fail(touch_id, client):
    touch_id("echo 'Biometrics unavailable' >&2; exit 1")

    result = phone.race(["A"], NOBODY, client("echo 'unlock: request gone' >&2; exit 1"), timing=FAST)

    assert result.rc == 1
    assert "Biometrics unavailable" in result.stderr and "unlock: request gone" in result.stderr


def test_should_give_up_when_neither_side_answers_by_the_deadline(touch_id, client):
    touch_id(WAIT_FOR_SIGNAL)
    started = time.monotonic()

    result = phone.race(["A"], NOBODY, client(WAIT_FOR_SIGNAL), timing=phone.Timing(delay=0.1, deadline=1.0, touch_id_timeout=5.0))

    assert result.timed_out
    assert time.monotonic() - started < 4


def test_should_keep_waiting_on_touch_id_when_the_phone_client_cannot_start(touch_id, tmp_path):
    touch_id("sleep 0.6; printf secret")
    missing = phone.PhoneClient(client=tmp_path / "no-such-unlock.py", signing_key=tmp_path / "mac-sign.key")

    result = phone.race(["A"], NOBODY, missing, timing=FAST)

    assert (result.ok, result.stdout) == (True, "secret")


# ---- The client's command line and answer ----


def test_should_ask_as_the_mac_with_the_caller_and_the_names_after_a_double_dash(tmp_path):
    asking = phone.PhoneClient(client=tmp_path / "unlock.py", signing_key=tmp_path / "mac-sign.key")

    argv = asking.argv(["A", "-B"], Caller(session="48f92bcb", title="Fix the build"))

    assert argv == [str(tmp_path / "unlock.py"), "--host", "mac", "--signing-key", str(tmp_path / "mac-sign.key"),
                    "--hold-seconds", "0", "--session", "48f92bcb", "--title", "Fix the build", "--", "A", "-B"]


def test_should_pass_an_empty_caller_so_the_client_never_guesses_one(tmp_path):
    asking = phone.PhoneClient(client=tmp_path / "unlock.py", signing_key=tmp_path / "mac-sign.key")

    argv = asking.argv(["A"], NOBODY)

    assert argv[argv.index("--session") + 1] == "" and argv[argv.index("--title") + 1] == ""


def test_should_find_openssl_3_before_any_other_openssl():
    assert phone.client_env()["PATH"].split(":")[0] == "/opt/homebrew/opt/openssl@3/bin"


def test_should_read_the_values_from_the_client_exports():
    exports = "export A='1'\nexport B='it'\"'\"'s\nmulti'\nexport C='Caf\u00e9'\n"

    assert phone.parse_exports(exports, ["A", "B", "C"]) == {"A": "1", "B": "it's\nmulti", "C": "Caf\u00e9"}


@pytest.mark.parametrize("stdout", [
    "export A='1'\n",
    "export A='1'\nexport B='2'\nexport C='3'\n",
    "export A='1'\nexport D='2'\n",
    "declare A='1'\nexport B='2'\n",
    "export A\nexport B='2'\n",
    "export A='unterminated\n",
    "",
])
def test_should_refuse_an_answer_that_does_not_name_exactly_the_keys(stdout):
    assert phone.parse_exports(stdout, ["A", "B"]) is None


# ---- Configuration ----


def test_should_stay_off_without_a_client_or_a_signing_key(tmp_path):
    key = script(tmp_path / "mac-sign.key", "")

    assert phone.configured(None, key) is None
    assert phone.configured(tmp_path / "unlock.py", None) is None


def test_should_stay_off_while_the_client_or_the_key_is_missing(tmp_path):
    assert phone.configured(tmp_path / "unlock.py", tmp_path / "mac-sign.key") is None


def test_should_turn_on_once_the_client_and_the_key_are_installed(tmp_path):
    asking, key = script(tmp_path / "unlock.py", ""), script(tmp_path / "mac-sign.key", "")

    assert phone.configured(asking, key) == phone.PhoneClient(client=asking, signing_key=key)


# ---- The handler ----


@pytest.mark.parametrize("path", ["/A,B", "/A,B?timeout=60"])
def test_should_race_the_phone_on_every_read_once_configured(server, monkeypatch, tmp_path, path):
    asking = phone.PhoneClient(client=tmp_path / "unlock.py", signing_key=tmp_path / "mac-sign.key")
    seen = {}
    monkeypatch.setattr(phone, "configured", lambda: asking)

    def race(keys, caller, client, cache_duration=None):
        seen.update(keys=keys, caller=caller, client=client)
        return CliResult(rc=0, stdout="A=1\nB=2\n", stderr="")

    monkeypatch.setattr(phone, "race", race)

    status, body = http_get(server, path, {"X-Keyguard-Session": "48f92bcb"})

    assert (status, body) == (200, "A=1\nB=2\n")
    assert seen == {"keys": ["A", "B"], "caller": Caller(session="48f92bcb"), "client": asking}
