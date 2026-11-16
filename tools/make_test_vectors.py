#!/usr/bin/env python3
"""Re-sign the requests in the page's test vectors with throwaway keys, keeping the store another writer made."""
from __future__ import annotations

import argparse
import base64
import json
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Final

REPOSITORY: Final = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPOSITORY))

from keyguard_passkey.service.client import Caller, Signer, serialize

VECTORS: Final = REPOSITORY / "tests" / "unlock-core-vectors.json"
KEPT: Final = ("remote_identity", "store", "expected")
HOST: Final = "devbox"
SIGNED_AT: Final = 1_700_000_000
NAMES: Final = ["JIRA_TOKEN", "GITHUB_TOKEN"]
CALLERS: Final = {
    "plain_request": Caller(),
    "caller_request": Caller(session="48f92bcb", title="Rechnungstag f\u00fcr den Kunden \u2013 Entwurf"),
}
RAW_KEY_BYTES: Final = 32
PROVENANCE: Final = (
    "Regenerate the requests with tools/make_test_vectors.py. The store is a keyguard store of test values; "
    "remote_identity is a throwaway age identity among its recipients. Each request is signed by a throwaway "
    "Ed25519 key that this client's request builder used, so a change in its serialisation shows here.")


def openssl(*arguments: str) -> bytes:
    return subprocess.run(["openssl", *arguments], check=True, capture_output=True).stdout


def signed(key: Path, recipient: str, caller: Caller) -> dict[str, object]:
    openssl("genpkey", "-algorithm", "ed25519", "-out", str(key))
    public = openssl("pkey", "-in", str(key), "-pubout", "-outform", "DER")[-RAW_KEY_BYTES:]
    request = Signer(key, HOST, now=lambda: SIGNED_AT).request(NAMES, recipient, caller)
    return {
        "request": request.payload(),
        "canonical_signed_bytes_b64": base64.b64encode(serialize(request.body)).decode(),
        "requester_pubkey_b64": base64.b64encode(public).decode(),
    }


def vectors(source: dict[str, object], recipient: str) -> dict[str, object]:
    with TemporaryDirectory() as work:
        return {
            "_provenance": PROVENANCE,
            **{name: signed(Path(work) / f"{name}.key", recipient, caller) for name, caller in CALLERS.items()},
            **{name: source[name] for name in KEPT},
        }


def recipient_of(identity: str) -> str:
    return subprocess.run(["age-keygen", "-y"], input=identity.encode(), check=True,
                          capture_output=True).stdout.decode().strip()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from", dest="source", type=Path, default=VECTORS,
                        help="vectors file whose store to keep (default: the repository's own)")
    arguments = parser.parse_args(argv)
    source = json.loads(arguments.source.read_text(encoding="utf-8"))
    regenerated = vectors(source, recipient_of(str(source["remote_identity"])))
    VECTORS.write_text(json.dumps(regenerated, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {VECTORS.relative_to(REPOSITORY)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
