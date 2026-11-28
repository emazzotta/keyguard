#!/usr/bin/env python3
"""Take the README's pictures from the real pages, with a virtual authenticator and a store of demo values.

uv run --locked --with playwright==1.63.0 --with pillow --with pyyaml python3 tools/readme_screenshots.py

The Mac kit's pictures come from its real HTTP server, run here with a stand-in for the Swift command that answers
every Touch ID prompt with yes and records its reason. The server writes the bridge reasons and the access log.

The pages name macOS system fonts. Where those are missing, install Source Serif 4, Inter and JetBrains Mono:
the pictures then use them in their place.
"""
from __future__ import annotations

import argparse
import html
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Final

REPOSITORY: Final = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPOSITORY / "tests"))

from browser import (  # type: ignore[import-not-found]
    PASSPHRASE,
    Running,
    approve,
    run_verb,
    wait_until_answered,
    with_authenticator,
)
from PIL import Image  # type: ignore[import-not-found]
from playwright import sync_api  # type: ignore[import-not-found]

SCALE: Final = 2
VIEWPORT: Final = {"width": 760, "height": 900}
SCHEMES: Final = ("light", "dark")
TOP_MARGIN_PX: Final = 56
BOTTOM_MARGIN_PX: Final = 48
COLOURS: Final = 256
DEMO_SECRETS: Final = "GITHUB_TOKEN=demo-token-1\nDATABASE_URL=demo-url-2\nSTRIPE_KEY=demo-key-3\n"
SESSION_ENV: Final = {"KEYGUARD_SESSION": "4b7e19c2", "KEYGUARD_TITLE": "Deploy the staging release"}
ASKED: Final = ("GITHUB_TOKEN", "DATABASE_URL")
BROWSER_ASK: Final = "Approve in the browser tab that opened."
DEPLOY_SCRIPT: Final = '#!/bin/sh\necho "Deploying staging with a ${#GITHUB_TOKEN}-character token... done"\n'
FONTS: Final = """:root:root { --font-display: "Source Serif 4", serif; --font-body: "Inter Variable", sans-serif;
  --font-mono: "JetBrains Mono", monospace; }"""
TERMINAL_PAGE: Final = """<!doctype html><meta charset="utf-8"><style>
body { margin: 0; padding: 28px; font: 14px/1.6 "JetBrains Mono", monospace; }
.window { width: 720px; background: #101512; color: #d7e0d9; border-radius: 12px; overflow: hidden; }
.bar { height: 36px; display: flex; align-items: center; gap: 8px; padding: 0 14px; background: #1a211c; }
.bar i { width: 12px; height: 12px; border-radius: 50%; display: block; }
.bar span { margin-left: 12px; color: #7f8c83; font-size: 12px; }
pre { margin: 0; padding: 18px 22px 22px; font: inherit; white-space: pre-wrap; }
.prompt { color: #6fcf97; } .cmd { color: #f2f5f3; } .note { color: #e2b866; } .out { color: #aab6ad; }
</style><div class="window"><div class="bar"><i style="background:#ff5f57"></i><i style="background:#febc2e"></i>
<i style="background:#28c840"></i><span>TITLE</span></div><pre>SESSION</pre></div>"""
MAC_SERVER: Final = REPOSITORY / "mac" / "src"
MAC_HOST: Final = "http://host.docker.internal:7777"
MAC_SESSION: Final = {"X-Keyguard-Session": "4b7e19c2", "X-Keyguard-Title": "Fix CI"}
MAC_BRIDGE: Final = """endpoints:
  ping:
    command: [/bin/echo, pong]
    method: GET
    public: true
  echo:
    command: [/bin/cat]
    stdin: true
  spotify-play:
    command: [/bin/true]
"""
# Stands in for /usr/local/bin/keyguard: approves, and records each reason as Commands.swift builds it.
MAC_CLI: Final = """#!/usr/bin/env python3
import os, sys
values = {"GITHUB_TOKEN": "demo-token-1", "DATABASE_URL": "demo-url-2", "STRIPE_KEY": "demo-key-3"}
args = sys.argv[1:]
def option(name):
    return args[args.index(name) + 1] if name in args else None
def reason(base):
    caller, cache = option("--caller"), option("--cache-duration")
    return base + (f" for {caller}" if caller else "") + (f", cached for {cache}s" if cache else "")
names = [a for a in args[1:] if not a.startswith("--") and a not in (option("--caller"), option("--cache-duration"))]
if args[0] == "confirm":
    told = sys.stdin.read()
elif args[0] == "set":
    sys.stdin.read()
    told = "Save " + names[0]
    print(f"Set '{names[0]}'")
elif args[0] == "list":
    told = reason("List secrets")
    print("\\n".join(values))
elif len(names) == 1:
    told = reason("Reveal " + names[0])
    print(values[names[0]], end="")
else:
    told = reason("Reveal " + ", ".join(names))
    print("\\n".join(f"{n}={values[n]}" for n in names))
with open(os.environ["KEYGUARD_README_PROMPTS"], "a") as prompts:
    prompts.write(told.strip() + "\\n")
"""
# Patches the server's two fixed constants: a free loopback port here, and the stand-in in place of the binary.
MAC_START: Final = """import sys
sys.path.insert(0, sys.argv[1])
from http.server import ThreadingHTTPServer
from keyguard_server import keyguard_cli
from keyguard_server.handler import KeyguardHandler
keyguard_cli.KEYGUARD_BIN = sys.argv[2]
ThreadingHTTPServer(("127.0.0.1", int(sys.argv[3])), KeyguardHandler).serve_forever()
"""


