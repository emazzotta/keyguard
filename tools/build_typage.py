#!/usr/bin/env python3
"""Build keyguard_passkey/pages/typage.js from the locked packages in tools/typage, or check that it is that build."""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Final

ROOT: Final = Path(__file__).resolve().parents[1]
SOURCE: Final = ROOT / "tools" / "typage"
BUNDLE: Final = ROOT / "keyguard_passkey" / "pages" / "typage.js"
PACKAGE: Final = "age-encryption"
DELETE: Final = 0x7F
ESCAPED_DELETE: Final = b"\\x7f"
ALLOWED_CONTROLS: Final = frozenset(b"\t\n\r")
FIRST_PRINTABLE: Final = 0x20


class BuildError(Exception):
    pass


def pinned_bun() -> str:
    return (SOURCE / ".bun-version").read_text(encoding="utf-8").strip()


def typage_version() -> str:
    package = json.loads((SOURCE / "package.json").read_text(encoding="utf-8"))
    return str(package["dependencies"][PACKAGE])


def require_pinned_bun(bun: str) -> None:
    found = subprocess.run([bun, "--version"], capture_output=True, text=True, check=True).stdout.strip()
    if found != pinned_bun():
        raise BuildError(f"bun {found} is on PATH, and the bundle is the output of bun {pinned_bun()}")


def header(version: str, bun_version: str) -> bytes:
    return (f"// {PACKAGE} (typage) {version} with its dependencies, bundled with bun {bun_version} by "
            "tools/build_typage.py from tools/typage. Do not edit: rebuild.\n").encode()


def finished(bundled: bytes, version: str, bun_version: str) -> bytes:
    """The DEL character becomes an escape, so that the file stays valid where control characters are not, as in YAML."""
    escaped = bundled.replace(bytes([DELETE]), ESCAPED_DELETE)
    controls = sorted({byte for byte in escaped if byte < FIRST_PRINTABLE and byte not in ALLOWED_CONTROLS})
    if controls:
        raise BuildError(f"the bundle holds raw control characters: {', '.join(hex(byte) for byte in controls)}")
    return header(version, bun_version) + escaped


def run_bun(bun: str, *arguments: str) -> None:
    subprocess.run([bun, *arguments], cwd=SOURCE, stdout=subprocess.DEVNULL, check=True)


def bundle(bun: str) -> bytes:
    run_bun(bun, "install", "--frozen-lockfile", "--ignore-scripts")
    with tempfile.TemporaryDirectory() as folder:
        output = Path(folder) / BUNDLE.name
        run_bun(bun, "build", "entry.js", "--format", "esm", "--minify", "--outfile", str(output))
        return output.read_bytes()


def build() -> bytes:
    bun = shutil.which("bun")
    if bun is None:
        raise BuildError("bun is not on PATH")
    require_pinned_bun(bun)
    return finished(bundle(bun), typage_version(), pinned_bun())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="write nothing; exit 1 when the file is not this build")
    args = parser.parse_args(argv)
    try:
        built = build()
    except (BuildError, subprocess.CalledProcessError) as error:
        print(f"build_typage: {error}", file=sys.stderr)
        return 1
    if not args.check:
        BUNDLE.write_bytes(built)
        print(f"Wrote {BUNDLE.relative_to(ROOT)}, {len(built)} bytes.")
        return 0
    if BUNDLE.read_bytes() != built:
        print(f"build_typage: {BUNDLE.relative_to(ROOT)} is not the build of the locked packages", file=sys.stderr)
        return 1
    print(f"{BUNDLE.relative_to(ROOT)} is the build of the locked packages.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
