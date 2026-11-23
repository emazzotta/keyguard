from __future__ import annotations

import io
from contextlib import redirect_stderr

import pytest

from keyguard_passkey.service.hint import NotifyHint
from keyguard_passkey.service.presence import Presence
from keyguard_passkey.service.requests import (
    MAX_SEALED_VALUE_CHARS,
    MAX_TITLE_CHARS,
    AlreadyAnswered,
    RateLimitError,
    RequestError,
    RequestQueue,
    UnlockRequest,
)

TITLE = "Rechnungstag f\u00fcr den Kunden \u2013 Entwurf"


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def payload(**over: object) -> dict[str, object]:
    return {"vars": ["A", "B"], "host": "devbox", "eph_pubkey": "pk", "ts": 1, "sig": "s", **over}


@pytest.mark.spec("docs/service.md")
def test_should_create_a_request_and_return_its_id():
    request_id = RequestQueue().create(payload())

    assert isinstance(request_id, str) and request_id


@pytest.mark.spec("docs/service.md")
@pytest.mark.parametrize("body", [{"vars": ["A"]}, [], "request"])
def test_should_refuse_a_request_without_its_fields(body):
    with pytest.raises(RequestError):
        RequestQueue().create(body)


@pytest.mark.spec("docs/service.md")
def test_should_hand_the_requester_the_request_fields():
    queue = RequestQueue()

    record = queue.get(queue.create(payload()))

    assert record == {"vars": ["A", "B"], "host": "devbox", "eph_pubkey": "pk", "ts": 1, "sig": "s",
                      "response": None}


@pytest.mark.spec("docs/service.md")
def test_should_carry_a_set_request_with_its_sealed_value():
    queue = RequestQueue()

    record = queue.get(queue.create(payload(vars=["A"], op="set", value="c2VhbGVk")))

    assert (record["op"], record["value"]) == ("set", "c2VhbGVk")


@pytest.mark.spec("docs/service.md")
def test_should_carry_a_removal_request_without_a_value():
    queue = RequestQueue()

    record = queue.get(queue.create(payload(vars=["A"], op="rm")))

    assert record["op"] == "rm" and "value" not in record


@pytest.mark.spec("docs/service.md")
def test_should_carry_a_listing_request_that_names_no_variable():
    queue = RequestQueue()

    record = queue.get(queue.create(payload(vars=[], op="list")))

    assert (record["op"], record["vars"]) == ("list", []) and "value" not in record


@pytest.mark.spec("docs/service.md")
def test_should_keep_a_reveal_request_free_of_set_fields():
    queue = RequestQueue()

    record = queue.get(queue.create(payload()))

    assert "op" not in record and "value" not in record


@pytest.mark.spec("docs/service.md")
@pytest.mark.parametrize("over", [
    {"op": "delete", "value": "x"},
    {"op": ["set"], "vars": ["A"]},
    {"op": "set"},
    {"op": "set", "value": ""},
    {"op": "set", "value": "x", "vars": ["A", "B"]},
    {"value": "x"},
    {"op": "set", "value": "x" * (MAX_SEALED_VALUE_CHARS + 1), "vars": ["A"]},
    {"op": "rm", "value": "x", "vars": ["A"]},
    {"op": "rm", "vars": ["A", "B"]},
    {"op": "list", "vars": ["A"]},
    {"op": "list", "vars": [], "value": "x"},
])
def test_should_refuse_a_malformed_change_request(over):
    with pytest.raises(RequestError):
        RequestQueue().create(payload(**over))


@pytest.mark.spec("docs/service.md")
def test_should_hand_both_sides_the_asking_session():
    queue = RequestQueue()
    request_id = queue.create(payload(session="48f92bcb", title=TITLE))

    peeked, read = queue.peek(request_id), queue.get(request_id)

    assert (peeked["session"], peeked["title"]) == ("48f92bcb", TITLE)
    assert (read["session"], read["title"]) == ("48f92bcb", TITLE)


