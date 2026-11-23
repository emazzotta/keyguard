from __future__ import annotations

import json
import os
import signal
import threading
import time
import urllib.error

import pytest
from conftest import REQUEST_TOKEN, REQUESTER, opened

from keyguard_passkey.ceremony import Op, Request
from keyguard_passkey.service import asking, client, pending
from keyguard_passkey.service.asking import ServiceAsk, UsageError
from keyguard_passkey.service.hint import NotifyHint
from keyguard_passkey.service.presence import Presence

SESSION = "48f92bcb"
TITLE = "Keyguard approval session names"
QUICK_TIMEOUT = 0.3
QUICK_INTERVAL = 0.05
MISSED_ANSWER_TIMEOUT = 5.0

pytestmark = pytest.mark.spec("docs/service.md")


@pytest.fixture
def make_ask(unlock_service, phone, tmp_path):
    def _make(decide=lambda record: None, **overrides):
        settings = {"url": unlock_service.base, "signing_key": unlock_service.signing_key, "requester": REQUESTER,
                    "token": REQUEST_TOKEN, "remote_recipient": unlock_service.remote.recipient,
                    "pending_dir": tmp_path / "pending", "stderr": phone(decide), "interval": QUICK_INTERVAL,
                    "timeout": MISSED_ANSWER_TIMEOUT, **overrides}
        return ServiceAsk(**settings)
    return _make


def request(op, names=(), values=None, session=SESSION, title=TITLE):
    return Request(op=op, names=tuple(names), values=values or {}, force=True, session=session, title=title)


def recorded(ask: ServiceAsk) -> list[dict[str, object]]:
    return [json.loads(path.read_text()) for path in ask.pending_dir.glob("*.json")]


def test_should_reveal_the_values_the_approver_releases(make_ask):
    ask = make_ask(lambda record: {"B": "two", "A": "one"})

    answer = ask(request(Op.GET, ["B", "A"]))

    assert answer == {"values": {"A": "one", "B": "two"}}
    [seen] = ask.stderr.seen
    assert (seen["vars"], seen["host"], seen["session"], seen["title"]) == (["B", "A"], REQUESTER, SESSION, TITLE)


def test_should_tell_where_to_approve(make_ask, unlock_service):
    ask = make_ask(lambda record: {"A": "one"})

    ask(request(Op.GET, ["A"]))

    [seen] = ask.stderr.seen
    assert f"Approve on your phone: {unlock_service.base}/approve?id={seen['id']}\n" in ask.stderr.getvalue()


def test_should_answer_absent_for_the_names_the_store_lacks(make_ask):
    ask = make_ask(lambda record: {"absent": ["B"]})

    assert ask(request(Op.GET, ["A", "B"])) == {"absent": ["B"]}


def test_should_list_the_names_sorted(make_ask):
    ask = make_ask(lambda record: {"list": ["B_TOKEN", "A_TOKEN"]})

    assert ask(request(Op.LIST)) == {"names": ["A_TOKEN", "B_TOKEN"]}
    assert ask.stderr.seen[0]["op"] == "list"


def test_should_propose_a_value_only_the_approving_device_can_open(make_ask, unlock_service):
    proposed = []

    def approve(record):
        proposed.append(opened(unlock_service.remote.identity, str(record["value"])))
        return {"set": "JIRA_TOKEN"}

    ask = make_ask(approve)

    assert ask(request(Op.SET, ["JIRA_TOKEN"], {"JIRA_TOKEN": "rotated"})) == {"written": ["JIRA_TOKEN"]}
    assert proposed == ["rotated"]


@pytest.mark.parametrize("answer, expected", [({"rm": "OLD"}, {}), ({"absent": ["OLD"]}, {"absent": ["OLD"]})])
def test_should_remove_one_name(make_ask, answer, expected):
    ask = make_ask(lambda record: answer)

    assert ask(request(Op.RM, ["OLD"])) == expected
    assert (ask.stderr.seen[0]["op"], ask.stderr.seen[0]["vars"]) == ("rm", ["OLD"])


