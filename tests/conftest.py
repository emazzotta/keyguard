from __future__ import annotations

import base64
import io
import json
import re
import subprocess
import sys
import tempfile
import threading
import urllib.request
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from keyguard_passkey.service import client
from keyguard_passkey.service.server import build_server
from keyguard_passkey.service.settings import DEFAULT_IDENTITY_HEADER, Requester, Settings

REQUEST_TOKEN = "requester-test-token"
PHONE_LOGIN = {DEFAULT_IDENTITY_HEADER: "owner@example.com"}
REQUESTER = "devbox"
LINK = re.compile(r"Approve on your phone: \S+/approve\?id=([A-Za-z0-9_-]+)")
SIGNED_FIELDS = ("vars", "host", "eph_pubkey", "ts", "op", "value", "session", "title", "text", "target",
                 "target_box", "target_title")

Decide = Callable[[dict[str, object]], object]


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "spec(document): the document, or its section, whose behaviour a test proves")


def ed25519_keypair(directory: Path) -> tuple[Path, Path]:
    private, public = directory / "sign.key", directory / "sign.pub"
    subprocess.run(["openssl", "genpkey", "-algorithm", "ed25519", "-out", str(private)], check=True,
                   capture_output=True)
    subprocess.run(["openssl", "pkey", "-in", str(private), "-pubout", "-out", str(public)], check=True,
                   capture_output=True)
    return private, public


def verified(payload: bytes, signature_b64: str, public_key: Path) -> bool:
    with tempfile.TemporaryDirectory() as work:
        message, signature = Path(work) / "msg", Path(work) / "sig"
        message.write_bytes(payload)
        signature.write_bytes(base64.b64decode(signature_b64))
        result = subprocess.run(["openssl", "pkeyutl", "-verify", "-pubin", "-inkey", str(public_key), "-rawin",
                                 "-in", str(message), "-sigfile", str(signature)], capture_output=True, check=False)
        return result.returncode == 0


def owner_token(text: str, conversation: str, box: str) -> str:
    """A token of the page's shape for one message, with a signature of zeros: the requester checks only its payload."""
    payload = {"v": 1, "text": text, "to": {"conversation": conversation, "box": box}, "nonce": "bm9uY2U",
               "iat": 1, "exp": 601}
    encode = lambda data: base64.urlsafe_b64encode(data).rstrip(b"=").decode()
    return f"{encode(json.dumps(payload).encode())}.{encode(bytes(64))}"


def signs_the_shown_message(record: dict[str, object]) -> dict[str, str]:
    return {"sign": owner_token(str(record["text"]), str(record["target"]), str(record["target_box"]))}


def sealed_to(recipient: str, payload: object) -> str:
    sealed = subprocess.run(["age", "-r", recipient], input=json.dumps(payload).encode(), capture_output=True,
                            check=True).stdout
    return base64.b64encode(sealed).decode()


def opened(identity: str, sealed_b64: str) -> str:
    with tempfile.TemporaryDirectory() as work:
        sealed = Path(work) / "value.age"
        sealed.write_bytes(base64.b64decode(sealed_b64))
        result = subprocess.run(["age", "-d", "-i", "-", str(sealed)], input=identity, capture_output=True,
                                text=True, check=False)
    return result.stdout if result.returncode == 0 else ""


@dataclass
class UnlockService:
    """The in-repo unlock service with one requester whose key the approving device has pinned."""
    base: str
    signing_key: Path
    public_key: Path
    token_file: Path
    remote: client.Ephemeral

    def call(self, path: str, method: str = "GET", body: object = None,
             headers: dict[str, str] | None = None) -> object:
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(f"{self.base}{path}", data=data, method=method,
                                         headers={"Content-Type": "application/json", **(headers or PHONE_LOGIN)})
        with urllib.request.urlopen(request, timeout=5) as response:
            raw = response.read()
        return json.loads(raw) if raw else None

    def approver_view(self, request_id: str) -> dict[str, object]:
        record = self.call(f"/r/{request_id}/request")
        assert isinstance(record, dict)
        return record

    def signature_holds(self, record: dict[str, object]) -> bool:
        body = {name: record[name] for name in SIGNED_FIELDS if name in record}
        return verified(client.serialize(body), str(record["sig"]), self.public_key)

    def respond(self, request_id: str, answer: object) -> None:
        record = self.approver_view(request_id)
        self.call(f"/r/{request_id}/response", "POST", {"response": sealed_to(str(record["eph_pubkey"]), answer)})


class Phone(io.StringIO):
    """The client's stderr: each approval link it prints reaches the phone, which checks and answers it."""

    def __init__(self, service: UnlockService, decide: Decide) -> None:
        super().__init__()
        self.service = service
        self.decide = decide
        self.seen: list[dict[str, object]] = []

    def write(self, text: str) -> int:
        written = super().write(text)
        for request_id in LINK.findall(text):
            view = self.service.approver_view(request_id)
            assert self.service.signature_holds(view)
            record = {**view, "id": request_id}
            self.seen.append(record)
            answer = self.decide(record)
            if answer is not None:
                self.service.respond(request_id, answer)
        return written


@pytest.fixture
def unlock_service(tmp_path: Path) -> Iterator[UnlockService]:
    keys = tmp_path / "keys"
    keys.mkdir()
    private, public = ed25519_keypair(keys)
    token_file = keys / "request-token"
    token_file.write_text(REQUEST_TOKEN + "\n")
    store = tmp_path / "served-store"
    store.mkdir()
    settings = Settings(store=store, port=0, request_token=REQUEST_TOKEN,
                        requesters=(Requester(REQUESTER, "dev box"),))
    server = build_server(settings)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield UnlockService(base=f"http://127.0.0.1:{server.server_address[1]}", signing_key=private,
                            public_key=public, token_file=token_file, remote=client.generate_ephemeral())
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def phone(unlock_service: UnlockService) -> Callable[[Decide], Phone]:
    return lambda decide: Phone(service=unlock_service, decide=decide)
