"""Service mode end to end: the requesting client, the in-repo service and its pages in a real browser."""
from __future__ import annotations

import base64
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import pytest
from browser import (
    HOLD_ENV,
    LAYOUT_WIDTHS_PX,
    PAGE_TIMEOUT_MS,
    PASSPHRASE,
    PROCESS_TIMEOUT_SECONDS,
    ROOT,
    STATE_ENV,
    Browser,
    browser_with_authenticator,
    layout_problems,
    run_verb,
    separate_window,
    sync_api,
)
from conftest import REQUESTER, ed25519_keypair

from keyguard_passkey.service import client
from keyguard_passkey.service.server import build_server
from keyguard_passkey.service.settings import DEFAULT_IDENTITY_HEADER, Requester, Settings

REQUEST_TOKEN = "requester-e2e-token"
LOGIN = {DEFAULT_IDENTITY_HEADER: "owner@example.com"}
LINK = re.compile(r"Approve on your phone: (\S+)")
RAW_KEY_BYTES = 32
SESSION = "0f5e2a1c"
PIN_STORAGE_KEY = f"keyguard.{REQUESTER}_pubkey"
HOUR_SECONDS = 3600
RELEASED = (0, "API_TOKEN=abc 123\n")
OPEN_REMOTE = """async (passphrase) => {
  const { age } = await import('/typage.js')
  const sealed = new Uint8Array(await (await fetch('/store/identity-remote.paper.age')).arrayBuffer())
  const opener = new age.Decrypter()
  opener.addPassphrase(passphrase)
  const remote = await opener.decrypt(sealed, 'text')
  return { remote, recipient: await age.identityToRecipient(remote) }
}"""
BUMP_RECIPIENT_VERSION = """async (remote) => {
  const { age } = await import('/typage.js')
  const core = await import('/unlock-core.js')
  const sealedIndex = new Uint8Array(await (await fetch('/store/index.age')).arrayBuffer())
  const index = await core.readIndex(sealedIndex, remote)
  const bumped = { ...index, recipients: { ...index.recipients, version: index.recipients.version + 1 } }
  const sealer = new age.Encrypter()
  for (const recipient of new Set(Object.values(bumped.recipients.tiers).flat())) sealer.addRecipient(recipient)
  const sealed = await sealer.encrypt(core.pad(new TextEncoder().encode(core.canonicalJson(bumped))))
  const { version } = await (await fetch('/store/meta')).json()
  const written = await fetch('/store/commit', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'If-Match': String(version) },
    body: JSON.stringify({ files: { 'index.age': core.b64encode(sealed) }, delete: [] }),
  })
  return written.status
}"""

WRITE_OWNER_KEY = """async ([remote, pem]) => {
  const core = await import('/unlock-core.js')
  const index = await core.readIndex(new Uint8Array(await (await fetch('/store/index.age')).arrayBuffer()), remote)
  const { files } = await core.writeVariable(index, core.OWNER_KEY_NAME, pem)
  const { version } = await (await fetch('/store/meta')).json()
  const written = await fetch('/store/commit', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'If-Match': String(version) },
    body: JSON.stringify({ files, delete: [] }),
  })
  return written.status
}"""
TARGET = "212df38e-7c4b-4a51-9d0e-3f6b2a8c1e57"
SIGN_ARGS = ("sign", "--target", TARGET, "--box", "macbook", "--title", "Mazzotta-bot")
OWNER_MESSAGE = "yes, push personal-infra 06be524c\nthen close the session"
TOKEN_LIFETIME_SECONDS = 600

pytestmark = pytest.mark.spec("docs/service.md")


@dataclass(frozen=True)
class Service:
    base: str
    folder: Path
    signing_key: Path
    public_key: str
    token_file: Path
    recipient: str

    @property
    def store(self) -> Path:
        return self.folder / "store"

    def stored_files(self) -> set[str]:
        return set(json.loads((self.store / "meta.json").read_text(encoding="utf-8"))["files"])


