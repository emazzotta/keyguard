from __future__ import annotations

import random

import pytest

from keyguard_passkey.service.longtext import MESSAGE_LIMIT, multiline, split_parts, utf16_length

PARTY = "\U0001f389"
LIMIT = 100


def squeezed(parts: list[str]) -> str:
    return "".join("".join(parts).split())


@pytest.mark.spec("docs/service.md")
@pytest.mark.parametrize(("text", "expected"), [
    ("one\ntwo", "one\ntwo"),
    ("one\r\ntwo\rthree", "one\ntwo\nthree"),
    ("one\n\n\n\n\ntwo", "one\n\ntwo"),
    ("one   \n  two  ", "one\n  two"),
    ("\n\n  one\n\n", "one"),
    ("a\tb\x07c\x00d", "a b c d"),
    ("one\u202etwo\u200bthree", "one two three"),
    ("one\u2028two\x0bthree", "one\ntwo\nthree"),
    (" \n\t\u200b\n", ""),
    ("", ""),
])
def test_should_keep_the_line_breaks_and_drop_control_and_format_characters(text, expected):
    assert multiline(text) == expected


@pytest.mark.spec("docs/service.md")
def test_should_count_a_character_outside_the_basic_plane_twice():
    assert (utf16_length("abc"), utf16_length(PARTY), utf16_length("a" + PARTY)) == (3, 2, 3)


@pytest.mark.spec("docs/service.md")
def test_should_leave_a_text_that_fits_in_one_part():
    assert split_parts("one\n\ntwo", LIMIT) == ["one\n\ntwo"]


@pytest.mark.spec("docs/service.md")
@pytest.mark.parametrize(("boundary", "text"), [
    ("blank line", "a" * 40 + "\n\n" + "b" * 30 + "\n" + "c" * 30 + " " + "d" * 40),
    ("line break", "a" * 40 + "\n" + "b" * 30 + " " + "c" * 30 + " " + "d" * 40),
    ("space", "a" * 80 + " " + "b" * 60),
])
def test_should_cut_at_the_last_boundary_in_the_last_quarter_of_a_part(boundary, text):
    first, second = split_parts(text, LIMIT)

    assert second == text.split(first)[1].lstrip()
    assert text.startswith(first)
    assert first.rstrip() == first and second.lstrip() == second


@pytest.mark.spec("docs/service.md")
def test_should_prefer_a_blank_line_to_a_line_break_to_a_space():
    text = "a" * 77 + "\n\n" + "b\nc d" + "e" * 100

    assert split_parts(text, LIMIT)[0] == "a" * 77


@pytest.mark.spec("docs/service.md")
def test_should_ignore_a_boundary_before_the_last_quarter():
    text = "a" * 10 + "\n\n" + "b" * 200

    first, *_ = split_parts(text, LIMIT)

    assert first == "a" * 10 + "\n\n" + "b" * 88


@pytest.mark.spec("docs/service.md")
def test_should_split_a_line_without_a_boundary_at_the_limit():
    parts = split_parts("x" * 250, LIMIT)

    assert [len(part) for part in parts] == [100, 100, 50]


@pytest.mark.spec("docs/service.md")
def test_should_count_units_not_characters_when_it_cuts():
    parts = split_parts(PARTY * 120, LIMIT)

    assert [utf16_length(part) for part in parts] == [100, 100, 40]


@pytest.mark.spec("docs/service.md")
def test_should_refuse_a_part_with_no_room():
    with pytest.raises(ValueError):
        split_parts("text", 4)


@pytest.mark.spec("docs/service.md")
@pytest.mark.parametrize("seed", range(40))
def test_should_lose_nothing_and_keep_every_part_within_the_limit(seed):
    rng = random.Random(seed)
    pieces = ["word", "x" * rng.randint(1, 150), PARTY * rng.randint(1, 30), "&<>", "\n", "\n\n", " ", "  "]
    text = multiline("".join(rng.choice(pieces) for _ in range(rng.randint(1, 120))))

    parts = split_parts(text, LIMIT)

    assert all(0 < utf16_length(part) <= LIMIT for part in parts)
    assert squeezed(parts) == squeezed([text])


@pytest.mark.spec("docs/service.md")
def test_should_split_at_the_telegram_message_limit():
    parts = split_parts("paragraph one\n\n" * 600, MESSAGE_LIMIT)

    assert all(utf16_length(part) <= MESSAGE_LIMIT for part in parts)
    assert len(parts) == 3
