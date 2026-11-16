"""The store folder: ciphertext files, and the manifest that holds a SHA-256 for each.

meta.json is written last, so a crash during a commit leaves the previous version whole.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

INDEX_FILE: Final = "index.age"
META_FILE: Final = "meta.json"
META_FORMAT: Final = 1
STAGED_SUFFIX: Final = ".tmp"
STORE_PATHS: Final = (
    re.compile(r"index\.age"),
    re.compile(r"vars/[0-9a-f]{64}\.age"),
    re.compile(r"identity-remote\.[a-z0-9]+\.(age|passkey)"),
)
_COMMIT_LOCK: Final = threading.Lock()


class StoreError(Exception):
    pass


class StaleStore(StoreError):
    def __init__(self, current: int) -> None:
        super().__init__(f"version is {current}")
        self.current = current


def is_store_path(path: str) -> bool:
    return any(pattern.fullmatch(path) for pattern in STORE_PATHS)


@dataclass(frozen=True)
class Meta:
    version: int = 0
    files: dict[str, str] = field(default_factory=dict)

    def as_json(self) -> dict[str, object]:
        return {"version": self.version, "files": dict(sorted(self.files.items())), "meta_version": META_FORMAT}


@dataclass(frozen=True)
class Store:
    root: Path

    @property
    def exists(self) -> bool:
        return (self.root / INDEX_FILE).is_file()

    def meta(self) -> Meta:
        try:
            raw = json.loads((self.root / META_FILE).read_text(encoding="utf-8"))
        except FileNotFoundError:
            return Meta()
        except ValueError as error:
            raise StoreError(f"{META_FILE} is not JSON") from error
        return Meta(version=int(raw.get("version", 0)), files=dict(raw.get("files", {})))

    def read(self, path: str) -> bytes:
        return self._file(path).read_bytes()

    def commit(self, files: dict[str, bytes], deletes: list[str], expected_version: int) -> Meta:
        with _COMMIT_LOCK:
            return self._commit(files, deletes, expected_version)

    def _commit(self, files: dict[str, bytes], deletes: list[str], expected_version: int) -> Meta:
        current = self.meta()
        if current.version != expected_version:
            raise StaleStore(current.version)
        targets = {path: self._file(path) for path in [*files, *deletes]}
        hashes = dict(current.files)
        for path, data in files.items():
            _replace(targets[path], data)
            hashes[path] = hashlib.sha256(data).hexdigest()
        for path in deletes:
            targets[path].unlink(missing_ok=True)
            hashes.pop(path, None)
        updated = Meta(version=current.version + 1, files=hashes)
        _replace(self.root / META_FILE, json.dumps(updated.as_json(), sort_keys=True).encode())
        return updated

    def _file(self, path: str) -> Path:
        if not is_store_path(path):
            raise StoreError(f"not a store file: {path!r}")
        return self.root / path


def _replace(target: Path, data: bytes) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    staged = target.with_name(target.name + STAGED_SUFFIX)
    with staged.open("wb") as out:
        out.write(data)
        out.flush()
        os.fsync(out.fileno())
    os.replace(staged, target)
