"""The whole client against a real browser page, with a virtual authenticator standing in for the passkey."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from browser import (
    HOLD_ENV,
    LAYOUT_WIDTHS_PX,
    PASSPHRASE,
    PROCESS_TIMEOUT_SECONDS,
    ROOT,
    STATE_ENV,
    Browser,
    Running,
    approve,
    browser_with_authenticator,
    layout_problems,
    run_verb,
    separate_window,
    wait_until_answered,
)

LENGTH_PREFIX_BYTES = 4
SESSION = "0f5e2a1c"
LONG_NAME = "DEPLOYMENT_SIGNING_CERTIFICATE_PRIVATE_KEY_FOR_THE_STAGING_ENVIRONMENT"
LONG_TITLE = "Rotate the deployment signing certificate before the staging environment rebuild on Friday"
OPEN_WITH_PAPER_PASSPHRASE = """async ([passphrase, token]) => {
  const { age } = await import('/typage.js')
  const core = await import('/unlock-core.js')
  const file = async (path) => new Uint8Array(await (await fetch('/store/' + path,
    { headers: { 'X-Keyguard-Token': token } })).arrayBuffer())
  const opener = new age.Decrypter()
  opener.addPassphrase(passphrase)
  const remote = await opener.decrypt(await file('identity-remote.paper.age'), 'text')
  return { remote, index: await core.readIndex(await file('index.age'), remote) }
}"""


REWRITE_INDEX_AS_FORMAT = """async ([passphrase, token, format, reader]) => {
  const { age } = await import('/typage.js')
  const core = await import('/unlock-core.js')
  const headers = { 'X-Keyguard-Token': token }
  const file = async (path) => new Uint8Array(await (await fetch('/store/' + path, { headers })).arrayBuffer())
  const opener = new age.Decrypter()
  opener.addPassphrase(passphrase)
  const remote = await opener.decrypt(await file('identity-remote.paper.age'), 'text')
  const index = { ...await core.readIndex(await file('index.age'), remote), version: format, reader }
  const sealer = new age.Encrypter()
  sealer.addRecipient(index.recipients.tiers.high[0])
  const sealed = await sealer.encrypt(core.pad(new TextEncoder().encode(core.canonicalJson(index))))
  const { version } = await (await fetch('/store/meta', { headers })).json()
  const written = await fetch('/store/commit', {
    method: 'POST',
    headers: { ...headers, 'Content-Type': 'application/json', 'If-Match': String(version) },
    body: JSON.stringify({ files: { 'index.age': core.b64encode(sealed) }, delete: [] }),
  })
  return written.status
}"""


REFUSE_THE_FIRST_PASSKEY_READ = """(() => {
  const read = navigator.credentials.get.bind(navigator.credentials)
  let refused = false
  navigator.credentials.get = (options) => {
    if (refused) return read(options)
    refused = true
    return Promise.reject(new DOMException('The document is not focused.', 'NotAllowedError'))
  }
})()"""


@pytest.fixture(scope="module")
def browser() -> Iterator[Browser]:
    with browser_with_authenticator() as opened:
        yield opened


@pytest.fixture(scope="module")
def page(browser):
    return browser.page


@pytest.fixture(scope="module")
def store(page, tmp_path_factory) -> Path:
    folder = tmp_path_factory.mktemp("e2e")
    root = folder / "store"
    source = folder / "secrets.env"
    source.write_text('export API_TOKEN="abc 123"\nDB_PASSWORD=p@ss=word#1\n', encoding="utf-8")
    assert run_verb(page, root, "init", passphrase=PASSPHRASE)[0] == 0
    assert run_verb(page, root, "import", str(source))[0] == 0
    return root


@pytest.mark.spec("README.md")
def test_should_release_imported_secrets_after_a_passkey_approval(page, store):
    code, printed = run_verb(page, store, "get", "DB_PASSWORD", "API_TOKEN")

    assert code == 0
    assert printed == "DB_PASSWORD=p@ss=word#1\nAPI_TOKEN=abc 123\n"


@pytest.mark.spec("README.md")
def test_should_list_names_without_values(page, store):
    code, printed = run_verb(page, store, "list")

    assert code == 0
    assert {"API_TOKEN", "DB_PASSWORD"} <= set(printed.split())
    assert "abc 123" not in printed


@pytest.mark.spec("README.md")
def test_should_run_a_command_with_the_secret_in_its_environment(page, store):
    code, printed = run_verb(page, store, "exec", "API_TOKEN", "--", sys.executable, "-c",
                             "import os; print(os.environ['API_TOKEN'])")

    assert (code, printed) == (0, "abc 123\n")


@pytest.mark.spec("README.md")
def test_should_show_the_names_and_the_asking_session_before_the_passkey_prompt(page, store):
    running = Running(store, ("get", "API_TOKEN"), None, {"KEYGUARD_SESSION": SESSION, "KEYGUARD_TITLE": "Fix CI"})
    page.goto(running.address)
    page.wait_for_selector("#approve:enabled")

    shown = (page.text_content("#names"), page.text_content("#session-row"))
    approve(page)

    assert "API_TOKEN" in shown[0]
    assert "Fix CI" in shown[1] and SESSION in shown[1]
    assert running.finish()[0] == 0


@pytest.mark.spec("README.md")
def test_should_hand_over_nothing_when_the_person_refuses(page, store):
    running = Running(store, ("get", "API_TOKEN"), None, None)
    page.goto(running.address)
    page.click("#refuse")
    wait_until_answered(page)

    assert running.finish() == (2, "")


@pytest.mark.spec("README.md")
def test_should_enrol_and_approve_with_the_keyboard_alone(browser, tmp_path):
    store = tmp_path / "store"
    with separate_window(browser) as keyboard_only:
        page = keyboard_only.page
        enrolling = Running(store, ("init",), None, None)
        page.goto(enrolling.address)
        page.wait_for_selector("#enrol:enabled")
        page.keyboard.press("Tab")
        page.keyboard.type(PASSPHRASE)
        page.keyboard.press("Tab")
        page.keyboard.type(PASSPHRASE)
        page.keyboard.press("Enter")
        wait_until_answered(page)
        setting = Running(store, ("set", "API_TOKEN"), "abc 123", None)
        page.goto(setting.address)
        page.wait_for_selector("#approve:enabled")
        page.keyboard.press("Enter")
        wait_until_answered(page)

    assert enrolling.finish()[0] == 0
    assert setting.finish()[0] == 0


@pytest.mark.spec("README.md")
def test_should_create_the_store_after_a_click_when_the_browser_refuses_the_second_passkey_prompt(browser, tmp_path):
    running = Running(tmp_path / "store", ("init",), None, None)
    with separate_window(browser) as refusing:
        page = refusing.page
        page.add_init_script(REFUSE_THE_FIRST_PASSKEY_READ)
        page.goto(running.address)
        page.fill("#pass", PASSPHRASE)
        page.fill("#pass2", PASSPHRASE)
        page.click("#enrol")

        page.click("#continue")
        wait_until_answered(page)

    assert running.finish()[0] == 0


@pytest.mark.spec("README.md")
@pytest.mark.parametrize("width", LAYOUT_WIDTHS_PX)
def test_should_fit_a_long_request_into_the_window(page, store, width):
    running = Running(store, ("get", LONG_NAME), None, {"KEYGUARD_SESSION": SESSION, "KEYGUARD_TITLE": LONG_TITLE})
    page.goto(running.address)
    page.wait_for_selector("#approve:enabled")

    problems = layout_problems(page, width)
    page.click("#refuse")
    wait_until_answered(page)

    assert problems == []
    assert running.finish()[0] == 2


@pytest.mark.spec("README.md")
@pytest.mark.parametrize("width", LAYOUT_WIDTHS_PX)
def test_should_fit_the_enrolment_form_into_the_window(page, tmp_path, width):
    running = Running(tmp_path / "store", ("init",), None, None)
    page.goto(running.address)
    page.wait_for_selector("#enrol:enabled")

    problems = layout_problems(page, width)
    page.click("#refuse")
    wait_until_answered(page)

    assert problems == []
    assert running.finish()[0] == 2


@pytest.mark.spec("README.md")
def test_should_exit_4_for_a_name_the_store_lacks(page, store):
    assert run_verb(page, store, "get", "NOT_IN_THE_STORE") == (4, "")


@pytest.mark.spec("README.md")
def test_should_store_a_value_with_line_breaks_and_release_it_as_base64(page, store):
    assert run_verb(page, store, "set", "PRIVATE_KEY", stdin="line one\nline two\n")[0] == 0

    code, printed = run_verb(page, store, "get", "PRIVATE_KEY")

    assert code == 0
    assert printed == "PRIVATE_KEY=base64:" + base64.b64encode(b"line one\nline two").decode() + "\n"


@pytest.mark.spec("README.md")
def test_should_keep_the_stored_value_on_a_second_import_without_force(page, store, tmp_path):
    changed = tmp_path / "changed.env"
    changed.write_text("API_TOKEN=overwritten\n", encoding="utf-8")

    assert run_verb(page, store, "import", str(changed))[0] == 0

    assert run_verb(page, store, "get", "API_TOKEN") == (0, "API_TOKEN=abc 123\n")


@pytest.mark.spec("README.md")
def test_should_replace_the_stored_value_on_an_import_with_force(page, store, tmp_path):
    assert run_verb(page, store, "set", "ROTATED", stdin="old")[0] == 0
    changed = tmp_path / "changed.env"
    changed.write_text("ROTATED=new\n", encoding="utf-8")

    assert run_verb(page, store, "import", str(changed), "--force")[0] == 0

    assert run_verb(page, store, "get", "ROTATED") == (0, "ROTATED=new\n")


@pytest.mark.spec("README.md")
def test_should_remove_a_secret_and_its_file(page, store):
    assert run_verb(page, store, "set", "SHORT_LIVED", stdin="gone soon")[0] == 0
    files_before = set((store / "vars").iterdir())

    assert run_verb(page, store, "rm", "SHORT_LIVED")[0] == 0

    assert len(set((store / "vars").iterdir())) == len(files_before) - 1
    assert run_verb(page, store, "get", "SHORT_LIVED") == (4, "")


@pytest.mark.spec("README.md")
def test_should_keep_a_manifest_that_matches_every_file(page, store):
    meta = json.loads((store / "meta.json").read_text(encoding="utf-8"))
    on_disk = {path.relative_to(store).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
               for path in store.rglob("*") if path.is_file() and path.name != "meta.json"}

    assert meta["files"] == on_disk


@pytest.mark.spec("README.md")
def test_should_open_with_the_paper_passphrase_and_keep_the_format_of_the_swift_store(page, store, tmp_path):
    running = Running(store, ("list",), None, None)
    page.goto(running.address)
    opened = page.evaluate(OPEN_WITH_PAPER_PASSPHRASE, [PASSPHRASE, running.token])
    approve(page)
    running.finish()
    index = opened["index"]
    salt = base64.b64decode(index["salt"])
    entry = index["entries"]["API_TOKEN"]

    assert index["version"] == 1
    assert len(salt) == 32
    assert entry == {"file": "vars/" + hashlib.sha256(salt + b"API_TOKEN").hexdigest() + ".age", "tier": "high"}
    assert len(index["recipients"]["tiers"]["high"]) == 1
    if shutil.which("age") is None:
        pytest.skip("no age binary to read the secret file with")
    identity = tmp_path / "identity"
    identity.write_text(opened["remote"] + "\n", encoding="utf-8")
    padded = subprocess.run(["age", "-d", "-i", str(identity), str(store / entry["file"])],
                            capture_output=True, check=True).stdout
    length = int.from_bytes(padded[:LENGTH_PREFIX_BYTES], "big")
    assert len(padded) % 256 == 0
    assert json.loads(padded[LENGTH_PREFIX_BYTES:LENGTH_PREFIX_BYTES + length]) == {"name": "API_TOKEN", "value": "abc 123"}


@pytest.mark.spec("README.md")
def test_should_answer_a_second_command_from_the_hold_until_it_is_locked(page, store, tmp_path):
    holding = {HOLD_ENV: "60", STATE_ENV: str(tmp_path / "state")}
    command = [sys.executable, "-m", "keyguard_passkey", "--store", str(store), "--no-browser"]
    env = {**os.environ, "PYTHONPATH": str(ROOT), **holding}
    assert run_verb(page, store, "get", "API_TOKEN", environ=holding) == (0, "API_TOKEN=abc 123\n")

    again = subprocess.run([*command, "get", "API_TOKEN"], capture_output=True, text=True, env=env,
                           timeout=PROCESS_TIMEOUT_SECONDS, check=False)
    locked = subprocess.run([*command, "lock"], capture_output=True, text=True, env=env,
                            timeout=PROCESS_TIMEOUT_SECONDS, check=False)

    assert (again.returncode, again.stdout) == (0, "API_TOKEN=abc 123\n")
    asked_again = run_verb(page, store, "get", "API_TOKEN", environ=holding)
    subprocess.run([*command, "lock"], capture_output=True, env=env, timeout=PROCESS_TIMEOUT_SECONDS, check=False)

    assert locked.returncode == 0
    assert asked_again == (0, "API_TOKEN=abc 123\n")


@pytest.mark.spec("docs/updating.md")
def test_should_bind_the_passkey_to_localhost_whatever_the_port(store, browser):
    assert [passkey["rpId"] for passkey in browser.passkeys()] == ["localhost"]


@pytest.mark.spec("docs/updating.md")
def test_should_stop_before_any_write_at_a_store_of_a_newer_format(page, store, tmp_path):
    newer = tmp_path / "newer"
    shutil.copytree(store, newer)
    carrier = Running(newer, ("list",), None, None)
    page.goto(carrier.address)
    rewritten = page.evaluate(REWRITE_INDEX_AS_FORMAT, [PASSPHRASE, carrier.token, 2, "9.0.0"])
    page.click("#refuse")
    wait_until_answered(page)
    carrier.finish()
    before = (newer / "meta.json").read_bytes()

    running = Running(newer, ("set", "API_TOKEN"), "changed", None)
    page.goto(running.address)
    approve(page)
    code, _ = running.finish()

    assert rewritten == 200
    assert code == 1
    assert "release 9.0.0 or later" in running.process.stderr.read()
    assert (newer / "meta.json").read_bytes() == before
