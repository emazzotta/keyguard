from __future__ import annotations

from pathlib import Path

import pytest

from keyguard_passkey.service.presence import FRESH_SECONDS, Presence, classify, read_presence

NOW = 1_000_000.0


def stamped(age: float, kind: str) -> str:
    return f"{int(NOW - age)} {kind}"


@pytest.mark.spec("docs/service.md")
@pytest.mark.parametrize(("lines", "expected"), [
    ([], Presence.AWAY),
    ([stamped(10, "computer")], Presence.COMPUTER),
    ([stamped(10, "phone")], Presence.PHONE),
    ([stamped(10, "phone"), stamped(60, "computer")], Presence.COMPUTER),
    ([stamped(FRESH_SECONDS - 1, "computer")], Presence.COMPUTER),
    ([stamped(FRESH_SECONDS, "computer")], Presence.AWAY),
    ([stamped(FRESH_SECONDS + 500, "phone")], Presence.AWAY),
    ([stamped(500, "computer"), stamped(5, "phone")], Presence.PHONE),
    ([stamped(500, "computer"), stamped(500, "phone")], Presence.AWAY),
    ([f"{NOW - 30.5} computer"], Presence.COMPUTER),
    ([f"  {int(NOW)} phone \n"], Presence.PHONE),
    ([stamped(-1000, "computer")], Presence.COMPUTER),
])
def test_should_take_the_kind_of_the_fresh_lines_computer_before_phone(lines, expected):
    assert classify(lines, NOW) == expected


FRESH = str(int(NOW) - 10)
ARABIC_FRESH = "".join(chr(0x0660 + int(digit)) for digit in FRESH)


@pytest.mark.spec("docs/service.md")
@pytest.mark.parametrize("line", [
    "", "garbage", FRESH, f"{FRESH} tablet", f"x {FRESH} computer", f"-{FRESH} computer", f"{FRESH} computer extra",
    f"{FRESH}  computer", f"{FRESH}\tcomputer", f"{FRESH}.5.0 computer", f"{FRESH * 3} computer", f"0x{FRESH} computer",
    f"{ARABIC_FRESH} computer", f"{FRESH}\u00a0computer", f"{FRESH} Computer", f"{FRESH}. computer",
])
def test_should_ignore_a_line_that_does_not_parse(line):
    assert classify([line, stamped(10, "phone")], NOW) == Presence.PHONE
    assert classify([line], NOW) == Presence.AWAY


@pytest.mark.spec("docs/service.md")
def test_should_say_unknown_when_the_folder_is_missing(tmp_path):
    assert read_presence(tmp_path / "presence", NOW) == Presence.UNKNOWN


@pytest.mark.spec("docs/service.md")
def test_should_say_unknown_when_the_path_is_a_file(tmp_path):
    (tmp_path / "presence").write_text(stamped(1, "computer"))

    assert read_presence(tmp_path / "presence", NOW) == Presence.UNKNOWN


@pytest.mark.spec("docs/service.md")
def test_should_say_unknown_when_the_folder_cannot_be_listed(tmp_path, monkeypatch):
    def refuse(self: Path):
        raise PermissionError(self)
    monkeypatch.setattr(Path, "iterdir", refuse)

    assert read_presence(tmp_path, NOW) == Presence.UNKNOWN


@pytest.mark.spec("docs/service.md")
def test_should_say_away_for_a_folder_without_a_fresh_line(tmp_path):
    (tmp_path / "one").write_text(stamped(900, "computer") + "\n")
    (tmp_path / "two").write_text("")

    assert read_presence(tmp_path, NOW) == Presence.AWAY


@pytest.mark.spec("docs/service.md")
def test_should_read_one_file_for_each_instance_and_take_the_best_kind(tmp_path):
    (tmp_path / "terminal-a").write_text(stamped(500, "computer") + "\n" + stamped(20, "phone") + "\n")
    (tmp_path / "terminal-b").write_text(stamped(30, "phone") + "\n")

    assert read_presence(tmp_path, NOW) == Presence.PHONE
    (tmp_path / "terminal-c").write_text(stamped(5, "computer") + "\n")
    assert read_presence(tmp_path, NOW) == Presence.COMPUTER


@pytest.mark.spec("docs/service.md")
def test_should_skip_what_is_not_a_readable_file(tmp_path):
    (tmp_path / "folder-with-a-name-like-a-file").mkdir()
    (tmp_path / "bad").write_bytes(b"\xff\xfe\x00 not text")
    (tmp_path / "good").write_text(stamped(10, "phone"))

    assert read_presence(tmp_path, NOW) == Presence.PHONE


@pytest.mark.spec("docs/service.md")
def test_should_read_only_the_start_of_a_large_file(tmp_path):
    (tmp_path / "big").write_text("junk\n" * 5000 + stamped(1, "computer") + "\n")

    assert read_presence(tmp_path, NOW) == Presence.AWAY