@dataclass(frozen=True)
class Step:
    command: str
    output: str
    note: str = BROWSER_ASK


def save(png: bytes, path: Path) -> None:
    """Quantised to a palette: the background gradient keeps a full-colour picture near 300 KB."""
    image = Image.open(io.BytesIO(png))
    method = Image.Quantize.FASTOCTREE if image.mode == "RGBA" else Image.Quantize.MEDIANCUT
    image.quantize(colors=COLOURS, method=method, dither=Image.Dither.FLOYDSTEINBERG).save(path, optimize=True)
    print(path)


def shoot(page: sync_api.Page, path: Path) -> None:
    page.add_style_tag(content=FONTS)
    page.evaluate("() => { document.activeElement?.blur(); return document.fonts.ready.then(() => true) }")
    box = page.locator("main").bounding_box()
    top = max(box["y"] - TOP_MARGIN_PX, 0)
    bottom = min(box["y"] + box["height"] + BOTTOM_MARGIN_PX, VIEWPORT["height"])
    save(page.screenshot(clip={"x": 0, "y": top, "width": VIEWPORT["width"], "height": bottom - top}), path)


def new_page(browser: sync_api.Browser, scheme: str) -> sync_api.Page:
    """The font override needs the page's style-src 'self' lifted, in this browser only."""
    context = browser.new_context(viewport=VIEWPORT, device_scale_factor=SCALE, color_scheme=scheme,
                                  reduced_motion="reduce", bypass_csp=True)
    return with_authenticator(context.new_page()).page


def capture_pages(browser: sync_api.Browser, scheme: str, folder: Path, out: Path) -> None:
    page = new_page(browser, scheme)
    store = folder / scheme / "store"
    store.parent.mkdir()
    enrolling = Running(store, ("init",), None, None)
    page.goto(enrolling.address)
    page.wait_for_selector("#enrol:enabled")
    page.fill("#pass", PASSPHRASE)
    page.fill("#pass2", PASSPHRASE)
    shoot(page, out / f"init-{scheme}.png")
    approve(page, PASSPHRASE)
    assert enrolling.finish()[0] == 0

    source = store.parent / "secrets.env"
    source.write_text(DEMO_SECRETS, encoding="utf-8")
    assert run_verb(page, store, "import", str(source))[0] == 0
    asking = Running(store, ("get", *ASKED), None, SESSION_ENV)
    page.goto(asking.address)
    page.wait_for_selector("#approve:enabled")
    shoot(page, out / f"approve-{scheme}.png")
    page.click("#approve")
    wait_until_answered(page)
    assert asking.finish()[0] == 0
    page.context.close()


def run_step(page: sync_api.Page, store: Path, shown: str, *args: str) -> Step:
    running = Running(store, args, None, None)
    page.goto(running.address)
    approve(page)
    code, printed = running.finish()
    told = running.process.stderr.read()
    assert code == 0, told
    return Step(f"keyguard-passkey {shown}", told + printed)


def capture_terminal(browser: sync_api.Browser, folder: Path, out: Path) -> None:
    """Real output of each command; the browser branch prints BROWSER_ASK where --no-browser prints the address."""
    page = new_page(browser, "dark")
    store = folder / "terminal" / "store"
    store.parent.mkdir()
    assert run_verb(page, store, "init", passphrase=PASSPHRASE)[0] == 0
    (folder / "secrets.env").write_text(DEMO_SECRETS, encoding="utf-8")
    deploy = folder / "deploy.sh"
    deploy.write_text(DEPLOY_SCRIPT, encoding="utf-8")
    deploy.chmod(0o755)
    steps = [
        run_step(page, store, "import secrets.env", "import", str(folder / "secrets.env")),
        run_step(page, store, "list", "list"),
        run_step(page, store, "exec GITHUB_TOKEN -- ./deploy.sh", "exec", "GITHUB_TOKEN", "--", str(deploy)),
    ]
    shoot_terminal(page, "~/my-project", render(steps, folder), out / "terminal.png")


def shoot_terminal(page: sync_api.Page, title: str, session: str, path: Path) -> None:
    page.set_content(TERMINAL_PAGE.replace("TITLE", html.escape(title)).replace("SESSION", session))
    page.evaluate("() => document.fonts.ready.then(() => true)")
    save(page.locator(".window").screenshot(omit_background=True), path)
    page.context.close()


