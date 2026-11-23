from __future__ import annotations

import pytest

from keyguard_passkey.service.hint import NotifyHint, clean_container, parse_hint
from keyguard_passkey.service.presence import Presence

LABEL = "devbox-7f7d78d989-tgd9t"


@pytest.mark.spec("docs/service.md")
@pytest.mark.parametrize(("raw", "expected"), [
    ({"presence": "away", "container": LABEL}, NotifyHint(Presence.AWAY, LABEL)),
    ({"presence": "computer"}, NotifyHint(Presence.COMPUTER)),
    ({"presence": "phone", "container": None}, NotifyHint(Presence.PHONE)),
    ({"presence": "unknown", "container": "c" * 63}, NotifyHint(Presence.UNKNOWN, "c" * 63)),
    ({"presence": "present"}, NotifyHint()),
    ({"presence": "AWAY"}, NotifyHint()),
    ({"presence": 1}, NotifyHint()),
    ({"presence": ["away"]}, NotifyHint()),
    ({"presence": {}}, NotifyHint()),
    ({"presence": "away", "container": "c" * 64}, NotifyHint(Presence.AWAY)),
    ({"presence": "away", "container": "dev box"}, NotifyHint(Presence.AWAY)),
    ({"presence": "away", "container": "<b>x</b>"}, NotifyHint(Presence.AWAY)),
    ({"presence": "away", "container": "a\nb"}, NotifyHint(Presence.AWAY)),
    ({"presence": "away", "container": 7}, NotifyHint(Presence.AWAY)),
    ({"presence": "away", "other": "ignored"}, NotifyHint(Presence.AWAY)),
    ({}, NotifyHint()),
])
def test_should_read_what_it_can_from_the_hint_and_never_refuse_it(raw, expected):
    assert parse_hint(raw) == expected


@pytest.mark.spec("docs/service.md")
@pytest.mark.parametrize("raw", [None, [], "away", 7, True])
def test_should_read_no_hint_at_all_from_a_field_that_is_not_an_object(raw):
    assert parse_hint(raw) is None


@pytest.mark.spec("docs/service.md")
@pytest.mark.parametrize(("hint", "wire"), [
    (NotifyHint(Presence.AWAY, LABEL), {"presence": "away", "container": LABEL}),
    (NotifyHint(Presence.COMPUTER), {"presence": "computer"}),
    (NotifyHint(), {"presence": "unknown"}),
])
def test_should_write_the_wire_form_that_it_reads_back(hint, wire):
    assert hint.wire() == wire
    assert parse_hint(hint.wire()) == hint


@pytest.mark.spec("docs/service.md")
@pytest.mark.parametrize(("label", "clean"), [(LABEL, LABEL), ("", None), ("dev.box", None), (None, None), (3, None)])
def test_should_keep_a_container_label_only_when_it_is_a_host_label(label, clean):
    assert clean_container(label) == clean