@pytest.mark.spec("docs/service.md")
def test_should_accept_a_session_of_mixed_case_letters():
    queue = RequestQueue()

    assert queue.peek(queue.create(payload(session="G3yIYwxa", title=TITLE)))["session"] == "G3yIYwxa"


@pytest.mark.spec("docs/service.md")
def test_should_keep_a_request_without_a_session_free_of_session_fields():
    queue = RequestQueue()

    assert {"session", "title"}.isdisjoint(queue.peek(queue.create(payload())))


@pytest.mark.spec("docs/service.md")
@pytest.mark.parametrize("over", [
    {"session": "48f92bc"},
    {"session": "48f92bc!"},
    {"session": "48f92bcb-f080"},
    {"session": ""},
    {"session": 48},
    {"title": ""},
    {"title": "x" * (MAX_TITLE_CHARS + 1)},
    {"title": "line\nbreak"},
    {"title": "bidi \u202e override"},
    {"title": ["list"]},
])
def test_should_refuse_a_malformed_session_or_title(over):
    with pytest.raises(RequestError):
        RequestQueue().create(payload(**over))


@pytest.mark.spec("docs/service.md")
def test_should_return_nothing_for_an_unknown_request():
    queue = RequestQueue()

    assert (queue.get("nope"), queue.peek("nope"), queue.respond("nope", "x"), queue.withdraw("nope")) == (
        None, None, False, False)


@pytest.mark.spec("docs/service.md")
def test_should_deliver_the_answer_once_then_forget_the_request():
    queue = RequestQueue()
    request_id = queue.create(payload())

    assert queue.respond(request_id, "sealed-ciphertext") is True

    assert queue.get(request_id)["response"] == "sealed-ciphertext"
    assert queue.get(request_id) is None


@pytest.mark.spec("docs/service.md")
def test_should_keep_a_pending_request_across_reads():
    queue = RequestQueue()
    request_id = queue.create(payload())

    assert queue.get(request_id)["response"] is None
    assert queue.get(request_id) is not None


@pytest.mark.spec("docs/service.md")
def test_should_forget_a_request_after_its_time_to_live():
    clock = Clock()
    queue = RequestQueue(ttl_seconds=120, now=clock)
    request_id = queue.create(payload())

    clock.now += 121

    assert (queue.get(request_id), queue.respond(request_id, "x")) == (None, False)


@pytest.mark.spec("docs/service.md")
def test_should_drop_expired_requests_on_a_sweep():
    clock = Clock()
    queue = RequestQueue(ttl_seconds=120, now=clock)
    request_id = queue.create(payload())
    clock.now += 121

    queue.sweep()
    clock.now -= 121

    assert queue.peek(request_id) is None


@pytest.mark.spec("docs/service.md")
def test_should_show_the_approver_a_pending_request_as_unanswered():
    queue = RequestQueue()

    view = queue.peek(queue.create(payload()))

    assert (view["vars"], view["sig"], view["answered"]) == (["A", "B"], "s", False)


@pytest.mark.spec("docs/service.md")
def test_should_keep_the_answer_out_of_the_approver_view():
    queue = RequestQueue()
    request_id = queue.create(payload())
    queue.respond(request_id, "sealed-ciphertext")

    view = queue.peek(request_id)

    assert view["answered"] is True and "response" not in view


@pytest.mark.spec("docs/service.md")
def test_should_leave_the_answer_for_the_requester_however_often_the_approver_looks():
    queue = RequestQueue()
    request_id = queue.create(payload())
    queue.respond(request_id, "sealed-ciphertext")

    queue.peek(request_id)
    queue.peek(request_id)

    assert queue.get(request_id)["response"] == "sealed-ciphertext"


@pytest.mark.spec("docs/service.md")
def test_should_keep_the_first_answer_and_refuse_a_second():
    queue = RequestQueue()
    request_id = queue.create(payload())
    queue.respond(request_id, "first")

    with pytest.raises(AlreadyAnswered):
        queue.respond(request_id, "second")

    assert queue.get(request_id)["response"] == "first"


