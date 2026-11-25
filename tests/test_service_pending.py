from __future__ import annotations

import io
import json
import os
import stat
from pathlib import Path

import pytest

from keyguard_passkey.service import pending

NOW = 1_700_000_000.0
REQUEST_ID = "AbCdEfGh12345678"
LINK = f"https://unlock.example.test/approve?id={REQUEST_ID}"
SESSION = "48f92bcb"
TITLE = "Rechnungstag f\u00fcr den Kunden \u2013 Entwurf"
DEAD_PID = 2 ** 22 + 7

pytestmark = pytest.mark.spec("docs/service.md")


class Cancelled(Exception):
    pass


def request(request_id: str = REQUEST_ID, **fields: str) -> pending.Request:
    return pending.Request(id=request_id, vars=("JIRA_TOKEN",), op="get", host="devbox", link=LINK, **fields)


def records(directory: Path) -> list[dict[str, object]]:
    return [json.loads(path.read_text()) for path in sorted(directory.glob("*.json"))]


def states(directory: Path) -> list[object]:
    return [record["state"] for record in records(directory)]


def seed(directory: Path, **fields: object) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    record = {"id": REQUEST_ID, "vars": ["A"], "op": "get", "state": "waiting", "host": "devbox", "link": LINK,
              "created": NOW, "ended": None, "pid": os.getpid(), **fields}
    path = directory / f"{record['id']}.json"
    path.write_text(json.dumps(record))
    return path


def cancel_marks(directory: Path) -> list[Path]:
    return list(directory.rglob(f"*{pending.CANCEL_SUFFIX}"))


def test_should_record_a_waiting_request_while_the_approver_is_asked(tmp_path):
    with pending.announced(request(), tmp_path, now=lambda: NOW):
        seen = records(tmp_path)

    assert [(record["id"], record["state"], record["vars"]) for record in seen] == [
        (REQUEST_ID, "waiting", ["JIRA_TOKEN"])]


def test_should_record_done_once_the_answer_arrives(tmp_path):
    with pending.announced(request(), tmp_path, now=lambda: NOW):
        pass

    [record] = records(tmp_path)
    assert (record["state"], record["ended"]) == ("done", NOW)


@pytest.mark.parametrize("raised, options, state", [
    (TimeoutError, {}, "failed"),
    (TimeoutError, {"expired": (TimeoutError,)}, "expired"),
    (ValueError, {"expired": (TimeoutError,)}, "failed"),
    (Cancelled, {"cancelled": (Cancelled,)}, "cancelled"),
], ids=["failed", "expired", "failed, not an expiry", "cancelled"])
def test_should_record_how_the_wait_ended_and_reraise(tmp_path, raised, options, state):
    with pytest.raises(raised), pending.announced(request(), tmp_path, now=lambda: NOW, **options):
        raise raised("the wait ended without an answer")

    assert states(tmp_path) == [state]


def test_should_tell_the_waiting_body_once_its_request_is_cancelled(tmp_path):
    with pending.announced(request(), tmp_path, now=lambda: NOW) as cancelled:
        before = cancelled()
        assert pending.cancel(tmp_path, REQUEST_ID, NOW)
        after = cancelled()

    assert (before, after) == (False, True)
    assert cancel_marks(tmp_path) == []


@pytest.mark.parametrize("request_id, fields", [
    (REQUEST_ID, {"state": "done", "ended": NOW}),
    (REQUEST_ID, {"pid": DEAD_PID}),
    ("Unknown000000001", {}),
])
def test_should_refuse_to_cancel_what_no_live_process_waits_on(tmp_path, request_id, fields):
    seed(tmp_path, **fields)

    assert pending.cancel(tmp_path, request_id, NOW) is False
    assert cancel_marks(tmp_path) == []


def test_should_refuse_an_id_that_reaches_outside_the_folder(tmp_path):
    seed(tmp_path)

    assert pending.cancel(tmp_path / "pending", f"../{REQUEST_ID}", NOW) is False
    assert cancel_marks(tmp_path) == []


def test_should_prune_the_cancel_mark_of_a_request_that_is_gone(tmp_path):
    kept = seed(tmp_path)
    pending.cancel(tmp_path, REQUEST_ID, NOW)
    orphan = tmp_path / f"Gone000000000001{pending.CANCEL_SUFFIX}"
    orphan.touch()

    pending.prune(tmp_path, NOW)

    assert (orphan.exists(), kept.with_suffix(pending.CANCEL_SUFFIX).exists()) == (False, True)


def test_should_forget_a_sessions_expired_request_once_a_later_one_is_answered(tmp_path):
    expired = seed(tmp_path, id="Expired00000001", state="expired", session=SESSION, ended=NOW)
    other = seed(tmp_path, id="OtherSession001", state="expired", session="0badc0de", ended=NOW)

    with pending.announced(request(session=SESSION), tmp_path, now=lambda: NOW):
        pass

    assert (expired.exists(), other.exists()) == (False, True)


def test_should_keep_a_sessions_expired_request_while_later_ones_get_no_answer(tmp_path):
    expired = seed(tmp_path, id="Expired00000001", state="expired", session=SESSION, ended=NOW)

    with pytest.raises(TimeoutError), pending.announced(request(session=SESSION), tmp_path, now=lambda: NOW,
                                                        expired=(TimeoutError,)):
        raise TimeoutError("the approver never answered")

    assert expired.exists()


