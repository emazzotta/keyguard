"""A message signed as the owner by the approve page, which the keyguard-passkey client asks as the Mac."""
from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from typing import Final, Optional

from .caller import Caller
from .config import PHONE_REQUESTER, PHONE_WAIT
from .phone import PhoneClient, client_env

OWNER_KEY_NAME: Final = "KEYGUARD_OWNER_SIGNING_KEY"
OWNER_KEY_REFUSED: Final = f"{OWNER_KEY_NAME} is never released or replaced: it only signs a message you approve"
SIGN_TIMEOUT: Final = PHONE_WAIT + 15
_REQUIRED: Final = ("text", "target", "box")
_OPTIONAL: Final = ("title",)
_TOKEN: Final = re.compile(r"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")
_EXIT_STATUS: Final = {2: 403, 3: 504, 4: 404, 64: 400}
_NO_TOKEN_STATUS: Final = 502


@dataclass(frozen=True)
class SignRequest:
    text: str
    target: str
    box: str
    title: str = ""


@dataclass(frozen=True)
class Signed:
    status: int
    body: str


def parse(body: str) -> Optional[SignRequest]:
    try:
        fields = json.loads(body)
    except ValueError:
        return None
    if not isinstance(fields, dict) or not set(_REQUIRED) <= set(fields) <= {*_REQUIRED, *_OPTIONAL}:
        return None
    if not all(isinstance(value, str) for value in fields.values()):
        return None
    return SignRequest(**fields)


def argv(client: PhoneClient, request: SignRequest) -> list[str]:
    title = ["--title", request.title] if request.title else []
    return [str(client.client), "--service", client.service, "--requester", PHONE_REQUESTER,
            "--signing-key", str(client.signing_key), "sign", "--target", request.target, "--box", request.box,
            *title]


def sign(request: SignRequest, caller: Caller, client: PhoneClient, timeout: float = SIGN_TIMEOUT) -> Signed:
    try:
        ran = subprocess.run(argv(client, request), input=request.text, capture_output=True, text=True,
                             errors="replace", env=client_env(caller), timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        return Signed(504, "no answer from the approve page in time")
    except OSError as error:
        return Signed(_NO_TOKEN_STATUS, f"the keyguard-passkey client did not start: {error}")
    token = ran.stdout.strip()
    if ran.returncode == 0 and _TOKEN.fullmatch(token):
        return Signed(200, token)
    return Signed(_EXIT_STATUS.get(ran.returncode, _NO_TOKEN_STATUS), ran.stderr.strip() or "no token")