def render(steps: list[Step], folder: Path) -> str:
    blocks = []
    for step in steps:
        lines = [f'<span class="prompt">$</span> <span class="cmd">{html.escape(step.command)}</span>']
        if step.note:
            lines.append(f'<span class="note">{html.escape(step.note)}</span>')
        output = step.output.replace(str(folder) + os.sep, "").rstrip("\n")
        if output:
            lines.append(f'<span class="out">{html.escape(output)}</span>')
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


@dataclass
class MacServer:
    """The Mac kit's server on a free loopback port, with the stand-in CLI and a bridge of three endpoints."""

    folder: Path
    process: subprocess.Popen[bytes] | None = None
    port: int = 0

    @property
    def prompts(self) -> Path:
        return self.folder / "prompts.txt"

    @property
    def access_log(self) -> Path:
        return self.folder / "access.log"

    def start(self) -> None:
        cli = self.folder / "keyguard"
        cli.write_text(MAC_CLI, encoding="utf-8")
        cli.chmod(0o755)
        bridge = self.folder / "bridge.yaml"
        bridge.write_text(MAC_BRIDGE, encoding="utf-8")
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            self.port = probe.getsockname()[1]
        env = {**os.environ, "KEYGUARD_BRIDGE_CONFIG_FILE": str(bridge), "KEYGUARD_LOG_FILE": str(self.access_log),
               "KEYGUARD_README_PROMPTS": str(self.prompts), "KEYGUARD_STORE_URL": ""}
        self.process = subprocess.Popen([sys.executable, "-c", MAC_START, str(MAC_SERVER), str(cli), str(self.port)],
                                        env=env, stderr=subprocess.DEVNULL)
        for _ in range(100):
            with socket.socket() as probe:
                if probe.connect_ex(("127.0.0.1", self.port)) == 0:
                    return
            time.sleep(0.05)
        raise RuntimeError("the Mac server did not start")

    def stop(self) -> None:
        if self.process:
            self.process.terminate()
            self.process.wait()

    def log_tail(self) -> Step:
        lines = self.access_log.read_text(encoding="utf-8").splitlines()[-2:]
        return Step("tail -2 ~/.keyguard/access.log  # on the Mac", "\n".join(lines), "")

    def call(self, path: str, body: str | None = None, headers: dict[str, str] | None = None) -> Step:
        """One request, shown as the curl command a container sends, with the Touch ID reason it raised."""
        asked = self.prompts.read_text(encoding="utf-8").splitlines() if self.prompts.exists() else []
        request = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", headers=headers or {},
                                         data=body.encode() if body is not None else None,
                                         method="POST" if body is not None else "GET")
        try:
            with urllib.request.urlopen(request) as response:
                printed = response.read().decode()
        except urllib.error.HTTPError as refused:
            printed = refused.read().decode()
        told = self.prompts.read_text(encoding="utf-8").splitlines()[len(asked):] if self.prompts.exists() else []
        return Step(curl(path, body, headers), tidy(printed), "".join(f"Touch ID: {line}" for line in told))


def curl(path: str, body: str | None, headers: dict[str, str] | None) -> str:
    parts = ["curl -s"] + [f"-H '{name}: {value}'" for name, value in (headers or {}).items()]
    if body is not None:
        parts.append(f"-X POST -d '{body}'")
    address = f"{MAC_HOST}{path}"
    parts.append(f"'{address}'" if "?" in path else address)
    return " ".join(parts)


def tidy(printed: str) -> str:
    """A JSON answer comes one object a line, as it reads best in a picture."""
    try:
        answer = json.loads(printed)
    except ValueError:
        return printed
    if isinstance(answer, list):
        return "\n".join(json.dumps(item) for item in answer)
    return json.dumps(answer)


def capture_mac(browser: sync_api.Browser, folder: Path, out: Path) -> None:
    server = MacServer(folder / "mac")
    server.folder.mkdir()
    server.start()
    try:
        reads = [
            server.call("/GITHUB_TOKEN,DATABASE_URL?timeout=120", headers=MAC_SESSION),
            server.call("/GITHUB_TOKEN,DATABASE_URL?timeout=120", headers=MAC_SESSION),
            server.call("/STRIPE_KEY", body="demo-key-4"),
        ]
        reads.append(server.log_tail())
        bridge = [
            server.call("/_bridge/list"),
            server.call("/_bridge/list?all=1"),
            server.call("/_bridge/echo", body="Deploy finished", headers=MAC_SESSION),
            server.call("/_bridge/ping"),
        ]
        bridge.append(server.log_tail())
    finally:
        server.stop()
    shoot_terminal(new_page(browser, "dark"), "a container on the Mac", render(reads, folder), out / "mac-read.png")
    shoot_terminal(new_page(browser, "dark"), "a container on the Mac", render(bridge, folder), out / "mac-bridge.png")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=REPOSITORY / "docs" / "images", help="folder for the pictures")
    out: Path = parser.parse_args().out
    out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as folder, sync_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch(args=["--disable-dev-shm-usage"])
        for scheme in SCHEMES:
            capture_pages(browser, scheme, Path(folder), out)
        capture_terminal(browser, Path(folder), out)
        capture_mac(browser, Path(folder), out)
        browser.close()


if __name__ == "__main__":
    main()
