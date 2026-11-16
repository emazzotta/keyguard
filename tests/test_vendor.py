from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import time
import urllib.request
import zipfile
from pathlib import Path

import pytest
from conftest import LINK, REQUESTER

from keyguard_passkey import __version__
from keyguard_passkey.install import LAUNCHER, PACKAGE, differences, manifest_path, read_manifest
from tools import vendor

IDENTITY = ("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "-c", "commit.gpgsign=false",
            "-c", "tag.gpgsign=false")
ROOT = Path(__file__).resolve().parents[1]
VERSION_FILE = f"{PACKAGE}/__init__.py"
PAGE_ADDRESS = re.compile(r"(http://localhost:\d+/\S*)#token=")
RUN_TIMEOUT_SECONDS = 30
HELD_SECONDS = "60"
LATER = 2_000_000_000
BASE_PYTHON = Path(sys.base_prefix) / ("python.exe" if os.name == "nt" else
                                      f"bin/python{sys.version_info.major}.{sys.version_info.minor}")


def git(repository, *arguments):
    done = subprocess.run(["git", "-C", str(repository), *IDENTITY, *arguments], check=True, capture_output=True,
                          text=True, env={**os.environ, "GIT_CONFIG_GLOBAL": os.devnull})
    return done.stdout.strip()


def release(repository, version, files):
    for path, content in {"keyguard_passkey/__init__.py": f'__version__ = "{version}"\n', **files}.items():
        (repository / path).parent.mkdir(parents=True, exist_ok=True)
        (repository / path).write_text(content, encoding="utf-8")
    git(repository, "add", "--all")
    git(repository, "commit", "--allow-empty", "-m", f"Release {version}")
    git(repository, "tag", f"v{version}")
    return git(repository, "rev-parse", "HEAD")


@pytest.fixture
def repository(tmp_path):
    folder = tmp_path / "source"
    folder.mkdir()
    git(folder, "init", "--quiet")
    return folder


@pytest.fixture
def destination(tmp_path):
    folder = tmp_path / "host" / "bin"
    folder.mkdir(parents=True)
    return folder


CLIENT = {"keyguard-passkey": "#!/usr/bin/env python3\n", "keyguard_passkey/pages/approve.js": "start()\n",
          "README.md": "not part of the client\n"}


@pytest.mark.spec("docs/updating.md")
def test_should_copy_the_launcher_and_the_package_with_a_manifest_of_tag_commit_and_hashes(repository, destination):
    commit = release(repository, "1.2.3", CLIENT)

    vendor.vendor(repository, "v1.2.3", destination)

    manifest = json.loads(manifest_path(destination).read_text(encoding="utf-8"))
    assert (manifest["tag"], manifest["commit"]) == ("v1.2.3", commit)
    assert sorted(manifest["files"]) == ["keyguard-passkey", "keyguard_passkey/__init__.py",
                                         "keyguard_passkey/pages/approve.js"]
    assert differences(destination, read_manifest(destination)) == []
    assert (destination / "keyguard-passkey").stat().st_mode & stat.S_IXUSR
    assert not (destination / "README.md").exists()


@pytest.mark.spec("docs/updating.md")
def test_should_copy_the_files_of_the_tag_and_not_of_the_working_tree(repository, destination):
    release(repository, "1.2.3", CLIENT)
    (repository / "keyguard_passkey" / "pages" / "approve.js").write_text("changed()\n", encoding="utf-8")

    vendor.vendor(repository, "v1.2.3", destination)

    assert (destination / "keyguard_passkey" / "pages" / "approve.js").read_text(encoding="utf-8") == "start()\n"


@pytest.mark.spec("docs/updating.md")
def test_should_drop_a_file_that_the_new_release_no_longer_has(repository, destination):
    release(repository, "1.2.3", {**CLIENT, "keyguard_passkey/old.py": "gone = True\n"})
    vendor.vendor(repository, "v1.2.3", destination)
    git(repository, "rm", "--quiet", "keyguard_passkey/old.py")
    release(repository, "1.3.0", {})

    vendor.vendor(repository, "v1.3.0", destination)

    assert not (destination / "keyguard_passkey" / "old.py").exists()
    assert read_manifest(destination).tag == "v1.3.0"
    assert differences(destination, read_manifest(destination)) == []


