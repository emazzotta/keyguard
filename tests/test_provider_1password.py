from __future__ import annotations

import base64
import os
import shutil
import subprocess
from pathlib import Path

import pytest

SHIM = Path(__file__).resolve().parents[1] / "providers" / "1password"
pytestmark = pytest.mark.skipif(shutil.which("sh") is None, reason="the shim needs a POSIX shell")

STAND_IN = """#!/bin/sh
[ "$1" = read ] && [ "$2" = --no-newline ] || exit 64
case "$3" in
    op://Work/API_TOKEN/password) printf 'abc 123' ;;
    op://Work/API_TOKEN/credential) printf 'from another field' ;;
    op://Work/PRIVATE_KEY/password) printf 'line one\\nline two' ;;
    *) echo "[ERROR] could not read secret" >&2; exit 1 ;;
esac
"""


@pytest.fixture
def shim(tmp_path):
    stand_in = tmp_path / "op"
    stand_in.write_text(STAND_IN, encoding="utf-8")
    stand_in.chmod(0o755)

    def run(*arguments, **environ):
        env = {"PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}", "KEYGUARD_OP_VAULT": "Work", **environ}
        done = subprocess.run(["sh", str(SHIM), *arguments], capture_output=True, text=True, env=env, check=False)
        return done.returncode, done.stdout

    return run


@pytest.mark.spec("docs/provider.md")
def test_should_read_each_name_as_an_item_of_the_vault(shim):
    multi_line = "PRIVATE_KEY=base64:" + base64.b64encode(b"line one\nline two").decode()

    assert shim("get", "PRIVATE_KEY", "API_TOKEN") == (0, multi_line + "\nAPI_TOKEN=abc 123\n")


@pytest.mark.spec("docs/provider.md")
def test_should_read_the_field_that_the_setting_names(shim):
    assert shim("get", "API_TOKEN", KEYGUARD_OP_FIELD="credential") == (0, "API_TOKEN=from another field\n")


@pytest.mark.spec("docs/provider.md")
def test_should_exit_3_and_print_no_value_when_1password_does_not_release(shim):
    assert shim("get", "API_TOKEN", "NOT_AN_ITEM") == (3, "")


@pytest.mark.spec("docs/provider.md")
def test_should_exit_3_without_a_vault(shim):
    assert shim("get", "API_TOKEN", KEYGUARD_OP_VAULT="") == (3, "")


@pytest.mark.spec("docs/provider.md")
@pytest.mark.parametrize("arguments", [("list",), ("set", "A"), ("rm", "A"), (), ("get",), ("get", "a/b")])
def test_should_exit_64_for_a_verb_or_a_name_it_does_not_support(shim, arguments):
    assert shim(*arguments) == (64, "")
