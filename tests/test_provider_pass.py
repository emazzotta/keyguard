from __future__ import annotations

import base64
import os
import shutil
import subprocess
from pathlib import Path

import pytest

SHIM = Path(__file__).resolve().parents[1] / "providers" / "pass"
pytestmark = pytest.mark.skipif(shutil.which("sh") is None, reason="the shim needs a POSIX shell")

STAND_IN = """#!/bin/sh
verb="$1"
for last in "$@"; do :; done
case "$verb" in
    show)
        [ -z "${STAND_IN_LOCKED:-}" ] || exit 2
        cat "$STAND_IN_PLAIN/$last"
        ;;
    insert)
        mkdir -p "$(dirname "$STAND_IN_PLAIN/$last")" "$(dirname "$PASSWORD_STORE_DIR/$last")"
        cat > "$STAND_IN_PLAIN/$last"
        : > "$PASSWORD_STORE_DIR/$last.gpg"
        echo "Enter contents of $last and press Ctrl+D when finished:"
        ;;
    rm) rm -f "$STAND_IN_PLAIN/$last" "$PASSWORD_STORE_DIR/$last.gpg" ;;
esac
"""


@pytest.fixture
def shim(tmp_path):
    bin_folder, plain, store = tmp_path / "bin", tmp_path / "plain", tmp_path / "password-store"
    for folder in (bin_folder, plain, store):
        folder.mkdir()
    stand_in = bin_folder / "pass"
    stand_in.write_text(STAND_IN, encoding="utf-8")
    stand_in.chmod(0o755)

    def run(*arguments, stdin="", **environ):
        env = {"PATH": f"{bin_folder}{os.pathsep}{os.environ['PATH']}", "HOME": str(tmp_path),
               "PASSWORD_STORE_DIR": str(store), "STAND_IN_PLAIN": str(plain), **environ}
        done = subprocess.run(["sh", str(SHIM), *arguments], input=stdin, capture_output=True, text=True, env=env,
                              check=False)
        return done.returncode, done.stdout

    return run


@pytest.mark.spec("docs/provider.md")
def test_should_print_one_line_per_name_in_the_asked_order(shim):
    shim("set", "A", stdin="one\n")
    shim("set", "B", stdin="two\n")

    assert shim("get", "B", "A") == (0, "B=two\nA=one\n")


@pytest.mark.spec("docs/provider.md")
@pytest.mark.parametrize("value", ["line one\nline two", "base64:not encoded"])
def test_should_print_a_value_that_a_reader_would_misread_as_base64(shim, value):
    shim("set", "KEY", stdin=value)

    assert shim("get", "KEY") == (0, "KEY=base64:" + base64.b64encode(value.encode()).decode() + "\n")


@pytest.mark.spec("docs/provider.md")
def test_should_exit_4_and_print_no_value_when_one_name_is_absent(shim):
    shim("set", "A", stdin="one")

    assert shim("get", "A", "MISSING") == (4, "")


@pytest.mark.spec("docs/provider.md")
def test_should_exit_3_and_print_no_value_when_pass_does_not_release(shim):
    shim("set", "A", stdin="one")

    assert shim("get", "A", STAND_IN_LOCKED="yes") == (3, "")


@pytest.mark.spec("docs/provider.md")
def test_should_list_the_names_under_the_prefix_only(shim):
    shim("set", "OUTSIDE", stdin="x")
    shim("set", "B", stdin="x", KEYGUARD_PASS_PREFIX="work")
    shim("set", "A", stdin="x", KEYGUARD_PASS_PREFIX="work")

    assert shim("list", KEYGUARD_PASS_PREFIX="work") == (0, "A\nB\n")


@pytest.mark.spec("docs/provider.md")
def test_should_remove_an_entry(shim):
    shim("set", "A", stdin="one")

    assert shim("rm", "A") == (0, "")
    assert shim("get", "A") == (4, "")
    assert shim("rm", "A") == (4, "")


@pytest.mark.spec("docs/provider.md")
@pytest.mark.parametrize("arguments", [("unknown",), (), ("get",), ("get", "../outside"), ("set", "not-a-name")])
def test_should_exit_64_for_a_verb_or_a_name_outside_the_contract(shim, arguments):
    assert shim(*arguments) == (64, "")