@pytest.mark.spec("docs/updating.md")
def test_should_refuse_a_tag_that_does_not_exist(repository, destination):
    release(repository, "1.2.3", CLIENT)

    with pytest.raises(vendor.VendorError):
        vendor.vendor(repository, "v1.2.4", destination)


@pytest.mark.spec("docs/updating.md")
def test_should_not_replace_a_folder_that_is_no_vendored_copy(repository, destination):
    release(repository, "1.2.3", CLIENT)
    own = destination / "keyguard_passkey" / "notes.txt"
    own.parent.mkdir()
    own.write_text("mine\n", encoding="utf-8")

    with pytest.raises(vendor.VendorError, match="not a vendored copy"):
        vendor.vendor(repository, "v1.2.3", destination)

    assert own.read_text(encoding="utf-8") == "mine\n"


@pytest.mark.spec("docs/updating.md")
def test_should_report_a_failure_and_exit_1(repository, tmp_path, capsys, monkeypatch):
    release(repository, "1.2.3", CLIENT)
    monkeypatch.setattr(vendor, "REPOSITORY", repository)

    assert vendor.main(["v1.2.3", str(tmp_path / "missing")]) == 1
    assert "is not a folder" in capsys.readouterr().err


def folder(path):
    path.mkdir(parents=True)
    return path


@pytest.fixture(scope="module")
def packed_client(tmp_path_factory):
    """This checkout's client, released under its own version and packed the way a release is."""
    source = tmp_path_factory.mktemp("client-source")
    git(source, "init", "--quiet")
    tracked = git(ROOT, "ls-files", "--", LAUNCHER, PACKAGE).splitlines()
    release(source, __version__, {path: (ROOT / path).read_text(encoding="utf-8") for path in tracked
                                  if path != VERSION_FILE})
    return vendor.zip_application(source, f"v{__version__}", tmp_path_factory.mktemp("packed")).path


@pytest.fixture
def packed_environment(tmp_path):
    kept = {name: os.environ[name] for name in ("PATH", "TMPDIR", "SYSTEMROOT") if name in os.environ}
    return {**kept, "HOME": str(tmp_path), "XDG_CONFIG_HOME": str(tmp_path / "config"),
            "KEYGUARD_PASSKEY_STATE_DIR": str(tmp_path / "state"), "KEYGUARD_PASSKEY_HOLD_SECONDS": "0"}


def packed_command(application, *argv):
    """Outside the test environment, so the client and the hold it starts find the package only in the zip."""
    return [str(BASE_PYTHON), "-S", str(application), *argv]


def run_packed(application, environment, *argv):
    return subprocess.run(packed_command(application, *argv), capture_output=True, text=True,
                          timeout=RUN_TIMEOUT_SECONDS, env=environment, check=False)


@pytest.mark.spec("docs/updating.md")
def test_should_pack_the_package_and_a_main_module_into_one_zip_application(repository, destination):
    release(repository, "1.2.3", CLIENT)

    built = vendor.zip_application(repository, "v1.2.3", destination)

    content = built.path.read_bytes()
    with zipfile.ZipFile(built.path) as archive:
        assert archive.namelist() == ["__main__.py", "keyguard_passkey/__init__.py",
                                      "keyguard_passkey/pages/approve.js"]
    assert content.startswith(b"#!/usr/bin/env python3\n")
    assert built.path.stat().st_mode & stat.S_IXUSR
    assert (built.tag, built.sha256) == ("v1.2.3", hashlib.sha256(content).hexdigest())


