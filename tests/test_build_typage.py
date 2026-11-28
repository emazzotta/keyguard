from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest

from tools import build_typage

DELETE = b"\x7f"


@pytest.mark.spec("README.md#third-party-code")
def test_should_put_the_header_first_and_escape_the_delete_character():
    built = build_typage.finished(b'var a="A0+' + DELETE + b'";\n', "0.3.1", "1.4.4")

    first, rest = built.split(b"\n", 1)
    assert first.startswith(b"// age-encryption (typage) 0.3.1 with its dependencies, bundled with bun 1.4.4 ")
    assert rest == b'var a="A0+\\x7f";\n'


@pytest.mark.spec("README.md#third-party-code")
def test_should_refuse_a_bundle_with_another_raw_control_character():
    with pytest.raises(build_typage.BuildError, match="0x1b"):
        build_typage.finished(b'var a="\x1b";', "0.3.1", "1.4.4")


@pytest.mark.spec("README.md#third-party-code")
def test_should_refuse_a_bun_that_is_not_the_pinned_release(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *_, **__: SimpleNamespace(stdout="0.0.1\n"))

    with pytest.raises(build_typage.BuildError, match=f"bun 0.0.1 is on PATH.*bun {build_typage.pinned_bun()}"):
        build_typage.require_pinned_bun("bun")


@pytest.mark.spec("README.md#third-party-code")
def test_should_ship_a_bundle_whose_header_names_the_pinned_typage_and_bun():
    shipped_header = build_typage.BUNDLE.read_bytes().split(b"\n", 1)[0] + b"\n"

    assert shipped_header == build_typage.header(build_typage.typage_version(), build_typage.pinned_bun())
