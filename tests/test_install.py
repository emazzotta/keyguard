from __future__ import annotations

import json

import pytest

from keyguard_passkey.install import InstallError, Manifest, differences, digests, manifest_path, read_manifest


@pytest.fixture
def copy(tmp_path):
    root = tmp_path / "bin"
    (root / "keyguard_passkey" / "pages").mkdir(parents=True)
    (root / "keyguard-passkey").write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    (root / "keyguard_passkey" / "cli.py").write_text("def main():\n    return 0\n", encoding="utf-8")
    (root / "keyguard_passkey" / "pages" / "approve.js").write_text("start()\n", encoding="utf-8")
    manifest = Manifest(tag="v1.2.3", commit="c0ffee", files=digests(root))
    manifest_path(root).write_text(json.dumps(manifest.as_json()), encoding="utf-8")
    return root


@pytest.mark.spec("docs/updating.md")
def test_should_list_the_launcher_and_every_file_of_the_package(copy):
    assert sorted(read_manifest(copy).files) == ["keyguard-passkey", "keyguard_passkey/cli.py",
                                                 "keyguard_passkey/pages/approve.js"]


@pytest.mark.spec("docs/updating.md")
def test_should_find_no_difference_in_an_untouched_copy(copy):
    assert differences(copy, read_manifest(copy)) == []


@pytest.mark.spec("docs/updating.md")
def test_should_name_a_changed_a_missing_and_an_added_file(copy):
    (copy / "keyguard_passkey" / "cli.py").write_text("def main():\n    return 1\n", encoding="utf-8")
    (copy / "keyguard_passkey" / "pages" / "approve.js").unlink()
    (copy / "keyguard_passkey" / "json.py").write_text("planted = True\n", encoding="utf-8")

    assert differences(copy, read_manifest(copy)) == [
        "changed: keyguard_passkey/cli.py",
        "missing: keyguard_passkey/pages/approve.js",
        "not in the manifest: keyguard_passkey/json.py",
    ]


@pytest.mark.spec("docs/updating.md")
def test_should_accept_a_checkout_with_windows_line_endings(copy):
    (copy / "keyguard_passkey" / "cli.py").write_bytes(b"def main():\r\n    return 0\r\n")

    assert differences(copy, read_manifest(copy)) == []


@pytest.mark.spec("docs/updating.md")
def test_should_ignore_the_bytecode_that_python_writes(copy):
    cache = copy / "keyguard_passkey" / "__pycache__"
    cache.mkdir()
    (cache / "cli.cpython-313.pyc").write_bytes(b"\x00")

    assert differences(copy, read_manifest(copy)) == []


@pytest.mark.spec("docs/updating.md")
def test_should_report_a_copy_without_a_manifest(copy):
    manifest_path(copy).unlink()

    with pytest.raises(InstallError, match="not a vendored copy"):
        read_manifest(copy)


@pytest.mark.spec("docs/updating.md")
@pytest.mark.parametrize("content", ["not json", "[]", '{"files": ["a"]}', '{"files": {"a": 1}}'])
def test_should_refuse_a_manifest_it_cannot_read(copy, content):
    manifest_path(copy).write_text(content, encoding="utf-8")

    with pytest.raises(InstallError):
        read_manifest(copy)
