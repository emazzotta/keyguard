"""A real Chromium with a virtual authenticator standing in for the passkey, and the local client asking it."""
from __future__ import annotations

import os
import re
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import pytest

REQUIRED_ENV = "KEYGUARD_PASSKEY_E2E"


def unavailable(reason: str) -> None:
    """Skips, or fails where the pipeline sets the variable so that a missing browser cannot pass unseen."""
    if os.environ.get(REQUIRED_ENV):
        pytest.fail(reason)
    pytest.skip(reason, allow_module_level=True)


try:
    from playwright import sync_api
except ImportError:
    unavailable("Playwright is not installed")

ROOT = Path(__file__).resolve().parents[1]
ADDRESS = re.compile(r"http://localhost:\d+/\S*#token=(\S+)")
PASSPHRASE = "correct horse battery staple"
HOLD_ENV = "KEYGUARD_PASSKEY_HOLD_SECONDS"
STATE_ENV = "KEYGUARD_PASSKEY_STATE_DIR"
PAGE_TIMEOUT_MS = 30_000
SMALL_SHARED_MEMORY_ARGS = ["--disable-dev-shm-usage"]
PROCESS_TIMEOUT_SECONDS = 30
VIRTUAL_AUTHENTICATOR = {
    "protocol": "ctap2", "ctap2Version": "ctap2_1", "transport": "internal", "hasResidentKey": True,
    "hasUserVerification": True, "isUserVerified": True, "hasPrf": True, "automaticPresenceSimulation": True,
}
VIEWPORT_HEIGHT_PX = 800
LAYOUT_WIDTHS_PX = (320, 640, 1440)
LAYOUT_PROBLEMS = """() => {
  const MIN_LABEL_INSET_PX = 6
  const shown = (element) => element.getClientRects().length > 0
  const named = (element) => element.id ? `#${element.id}` : `${element.tagName.toLowerCase()}.${element.className}`
  const problems = []
  const root = document.documentElement
  if (root.scrollWidth > root.clientWidth) problems.push(`page scrolls sideways (${root.scrollWidth} in ${root.clientWidth})`)
  for (const element of document.querySelectorAll('main *')) {
    const scrollsByDesign = element.matches('input, select, textarea')
    if (!shown(element) || scrollsByDesign || getComputedStyle(element).display === 'inline') continue
    if (element.scrollWidth > element.clientWidth + 1) problems.push(`${named(element)} overflows its box`)
  }
  for (const control of document.querySelectorAll('main button, main a')) {
    if (!shown(control)) continue
    const label = document.createRange()
    label.selectNodeContents(control)
    const text = label.getBoundingClientRect()
    const box = control.getBoundingClientRect()
    const inset = Math.min(text.left - box.left, box.right - text.right)
    if (inset < MIN_LABEL_INSET_PX) problems.push(`${named(control)} label sits ${inset.toFixed(1)}px from its edge`)
  }
  const measure = document.createElement('canvas').getContext('2d')
  for (const input of document.querySelectorAll('main input[placeholder]')) {
    if (!shown(input)) continue
    const style = getComputedStyle(input)
    measure.font = `${style.fontWeight} ${style.fontSize} ${style.fontFamily}`
    const room = input.clientWidth - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight)
    if (measure.measureText(input.placeholder).width > room) problems.push(`${named(input)} cuts its placeholder off`)
  }
  return problems
}"""


@dataclass(frozen=True)
class Browser:
    page: sync_api.Page
    devtools: sync_api.CDPSession
    authenticator: str

    def passkeys(self) -> list[dict[str, object]]:
        return self.devtools.send("WebAuthn.getCredentials", {"authenticatorId": self.authenticator})["credentials"]

    def forget(self, credential_id: str) -> None:
        removed = {"authenticatorId": self.authenticator, "credentialId": credential_id}
        self.devtools.send("WebAuthn.removeCredential", removed)


@contextmanager
def browser_with_authenticator(headers: dict[str, str] | None = None) -> Iterator[Browser]:
    with sync_api.sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch(args=SMALL_SHARED_MEMORY_ARGS)
        except sync_api.Error as error:
            unavailable(f"no Chromium for Playwright: {error}")
        yield with_authenticator(browser.new_page(reduced_motion="reduce", extra_http_headers=headers))
        browser.close()


@contextmanager
def separate_window(opened: Browser, headers: dict[str, str] | None = None) -> Iterator[Browser]:
    """A window of the same browser with storage and an authenticator of its own."""
    assert opened.page.context.browser is not None
    context = opened.page.context.browser.new_context(reduced_motion="reduce", extra_http_headers=headers)
    try:
        yield with_authenticator(context.new_page())
    finally:
        context.close()


def with_authenticator(page: sync_api.Page) -> Browser:
    page.set_default_timeout(PAGE_TIMEOUT_MS)
    devtools = page.context.new_cdp_session(page)
    devtools.send("WebAuthn.enable")
    added = devtools.send("WebAuthn.addVirtualAuthenticator", {"options": VIRTUAL_AUTHENTICATOR})
    return Browser(page, devtools, added["authenticatorId"])


class Running:
    def __init__(self, store: Path, args: tuple[str, ...], stdin: str | None, environ: dict[str, str] | None) -> None:
        env = {**os.environ, "PYTHONPATH": str(ROOT), HOLD_ENV: "0", **(environ or {})}
        self.process = subprocess.Popen(
            [sys.executable, "-m", "keyguard_passkey", "--store", str(store), "--no-browser", *args],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
        assert self.process.stdin is not None
        self.process.stdin.write(stdin or "")
        self.process.stdin.close()
        self.address, self.token = self._read_address()

    def _read_address(self) -> tuple[str, str]:
        assert self.process.stderr is not None
        for line in self.process.stderr:
            found = ADDRESS.search(line)
            if found:
                return found.group(0), found.group(1)
        raise AssertionError(f"the command printed no approval address and exited {self.process.wait()}")

    def finish(self) -> tuple[int, str]:
        assert self.process.stdout is not None
        code = self.process.wait(timeout=PROCESS_TIMEOUT_SECONDS)
        return code, self.process.stdout.read()


def wait_until_answered(page: sync_api.Page) -> None:
    try:
        page.locator("#status", has_text="close this tab").wait_for(timeout=PAGE_TIMEOUT_MS)
    except sync_api.TimeoutError:
        pytest.fail(f"the page did not finish; its status reads {page.inner_text('#status')!r}")


def layout_problems(page: sync_api.Page, width: int) -> list[str]:
    """Overflow, sideways scroll, cramped button labels and cut placeholders at one window width."""
    kept = page.viewport_size
    page.set_viewport_size({"width": width, "height": VIEWPORT_HEIGHT_PX})
    try:
        return page.evaluate(LAYOUT_PROBLEMS)
    finally:
        if kept is not None:
            page.set_viewport_size(kept)


def approve(page: sync_api.Page, passphrase: str | None = None) -> None:
    if passphrase is not None:
        page.fill("#pass", passphrase)
        page.fill("#pass2", passphrase)
    page.click("#enrol" if passphrase is not None else "#approve")
    wait_until_answered(page)


def run_verb(page: sync_api.Page, store: Path, *args: str, stdin: str | None = None,
             environ: dict[str, str] | None = None, passphrase: str | None = None) -> tuple[int, str]:
    running = Running(store, args, stdin, environ)
    page.goto(running.address)
    approve(page, passphrase)
    return running.finish()
