"""The requesting side of the unlock service: signed requests, answers sealed to a one-time age key.

Signing takes OpenSSL 3 (Ed25519) and sealing takes age, so service mode needs both on PATH; local mode needs neither.
"""
from __future__ import annotations

import base64
import binascii
import json
import re
import subprocess
import time
import unicodedata
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Final

from .requests import HIDDEN_TITLE_CATEGORIES, MAX_TITLE_CHARS, SESSION_ID

DEFAULT_TIMEOUT_SECONDS: Final = 300
DEFAULT_POLL_INTERVAL_SECONDS: Final = 2.0
HTTP_TIMEOUT_SECONDS: Final = 10
VARIABLE_NAME: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
SET_OP: Final = "set"
RM_OP: Final = "rm"
LIST_OP: Final = "list"
ABSENT_KEY: Final = "absent"
ELLIPSIS: Final = "\u2026"
NOT_FOUND: Final = 404

Body = dict[str, object]


class UnlockError(Exception):
    pass


class UnlockTimeout(UnlockError):
    pass


class RequestGone(UnlockError):
    pass


class Cancelled(UnlockError):
    pass


class NotInStore(UnlockError):
    def __init__(self, names: list[str]) -> None:
        super().__init__(f"{', '.join(names)} {'is' if len(names) == 1 else 'are'} not in the store")
        self.names = names


class ServiceRefused(UnlockError):
    def __init__(self, code: int) -> None:
        super().__init__(f"unlock service returned {code}")
        self.code = code


@dataclass(frozen=True)
class Ephemeral:
    identity: str
    recipient: str


@dataclass(frozen=True)
class Caller:
    """The session that asks, shown to the approver: never a reason to trust the request."""
    session: str = ""
    title: str = ""

    def fields(self) -> dict[str, str]:
        return {name: value for name, value in (("session", self.session), ("title", self.title)) if value}


NO_CALLER: Final = Caller()


def caller(session: str, title: str) -> Caller:
    """Keep only what the service accepts, so a stray label never costs the approval."""
    return Caller(session=session if SESSION_ID.fullmatch(session) else "", title=clean_title(title))


def clean_title(title: str) -> str:
    visible = "".join(" " if unicodedata.category(char) in HIDDEN_TITLE_CATEGORIES else char for char in title)
    collapsed = " ".join(visible.split())
    if len(collapsed) <= MAX_TITLE_CHARS:
        return collapsed
    return collapsed[:MAX_TITLE_CHARS - 1].rstrip() + ELLIPSIS


@dataclass(frozen=True)
class UnlockRequest:
    body: Body
    signature: str

    def payload(self) -> Body:
        return {**self.body, "sig": self.signature}


@dataclass(frozen=True)
class Signer:
    key: Path
    host: str
    now: Callable[[], float] = time.time

    def request(self, names: list[str], recipient: str, caller: Caller = NO_CALLER, **extra: object) -> UnlockRequest:
        body: Body = {"vars": list(names), "host": self.host, "eph_pubkey": recipient, "ts": int(self.now()),
                      **caller.fields(), **extra}
        return UnlockRequest(body=body, signature=sign(serialize(body), self.key))


def generate_ephemeral() -> Ephemeral:
    result = run_tool(["age-keygen"], b"")
    identity = recipient = ""
    for line in result.decode().splitlines():
        stripped = line.strip()
        if stripped.startswith("AGE-SECRET-KEY-"):
            identity = stripped
        elif "public key:" in stripped:
            recipient = stripped.split("public key:", 1)[1].strip()
    if not identity or not recipient:
        raise UnlockError("age-keygen produced no key")
    return Ephemeral(identity=identity, recipient=recipient)


def run_tool(command: list[str], stdin: bytes) -> bytes:
    try:
        result = subprocess.run(command, input=stdin, capture_output=True, check=False)
    except FileNotFoundError:
        raise UnlockError(f"service mode needs {command[0]} on PATH") from None
    if result.returncode != 0:
        raise UnlockError(f"{command[0]} failed: {result.stderr.decode(errors='replace').strip()}")
    return result.stdout


def sign(payload: bytes, signing_key: Path) -> str:
    if not signing_key.is_file():
        raise UnlockError(f"no signing key at {signing_key}")
    with TemporaryDirectory() as work:
        message = Path(work) / "msg"
        signature = Path(work) / "sig"
        message.write_bytes(payload)
        run_tool(["openssl", "pkeyutl", "-sign", "-inkey", str(signing_key), "-rawin", "-in", str(message),
                  "-out", str(signature)], b"")
        return base64.b64encode(signature.read_bytes()).decode()


def request_for(signer: Signer, names: list[str], recipient: str, caller: Caller = NO_CALLER) -> UnlockRequest:
    validate_names(names)
    return signer.request(names, recipient, caller)


def set_request_for(signer: Signer, name: str, value: str, recipient: str, remote_recipient: str,
                    caller: Caller = NO_CALLER) -> UnlockRequest:
    validate_names([name])
    return signer.request([name], recipient, caller, op=SET_OP, value=seal_to(remote_recipient, value.encode()))


def rm_request_for(signer: Signer, name: str, recipient: str, caller: Caller = NO_CALLER) -> UnlockRequest:
    validate_names([name])
    return signer.request([name], recipient, caller, op=RM_OP)


def list_request_for(signer: Signer, recipient: str, caller: Caller = NO_CALLER) -> UnlockRequest:
    return signer.request([], recipient, caller, op=LIST_OP)


