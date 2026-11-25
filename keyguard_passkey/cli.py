"""The keyguard-passkey command: each verb is one approval in the browser."""
from __future__ import annotations

import argparse
import contextlib
import getpass
import json
import os
import re
import subprocess
import sys
import time
import webbrowser
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final, NoReturn, Protocol, TextIO

from . import __version__, install
from .ceremony import Ceremony, CeremonyServer, Op, Request, Unanswered
from .encoding import as_env_line, as_shell_export
from .envfile import EnvFileError, read_env_file, read_env_text
from .hold import (
    DEFAULT_HOLD_SECONDS,
    MAX_HOLD_SECONDS,
    HoldClient,
    HoldError,
    scope_of,
    scope_of_service,
)
from .service import client, connection, pending
from .service.asking import ServiceAsk, UsageError
from .service.message import OwnerMessage
from .service.server import serve
from .service.settings import SettingsError, settings_from_env
from .store import Store

PROGRAM: Final = "keyguard-passkey"
STORE_ENV: Final = "KEYGUARD_PASSKEY_STORE"
STATE_ENV: Final = "KEYGUARD_PASSKEY_STATE_DIR"
HOLD_ENV: Final = "KEYGUARD_PASSKEY_HOLD_SECONDS"
SESSION_ENV: Final = "KEYGUARD_SESSION"
TITLE_ENV: Final = "KEYGUARD_TITLE"
VARIABLE_NAME: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
RELEASE: Final = re.compile(r"\d+\.\d+\.\d+")
EXEC_VERB: Final = "exec"
EXEC_SEPARATOR: Final = "--"
STANDARD_INPUT: Final = Path("-")
FORMATS: Final = {"env": as_env_line, "shell": as_shell_export}

EXIT_ERROR: Final = 1
EXIT_REFUSED: Final = 2
EXIT_UNAVAILABLE: Final = 3
EXIT_ABSENT: Final = 4
EXIT_USAGE: Final = 64
EXIT_COMMAND_NOT_FOUND: Final = 127

Ask = Callable[[Request], Mapping[str, object]]


class Failure(Exception):
    def __init__(self, message: str, code: int) -> None:
        super().__init__(message)
        self.code = code


class Holder(Protocol):
    def lookup(self, names: list[str]) -> dict[str, str]: ...
    def keep(self, values: dict[str, str]) -> None: ...
    def forget(self, names: list[str] | None = None) -> None: ...


@dataclass(frozen=True)
class App:
    store: Store
    ask: Ask
    hold: Holder
    environ: Mapping[str, str]
    stdin: TextIO
    stdout: TextIO
    stderr: TextIO
    service: ServiceAsk | None = None

    def say(self, message: str) -> None:
        print(message, file=self.stderr)


class Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        self.print_usage(sys.stderr)
        self.exit(EXIT_USAGE, f"{self.prog}: error: {message}\n")


def config_folder(environ: Mapping[str, str]) -> Path:
    config_home = environ.get("APPDATA") if os.name == "nt" else environ.get("XDG_CONFIG_HOME")
    return (Path(config_home) if config_home else Path.home() / ".config") / PROGRAM


def default_store_root(environ: Mapping[str, str]) -> Path:
    if environ.get(STORE_ENV):
        return Path(environ[STORE_ENV]).expanduser()
    return config_folder(environ) / "store"


def state_folder(environ: Mapping[str, str]) -> Path:
    return Path(environ[STATE_ENV]).expanduser() if environ.get(STATE_ENV) else config_folder(environ)


def hold_seconds(environ: Mapping[str, str]) -> int:
    return max(0, min(int(environ.get(HOLD_ENV, DEFAULT_HOLD_SECONDS)), MAX_HOLD_SECONDS))


def variable_name(text: str) -> str:
    if not VARIABLE_NAME.fullmatch(text):
        raise argparse.ArgumentTypeError(f"not a variable name: {text!r}")
    return text