@pytest.mark.spec("docs/service.md")
def test_should_leave_nothing_to_answer_once_a_request_is_withdrawn():
    queue = RequestQueue()
    request_id = queue.create(payload())

    assert queue.withdraw(request_id) is True

    assert (queue.peek(request_id), queue.respond(request_id, "sealed")) == (None, False)


@pytest.mark.spec("docs/service.md")
def test_should_allow_requests_up_to_the_rate_limit():
    queue = RequestQueue(rate_limit=3, rate_window=60, now=Clock())

    assert all(queue.create(payload()) for _ in range(3))


@pytest.mark.spec("docs/service.md")
def test_should_refuse_a_request_over_the_rate_limit_until_the_window_passes():
    clock = Clock()
    queue = RequestQueue(rate_limit=1, rate_window=60, now=clock)
    queue.create(payload())

    with pytest.raises(RateLimitError):
        queue.create(payload())
    clock.now += 61

    assert queue.create(payload())


@pytest.mark.spec("docs/service.md")
def test_should_not_count_a_malformed_request_against_the_rate_limit():
    queue = RequestQueue(rate_limit=1, rate_window=60, now=Clock())

    with pytest.raises(RequestError):
        queue.create({"vars": ["A"]})

    assert queue.create(payload())


class RecordingEvents:
    def __init__(self) -> None:
        self.created_ids: list[str] = []
        self.expired_ids: list[str] = []
        self.expired_requests: list[UnlockRequest] = []

    def created(self, request: UnlockRequest) -> None:
        self.created_ids.append(request.id)

    def expired(self, request: UnlockRequest) -> None:
        self.expired_ids.append(request.id)
        self.expired_requests.append(request)


TTL = 120


def queue_with(events, clock: Clock | None = None) -> tuple[RequestQueue, Clock]:
    clock = clock or Clock()
    return RequestQueue(ttl_seconds=TTL, now=clock, events=events), clock


@pytest.mark.spec("docs/service.md")
def test_should_report_each_new_request_with_its_hint():
    events = RecordingEvents()
    queue, _ = queue_with(events)

    request_id = queue.create(payload(notify={"presence": "away", "container": "box-1"}))

    assert events.created_ids == [request_id]


@pytest.mark.spec("docs/service.md")
def test_should_read_the_hint_of_a_request_and_default_it_to_unknown():
    events = RecordingEvents()
    queue, clock = queue_with(events)
    queue.create(payload(notify={"presence": "phone", "container": "box-1"}))
    queue.create(payload())
    queue.create(payload(notify="away"))

    clock.now += TTL + 1
    queue.sweep()

    assert [request.hint for request in events.expired_requests] == [
        NotifyHint(Presence.PHONE, "box-1"), NotifyHint(), NotifyHint()]


@pytest.mark.spec("docs/service.md")
def test_should_keep_the_hint_out_of_both_views_of_a_request():
    queue = RequestQueue()
    request_id = queue.create(payload(notify={"presence": "away", "container": "box-1"}))

    approver, requester = queue.peek(request_id), queue.get(request_id)

    assert "notify" not in approver and "hint" not in approver
    assert "notify" not in requester and "hint" not in requester


@pytest.mark.spec("docs/service.md")
def test_should_not_report_a_request_that_the_queue_refused():
    events = RecordingEvents()
    queue = RequestQueue(rate_limit=1, events=events)
    queue.create(payload())

    with pytest.raises(RateLimitError):
        queue.create(payload())
    with pytest.raises(RequestError):
        queue.create({"vars": []})

    assert len(events.created_ids) == 1


