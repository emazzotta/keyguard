"""Tests for the presence reader: the folder that each web-terminal instance writes, read with a frozen clock."""
from __future__ import annotations

from pathlib import Path

import pytest

from keyguard_server import presence
from keyguard_server.presence import Presence

NOW = 1_800_000_000


@pytest.fixture()
def folder(tmp_path) -> Path:
    found = tmp_path / "presence"
    found.mkdir()
    return found


def write(folder: Path, name: str, *lines: str) -> None:
    (folder / name).write_text("".join(f"{line}\n" for line in lines))


def test_should_read_computer_when_a_fresh_line_says_computer(folder):
    write(folder, "web-terminal", f"{NOW - 5} computer")

    assert presence.read_presence(folder, NOW) is Presence.COMPUTER


def test_should_read_phone_when_a_fresh_line_says_phone(folder):
    write(folder, "web-terminal", f"{NOW - 5} phone")

    assert presence.read_presence(folder, NOW) is Presence.PHONE


def test_should_read_computer_before_phone_when_both_are_fresh(folder):
    write(folder, "web-terminal", f"{NOW - 5} phone", f"{NOW - 9} computer")

    assert presence.read_presence(folder, NOW) is Presence.COMPUTER


@pytest.mark.parametrize("age,expected", [(119, Presence.COMPUTER), (120, Presence.AWAY), (3600, Presence.AWAY)])
def test_should_count_a_line_as_fresh_until_it_is_120_seconds_old(folder, age, expected):
    write(folder, "web-terminal", f"{NOW - age} computer")

    assert presence.read_presence(folder, NOW) is expected


def test_should_count_a_line_dated_in_the_future_as_fresh(folder):
    write(folder, "web-terminal", f"{NOW + 600} phone")

    assert presence.read_presence(folder, NOW) is Presence.PHONE


def test_should_read_away_when_the_folder_holds_no_fresh_line(folder):
    write(folder, "web-terminal", f"{NOW - 500} computer")
    write(folder, "other-instance")

    assert presence.read_presence(folder, NOW) is Presence.AWAY


def test_should_read_away_when_the_folder_is_empty(folder):
    assert presence.read_presence(folder, NOW) is Presence.AWAY


def test_should_read_unknown_when_the_folder_is_missing(tmp_path):
    assert presence.read_presence(tmp_path / "missing", NOW) is Presence.UNKNOWN


def test_should_read_unknown_when_the_folder_cannot_be_listed(tmp_path):
    not_a_folder = tmp_path / "presence"
    not_a_folder.write_text("")

    assert presence.read_presence(not_a_folder, NOW) is Presence.UNKNOWN


def test_should_merge_the_files_of_several_instances(folder):
    write(folder, "instance-a", f"{NOW - 500} computer")
    write(folder, "instance-b", f"{NOW - 10} phone")

    assert presence.read_presence(folder, NOW) is Presence.PHONE


def test_should_skip_a_dot_named_file(folder):
    write(folder, ".web-terminal.tmp", f"{NOW - 5} computer")

    assert presence.read_presence(folder, NOW) is Presence.AWAY


def test_should_skip_a_folder_inside_the_presence_folder(folder):
    (folder / "nested").mkdir()
    write(folder / "nested", "web-terminal", f"{NOW - 5} computer")

    assert presence.read_presence(folder, NOW) is Presence.AWAY


@pytest.mark.parametrize("line", [
    "",
    "computer",
    f"{NOW - 5} tablet",
    f"{NOW - 5} computer extra",
    f"-{NOW - 5} computer",
    "1234567890123 computer",
    "twelve computer",
])
def test_should_ignore_a_line_that_does_not_parse(folder, line):
    write(folder, "web-terminal", line, f"{NOW - 500} phone")

    assert presence.read_presence(folder, NOW) is Presence.AWAY


def test_should_keep_reading_the_good_lines_beside_a_bad_one(folder):
    write(folder, "web-terminal", "garbage", f"{NOW - 5} phone")

    assert presence.read_presence(folder, NOW) is Presence.PHONE


@pytest.mark.parametrize("epoch", ["7", "999999999999"])
def test_should_accept_an_epoch_of_one_to_twelve_digits(epoch):
    assert presence.classify([f"{epoch} computer"], int(epoch)) is Presence.COMPUTER


def test_should_count_a_file_that_vanished_after_the_listing_as_no_lines(folder, monkeypatch):
    write(folder, "gone", f"{NOW - 5} computer")
    write(folder, "kept", f"{NOW - 5} phone")
    real_open = Path.open

    def open_unless_gone(self, *args, **kwargs):
        if self.name == "gone":
            raise FileNotFoundError(self.name)
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", open_unless_gone)

    assert presence.read_presence(folder, NOW) is Presence.PHONE


def test_should_read_only_the_start_of_an_oversized_file(folder):
    write(folder, "web-terminal", *["junk"] * presence.MAX_FILE_BYTES, f"{NOW - 5} computer")

    assert presence.read_presence(folder, NOW) is Presence.AWAY


def test_should_read_the_configured_folder_at_the_time_the_clock_gives(folder, monkeypatch):
    write(folder, "web-terminal", f"{NOW - 5} computer")
    monkeypatch.setattr(presence, "PRESENCE_DIR", folder)

    assert presence.current(lambda: NOW) is Presence.COMPUTER
    assert presence.current(lambda: NOW + 3600) is Presence.AWAY


@pytest.mark.parametrize("seen,attended", [
    (Presence.COMPUTER, True),
    (Presence.PHONE, True),
    (Presence.AWAY, False),
    (Presence.UNKNOWN, False),
])
def test_should_count_unknown_as_away_when_asking_whether_the_owner_is_attending(seen, attended):
    assert seen.attended is attended