@pytest.mark.parametrize("overrides, asked, message", [
    ({}, request(Op.INIT), "/enroll"),
    ({"requester": ""}, request(Op.GET, ["A"]), "--requester"),
    ({"remote_recipient": ""}, request(Op.SET, ["A"], {"A": "v"}), "--remote-recipient"),
    ({}, request(Op.SET, ["A", "B"], {"A": "1", "B": "2"}), "one name per approval"),
    ({}, request(Op.RM, ["A", "B"]), "one name per approval"),
], ids=["init", "no requester", "no remote recipient", "set of two", "rm of two"])
def test_should_refuse_what_the_service_cannot_take_before_asking(make_ask, overrides, asked, message):
    ask = make_ask(**overrides)

    with pytest.raises(UsageError, match=message):
        ask(asked)
    assert ask.stderr.seen == []


def test_should_drop_a_session_and_title_the_service_would_refuse(make_ask):
    ask = make_ask(lambda record: {"A": "one"})

    ask(request(Op.GET, ["A"], session="not-a-session", title="two\nlines"))

    [seen] = ask.stderr.seen
    assert ("session" in seen, seen["title"]) == (False, "two lines")


def test_should_record_the_request_while_the_approver_is_asked(make_ask):
    states = []
    ask = make_ask(lambda record: states.append(recorded(ask)) or {"A": "one"}, pane="%6")

    ask(request(Op.GET, ["A"]))

    [[waiting]] = states
    assert (waiting["state"], waiting["vars"], waiting["op"], waiting["host"]) == ("waiting", ["A"], "get", REQUESTER)
    assert (waiting["session"], waiting["title"], waiting["pane"]) == (SESSION, TITLE, "%6")
    assert waiting["link"] == ask.approve_link(str(waiting["id"]))
    assert [record["state"] for record in recorded(ask)] == ["done"]


@pytest.mark.parametrize("pane, found", [("%6", "%6"), (None, None), ("", None), ("6", None), ("%x", None)])
def test_should_name_only_a_tmux_pane(pane, found):
    environ = {} if pane is None else {asking.PANE_ENV: pane}

    assert asking.asking_pane(environ) == found


def test_should_record_an_expiry_when_nobody_answers_in_time(make_ask):
    ask = make_ask(timeout=QUICK_TIMEOUT)

    with pytest.raises(client.UnlockTimeout):
        ask(request(Op.GET, ["A"]))

    assert [record["state"] for record in recorded(ask)] == ["expired"]


def test_should_record_an_expiry_when_the_service_drops_the_request(make_ask, unlock_service):
    ask = make_ask(lambda record: unlock_service.call(f"/r/{record['id']}", "DELETE") and None)

    with pytest.raises(client.RequestGone):
        ask(request(Op.GET, ["A"]))

    assert [record["state"] for record in recorded(ask)] == ["expired"]


def test_should_record_a_failure_that_is_not_an_expiry(make_ask, monkeypatch):
    def refused(*_args, **_kwargs):
        raise client.ServiceRefused(500)

    monkeypatch.setattr(client, "poll", refused)
    ask = make_ask()

    with pytest.raises(client.ServiceRefused):
        ask(request(Op.GET, ["A"]))

    assert [record["state"] for record in recorded(ask)] == ["failed"]


def withdrawn(unlock_service, request_id) -> bool:
    try:
        unlock_service.approver_view(request_id)
    except urllib.error.HTTPError as error:
        return error.code == 404
    return False


def test_should_withdraw_a_request_cancelled_from_the_pending_list(make_ask, unlock_service):
    ask = make_ask(lambda record: pending.cancel(ask.pending_dir, str(record["id"]), time.time()) and None)

    with pytest.raises(client.Cancelled):
        ask(request(Op.GET, ["A"]))

    assert withdrawn(unlock_service, ask.stderr.seen[0]["id"])
    assert [record["state"] for record in recorded(ask)] == ["cancelled"]