def build_parser() -> Parser:
    parser = Parser(prog=PROGRAM, description="A passkey-gated secret store. Each command asks once in the browser.")
    parser.add_argument("--store", type=Path, help=f"store folder (default: ${STORE_ENV}, else the user's config folder)")
    parser.add_argument("--no-browser", action="store_true", help="print the approval address without opening it")
    connection.add_arguments(parser)
    verbs = parser.add_subparsers(dest="verb", required=True, parser_class=Parser)
    verbs.add_parser("init", help="create the store, its passkey and its paper passphrase")
    getter = verbs.add_parser("get", help="print NAME=value lines")
    getter.add_argument("names", nargs="+", type=variable_name)
    getter.add_argument("--format", choices=tuple(FORMATS), default="env",
                        help="env: NAME=value, a value with a line break as base64; shell: export NAME='value'")
    verbs.add_parser("list", help="print the secret names")
    verbs.add_parser("set", help="store a value read from standard input").add_argument("name", type=variable_name)
    verbs.add_parser("rm", help="remove a secret").add_argument("name", type=variable_name)
    importer = verbs.add_parser("import", help="store every NAME=value line of a .env file")
    importer.add_argument("file", type=Path, help="a .env file, or - for standard input")
    importer.add_argument("--force", action="store_true", help="replace the names the store already holds")
    runner = verbs.add_parser(EXEC_VERB, help="run COMMAND with the secrets in its environment",
                              usage=f"{PROGRAM} {EXEC_VERB} NAME... -- COMMAND...")
    runner.add_argument("names", nargs="+", type=variable_name)
    signer = verbs.add_parser("sign", help="print a token of a message from standard input, signed as the owner "
                                           "on the approving device, for one session")
    signer.add_argument("--target", required=True, help="the session's full conversation id")
    signer.add_argument("--box", required=True, help="the session's box, as the web terminal names it")
    signer.add_argument("--title", default="", help="the session's title, shown to the approver")
    verbs.add_parser("lock", help="drop the values held from earlier approvals")
    verbs.add_parser("version", help="print the version")
    verbs.add_parser("verify-install", help="check a vendored copy of this client against its manifest")
    verbs.add_parser("serve", help="run the unlock service for the store, set up by KEYGUARD_PASSKEY_* variables")
    add_pending_parser(verbs)
    return parser


def add_pending_parser(verbs: argparse._SubParsersAction[Parser]) -> None:
    lister = verbs.add_parser("pending", help="print this machine's service requests that wait for an approval")
    mode = lister.add_mutually_exclusive_group()
    mode.add_argument("--watch", action="store_true", help="stream changes as JSON lines until stopped")
    mode.add_argument("--cancel", metavar="ID", help="make the command that waits on this request give up")
    lister.add_argument("--dir", type=Path, help=f"where requests are recorded (default: ${connection.PENDING_ENV}, "
                                                  "else pending in the state folder)")


def split_command(argv: list[str]) -> tuple[list[str], list[str]]:
    """`exec NAME... -- COMMAND...`: the command leaves argv before argparse sees it."""
    if EXEC_VERB not in argv or EXEC_SEPARATOR not in argv[argv.index(EXEC_VERB):]:
        return argv, []
    separator = argv.index(EXEC_SEPARATOR, argv.index(EXEC_VERB))
    return argv[:separator], argv[separator + 1:]


def newer_format_message(newer: Mapping[str, object]) -> str:
    found, reader = newer.get("format"), newer.get("reader")
    number = f"format {found}" if isinstance(found, int) else "a newer format"
    release = f"release {reader} or later" if isinstance(reader, str) and RELEASE.fullmatch(reader) else "a newer release"
    return f"the store has {number}, which {PROGRAM} {__version__} does not read; {release} does. Nothing was changed."


def checked(answer: Mapping[str, object]) -> Mapping[str, object]:
    if answer.get("refused") is True:
        raise Failure("refused in the browser", EXIT_REFUSED)
    newer = answer.get("newer")
    if isinstance(newer, dict):
        raise Failure(newer_format_message(newer), EXIT_ERROR)
    if "error" in answer:
        raise Failure(f"the browser could not answer: {answer['error']}", EXIT_UNAVAILABLE)
    absent = answer.get("absent")
    if isinstance(absent, list) and absent:
        raise Failure(f"not in the store: {', '.join(str(name) for name in absent)}", EXIT_ABSENT)
    return answer


