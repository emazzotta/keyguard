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

from keyguard_passkey.service.client import Caller, Signer, UnlockRequest, serialize, sign, sign_request_for
from keyguard_passkey.service.message import OwnerMessage

VECTORS: Final = REPOSITORY / "tests" / "unlock-core-vectors.json"
KEPT: Final = ("remote_identity", "store", "expected")
HOST: Final = "devbox"
SIGNED_AT: Final = 1_700_000_000
NAMES: Final = ["JIRA_TOKEN", "GITHUB_TOKEN"]
CALLERS: Final = {
    "plain_request": Caller(),
    "caller_request": Caller(session="48f92bcb", title="Rechnungstag f\u00fcr den Kunden \u2013 Entwurf"),
}
MESSAGE: Final = OwnerMessage(text="yes, push personal-infra 06be524c\nf\u00fcr dich \u2013 ok",
                              target="212df38e-7c4b-4a51-9d0e-3f6b2a8c1e57", box="macbook", title="Mazzotta-bot")
SIGNED_MESSAGE_AT: Final = 1_795_000_000
TOKEN_LIFETIME_SECONDS: Final = 600
NONCE: Final = bytes(range(16))
RAW_KEY_BYTES: Final = 32
PROVENANCE: Final = (
    "Regenerate the requests with tools/make_test_vectors.py. The store is a keyguard store of test values; "
    "remote_identity is a throwaway age identity among its recipients. Each request is signed by a throwaway "
    "Ed25519 key that this client's request builder used, so a change in its serialisation shows here. owner_token "
    "is a message signed by a throwaway owner key as the approve page signs it.")


def openssl(*arguments: str) -> bytes:
    return subprocess.run(["openssl", *arguments], check=True, capture_output=True).stdout


def public_key_of(key: Path) -> bytes:
    openssl("genpkey", "-algorithm", "ed25519", "-out", str(key))
    return openssl("pkey", "-in", str(key), "-pubout", "-outform", "DER")[-RAW_KEY_BYTES:]


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def signed(key: Path, recipient: str, caller: Caller) -> dict[str, object]:
    public = public_key_of(key)
    request = Signer(key, HOST, now=lambda: SIGNED_AT).request(NAMES, recipient, caller)
    return signed_vector(request, public)


def signed_message(key: Path, recipient: str) -> dict[str, object]:
    public = public_key_of(key)
    request = sign_request_for(Signer(key, HOST, now=lambda: SIGNED_AT), MESSAGE, recipient)
    return signed_vector(request, public)


def signed_vector(request: UnlockRequest, public: bytes) -> dict[str, object]:
    return {
        "request": request.payload(),
        "canonical_signed_bytes_b64": base64.b64encode(serialize(request.body)).decode(),
        "requester_pubkey_b64": base64.b64encode(public).decode(),
    }


def owner_token(key: Path) -> dict[str, object]:
    """A token as the page signs it: the payload's exact bytes, in the page's key order."""
    public = public_key_of(key)
    payload = json.dumps({"v": 1, "text": MESSAGE.text, "to": MESSAGE.addressee(), "nonce": b64url(NONCE),
                          "iat": SIGNED_MESSAGE_AT, "exp": SIGNED_MESSAGE_AT + TOKEN_LIFETIME_SECONDS},
                         separators=(",", ":"), ensure_ascii=False).encode()
    signature = base64.b64decode(sign(payload, key))
    return {
        "private_key_pem": key.read_text(encoding="utf-8"),
        "public_key_b64": base64.b64encode(public).decode(),
        "message": {"text": MESSAGE.text, "conversation": MESSAGE.target, "box": MESSAGE.box},
        "nonce_b64url": b64url(NONCE),
        "iat": SIGNED_MESSAGE_AT,
        "payload": payload.decode(),
        "token": f"{b64url(payload)}.{b64url(signature)}",
    }


def vectors(source: dict[str, object], recipient: str) -> dict[str, object]:
    with TemporaryDirectory() as work:
        return {
            "_provenance": PROVENANCE,
            **{name: signed(Path(work) / f"{name}.key", recipient, caller) for name, caller in CALLERS.items()},
            "sign_request": signed_message(Path(work) / "sign_request.key", recipient),
            "owner_token": owner_token(Path(work) / "owner.key"),
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
