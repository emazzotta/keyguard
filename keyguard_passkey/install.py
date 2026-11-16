"""A vendored copy of the client, checked against the manifest that the vendor tool wrote beside it."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Final

LAUNCHER: Final = "keyguard-passkey"
PACKAGE: Final = "keyguard_passkey"
MANIFEST_FILE: Final = "VENDORED.json"
BYTECODE_FOLDER: Final = "__pycache__"
ROOT: Final = Path(__file__).resolve().parent.parent


class InstallError(Exception):
    pass


@dataclass(frozen=True)
class Manifest:
    tag: str
    commit: str
    files: dict[str, str]

    def as_json(self) -> dict[str, object]:
        return {"tag": self.tag, "commit": self.commit, "files": dict(sorted(self.files.items()))}


def manifest_path(root: Path) -> Path:
    return root / PACKAGE / MANIFEST_FILE


def digest(file: Path) -> str:
    """Line endings are normalised: a host repository may check the files out with CRLF on Windows."""
    return hashlib.sha256(file.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def client_files(root: Path) -> list[str]:
    in_package = [file.relative_to(root).as_posix() for file in (root / PACKAGE).rglob("*")
                  if file.is_file() and BYTECODE_FOLDER not in file.parts and file.name != MANIFEST_FILE]
    launcher = [LAUNCHER] if (root / LAUNCHER).is_file() else []
    return sorted([*launcher, *in_package])


def digests(root: Path) -> dict[str, str]:
    return {path: digest(root / path) for path in client_files(root)}


def read_manifest(root: Path) -> Manifest:
    source = manifest_path(root)
    try:
        raw = json.loads(source.read_text(encoding="utf-8"))
    except (FileNotFoundError, NotADirectoryError):
        raise InstallError(f"no {MANIFEST_FILE} in {source.parent}: this is not a vendored copy, so there is "
                           "nothing to check it against") from None
    except ValueError as error:
        raise InstallError(f"{source} is not JSON") from error
    files = raw.get("files") if isinstance(raw, dict) else None
    if not isinstance(files, dict) or not all(isinstance(value, str) for value in files.values()):
        raise InstallError(f"{source} holds no file list")
    return Manifest(tag=str(raw.get("tag", "")), commit=str(raw.get("commit", "")), files=files)


def differences(root: Path, manifest: Manifest) -> list[str]:
    found = digests(root)
    changed = [f"changed: {path}" for path in manifest.files if path in found and found[path] != manifest.files[path]]
    missing = [f"missing: {path}" for path in manifest.files if path not in found]
    added = [f"not in the manifest: {path}" for path in found if path not in manifest.files]
    return sorted([*changed, *missing, *added])