def test_should_end_cancelled_even_when_the_service_refuses_the_withdrawal(make_ask, monkeypatch):
    def refuse(*_args):
        raise client.ServiceRefused(500)

    monkeypatch.setattr(client, "withdraw", refuse)
    ask = make_ask(cancelled=lambda: True)

    with pytest.raises(client.Cancelled):
        ask(request(Op.GET, ["A"]))

    assert [record["state"] for record in recorded(ask)] == ["cancelled"]
    assert "can still be answered until it expires" in ask.stderr.getvalue()


def test_should_withdraw_a_waiting_request_on_sigterm_and_restore_the_handler(make_ask, unlock_service):
    before = signal.getsignal(signal.SIGTERM)
    ask = make_ask(lambda record: os.kill(os.getpid(), signal.SIGTERM))

    with pytest.raises(client.Cancelled):
        ask(request(Op.GET, ["A"]))

    assert withdrawn(unlock_service, ask.stderr.seen[0]["id"])
    assert [record["state"] for record in recorded(ask)] == ["cancelled"]
    assert signal.getsignal(signal.SIGTERM) is before


def test_should_leave_sigterm_alone_outside_the_main_thread():
    found = []

    def wait():
        with asking.sigterm_cancels() as terminated:
            found.append(terminated())

    thread = threading.Thread(target=wait)
    thread.start()
    thread.join()

    assert found == [False]


@pytest.mark.parametrize("session, kept", [(SESSION, False), ("", True)])
def test_should_settle_a_sessions_expired_requests_once_the_hold_answers_it(make_ask, session, kept):
    ask = make_ask()
    ask.pending_dir.mkdir()
    expired = ask.pending_dir / "Expired00000001.json"
    expired.write_text(json.dumps({"id": "Expired00000001", "state": "expired", "session": SESSION}))

    ask.held(request(Op.GET, ["A"], session=session))

    assert expired.exists() is kept


@pytest.mark.parametrize("token, headers", [(REQUEST_TOKEN, {"Authorization": f"Bearer {REQUEST_TOKEN}"}),
                                            ("", {})])
def test_should_send_the_request_token_only_when_there_is_one(make_ask, token, headers):
    assert make_ask(token=token).headers == headers


NOW = 1_000_000.0


def test_should_hint_unknown_presence_when_it_has_no_presence_folder(make_ask):
    assert make_ask(container="box-1")._hint() == NotifyHint(Presence.UNKNOWN, "box-1")


@pytest.mark.parametrize(("lines", "presence"), [
    ([f"{int(NOW) - 5} computer"], Presence.COMPUTER),
    ([f"{int(NOW) - 5} phone"], Presence.PHONE),
    ([f"{int(NOW) - 500} computer"], Presence.AWAY),
])
def test_should_hint_the_presence_that_its_folder_shows_at_the_time_of_asking(make_ask, tmp_path, lines, presence):
    folder = tmp_path / "presence"
    folder.mkdir()
    (folder / "terminal").write_text("\n".join(lines) + "\n")

    assert make_ask(presence_dir=folder, now=lambda: NOW)._hint().presence == presence


def test_should_hint_unknown_when_the_presence_folder_is_missing(make_ask, tmp_path):
    assert make_ask(presence_dir=tmp_path / "absent", now=lambda: NOW)._hint().presence == Presence.UNKNOWN


@pytest.mark.parametrize(("label", "container"), [("devbox-1", "devbox-1"), ("", None), ("dev box", None)])
def test_should_hint_a_container_only_when_it_is_a_host_label(make_ask, label, container):
    assert make_ask(container=label)._hint().container == container


def test_should_send_the_hint_with_the_request(make_ask, monkeypatch, tmp_path):
    folder = tmp_path / "presence"
    folder.mkdir()
    (folder / "terminal").write_text(f"{int(time.time())} phone\n")
    sent: list[client.NotifyHint | None] = []
    submit = client.submit
    monkeypatch.setattr(client, "submit", lambda *args: sent.append(args[3]) or submit(*args))
    ask = make_ask(lambda record: {"A": "one"}, presence_dir=folder, container="box-1")

    ask(request(Op.GET, ["A"]))

    assert sent == [NotifyHint(Presence.PHONE, "box-1")]
