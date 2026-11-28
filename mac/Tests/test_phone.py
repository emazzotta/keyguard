"""Tests for the phone fallback raced against the Touch ID prompt, with stand-in processes for both sides."""
from __future__ import annotations

import time
from pathlib import Path

import pytest
from conftest import eventually, http_get

from keyguard_server import failure_report, keyguard_cli, phone, presence
from keyguard_server.caller import Caller
from keyguard_server.failure_report import ReadFailure, Reason
from keyguard_server.keyguard_cli import CliResult
from keyguard_server.presence import Presence

FAST = phone.Timing(delay=0.3, deadline=5.0, touch_id_timeout=5.0, stop_grace=2.0)
NOBODY = Caller()
WAIT_FOR_SIGNAL = "sleep 30 & wait"
EXPORTS_A_B = "printf \"export A='1'\\nexport B='two words'\\n\""
SERVICE = "https://unlock.example.ts.net"
SESSION = Caller(session="48f92bcb")
TOUCH_ID_FAILS = "echo 'Biometrics unavailable: no fingerprint sensor' >&2; exit 1"
LOCKED_SCREEN = "echo 'Authentication failed: Authentication canceled.' >&2; exit 3"
OWNER_CANCELS = "echo 'Authentication failed: User canceled.' >&2; exit 2"
REQUEST_SENT = "echo 'Approve on your phone: https://unlock.example.ts.net/approve?id=7' >&2"
EXPIRED = f"{REQUEST_SENT}; echo 'keyguard-passkey: no answer within 300s' >&2; exit 3"


def script(path: Path, body: str) -> Path:
    path.write_text(f"#!/bin/sh\n{body}\n")
    path.chmod(0o755)
    return path


def logged(events: Path, text: str, seconds: float = 3.0) -> bool:
    return eventually(lambda: events.exists() and text in events.read_text(), seconds)


@pytest.fixture(autouse=True)
def owner_at_a_web_terminal(tmp_path, monkeypatch) -> None:
    """Every race reads the real presence folder unless a test says otherwise: pin it to a page in view. The line
    is dated far ahead, which counts as fresh whatever the clock says."""
    folder = tmp_path / "presence"
    folder.mkdir()
    (folder / "web-terminal").write_text("999999999999 computer\n")
    monkeypatch.setattr(presence, "PRESENCE_DIR", folder)


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
        return phone.PhoneClient(client=script(tmp_path / "keyguard-passkey.pyz", body),
                                 signing_key=tmp_path / "mac-sign.key", service=SERVICE)
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


def test_should_clear_the_sessions_expired_phone_requests_when_touch_id_approves(touch_id, client, events):
    touch_id("printf secret")

    result = phone.race(["A"], SESSION, client(f'echo "$@" >> {events}'), timing=FAST)

    assert (result.ok, result.stdout) == (True, "secret")
    assert logged(events, "pending --clear-expired=48f92bcb")
    assert events.read_text() == "pending --clear-expired=48f92bcb\n"


@pytest.mark.parametrize(("touch_id_ends", "phone_ends", "answered"), [
    (OWNER_CANCELS, "", False),
    (WAIT_FOR_SIGNAL, "printf \"export A='1'\\n\"", True),
], ids=["owner cancels Touch ID", "phone answers"])
def test_should_leave_the_expired_phone_requests_to_the_client_unless_touch_id_approves(
        touch_id, client, events, touch_id_ends, phone_ends, answered):
    touch_id(touch_id_ends)

    result = phone.race(["A"], SESSION, client(f'echo "$@" >> {events}; {phone_ends}'), timing=FAST)

    assert result.ok is answered
    settled()
    assert "--clear-expired" not in (events.read_text() if events.exists() else "")


def test_should_answer_from_touch_id_alike_when_the_client_is_too_old_to_clear(touch_id, client, events, failures,
                                                                               reporting):
    touch_id("printf secret")
    too_old = client(f'echo "$@" >> {events}; echo "keyguard-passkey: error: unrecognized arguments" >&2; exit 2')

    result = phone.race(["A"], SESSION, too_old, timing=FAST, outside=reporting)

    assert (result.ok, result.stdout) == (True, "secret")
    assert logged(events, "--clear-expired")
    settled()
    assert failures == []


def test_should_answer_a_single_key_with_its_bare_value_as_keyguard_does(touch_id, client):
    touch_id(WAIT_FOR_SIGNAL)

    result = phone.race(["A"], NOBODY, client("printf \"export A='multi\\nline'\\n\""), timing=FAST)

    assert (result.ok, result.stdout) == (True, "multi\nline")