def request_for(app: App, op: Op, names: tuple[str, ...] = (), values: dict[str, str] | None = None,
                force: bool = False, message: OwnerMessage | None = None) -> Request:
    return Request(op=op, names=names, values=values or {}, force=force,
                   session=app.environ.get(SESSION_ENV, ""), title=app.environ.get(TITLE_ENV, ""), message=message)


def require_store(app: App) -> None:
    if app.service is None and not app.store.exists:
        raise Failure(f"no store at {app.store.root}; run '{PROGRAM} init'", EXIT_UNAVAILABLE)


def approved(app: App, names: list[str]) -> dict[str, str]:
    values = checked(app.ask(request_for(app, Op.GET, tuple(names)))).get("values")
    if not isinstance(values, dict) or set(values) != set(names) or not all(isinstance(v, str) for v in values.values()):
        raise Failure("the browser answered without exactly the asked values", EXIT_UNAVAILABLE)
    return values


def held(app: App, names: list[str]) -> dict[str, str]:
    try:
        values = app.hold.lookup(names)
    except HoldError as error:
        app.say(f"{PROGRAM}: asking again, the hold failed: {error}")
        return {}
    if values:
        app.say(f"{PROGRAM}: {', '.join(values)} still held from an earlier approval")
    return values


def keep(app: App, values: dict[str, str]) -> None:
    try:
        app.hold.keep(values)
    except HoldError as error:
        app.say(f"{PROGRAM}: not holding the released values: {error}")


def forget(app: App, names: list[str] | None) -> None:
    try:
        app.hold.forget(names)
    except HoldError as error:
        app.say(f"{PROGRAM}: an old value may still be held for up to {MAX_HOLD_SECONDS} s: {error}")


def released(app: App, names: list[str]) -> dict[str, str]:
    require_store(app)
    values = held(app, names)
    missing = [name for name in names if name not in values]
    if missing:
        fresh = approved(app, missing)
        keep(app, fresh)
        values.update(fresh)
    elif app.service is not None:
        app.service.held(request_for(app, Op.GET, tuple(names)))
    return values


def refuse_through_service(app: App, reason: str) -> None:
    if app.service is not None:
        raise Failure(reason, EXIT_USAGE)


def run_init(app: App, _: argparse.Namespace) -> int:
    if app.service is None and app.store.exists:
        raise Failure(f"a store exists at {app.store.root}", EXIT_ERROR)
    checked(app.ask(request_for(app, Op.INIT)))
    app.say(f"Store created at {app.store.root}. Keep the paper passphrase somewhere safe.")
    return 0


def run_get(app: App, args: argparse.Namespace) -> int:
    values = released(app, args.names)
    line = FORMATS[args.format]
    app.stdout.write("".join(line(name, values[name]) for name in args.names))
    return 0


def run_list(app: App, _: argparse.Namespace) -> int:
    require_store(app)
    names = checked(app.ask(request_for(app, Op.LIST))).get("names")
    if not isinstance(names, list) or not all(isinstance(name, str) and VARIABLE_NAME.fullmatch(name) for name in names):
        raise Failure("the browser answered without a list of names", EXIT_UNAVAILABLE)
    app.stdout.write("".join(f"{name}\n" for name in sorted(names)))
    return 0


def read_value(name: str, stream: TextIO) -> str:
    raw = getpass.getpass(f"Value for {name}: ") if stream.isatty() else stream.read()
    value = raw.removesuffix("\n")
    if not value:
        raise Failure(f"no value to store for {name}", EXIT_ERROR)
    return value


def run_set(app: App, args: argparse.Namespace) -> int:
    require_store(app)
    value = read_value(args.name, app.stdin)
    checked(app.ask(request_for(app, Op.SET, (args.name,), {args.name: value}, force=True)))
    forget(app, [args.name])
    app.say(f"Stored {args.name}")
    return 0