def test_should_keep_records_readable_by_the_owner_only(tmp_path):
    folder = tmp_path / "pending"

    with pending.announced(request(), folder, now=lambda: NOW):
        pass

    assert stat.S_IMODE(folder.stat().st_mode) == 0o700
    assert stat.S_IMODE((folder / f"{REQUEST_ID}.json").stat().st_mode) == 0o600


def test_should_keep_waiting_when_the_record_cannot_be_written(tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("a file where the folder should be")
    stderr = io.StringIO()

    with pending.announced(request(), blocker / "pending", now=lambda: NOW, stderr=stderr):
        answered = True

    assert answered
    assert "the pending list will not show this request" in stderr.getvalue()


def test_should_refuse_an_id_that_is_not_a_plain_filename():
    with pytest.raises(ValueError, match="not a request id"):
        request("../escape")


@pytest.mark.parametrize("fields, age, kept", [
    ({"state": "done", "ended": NOW}, pending.ENDED_LINGER_SECONDS - 1, True),
    ({"state": "done", "ended": NOW}, pending.ENDED_LINGER_SECONDS + 1, False),
    ({"state": "expired", "ended": NOW, "session": SESSION}, pending.EXPIRED_LINGER_SECONDS - 1, True),
    ({"state": "expired", "ended": NOW, "session": SESSION}, pending.EXPIRED_LINGER_SECONDS + 1, False),
    ({"state": "expired", "ended": NOW}, pending.ENDED_LINGER_SECONDS + 1, False),
    ({"state": "failed", "ended": NOW, "session": SESSION}, pending.ENDED_LINGER_SECONDS + 1, False),
    ({}, pending.REQUEST_TTL_SECONDS - 1, True),
    ({}, pending.REQUEST_TTL_SECONDS + 1, True),
    ({}, pending.REQUEST_TTL_SECONDS + pending.OUTCOME_GRACE_SECONDS + 1, False),
    ({"created": None}, 1, False),
    ({"pid": DEAD_PID}, 1, False),
])
def test_should_prune_what_no_longer_waits(tmp_path, fields, age, kept):
    path = seed(tmp_path, **fields)

    pending.prune(tmp_path, NOW + age)

    assert path.exists() is kept


def test_should_prune_a_record_that_is_not_json(tmp_path):
    broken = tmp_path / f"{REQUEST_ID}.json"
    broken.write_text("{")

    pending.prune(tmp_path, NOW)

    assert not broken.exists()


def test_should_list_only_the_public_fields_oldest_first(tmp_path):
    seed(tmp_path, id="Later0000000001", created=NOW + 5)
    seed(tmp_path, id="Earlier00000001", created=NOW)

    listed = pending.snapshot(tmp_path, NOW + 10)

    assert [record["id"] for record in listed] == ["Earlier00000001", "Later0000000001"]
    assert set(listed[0]) == set(pending.PUBLIC_FIELDS)
    assert set(pending.PUBLIC_FIELDS) == {"id", "vars", "op", "state", "host", "link", "session", "title", "pane",
                                          "created", "text", "target", "target_box", "target_title"}


def test_should_show_the_message_of_a_sign_request_in_the_snapshot(tmp_path):
    message = {"text": "yes, push", "target": "212df38e-7c4b-4a51-9d0e-3f6b2a8c1e57", "target_box": "macbook",
               "target_title": "Mazzotta-bot"}
    signing = pending.Request(id=REQUEST_ID, vars=(), op="sign", host="devbox", link=LINK, **message)

    with pending.announced(signing, tmp_path, now=lambda: NOW):
        [listed] = pending.snapshot(tmp_path, NOW)

    assert {key: listed[key] for key in message} == message
    assert (listed["op"], listed["vars"]) == ("sign", [])


def test_should_name_the_asking_session_in_the_record_and_the_snapshot(tmp_path):
    with pending.announced(request(session=SESSION, title=TITLE), tmp_path, now=lambda: NOW):
        recorded = records(tmp_path)[0]
        listed = pending.snapshot(tmp_path, NOW)[0]

    assert (recorded["session"], recorded["title"]) == (SESSION, TITLE)
    assert (listed["session"], listed["title"]) == (SESSION, TITLE)


def test_should_list_a_record_without_a_session_as_null(tmp_path):
    seed(tmp_path)

    listed = pending.snapshot(tmp_path, NOW)[0]

    assert (listed["session"], listed["title"], listed["pane"]) == (None, None, None)


def test_should_name_the_asking_pane_in_the_snapshot(tmp_path):
    with pending.announced(request(title="shell in tmux main:3", pane="%6"), tmp_path, now=lambda: NOW):
        listed = pending.snapshot(tmp_path, NOW)[0]

    assert (listed["title"], listed["pane"]) == ("shell in tmux main:3", "%6")


def test_should_stream_a_line_only_when_the_requests_change(tmp_path):
    out = io.StringIO()
    clock = {"t": NOW}
    seeded = {"done": False}

    def sleep(seconds: float) -> None:
        clock["t"] += seconds
        if not seeded["done"]:
            seed(tmp_path, created=clock["t"])
            seeded["done"] = True

    pending.watch(tmp_path, out=out, now=lambda: clock["t"], sleep=sleep, rounds=4)

    assert [len(json.loads(line)["pending"]) for line in out.getvalue().splitlines()] == [0, 1]


def test_should_repeat_the_snapshot_as_a_heartbeat(tmp_path):
    out = io.StringIO()
    clock = {"t": NOW}

    def sleep(_seconds: float) -> None:
        clock["t"] += pending.HEARTBEAT_SECONDS

    pending.watch(tmp_path, out=out, now=lambda: clock["t"], sleep=sleep, rounds=3)

    assert len(out.getvalue().splitlines()) == 3