def test_should_run_the_client_in_the_pane_the_caller_reports(touch_id, client):
    touch_id(WAIT_FOR_SIGNAL)

    answers_its_pane = client("printf \"export A='%s'\\n\" \"$TMUX_PANE\"")

    result = phone.race(["A"], Caller(pane="%6"), answers_its_pane, timing=FAST)

    assert (result.ok, result.stdout) == (True, "%6")


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
    missing = phone.PhoneClient(client=tmp_path / "no-such-client.pyz", signing_key=tmp_path / "mac-sign.key",
                                service=SERVICE)

    result = phone.race(["A"], NOBODY, missing, timing=FAST)

    assert (result.ok, result.stdout) == (True, "secret")


# ---- Presence ----

DELAY = 2.0
SEEN_AT_ONCE = [Presence.AWAY, Presence.UNKNOWN]
SEEN_AFTER_THE_DELAY = [Presence.COMPUTER, Presence.PHONE]


def sitting(seen: Presence) -> phone.Outside:
    return phone.Outside(presence_reader=lambda: seen)


def timed_race(client: phone.PhoneClient, outside: phone.Outside) -> tuple[CliResult, float]:
    started = time.monotonic()
    result = phone.race(["A", "B"], NOBODY, client, timing=phone.Timing(delay=DELAY, deadline=30.0),
                        outside=outside)
    return result, time.monotonic() - started


@pytest.mark.parametrize("seen", SEEN_AT_ONCE)
def test_should_ask_the_phone_at_once_when_nobody_sits_at_a_web_terminal(touch_id, client, events, seen):
    touch_id(f"echo shown >> {events}; {WAIT_FOR_SIGNAL}")

    result, elapsed = timed_race(client(EXPORTS_A_B), sitting(seen))

    assert result.ok
    assert elapsed < DELAY
    assert logged(events, "shown")


@pytest.mark.parametrize("seen", SEEN_AFTER_THE_DELAY)
def test_should_keep_the_delay_before_asking_the_phone_when_someone_sits_at_a_web_terminal(touch_id, client, seen):
    touch_id(WAIT_FOR_SIGNAL)

    result, elapsed = timed_race(client(EXPORTS_A_B), sitting(seen))

    assert result.ok
    assert elapsed >= DELAY


@pytest.mark.parametrize("seen", SEEN_AFTER_THE_DELAY)
def test_should_not_ask_the_phone_when_touch_id_answers_within_the_delay_while_someone_sits_at_a_web_terminal(
        touch_id, client, events, seen):
    touch_id("sleep 0.5; printf secret")

    result = phone.race(["A"], NOBODY, client(f"echo started >> {events}"),
                        timing=phone.Timing(delay=DELAY, deadline=30.0), outside=sitting(seen))

    assert (result.ok, result.stdout) == (True, "secret")
    time.sleep(0.3)
    assert not events.exists()


def test_should_read_the_presence_once_per_read(touch_id, client):
    touch_id(WAIT_FOR_SIGNAL)
    reads: list[Presence] = []

    def reader() -> Presence:
        reads.append(Presence.AWAY)
        return Presence.AWAY

    phone.race(["A", "B"], NOBODY, client(f"sleep 0.5; {EXPORTS_A_B}"), timing=FAST,
               outside=phone.Outside(presence_reader=reader))

    assert len(reads) == 1


def test_should_read_the_presence_folder_by_default(touch_id, client, tmp_path, monkeypatch):
    touch_id(WAIT_FOR_SIGNAL)
    monkeypatch.setattr(presence, "PRESENCE_DIR", tmp_path / "no-such-folder")
    started = time.monotonic()

    result = phone.race(["A", "B"], NOBODY, client(EXPORTS_A_B),
                        timing=phone.Timing(delay=DELAY, deadline=30.0))

    assert result.ok
    assert time.monotonic() - started < DELAY


def test_should_wait_fifteen_seconds_for_touch_id_by_default():
    assert phone.DEFAULT_TIMING.delay == 15


# ---- Failure reports ----


@pytest.fixture()
def failures() -> list[ReadFailure]:
    return []


@pytest.fixture()
def reporting(failures) -> phone.Outside:
    return phone.Outside(report_failure=failures.append)


def settled(seconds: float = 0.4) -> None:
    time.sleep(seconds)