def seal_to(recipient: str, plaintext: bytes) -> str:
    try:
        return base64.b64encode(run_tool(["age", "-r", recipient], plaintext)).decode()
    except UnlockError as error:
        raise UnlockError(f"could not seal the value: {error}") from error


def validate_names(names: list[str]) -> None:
    invalid = [name for name in names if not VARIABLE_NAME.fullmatch(name)]
    if invalid:
        raise UnlockError(f"not valid variable names: {', '.join(invalid)}")


def serialize(body: Body) -> bytes:
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _send(request: urllib.request.Request) -> bytes:
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
            return bytes(response.read())
    except urllib.error.HTTPError as error:
        raise ServiceRefused(error.code) from error
    except OSError as error:
        raise UnlockError(f"unlock service unreachable: {getattr(error, 'reason', error)}") from error


def _get(url: str, headers: dict[str, str]) -> bytes | None:
    try:
        return _send(urllib.request.Request(url, headers=headers))
    except ServiceRefused as error:
        if error.code == NOT_FOUND:
            return None
        raise


def submit(base_url: str, headers: dict[str, str], request: UnlockRequest) -> str:
    raw = _send(urllib.request.Request(f"{base_url}/requests", data=serialize(request.payload()), method="POST",
                                       headers={"Content-Type": "application/json", **headers}))
    request_id = _json_object(raw).get("id")
    if not isinstance(request_id, str) or not request_id:
        raise UnlockError("unlock service accepted the request but returned no id")
    return request_id


def poll(base_url: str, headers: dict[str, str], request_id: str,
         timeout: float = DEFAULT_TIMEOUT_SECONDS,
         interval: float = DEFAULT_POLL_INTERVAL_SECONDS,
         now: Callable[[], float] = time.monotonic,
         sleep: Callable[[float], None] = time.sleep,
         cancelled: Callable[[], bool] = lambda: False) -> str:
    deadline = now() + timeout
    while now() < deadline:
        if cancelled():
            raise Cancelled("the request was cancelled before it was answered")
        raw = _get(f"{base_url}/r/{request_id}", headers)
        if raw is None:
            raise RequestGone("the unlock service no longer holds the request: it expired, the service "
                              "restarted, or another reader collected the answer; ask again")
        response = _json_object(raw).get("response")
        if isinstance(response, str):
            return response
        sleep(interval)
    raise UnlockTimeout(f"no answer within {int(timeout)}s")


def withdraw(base_url: str, headers: dict[str, str], request_id: str) -> None:
    """Drops the request from the service, so nobody can answer it any more; one already gone is fine."""
    try:
        _send(urllib.request.Request(f"{base_url}/r/{request_id}", method="DELETE", headers=headers))
    except ServiceRefused as error:
        if error.code != NOT_FOUND:
            raise


def _json_object(raw: bytes) -> dict[str, object]:
    try:
        found = json.loads(raw)
    except ValueError as error:
        raise UnlockError("the unlock service answered with something other than JSON") from error
    if not isinstance(found, dict):
        raise UnlockError("the unlock service answered with something other than a JSON object")
    return found


def open_response(response_b64: str, identity: str) -> object:
    try:
        ciphertext = base64.b64decode(response_b64, validate=True)
    except (binascii.Error, ValueError) as error:
        raise UnlockError("the answer is not base64") from error
    with TemporaryDirectory() as work:
        sealed = Path(work) / "response.age"
        sealed.write_bytes(ciphertext)
        try:
            plaintext = run_tool(["age", "-d", "-i", "-", str(sealed)], identity.encode())
        except UnlockError as error:
            raise UnlockError(f"could not decrypt the answer: {error}") from error
    try:
        return json.loads(plaintext)
    except ValueError as error:
        raise UnlockError("the answer is not JSON") from error


def decrypt_ack(response_b64: str, identity: str, op: str, name: str) -> None:
    answer = open_response(response_b64, identity)
    if op == RM_OP:
        _raise_if_absent(answer, [name])
    if answer != {op: name}:
        raise UnlockError(f"the approver did not confirm {op} for exactly this variable")


def decrypt_response(response_b64: str, identity: str, names: list[str]) -> dict[str, str]:
    answer = open_response(response_b64, identity)
    _raise_if_absent(answer, names)
    values = _answers(answer, names)
    if values is None:
        raise UnlockError("the answer does not carry exactly the requested names")
    return values


def decrypt_names(response_b64: str, identity: str) -> list[str]:
    """The store's variable names, checked as names so a forged answer cannot print terminal escapes."""
    answer = open_response(response_b64, identity)
    names = answer.get(LIST_OP) if isinstance(answer, dict) and set(answer) == {LIST_OP} else None
    if not isinstance(names, list) or not all(isinstance(name, str) and VARIABLE_NAME.fullmatch(name)
                                              for name in names):
        raise UnlockError("the approver did not answer with a list of variable names")
    return sorted(names)


def _raise_if_absent(answer: object, names: list[str]) -> None:
    absent = _absent_names(answer, names)
    if absent:
        raise NotInStore(absent)


def _absent_names(answer: object, names: list[str]) -> list[str]:
    if not isinstance(answer, dict) or set(answer) != {ABSENT_KEY}:
        return []
    claimed = answer[ABSENT_KEY]
    if not isinstance(claimed, list) or not all(name in names for name in claimed):
        return []
    return [name for name in names if name in claimed]


def _answers(answer: object, names: list[str]) -> dict[str, str] | None:
    if not isinstance(answer, dict) or set(answer) != set(names):
        return None
    values = {name: value for name, value in answer.items() if isinstance(value, str)}
    return values if len(values) == len(names) else None