@pytest.mark.spec("docs/updating.md")
def test_should_pack_the_same_bytes_from_the_same_tag_at_any_time(repository, tmp_path, monkeypatch):
    release(repository, "1.2.3", CLIENT)
    first = vendor.zip_application(repository, "v1.2.3", folder(tmp_path / "first"))
    (repository / "keyguard_passkey" / "pages" / "approve.js").write_text("changed()\n", encoding="utf-8")
    monkeypatch.setattr(time, "time", lambda: LATER)

    second = vendor.zip_application(repository, "v1.2.3", folder(tmp_path / "second"))

    assert second.path.read_bytes() == first.path.read_bytes()


@pytest.mark.spec("docs/updating.md")
@pytest.mark.parametrize("build", [vendor.vendor, vendor.zip_application])
def test_should_refuse_to_pack_or_copy_a_tag_whose_files_hold_another_version(repository, destination, build):
    release(repository, "1.2.3", CLIENT)
    git(repository, "tag", "v9.9.9")

    with pytest.raises(vendor.VendorError, match="not a release"):
        build(repository, "v9.9.9", destination)

    assert list(destination.iterdir()) == []


@pytest.mark.spec("docs/updating.md")
def test_should_report_the_hash_of_a_packed_zip_application(repository, destination, capsys, monkeypatch):
    release(repository, "1.2.3", CLIENT)
    monkeypatch.setattr(vendor, "REPOSITORY", repository)

    code = vendor.main(["--pyz", "v1.2.3", str(destination)])

    digest = hashlib.sha256((destination / "keyguard-passkey.pyz").read_bytes()).hexdigest()
    assert (code, digest in capsys.readouterr().out) == (0, True)


@pytest.mark.spec("docs/updating.md")
def test_should_print_its_release_from_the_zip_application(packed_client, packed_environment):
    ran = run_packed(packed_client, packed_environment, "version")

    assert (ran.returncode, ran.stdout) == (0, f"{__version__}\n")


@pytest.mark.spec("docs/updating.md")
def test_should_serve_its_pages_from_inside_the_zip_application(packed_client, packed_environment, tmp_path):
    asking = subprocess.Popen(packed_command(packed_client, "--store", str(tmp_path / "store"), "--no-browser", "init"),
                              stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, env=packed_environment)
    try:
        assert asking.stderr is not None
        address = next(found.group(1) for found in map(PAGE_ADDRESS.search, asking.stderr) if found)
        with urllib.request.urlopen(address, timeout=RUN_TIMEOUT_SECONDS) as page:
            served = page.read()
    finally:
        asking.kill()
        asking.wait()

    assert b'<script type="module" src="./enroll.js">' in served


@pytest.mark.spec("docs/updating.md")
def test_should_release_and_hold_a_value_through_the_service_from_the_zip_application(packed_client, packed_environment,
                                                                                      unlock_service):
    environment = {**packed_environment, "KEYGUARD_PASSKEY_HOLD_SECONDS": HELD_SECONDS}
    service = ("--service", unlock_service.base, "--requester", REQUESTER, "--signing-key",
               str(unlock_service.signing_key), "--token-file", str(unlock_service.token_file),
               "--remote-recipient", unlock_service.remote.recipient)
    asking = subprocess.Popen(packed_command(packed_client, *service, "get", "A"), stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True, env=environment)
    try:
        assert asking.stderr is not None
        request_id = next(found.group(1) for found in map(LINK.search, asking.stderr) if found)
        unlock_service.respond(request_id, {"A": "it's"})
        released, _ = asking.communicate(timeout=RUN_TIMEOUT_SECONDS)
        held = run_packed(packed_client, environment, *service, "get", "A")
    finally:
        asking.kill()
        run_packed(packed_client, environment, *service, "lock")

    assert (asking.returncode, released) == (0, "A=it's\n")
    assert (held.returncode, held.stdout, LINK.search(held.stderr)) == (0, "A=it's\n", None)


@pytest.mark.spec("docs/updating.md")
def test_should_name_a_zip_application_as_no_vendored_copy(packed_client, packed_environment):
    ran = run_packed(packed_client, packed_environment, "verify-install")

    assert (ran.returncode, "not a vendored copy" in ran.stderr, "Traceback" in ran.stderr) == (1, True, False)