@pytest.mark.parametrize("phone_ends", [
    "echo 'keyguard-passkey: unlock service unreachable: timed out' >&2; exit 3",
    "echo 'keyguard-passkey: error: signing key unreadable' >&2; exit 64",
    "exit 1",
])
def test_should_report_once_when_touch_id_fails_and_the_phone_request_never_reached_the_service(
        touch_id, client, failures, reporting, phone_ends):
    touch_id(TOUCH_ID_FAILS)

    result = phone.race(["A", "B"], SESSION, client(phone_ends), timing=FAST, outside=reporting)

    assert not result.ok
    assert failures == [ReadFailure(Reason.NOT_REACHED, variables=2, session="48f92bcb")]


def test_should_report_once_when_the_phone_client_cannot_start(touch_id, tmp_path, failures, reporting):
    touch_id(TOUCH_ID_FAILS)
    missing = phone.PhoneClient(client=tmp_path / "no-such-client.pyz", signing_key=tmp_path / "mac-sign.key",
                                service=SERVICE)

    phone.race(["A"], NOBODY, missing, timing=FAST, outside=reporting)

    assert failures == [ReadFailure(Reason.NOT_REACHED, variables=1)]


def test_should_report_once_when_the_phone_fails_before_touch_id_does(touch_id, client, failures, reporting):
    touch_id(f"sleep 0.8; {LOCKED_SCREEN}")

    phone.race(["A"], NOBODY, client("echo 'keyguard-passkey: unlock service unreachable' >&2; exit 3"),
               timing=FAST, outside=reporting)

    assert failures == [ReadFailure(Reason.NOT_REACHED, variables=1)]


@pytest.mark.parametrize("phone_ends", [
    EXPIRED,
    f"{REQUEST_SENT}; echo 'keyguard-passkey: refused in the browser' >&2; exit 2",
    f"{REQUEST_SENT}; echo 'keyguard-passkey: the request was cancelled before it was answered' >&2; exit 2",
    f"{REQUEST_SENT}; echo 'keyguard-passkey: the unlock service no longer holds the request' >&2; exit 3",
    f"{REQUEST_SENT}; echo 'keyguard-passkey: not in the store: A' >&2; exit 4",
])
def test_should_report_nothing_when_the_phone_request_reached_the_service(
        touch_id, client, failures, reporting, phone_ends):
    touch_id(f"sleep 0.8; {LOCKED_SCREEN}")

    result = phone.race(["A"], SESSION, client(phone_ends), timing=FAST, outside=reporting)

    assert not result.ok
    settled()
    assert failures == []


def test_should_report_nothing_at_the_deadline_when_the_phone_request_reached_the_service(
        touch_id, client, failures, reporting):
    """The service's 300 s expiry comes before the server's 345 s deadline, so it has reported that request."""
    touch_id(WAIT_FOR_SIGNAL)
    timing = phone.Timing(delay=0.1, deadline=1.0, touch_id_timeout=5.0, stop_grace=2.0)

    result = phone.race(["A"], SESSION, client(f"{REQUEST_SENT}; {WAIT_FOR_SIGNAL}"), timing=timing, outside=reporting)

    assert result.timed_out
    settled()
    assert failures == []


def test_should_report_nothing_when_touch_id_approves(touch_id, client, failures, reporting):
    touch_id("printf secret")

    result = phone.race(["A"], SESSION, client("exit 1"), timing=FAST, outside=reporting)

    assert result.ok
    settled()
    assert failures == []


def test_should_report_nothing_when_the_phone_approves(touch_id, client, failures, reporting):
    touch_id(TOUCH_ID_FAILS)

    result = phone.race(["A", "B"], SESSION, client(EXPORTS_A_B), timing=FAST, outside=reporting)

    assert result.ok
    settled()
    assert failures == []


def test_should_report_nothing_when_touch_id_answers_after_the_phone_client_failed(
        touch_id, client, failures, reporting):
    touch_id("sleep 1; printf secret")

    result = phone.race(["A"], SESSION, client("echo 'unlock: unreachable' >&2; exit 3"), timing=FAST,
                        outside=reporting)

    assert result.ok
    settled()
    assert failures == []


@pytest.mark.parametrize("seen", [Presence.AWAY, Presence.COMPUTER])
def test_should_report_nothing_when_the_owner_cancels_touch_id_whether_or_not_the_phone_was_asked(
        touch_id, client, failures, seen):
    touch_id(f"sleep 1; {OWNER_CANCELS}")
    outside = phone.Outside(presence_reader=lambda: seen, report_failure=failures.append)

    result = phone.race(["A"], SESSION, client(WAIT_FOR_SIGNAL), timing=FAST, outside=outside)

    assert result.touch_id_cancelled
    settled()
    assert failures == []