@pytest.mark.spec("docs/service.md")
def test_should_report_an_unanswered_request_once_when_it_expires():
    events = RecordingEvents()
    queue, clock = queue_with(events)
    request_id = queue.create(payload())

    clock.now += TTL + 1
    queue.sweep()
    queue.sweep()
    queue.get(request_id)
    queue.peek(request_id)
    queue.respond(request_id, "late")

    assert events.expired_ids == [request_id]


@pytest.mark.spec("docs/service.md")
def test_should_report_nothing_before_the_time_to_live_has_passed():
    events = RecordingEvents()
    queue, clock = queue_with(events)
    queue.create(payload())

    clock.now += TTL
    queue.sweep()

    assert events.expired_ids == []


@pytest.mark.spec("docs/service.md")
@pytest.mark.parametrize("read", ["get", "peek", "respond"])
def test_should_report_an_expiry_that_a_read_finds_before_the_sweeper_does(read):
    events = RecordingEvents()
    queue, clock = queue_with(events)
    request_id = queue.create(payload())
    clock.now += TTL + 1

    reader = {"get": lambda: queue.get(request_id), "peek": lambda: queue.peek(request_id),
              "respond": lambda: queue.respond(request_id, "x")}[read]
    reader()
    queue.sweep()

    assert events.expired_ids == [request_id]


@pytest.mark.spec("docs/service.md")
def test_should_report_each_of_several_expired_requests_once():
    events = RecordingEvents()
    queue, clock = queue_with(events)
    first = queue.create(payload())
    clock.now += 10
    second = queue.create(payload())
    clock.now += TTL - 5
    queue.sweep()
    clock.now += 10
    queue.sweep()

    assert events.expired_ids == [first, second]


@pytest.mark.spec("docs/service.md")
def test_should_not_report_a_request_that_was_answered_even_if_nobody_collected_it():
    events = RecordingEvents()
    queue, clock = queue_with(events)
    queue.respond(queue.create(payload()), "sealed")

    clock.now += TTL + 1
    queue.sweep()

    assert events.expired_ids == []


@pytest.mark.spec("docs/service.md")
def test_should_not_report_a_request_that_the_requester_collected_or_withdrew():
    events = RecordingEvents()
    queue, clock = queue_with(events)
    collected, withdrawn = queue.create(payload()), queue.create(payload())
    queue.respond(collected, "sealed")
    queue.get(collected)
    queue.withdraw(withdrawn)

    clock.now += TTL + 1
    queue.sweep()

    assert events.expired_ids == []


@pytest.mark.spec("docs/service.md")
def test_should_report_outside_the_lock_so_a_listener_may_read_the_queue():
    seen: list[object] = []

    class Reading(RecordingEvents):
        def created(self, request: UnlockRequest) -> None:
            seen.append(queue.peek(request.id))

        def expired(self, request: UnlockRequest) -> None:
            seen.append(queue.peek(request.id))
    queue, clock = queue_with(Reading())
    queue.create(payload())
    clock.now += TTL + 1

    queue.sweep()

    assert [view is None for view in seen] == [False, True]


@pytest.mark.spec("docs/service.md")
def test_should_survive_a_listener_that_raises_and_log_no_detail_of_the_request():
    class Failing(RecordingEvents):
        def created(self, request: UnlockRequest) -> None:
            raise RuntimeError(f"secret detail {request.title}")

        def expired(self, request: UnlockRequest) -> None:
            super().expired(request)
            raise RuntimeError(f"secret detail {request.title}")
    events = Failing()
    queue, clock = queue_with(events)
    stderr = io.StringIO()

    with redirect_stderr(stderr):
        first = queue.create(payload(title="private title"))
        second = queue.create(payload(title="private title"))
        clock.now += TTL + 1
        queue.sweep()

    assert queue.peek(first) is None and queue.peek(second) is None
    assert events.expired_ids == [first, second]
    assert stderr.getvalue().splitlines() == [
        "requests: created notice failed (RuntimeError)", "requests: created notice failed (RuntimeError)",
        "requests: expired notice failed (RuntimeError)", "requests: expired notice failed (RuntimeError)"]