class Asking:
    """The client asking in a process of its own, as a shell on the requesting machine does."""

    def __init__(self, service: Service, *args: str, stdin: str = "", signing_key: Path | None = None,
                 environ: dict[str, str] | None = None) -> None:
        env = {name: value for name, value in os.environ.items() if not name.startswith("KEYGUARD")}
        env.update({"PYTHONPATH": str(ROOT), "XDG_CONFIG_HOME": str(service.folder / "config"),
                    STATE_ENV: str(service.folder / "state"), HOLD_ENV: "0", **(environ or {})})
        command = [sys.executable, "-m", "keyguard_passkey", "--service", service.base, "--requester", REQUESTER,
                   "--signing-key", str(signing_key or service.signing_key), "--token-file", str(service.token_file),
                   "--remote-recipient", service.recipient, *args]
        self.process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, text=True, env=env)
        assert self.process.stdin is not None
        self.process.stdin.write(stdin)
        self.process.stdin.close()
        self.link = self._read_link()

    def _read_link(self) -> str:
        assert self.process.stderr is not None
        for line in self.process.stderr:
            found = LINK.search(line)
            if found:
                return found.group(1)
        raise AssertionError(f"the client printed no approval link and exited {self.process.wait()}")

    def finish(self) -> tuple[int, str]:
        assert self.process.stdout is not None
        try:
            code = self.process.wait(timeout=PROCESS_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            self.process.kill()
            raise
        return code, self.process.stdout.read()


def raw_public_key(public_pem: Path) -> str:
    der = subprocess.run(["openssl", "pkey", "-pubin", "-in", str(public_pem), "-outform", "DER"],
                         capture_output=True, check=True).stdout
    return base64.b64encode(der[-RAW_KEY_BYTES:]).decode()


def requester_keys(folder: Path) -> tuple[Path, str, Path]:
    keys = folder / "requester"
    keys.mkdir(parents=True)
    private, public = ed25519_keypair(keys)
    token_file = keys / "request-token"
    token_file.write_text(REQUEST_TOKEN + "\n", encoding="utf-8")
    return private, raw_public_key(public), token_file


@contextmanager
def served(store: Path) -> Iterator[str]:
    server = build_server(Settings(store=store, bind="127.0.0.1", port=0, request_token=REQUEST_TOKEN,
                                   requesters=(Requester(REQUESTER, "dev box"),)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://localhost:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def pin(page: sync_api.Page, base: str, public_key: str) -> None:
    page.goto(f"{base}/approve?pin={REQUESTER}")
    page.fill("#pubkey", public_key)
    page.click("#pin")
    page.wait_for_selector("#status.ok")


def mint(page: sync_api.Page, base: str) -> None:
    page.goto(f"{base}/enroll")
    page.fill("#device", "phone")
    page.fill("#pass", PASSPHRASE)
    page.fill("#pass2", PASSPHRASE)
    page.click("#enrol")
    try:
        page.wait_for_selector("#status.ok")
    except sync_api.TimeoutError:
        pytest.fail(f"the store was not created at {base}; its status reads {page.inner_text('#status')!r}")


def approved(page: sync_api.Page, asking: Asking) -> tuple[int, str]:
    page.goto(asking.link)
    try:
        page.click("#approve", timeout=PAGE_TIMEOUT_MS)
    except sync_api.TimeoutError:
        pytest.fail(f"the page offered no approval; its status reads {page.inner_text('#status')!r}")
    return asking.finish()


@pytest.fixture(scope="module")
def browser() -> Iterator[Browser]:
    with browser_with_authenticator(LOGIN) as opened:
        yield opened


@pytest.fixture(scope="module")
def page(browser):
    return browser.page


@pytest.fixture(scope="module")
def remote(page, tmp_path_factory) -> Iterator[tuple[Service, str]]:
    folder = tmp_path_factory.mktemp("service")
    source = folder / "secrets.env"
    source.write_text('API_TOKEN="abc 123"\n', encoding="utf-8")
    assert run_verb(page, folder / "store", "init", passphrase=PASSPHRASE)[0] == 0
    assert run_verb(page, folder / "store", "import", str(source))[0] == 0
    signing_key, public_key, token_file = requester_keys(folder)
    with served(folder / "store") as base:
        pin(page, base, public_key)
        opened = page.evaluate(OPEN_REMOTE, PASSPHRASE)
        yield Service(base, folder, signing_key, public_key, token_file, opened["recipient"]), opened["remote"]


@pytest.fixture(scope="module")
def service(remote) -> Service:
    return remote[0]


def test_should_release_a_value_to_the_requester_after_a_passkey_approval(page, service):
    assert approved(page, Asking(service, "get", "--format", "shell", "API_TOKEN")) == (
        0, "export API_TOKEN='abc 123'\n")


def test_should_show_the_names_and_the_asking_session_before_the_passkey_prompt(page, service):
    asking = Asking(service, "get", "API_TOKEN", environ={"KEYGUARD_SESSION": SESSION, "KEYGUARD_TITLE": "Fix CI"})
    page.goto(asking.link)
    page.wait_for_selector("#approve:enabled")

    shown = (page.text_content("#review"), page.text_content("#session-row"))
    page.click("#approve")

    assert asking.finish() == RELEASED
    assert "API_TOKEN" in shown[0]
    assert "Fix CI" in shown[1] and SESSION in shown[1]


def test_should_answer_absent_for_a_name_the_store_lacks(page, service):
    assert approved(page, Asking(service, "get", "NOT_IN_THE_STORE")) == (4, "")


def test_should_list_names_without_values(page, service):
    code, printed = approved(page, Asking(service, "list"))

    assert code == 0
    assert "API_TOKEN" in printed.split()
    assert "abc 123" not in printed


def test_should_write_a_proposed_value_that_a_later_read_returns(page, service):
    assert approved(page, Asking(service, "set", "PROPOSED", stdin="line one\nline two"))[0] == 0

    code, printed = approved(page, Asking(service, "get", "--format", "shell", "PROPOSED"))

    assert (code, printed) == (0, "export PROPOSED='line one\nline two'\n")


def test_should_remove_a_secret(page, service):
    assert approved(page, Asking(service, "set", "SHORT_LIVED", stdin="gone soon"))[0] == 0

    assert approved(page, Asking(service, "rm", "SHORT_LIVED"))[0] == 0

    assert approved(page, Asking(service, "get", "SHORT_LIVED")) == (4, "")


def test_should_offer_nothing_to_approve_for_a_request_the_pinned_key_did_not_sign(page, service, tmp_path):
    forger, _ = ed25519_keypair(tmp_path)
    asking = Asking(service, "get", "API_TOKEN", signing_key=forger)
    page.goto(asking.link)
    page.wait_for_selector("#status.err")
    offered = page.is_visible("#approve")
    asking.process.send_signal(signal.SIGTERM)

    assert offered is False
    assert asking.finish() == (2, "")


def test_should_ask_for_the_signing_key_before_offering_a_request_from_an_unpinned_host(page, service):
    page.goto(f"{service.base}/approve?pin={REQUESTER}")
    page.evaluate(f"localStorage.removeItem('{PIN_STORAGE_KEY}')")
    asking = Asking(service, "get", "API_TOKEN")
    page.goto(asking.link)
    page.wait_for_selector("#setup", state="visible")
    offered = page.is_visible("#review")

    page.fill("#pubkey", service.public_key)
    page.click("#pin")
    page.click("#approve", timeout=PAGE_TIMEOUT_MS)

    assert offered is False
    assert asking.finish() == RELEASED


def test_should_offer_nothing_to_approve_for_a_request_signed_too_long_ago(page, service):
    signer = client.Signer(service.signing_key, REQUESTER, now=lambda: time.time() - HOUR_SECONDS)
    request = client.request_for(signer, ["API_TOKEN"], client.generate_ephemeral().recipient)
    request_id = client.submit(service.base, {"Authorization": f"Bearer {REQUEST_TOKEN}"}, request)

    page.goto(f"{service.base}/approve?id={request_id}")
    page.wait_for_selector("#status.err")

    assert page.is_visible("#approve") is False
    assert "may be a replay" in page.inner_text("#status")


def from_b64url(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def verified_by(public_pem: Path, token: str, work: Path) -> bool:
    payload, signature = token.split(".")
    (work / "payload").write_bytes(from_b64url(payload))
    (work / "signature").write_bytes(from_b64url(signature))
    return subprocess.run(["openssl", "pkeyutl", "-verify", "-pubin", "-inkey", str(public_pem), "-rawin",
                           "-in", str(work / "payload"), "-sigfile", str(work / "signature")],
                          check=False, capture_output=True).returncode == 0


@pytest.fixture(scope="module")
def owner_public_key(page, remote, tmp_path_factory) -> Path:
    service, identity = remote
    private, public = ed25519_keypair(tmp_path_factory.mktemp("owner"))
    page.goto(f"{service.base}/approve?pin={REQUESTER}")
    assert page.evaluate(WRITE_OWNER_KEY, [identity, private.read_text(encoding="utf-8")]) == 200
    return public


def test_should_answer_absent_for_a_sign_request_while_the_store_lacks_the_owner_key(page, service):
    assert approved(page, Asking(service, *SIGN_ARGS, stdin="yes, push\n")) == (4, "")


def test_should_show_the_message_and_its_target_then_print_the_token_the_owner_key_signed(
        page, service, owner_public_key, tmp_path):
    asking = Asking(service, *SIGN_ARGS, stdin=f"{OWNER_MESSAGE}\n")
    page.goto(asking.link)
    page.wait_for_selector("#approve:enabled")
    shown = (page.inner_text("#title"), page.inner_text("#message"), page.inner_text("#target"),
             page.is_visible("#names-row"))

    page.click("#approve")
    code, printed = asking.finish()

    token = printed.strip()
    payload = json.loads(from_b64url(token.split(".")[0]))
    assert shown == ("Sign a message as you", OWNER_MESSAGE, "Mazzotta-bot (212df38e) on macbook", False)
    assert (code, printed) == (0, f"{token}\n")
    assert (payload["text"], payload["to"]) == (OWNER_MESSAGE, {"conversation": TARGET, "box": "macbook"})
    assert payload["exp"] - payload["iat"] == TOKEN_LIFETIME_SECONDS
    assert verified_by(owner_public_key, token, tmp_path)


def test_should_ask_before_trusting_a_changed_recipient_set(page, remote):
    service, identity = remote
    page.goto(f"{service.base}/approve?pin={REQUESTER}")
    assert page.evaluate(BUMP_RECIPIENT_VERSION, identity) == 200
    asking = Asking(service, "get", "API_TOKEN")
    page.goto(asking.link)

    page.click("#approve")
    page.click("#trust")
    page.click("#approve")

    assert asking.finish() == RELEASED


def test_should_add_this_device_with_a_passkey_of_its_own(page, service, browser):
    known = {passkey["credentialId"] for passkey in browser.passkeys()}
    page.goto(f"{service.base}/enroll")
    page.fill("#name", "second")
    page.click("#add-device")
    page.wait_for_selector("#status.ok")
    [added] = [passkey for passkey in browser.passkeys() if passkey["credentialId"] not in known]

    released = approved(page, Asking(service, "get", "API_TOKEN"))

    [used] = [passkey for passkey in browser.passkeys() if passkey["credentialId"] == added["credentialId"]]
    assert released == RELEASED
    assert {"identity-remote.second.age", "identity-remote.second.passkey"} <= service.stored_files()
    assert used["signCount"] > added["signCount"]


def test_should_create_a_store_in_an_empty_folder(page, tmp_path):
    folder = tmp_path / "fresh"
    signing_key, public_key, token_file = requester_keys(folder)
    with served(folder / "store") as base:
        mint(page, base)
        service = Service(base, folder, signing_key, public_key, token_file, page.inner_text("#pubkey").strip())
        pin(page, base, public_key)

        written = approved(page, Asking(service, "set", "FIRST", stdin="one"))[0]
        read = approved(page, Asking(service, "get", "FIRST"))

    assert (written, read) == (0, (0, "FIRST=one\n"))


@pytest.mark.parametrize("width", LAYOUT_WIDTHS_PX)
@pytest.mark.parametrize(("path", "form"), [(f"/approve?pin={REQUESTER}", "#setup"), ("/enroll", "#add")])
def test_should_fit_the_service_forms_into_the_window(page, service, path, form, width):
    page.goto(f"{service.base}{path}")
    page.wait_for_selector(form, state="visible")

    problems = layout_problems(page, width)

    assert problems == []


@pytest.mark.parametrize("width", LAYOUT_WIDTHS_PX)
def test_should_fit_the_new_store_public_key_into_the_window(browser, tmp_path, width):
    with separate_window(browser, LOGIN) as window, served(tmp_path / "store") as base:
        mint(window.page, base)

        problems = layout_problems(window.page, width)

    assert problems == []