@pytest.mark.parametrize("seen", [Presence.AWAY, Presence.COMPUTER])
def test_should_report_the_same_failure_whatever_the_presence(touch_id, client, failures, seen):
    touch_id(TOUCH_ID_FAILS)
    outside = phone.Outside(presence_reader=lambda: seen, report_failure=failures.append)

    phone.race(["A"], NOBODY, client("exit 3"), timing=FAST, outside=outside)

    assert failures == [ReadFailure(Reason.NOT_REACHED, variables=1)]


def test_should_answer_the_same_when_the_failure_report_cannot_be_posted(
        touch_id, client, failures, reporting, dead_service_url, monkeypatch, capsys):
    touch_id(TOUCH_ID_FAILS)
    asking = client("echo 'keyguard-passkey: unlock service unreachable' >&2; exit 3")
    expected = phone.race(["A"], SESSION, asking, timing=FAST, outside=reporting)
    monkeypatch.setattr(failure_report, "PHONE_SERVICE", dead_service_url)

    result = phone.race(["A"], SESSION, asking, timing=FAST)

    assert result == expected
    assert eventually(lambda: "failure report not sent" in capsys.readouterr().err)


# ---- Touch ID alone, while the phone fallback is off ----


@pytest.mark.parametrize("ending", [TOUCH_ID_FAILS, LOCKED_SCREEN, "echo 'Keys not found: A' >&2; exit 1"])
def test_should_report_once_when_touch_id_fails_and_the_phone_fallback_is_off(touch_id, failures, reporting, ending):
    touch_id(ending)

    result = phone.touch_id_alone(["A", "B"], SESSION, outside=reporting)

    assert not result.ok
    assert failures == [ReadFailure(Reason.PHONE_OFF, variables=2, session="48f92bcb")]


def test_should_report_once_when_touch_id_runs_out_and_the_phone_fallback_is_off(
        touch_id, failures, reporting, monkeypatch):
    touch_id(WAIT_FOR_SIGNAL)
    monkeypatch.setattr(keyguard_cli, "SUBPROCESS_TIMEOUT", 0.3)

    result = phone.touch_id_alone(["A"], NOBODY, outside=reporting)

    assert result.timed_out
    assert failures == [ReadFailure(Reason.PHONE_OFF, variables=1)]


def test_should_report_once_when_the_keyguard_binary_is_missing_and_the_phone_fallback_is_off(
        tmp_path, monkeypatch, failures, reporting):
    monkeypatch.setattr(keyguard_cli, "KEYGUARD_BIN", tmp_path / "no-such-keyguard")

    result = phone.touch_id_alone(["A"], NOBODY, outside=reporting)

    assert result.not_found
    assert failures == [ReadFailure(Reason.PHONE_OFF, variables=1)]


@pytest.mark.parametrize("body", ["printf secret", OWNER_CANCELS])
def test_should_report_nothing_when_touch_id_alone_approves_or_the_owner_cancels(touch_id, failures, reporting, body):
    touch_id(body)

    phone.touch_id_alone(["A"], SESSION, outside=reporting)

    assert failures == []


def test_should_ask_touch_id_for_the_cache_duration_and_the_caller_when_the_phone_fallback_is_off(
        touch_id, tmp_path, reporting):
    touch_id(f'printf "%s" "$*" > {tmp_path / "argv"}')

    phone.touch_id_alone(["A"], Caller(session="48f92bcb", title="Fix CI"), cache_duration=60, outside=reporting)

    assert (tmp_path / "argv").read_text() == 'get A --cache-duration 60 --caller "Fix CI" (48f92bcb)'


# ---- The client's command line and answer ----


def test_should_ask_the_store_service_as_the_mac_for_shell_exports_with_the_names_after_a_double_dash(tmp_path):
    asking = phone.PhoneClient(client=tmp_path / "keyguard-passkey.pyz", signing_key=tmp_path / "mac-sign.key",
                               service=SERVICE)

    argv = asking.argv(["A", "-B"])

    assert argv == [str(tmp_path / "keyguard-passkey.pyz"), "--service", SERVICE, "--requester", "mac",
                    "--signing-key", str(tmp_path / "mac-sign.key"), "get", "--format", "shell", "--", "A", "-B"]


def test_should_name_the_asking_session_title_and_pane_to_the_client():
    env = phone.client_env(Caller(session="48f92bcb", title="Fix the build", pane="%6"))

    assert (env["KEYGUARD_SESSION"], env["KEYGUARD_TITLE"], env["TMUX_PANE"]) == ("48f92bcb", "Fix the build", "%6")


