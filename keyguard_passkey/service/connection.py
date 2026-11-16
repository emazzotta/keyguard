"""Where service mode finds the service and its keys: a flag, else a variable, else a file in the config folder.

The request token is read from a file only, so a command started by `exec` never inherits it.
"""
from __future__ import annotations

import argparse
from collections.abc import Mapping
from pathlib import Path
from typing import Final, TextIO

from .asking import ServiceAsk, UsageError, asking_pane

SERVICE_ENV: Final = "KEYGUARD_PASSKEY_SERVICE"
REQUESTER_ENV: Final = "KEYGUARD_PASSKEY_REQUESTER"
SIGNING_KEY_ENV: Final = "KEYGUARD_PASSKEY_SIGNING_KEY"
TOKEN_FILE_ENV: Final = "KEYGUARD_PASSKEY_REQUEST_TOKEN_FILE"
REMOTE_RECIPIENT_ENV: Final = "KEYGUARD_PASSKEY_REMOTE_RECIPIENT"
PENDING_ENV: Final = "KEYGUARD_PASSKEY_PENDING_DIR"
SIGNING_KEY_FILE: Final = "sign.key"
TOKEN_FILE: Final = "request-token"
REMOTE_RECIPIENT_FILE: Final = "remote-recipient"
PENDING_FOLDER: Final = "pending"


def add_arguments(parser: argparse.ArgumentParser) -> None:
    group = parser.add_argument_group("service mode", "approve on another device through an unlock service")
    group.add_argument("--service", metavar="URL", help=f"the unlock service (default: ${SERVICE_ENV}; unset means "
                                                          "approving in a browser on this machine)")
    group.add_argument("--requester", help=f"the id this machine signs as (default: ${REQUESTER_ENV})")
    group.add_argument("--signing-key", type=Path, help=f"Ed25519 private key in PEM (default: ${SIGNING_KEY_ENV}, "
                                                         f"else {SIGNING_KEY_FILE} in the config folder)")
    group.add_argument("--token-file", type=Path, help=f"file holding the service's request token (default: "
                                                        f"${TOKEN_FILE_ENV}, else {TOKEN_FILE} in the config folder)")
    group.add_argument("--remote-recipient", help=f"the approving device's age recipient, for set (default: "
                                                   f"${REMOTE_RECIPIENT_ENV}, else {REMOTE_RECIPIENT_FILE} in the "
                                                   "config folder)")


def service_url(args: argparse.Namespace, environ: Mapping[str, str]) -> str:
    return str(args.service or environ.get(SERVICE_ENV, "")).strip().rstrip("/")


def pending_folder(environ: Mapping[str, str], state: Path) -> Path:
    return Path(environ[PENDING_ENV]).expanduser() if environ.get(PENDING_ENV) else state / PENDING_FOLDER


def connect(args: argparse.Namespace, environ: Mapping[str, str], config: Path, state: Path,
            stderr: TextIO) -> ServiceAsk:
    def chosen(flag: object, name: str, default: Path) -> Path:
        return Path(str(flag or environ.get(name) or default)).expanduser()

    recipient = args.remote_recipient or environ.get(REMOTE_RECIPIENT_ENV) or read_pinned(config / REMOTE_RECIPIENT_FILE)
    return ServiceAsk(
        url=service_url(args, environ),
        requester=str(args.requester or environ.get(REQUESTER_ENV, "")).strip(),
        signing_key=chosen(args.signing_key, SIGNING_KEY_ENV, config / SIGNING_KEY_FILE),
        token=read_pinned(chosen(args.token_file, TOKEN_FILE_ENV, config / TOKEN_FILE)),
        remote_recipient=recipient.strip(),
        pending_dir=pending_folder(environ, state),
        pane=asking_pane(environ),
        stderr=stderr,
    )


def read_pinned(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return ""
    except OSError as error:
        raise UsageError(f"cannot read {path}: {error}") from error