def run_rm(app: App, args: argparse.Namespace) -> int:
    require_store(app)
    checked(app.ask(request_for(app, Op.RM, (args.name,))))
    forget(app, [args.name])
    app.say(f"Removed {args.name}")
    return 0


def names_in(answer: Mapping[str, object], key: str) -> list[str]:
    found = answer.get(key)
    return [name for name in found if isinstance(name, str)] if isinstance(found, list) else []


def read_entries(app: App, file: Path) -> dict[str, str]:
    try:
        return read_env_text(app.stdin.read(), "standard input") if file == STANDARD_INPUT else read_env_file(file)
    except EnvFileError as error:
        raise Failure(str(error), EXIT_ERROR) from error


def run_import(app: App, args: argparse.Namespace) -> int:
    refuse_through_service(app, "import reads back through the browser on this machine; through the service, "
                                "store each name with set")
    require_store(app)
    entries = read_entries(app, args.file)
    answer = checked(app.ask(request_for(app, Op.SET, tuple(entries), entries, force=args.force)))
    written, skipped = names_in(answer, "written"), names_in(answer, "skipped")
    forget(app, written)
    app.say(f"Stored {len(written)} of {len(entries)} secrets.")
    if skipped:
        app.say(f"Kept the store's value for: {', '.join(skipped)} (--force replaces them)")
    if not written:
        return 0
    if not isinstance(answer.get("verified"), str):
        raise Failure("nothing was read back, so keep the plaintext", EXIT_ERROR)
    app.say(f"Read {answer['verified']} back from the store.")
    if args.file != STANDARD_INPUT:
        app.say(f"{args.file} still holds the plaintext: delete it once you have checked the store.")
    return 0


def run_exec(app: App, args: argparse.Namespace) -> int:
    if not args.command:
        raise Failure(f"usage: {PROGRAM} {EXEC_VERB} NAME... {EXEC_SEPARATOR} COMMAND...", EXIT_USAGE)
    missing = [name for name in args.names if name not in app.environ]
    values = released(app, missing) if missing else {}
    try:
        return subprocess.run(args.command, env={**app.environ, **values}, check=False).returncode
    except FileNotFoundError:
        raise Failure(f"command not found: {args.command[0]}", EXIT_COMMAND_NOT_FOUND) from None


def run_sign(app: App, args: argparse.Namespace) -> int:
    if app.service is None:
        raise Failure("sign works only through the unlock service, whose approve page holds the owner key", EXIT_USAGE)
    message = OwnerMessage(app.stdin.read().removesuffix("\n"), args.target, args.box, args.title)
    problem = message.problem()
    if problem:
        raise Failure(problem, EXIT_USAGE)
    token = checked(app.ask(request_for(app, Op.SIGN, message=message))).get("token")
    if not isinstance(token, str):
        raise Failure("the browser answered without a token", EXIT_UNAVAILABLE)
    app.stdout.write(f"{token}\n")
    return 0


def run_lock(app: App, _: argparse.Namespace) -> int:
    forget(app, None)
    app.say("Dropped every held value of this store.")
    return 0


def run_version(app: App, _: argparse.Namespace) -> int:
    app.stdout.write(f"{__version__}\n")
    return 0


def run_verify_install(app: App, _: argparse.Namespace) -> int:
    try:
        manifest = install.read_manifest(install.ROOT)
    except install.InstallError as error:
        raise Failure(str(error), EXIT_ERROR) from error
    different = install.differences(install.ROOT, manifest)
    if different:
        app.say("\n".join(different))
        raise Failure(f"{len(different)} files differ from {manifest.tag}", EXIT_ERROR)
    app.stdout.write(f"{len(manifest.files)} files match {manifest.tag} ({manifest.commit})\n")
    return 0