def test_should_pass_an_empty_session_and_title_and_no_pane_when_the_caller_reports_none():
    env = phone.client_env(NOBODY)

    assert (env["KEYGUARD_SESSION"], env["KEYGUARD_TITLE"], "TMUX_PANE" in env) == ("", "", False)


def test_should_keep_the_client_from_holding_a_value_after_the_read():
    assert phone.client_env(NOBODY)["KEYGUARD_PASSKEY_HOLD_SECONDS"] == "0"


def test_should_find_openssl_3_before_any_other_openssl():
    assert phone.client_env(NOBODY)["PATH"].split(":")[0] == "/opt/homebrew/opt/openssl@3/bin"


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


def test_should_stay_off_without_a_client_a_signing_key_or_a_store_service(tmp_path):
    asking, key = script(tmp_path / "keyguard-passkey.pyz", ""), script(tmp_path / "mac-sign.key", "")

    assert phone.configured(None, key, SERVICE) is None
    assert phone.configured(asking, None, SERVICE) is None
    assert phone.configured(asking, key, "") is None


def test_should_stay_off_while_the_client_or_the_key_is_missing(tmp_path):
    assert phone.configured(tmp_path / "keyguard-passkey.pyz", tmp_path / "mac-sign.key", SERVICE) is None


def test_should_turn_on_once_the_client_and_the_key_are_installed_and_the_store_service_is_set(tmp_path):
    asking, key = script(tmp_path / "keyguard-passkey.pyz", ""), script(tmp_path / "mac-sign.key", "")

    assert phone.configured(asking, key, SERVICE) == phone.PhoneClient(client=asking, signing_key=key,
                                                                       service=SERVICE)


# ---- The handler ----


@pytest.mark.parametrize("path", ["/A,B", "/A,B?timeout=60"])
def test_should_race_the_phone_on_every_read_once_configured(server, monkeypatch, tmp_path, path):
    asking = phone.PhoneClient(client=tmp_path / "keyguard-passkey.pyz", signing_key=tmp_path / "mac-sign.key",
                               service=SERVICE)
    seen = {}
    monkeypatch.setattr(phone, "configured", lambda: asking)

    def race(keys, caller, client, cache_duration=None):
        seen.update(keys=keys, caller=caller, client=client)
        return CliResult(rc=0, stdout="A=1\nB=2\n", stderr="")

    monkeypatch.setattr(phone, "race", race)

    status, body = http_get(server, path, {"X-Keyguard-Session": "48f92bcb", "X-Keyguard-Pane": "%6"})

    assert (status, body) == (200, "A=1\nB=2\n")
    assert seen == {"keys": ["A", "B"], "caller": Caller(session="48f92bcb", pane="%6"), "client": asking}


def failed_read(server, monkeypatch, service: str) -> tuple[int, str]:
    """A read with the phone fallback off and Touch ID failing, which the server reports to `service`."""
    monkeypatch.setattr(phone, "configured", lambda: None)
    monkeypatch.setattr(failure_report, "PHONE_SERVICE", service)
    return http_get(server, "/A,B", {"X-Keyguard-Session": "48f92bcb"})


def test_should_post_one_failure_line_for_a_read_that_fails_with_the_phone_fallback_off(
        server, touch_id, monkeypatch, unlock_service):
    touch_id(TOUCH_ID_FAILS)
    unreported = failed_read(server, monkeypatch, "")
    assert unlock_service.posts == []

    answer = failed_read(server, monkeypatch, unlock_service.url)

    assert answer == unreported
    assert answer[0] != 200
    assert unlock_service.arrived()
    assert [post.body["text"] for post in unlock_service.posts] == [
        "Keyguard read of 2 variables failed at the Mac, and the phone fallback is off."]


@pytest.mark.parametrize("service", ["refusing", "unreachable", "hanging"])
def test_should_answer_a_failed_read_the_same_whatever_becomes_of_its_failure_line(
        server, touch_id, monkeypatch, unlock_service, dead_service_url, service):
    touch_id(TOUCH_ID_FAILS)
    unlock_service.status = 429
    if service == "hanging":
        unlock_service.gate.clear()
    address = dead_service_url if service == "unreachable" else unlock_service.url
    unreported = failed_read(server, monkeypatch, "")

    started = time.monotonic()
    answer = failed_read(server, monkeypatch, address)

    assert answer == unreported
    assert time.monotonic() - started < 3
    unlock_service.gate.set()
