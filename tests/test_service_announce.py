from __future__ import annotations

import pytest

from keyguard_passkey.service.announce import KeyguardAnnouncer
from keyguard_passkey.service.notify import ClaudeNotice, NoticeKind, notice_messages
from keyguard_passkey.service.requests import RequestQueue

PUBLIC_URL = "https://unlock.example.test"
TTL = 300
FAILED_MARK = "\u274c\U0001f510"
LOCK_MARK = "\U0001f510"


class RecordingNotifier:
    def __init__(self) -> None:
        self.notices: list[ClaudeNotice] = []

    def claude_notice(self, notice: ClaudeNotice) -> None:
        self.notices.append(notice)


class Clock:
    now = 1000.0

    def __call__(self) -> float:
        return self.now


def payload(**over: object) -> dict[str, object]:
    return {"vars": ["DB_PASSWORD", "API_TOKEN"], "host": "devbox", "eph_pubkey": "pk", "ts": 1, "sig": "s",
            "session": "4a015f0f", "title": "Plan the migration", "notify": {"presence": "away", "container": "box-1"},
            **over}


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def notifier() -> RecordingNotifier:
    return RecordingNotifier()


@pytest.fixture
def queue(notifier, clock) -> RequestQueue:
    announcer = KeyguardAnnouncer(notifier, {"devbox": "dev box"}, PUBLIC_URL)
    return RequestQueue(ttl_seconds=TTL, now=clock, events=announcer)


def expire(queue: RequestQueue, clock: Clock) -> None:
    clock.now += TTL + 1
    queue.sweep()


@pytest.mark.spec("docs/service.md")
@pytest.mark.parametrize("notify", [{"presence": "away"}, {"presence": "unknown"}, {}, {"presence": "present"}])
def test_should_post_a_wait_notice_at_once_when_the_hint_says_the_owner_is_away_or_unknown(queue, notifier, notify):
    request_id = queue.create(payload(notify=notify))

    wait, = notifier.notices
    assert wait.kind is NoticeKind.KEYGUARD_WAIT
    assert wait.text == "Waiting for your approval: dev box asks to read 2 variables."
    assert wait.link == f"{PUBLIC_URL}/approve?id={request_id}"


@pytest.mark.spec("docs/service.md")
@pytest.mark.parametrize("notify", [None, "away", ["away"], 7])
def test_should_post_no_wait_notice_for_an_old_client_that_sends_no_hint_but_still_one_failure_line(
        queue, notifier, clock, notify):
    body = payload()
    del body["notify"]
    queue.create(body if notify is None else payload(notify=notify))

    expire(queue, clock)
    expire(queue, clock)

    failure, = notifier.notices
    assert (failure.kind, failure.container) == (NoticeKind.KEYGUARD_FAILED, None)


@pytest.mark.spec("docs/service.md")
@pytest.mark.parametrize("presence", ["computer", "phone"])
def test_should_post_nothing_for_the_wait_when_the_owner_is_present(queue, notifier, presence):
    queue.create(payload(notify={"presence": presence}))

    assert notifier.notices == []


@pytest.mark.spec("docs/service.md")
@pytest.mark.parametrize("presence", ["computer", "phone", "away", "unknown"])
def test_should_post_exactly_one_failure_line_when_a_request_expires_whatever_the_presence(
        queue, notifier, clock, presence):
    queue.create(payload(notify={"presence": presence}))
    notifier.notices.clear()

    expire(queue, clock)
    expire(queue, clock)

    failure, = notifier.notices
    assert failure.kind is NoticeKind.KEYGUARD_FAILED
    assert failure.text == "Request expired with no answer: dev box asked to read 2 variables."


@pytest.mark.spec("docs/service.md")
def test_should_post_no_failure_line_for_an_approval_or_an_owner_who_cancelled(queue, notifier, clock):
    approved, cancelled = queue.create(payload()), queue.create(payload())
    queue.respond(approved, "sealed")
    queue.get(approved)
    queue.withdraw(cancelled)
    notifier.notices.clear()

    expire(queue, clock)

    assert notifier.notices == []


@pytest.mark.spec("docs/service.md")
def test_should_post_no_failure_line_for_an_answered_request_that_nobody_collected(queue, notifier, clock):
    queue.respond(queue.create(payload()), "sealed")
    notifier.notices.clear()

    expire(queue, clock)

    assert notifier.notices == []