def run_pending(app: App, args: argparse.Namespace) -> int:
    folder = args.dir or connection.pending_folder(app.environ, state_folder(app.environ))
    if args.cancel is not None:
        if not pending.cancel(folder, args.cancel, time.time()):
            raise Failure(f"no request {args.cancel!r} waits for an answer", EXIT_ERROR)
        return 0
    if not args.watch:
        app.stdout.write(json.dumps({"pending": pending.snapshot(folder, time.time())}) + "\n")
        return 0
    with contextlib.suppress(KeyboardInterrupt, BrokenPipeError):
        pending.watch(folder, app.stdout)
    return 0


def run_serve(app: App, _: argparse.Namespace) -> int:
    try:
        settings = settings_from_env(app.environ, app.store.root)
    except SettingsError as error:
        raise Failure(str(error), EXIT_USAGE) from error
    serve(settings)
    return 0


VERBS: Final[dict[str, Callable[[App, argparse.Namespace], int]]] = {
    "init": run_init, "get": run_get, "list": run_list, "set": run_set, "rm": run_rm,
    "import": run_import, EXEC_VERB: run_exec, "sign": run_sign, "lock": run_lock, "version": run_version,
    "verify-install": run_verify_install, "serve": run_serve, "pending": run_pending,
}


def browser_ask(store: Store, open_browser: bool, stderr: TextIO) -> Ask:
    def ask(request: Request) -> Mapping[str, object]:
        with CeremonyServer(Ceremony(request=request, store=store)) as server:
            if not open_browser:
                print(f"Approve in a browser on this machine: {server.page_url}", file=stderr, flush=True)
            elif not webbrowser.open(server.page_url):
                raise Failure("no browser could be opened on this machine; rerun with --no-browser to get the "
                              "address, or approve from another device through the unlock service", EXIT_UNAVAILABLE)
            else:
                print("Approve in the browser tab that opened.", file=stderr, flush=True)
            try:
                return server.wait()
            except Unanswered as error:
                raise Failure(str(error), EXIT_UNAVAILABLE) from error
    return ask


def service_ask(service: ServiceAsk) -> Ask:
    def ask(request: Request) -> Mapping[str, object]:
        try:
            return service(request)
        except UsageError as error:
            raise Failure(str(error), EXIT_USAGE) from error
        except client.Cancelled as error:
            raise Failure(str(error), EXIT_REFUSED) from error
        except client.UnlockError as error:
            raise Failure(str(error), EXIT_UNAVAILABLE) from error
    return ask


def connect_service(args: argparse.Namespace, environ: Mapping[str, str]) -> ServiceAsk | None:
    if not connection.service_url(args, environ):
        return None
    try:
        return connection.connect(args, environ, config_folder(environ), state_folder(environ), sys.stderr)
    except UsageError as error:
        raise Failure(str(error), EXIT_USAGE) from error


def build_app(args: argparse.Namespace, environ: Mapping[str, str]) -> App:
    try:
        seconds = hold_seconds(environ)
    except ValueError:
        raise Failure(f"{HOLD_ENV} is not a whole number of seconds", EXIT_USAGE) from None
    store = Store(args.store or default_store_root(environ))
    service = connect_service(args, environ)
    scope = scope_of_service(service.url) if service else scope_of(store.root)
    ask = service_ask(service) if service else browser_ask(store, not args.no_browser, sys.stderr)
    return App(store=store, ask=ask, hold=HoldClient(state_dir=state_folder(environ), scope=scope, seconds=seconds),
               environ=environ, stdin=sys.stdin, stdout=sys.stdout, stderr=sys.stderr, service=service)


def main(argv: list[str] | None = None) -> int:
    arguments, command = split_command(list(sys.argv[1:] if argv is None else argv))
    parser = build_parser()
    args = parser.parse_args(arguments)
    args.command = command
    try:
        app = build_app(args, os.environ)
    except Failure as failure:
        parser.error(str(failure))
    return run(app, args)


def run(app: App, args: argparse.Namespace) -> int:
    try:
        return VERBS[args.verb](app, args)
    except Failure as failure:
        app.say(f"{PROGRAM}: {failure}")
        return failure.code
    except KeyboardInterrupt:
        return EXIT_REFUSED
