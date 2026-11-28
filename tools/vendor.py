#!/usr/bin/env python3
"""Copy the client at a release tag into another repository, with the manifest that verify-install checks,
or pack it into one zip application whose bytes depend only on the tag."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import shutil
import stat
import subprocess
import sys
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Final

REPOSITORY: Final = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPOSITORY))

from keyguard_passkey.install import LAUNCHER, PACKAGE, Manifest, digests, manifest_path

TAG_PREFIX: Final = "v"
VERSION_FILE: Final = f"{PACKAGE}/__init__.py"
EXECUTABLE: Final = 0o755
ZIP_APPLICATION: Final = f"{LAUNCHER}.pyz"
ZIP_SHEBANG: Final = b"#!/usr/bin/env python3\n"
ZIP_MAIN_MODULE: Final = "__main__.py"
ZIP_MAIN_SOURCE: Final = f"from {PACKAGE}.cli import main\n\nraise SystemExit(main())\n".encode()
ZIP_DATE: Final = (1980, 1, 1, 0, 0, 0)
ZIP_FILE_MODE: Final = stat.S_IFREG | 0o644
ZIP_MADE_ON_UNIX: Final = 3
ZIP_MODE_SHIFT: Final = 16


class VendorError(Exception):
    pass


@dataclass(frozen=True)
class ZipApplication:
    path: Path
    tag: str
    commit: str
    sha256: str


def git(repository: Path, *arguments: str) -> bytes:
    try:
        return subprocess.run(["git", "-C", str(repository), *arguments], check=True, capture_output=True).stdout
    except subprocess.CalledProcessError as error:
        raise VendorError(error.stderr.decode(errors="replace").strip()) from error


def commit_of(repository: Path, tag: str) -> str:
    return git(repository, "rev-parse", "--verify", f"refs/tags/{tag}^{{commit}}").decode().strip()


def require_release(repository: Path, tag: str) -> None:
    version = tag[len(TAG_PREFIX):] if tag.startswith(TAG_PREFIX) else None
    if version is None or f'__version__ = "{version}"' not in git(repository, "show", f"{tag}:{VERSION_FILE}").decode():
        raise VendorError(f"{tag} is not a release: {VERSION_FILE} at that tag holds another version")


def require_folder(destination: Path) -> None:
    if not destination.is_dir():
        raise VendorError(f"{destination} is not a folder")


def require_replaceable(destination: Path) -> None:
    require_folder(destination)
    if (destination / PACKAGE).exists() and not manifest_path(destination).is_file():
        raise VendorError(f"{destination / PACKAGE} is not a vendored copy, so it is not replaced")


def release_files(repository: Path, tag: str, *paths: str) -> dict[str, bytes]:
    """The files as the tag holds them, whatever the working tree or its line endings."""
    require_release(repository, tag)
    listed = git(repository, "ls-tree", "-r", "--name-only", tag, "--", *paths).decode().splitlines()
    return {path: git(repository, "show", f"{tag}:{path}") for path in listed}


def vendor(repository: Path, tag: str, destination: Path) -> Manifest:
    commit = commit_of(repository, tag)
    files = release_files(repository, tag, LAUNCHER, PACKAGE)
    require_replaceable(destination)
    shutil.rmtree(destination / PACKAGE, ignore_errors=True)
    for path, content in files.items():
        target = destination / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    (destination / LAUNCHER).chmod(EXECUTABLE)
    manifest = Manifest(tag=tag, commit=commit, files=digests(destination))
    manifest_path(destination).write_text(json.dumps(manifest.as_json(), indent=2) + "\n", encoding="utf-8")
    return manifest


def zip_entry(name: str) -> zipfile.ZipInfo:
    entry = zipfile.ZipInfo(name, date_time=ZIP_DATE)
    entry.create_system = ZIP_MADE_ON_UNIX
    entry.external_attr = ZIP_FILE_MODE << ZIP_MODE_SHIFT
    return entry


def packed(files: dict[str, bytes]) -> bytes:
    """Stored, not deflated: zlib builds compress the same input to different bytes."""
    archive = io.BytesIO()
    archive.write(ZIP_SHEBANG)
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_STORED) as entries:
        for name in sorted(files):
            entries.writestr(zip_entry(name), files[name])
    return archive.getvalue()


def zip_application(repository: Path, tag: str, destination: Path) -> ZipApplication:
    commit = commit_of(repository, tag)
    files = {**release_files(repository, tag, PACKAGE), ZIP_MAIN_MODULE: ZIP_MAIN_SOURCE}
    require_folder(destination)
    content = packed(files)
    target = destination / ZIP_APPLICATION
    target.write_bytes(content)
    target.chmod(EXECUTABLE)
    return ZipApplication(path=target, tag=tag, commit=commit, sha256=hashlib.sha256(content).hexdigest())


def run(args: argparse.Namespace) -> str:
    if args.pyz:
        built = zip_application(REPOSITORY, args.tag, args.destination)
        return f"Packed {built.tag} ({built.commit}) into {built.path}\nSHA-256 {built.sha256}"
    manifest = vendor(REPOSITORY, args.tag, args.destination)
    return f"Copied {len(manifest.files)} files of {manifest.tag} ({manifest.commit}) into {args.destination}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pyz", action="store_true", help=f"pack the client into DESTINATION/{ZIP_APPLICATION}")
    parser.add_argument("tag", help="a release tag of this repository, as v1.2.3")
    parser.add_argument("destination", type=Path, help="the folder that gets the launcher and the package")
    args = parser.parse_args(argv)
    try:
        print(run(args))
    except VendorError as error:
        print(f"vendor: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
