from __future__ import annotations

import pytest

from keyguard_passkey.envfile import EnvFileError, parse_env, read_env_file


@pytest.mark.spec("README.md")
@pytest.mark.parametrize("line", [
    "TOKEN=abc123",
    "export TOKEN=abc123",
    'export TOKEN="abc123"',
    "export TOKEN='abc123'",
    'export TOKEN="abc123" # inline comments are stripped',
    "TOKEN=abc123 # trailing comment",
    "  TOKEN=abc123  ",
])
def test_should_read_each_accepted_line_format(line):
    assert parse_env(line) == {"TOKEN": "abc123"}


@pytest.mark.spec("README.md")
def test_should_keep_a_hash_inside_quotes():
    assert parse_env('TOKEN="abc #123"') == {"TOKEN": "abc #123"}


@pytest.mark.spec("README.md")
def test_should_decode_a_value_that_get_printed_as_base64():
    assert parse_env("KEY=base64:bGluZSBvbmUKbGluZSB0d28=") == {"KEY": "line one\nline two"}


@pytest.mark.spec("README.md")
def test_should_skip_blank_lines_and_comments_and_keep_file_order():
    assert list(parse_env("# header\n\nB=2\nA=1\n")) == ["B", "A"]


@pytest.mark.spec("README.md")
def test_should_name_the_line_number_and_never_the_content_of_a_bad_line():
    with pytest.raises(EnvFileError) as raised:
        parse_env("A=1\nsupersecret without equals\n")

    assert str(raised.value) == "line 2 is not NAME=value"


@pytest.mark.spec("README.md")
def test_should_refuse_a_file_without_any_secret(tmp_path):
    empty = tmp_path / "empty.env"
    empty.write_text("# nothing\n", encoding="utf-8")

    with pytest.raises(EnvFileError, match="holds no NAME=value line"):
        read_env_file(empty)


@pytest.mark.spec("README.md")
def test_should_report_a_missing_file(tmp_path):
    with pytest.raises(EnvFileError, match="cannot read"):
        read_env_file(tmp_path / "missing.env")
