from __future__ import annotations

import base64
import shlex
import subprocess

import pytest

from keyguard_passkey.encoding import as_env_line, as_shell_export, from_one_line, on_one_line

EXPORTED = {"PLAIN": "value", "SPACES": "a b c", "QUOTE": "it's \"quoted\"", "INJECT": "x; rm -rf / $(id) `id`",
            "EQ": "a=b=c", "MULTI": "line one\nline two\n", "UNICODE": "Caf\u00e9 \u2013 ok"}


@pytest.mark.spec("docs/provider.md")
def test_should_leave_a_plain_value_as_it_is():
    assert on_one_line("p@ss=word#1") == "p@ss=word#1"


@pytest.mark.spec("docs/provider.md")
@pytest.mark.parametrize("value", ["line one\nline two", "carriage\rreturn", "base64:not encoded"])
def test_should_encode_a_value_that_a_reader_would_misread(value):
    assert on_one_line(value) == "base64:" + base64.b64encode(value.encode()).decode()


@pytest.mark.spec("docs/provider.md")
@pytest.mark.parametrize("value", ["plain", "line one\nline two", "base64:not encoded", "base64:aGVsbG8=", ""])
def test_should_read_back_every_value_it_wrote(value):
    assert from_one_line(on_one_line(value)) == value


@pytest.mark.spec("README.md")
@pytest.mark.parametrize("text", ["base64:not base64", "base64:/w=="])
def test_should_keep_text_after_the_marker_that_is_not_base64_of_utf8(text):
    assert from_one_line(text) == text


@pytest.mark.spec("docs/service.md")
def test_should_export_values_that_a_shell_evaluates_to_exactly_the_value():
    exports = "".join(as_shell_export(name, value) for name, value in EXPORTED.items())
    checks = "".join(f'printf "%s\\0%s\\0" {name} "${{{name}}}"\n' for name in EXPORTED)

    shell = subprocess.run(["bash", "-c", exports + checks], capture_output=True, text=True, check=True)

    fields = shell.stdout.split("\0")[:-1]
    assert dict(zip(fields[::2], fields[1::2])) == EXPORTED


@pytest.mark.spec("docs/service.md")
def test_should_export_values_that_shlex_reads_back_whole():
    tokens = shlex.split("".join(as_shell_export(name, value) for name, value in EXPORTED.items()))

    assert tokens[::2] == ["export"] * len(EXPORTED)
    assert dict(token.split("=", 1) for token in tokens[1::2]) == EXPORTED


@pytest.mark.spec("docs/provider.md")
@pytest.mark.parametrize("value, line", [("p@ss=word", "A=p@ss=word\n"),
                                         ("two\nlines", "A=base64:" + base64.b64encode(b"two\nlines").decode() + "\n")])
def test_should_print_one_env_line_per_name(value, line):
    assert as_env_line("A", value) == line