@pytest.mark.spec("docs/service.md")
@pytest.mark.parametrize(("over", "action"), [
    ({"vars": ["A"]}, "read 1 variable"),
    ({"vars": ["A", "B", "C"]}, "read 3 variables"),
    ({"vars": ["A"], "op": "set", "value": "c2VhbGVk"}, "set 1 variable"),
    ({"vars": ["A"], "op": "rm"}, "remove 1 variable"),
    ({"vars": [], "op": "list"}, "list the variable names"),
])
def test_should_say_what_a_request_asks_by_its_operation_and_count(queue, notifier, over, action):
    queue.create(payload(**over))

    assert notifier.notices[0].text == f"Waiting for your approval: dev box asks to {action}."


@pytest.mark.spec("docs/service.md")
def test_should_never_name_a_variable_in_a_notice(queue, notifier, clock):
    queue.create(payload())
    expire(queue, clock)

    texts = [message for notice in notifier.notices for message in notice_messages(notice)]

    assert len(texts) == 2
    assert not any("DB_PASSWORD" in text or "API_TOKEN" in text for text in texts)


@pytest.mark.spec("docs/service.md")
def test_should_name_a_requester_by_its_label_and_an_unlisted_one_as_such(queue, notifier):
    queue.create(payload(host="stranger"))
    queue.create(payload(host=["devbox"]))

    assert [notice.text for notice in notifier.notices] == [
        "Waiting for your approval: an unlisted machine asks to read 2 variables."] * 2


@pytest.mark.spec("docs/service.md")
def test_should_carry_the_session_title_and_container_of_the_request(queue, notifier, clock):
    queue.create(payload())
    expire(queue, clock)

    for notice in notifier.notices:
        assert (notice.session, notice.title, notice.container) == ("4a015f0f", "Plan the migration", "box-1")


@pytest.mark.spec("docs/service.md")
def test_should_carry_no_session_when_the_requester_has_none(queue, notifier):
    body = payload()
    del body["session"], body["title"]

    queue.create(body)

    wait, = notifier.notices
    assert (wait.session, wait.title) == (None, None)


@pytest.mark.spec("docs/service.md")
def test_should_build_the_link_from_the_settings_and_never_from_the_request(notifier):
    announcer = KeyguardAnnouncer(notifier, {"devbox": "dev box"}, "")
    queue = RequestQueue(events=announcer)

    queue.create(payload(title="https://evil.example/approve", notify={"presence": "away", "link": "https://evil.example"}))

    wait, = notifier.notices
    assert wait.link is None


@pytest.mark.spec("docs/service.md")
def test_should_write_the_wait_notice_with_its_mark_first_and_the_approve_link(queue, notifier):
    request_id = queue.create(payload())

    message, = notice_messages(notifier.notices[0])

    assert message.splitlines() == [
        f"{LOCK_MARK} Waiting for your approval: dev box asks to read 2 variables.", "",
        f'<a href="{PUBLIC_URL}/approve?id={request_id}">Approve in Safari</a>', "",
        "Plan the migration \u00b7 box-1", "[session 4a015f0f]"]


@pytest.mark.spec("docs/service.md")
def test_should_write_the_failure_line_with_its_mark_first(queue, notifier, clock):
    queue.create(payload(notify={"presence": "computer", "container": "box-1"}))
    expire(queue, clock)

    message, = notice_messages(notifier.notices[0])

    assert message.splitlines() == [
        f"{FAILED_MARK} Request expired with no answer: dev box asked to read 2 variables.", "",
        "Plan the migration \u00b7 box-1", "[session 4a015f0f]"]


@pytest.mark.spec("docs/service.md")
def test_should_write_a_notice_of_a_requester_without_a_session_as_one_line(queue, notifier, clock):
    queue.create(payload(notify={"presence": "computer"}, session=None, title=None))
    expire(queue, clock)
    notice, = notifier.notices
    notice = ClaudeNotice(notice.session, None, notice.text, notice.origin, notice.kind)

    message, = notice_messages(notice)

    assert message == f"{FAILED_MARK} Request expired with no answer: dev box asked to read 2 variables."
