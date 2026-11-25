from __future__ import annotations

import base64
import dataclasses
import io
import json
import os
import signal
import sys
import time

import pytest
from conftest import REQUESTER, opened, owner_token, signs_the_shown_message

from keyguard_passkey import cli, install
from keyguard_passkey.ceremony import Op
from keyguard_passkey.hold import HoldError, scope_of, scope_of_service
from keyguard_passkey.service import client, connection
from keyguard_passkey.service.asking import UsageError
from keyguard_passkey.store import Store

SERVICE_URL = "https://unlock.example.test"
MISSED_ANSWER_TIMEOUT = 5.0
WAITING_ID = "-bCdEfGh12345678"
TARGET = "212df38e-7c4b-4a51-9d0e-3f6b2a8c1e57"


class Asked:
    def __init__(self, answer: dict[str, object]) -> None:
        self.answer = answer
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        return self.answer


class FakeHold:
    def __init__(self, values=None, broken=False) -> None:
        self.values = dict(values or {})
        self.broken = broken
        self.kept = []
        self.forgotten = []

    def lookup(self, names):
        if self.broken:
            raise HoldError("no answer")
        return {name: self.values[name] for name in names if name in self.values}

    def keep(self, values):
        if self.broken:
            raise HoldError("no answer")
        self.kept.append(values)

    def forget(self, names=None):
        self.forgotten.append(names)


@pytest.fixture
def make_app(tmp_path):
    def _make(answer=None, environ=None, stdin="", with_store=True, hold=None):
        store = Store(tmp_path / "store")
        if with_store:
            store.commit({"index.age": b"index"}, [], expected_version=0)
        asked = Asked(answer or {})
        app = cli.App(store=store, ask=asked, hold=hold or FakeHold(), environ=environ or {}, stdin=io.StringIO(stdin),
                      stdout=io.StringIO(), stderr=io.StringIO())
        return app, asked
    return _make


def run(app, *argv):
    arguments, command = cli.split_command(list(argv))
    args = cli.build_parser().parse_args(arguments)
    args.command = command
    return cli.run(app, args)


@pytest.mark.spec("docs/provider.md")
def test_should_print_one_line_per_name_in_the_asked_order(make_app):
    app, asked = make_app({"values": {"B": "two", "A": "one"}})

    code = run(app, "get", "B", "A")

    assert code == 0
    assert app.stdout.getvalue() == "B=two\nA=one\n"
    assert asked.requests[0].op is Op.GET
    assert asked.requests[0].names == ("B", "A")


@pytest.mark.spec("docs/provider.md")
def test_should_print_a_value_with_a_line_break_as_base64(make_app):
    app, _ = make_app({"values": {"KEY": "line one\nline two"}})

    run(app, "get", "KEY")

    assert app.stdout.getvalue() == "KEY=base64:" + base64.b64encode(b"line one\nline two").decode() + "\n"


@pytest.mark.spec("docs/provider.md")
def test_should_print_a_value_that_starts_with_the_base64_marker_as_base64(make_app):
    app, _ = make_app({"values": {"KEY": "base64:not encoded"}})

    run(app, "get", "KEY")

    assert app.stdout.getvalue() == "KEY=base64:" + base64.b64encode(b"base64:not encoded").decode() + "\n"


@pytest.mark.spec("docs/provider.md")
@pytest.mark.parametrize("answer,code", [
    ({"refused": True}, 2),
    ({"error": "no authenticator answered within 2 minutes"}, 3),
    ({"absent": ["A"]}, 4),
    ({"values": {"A": "one", "EXTRA": "x"}}, 3),
    ({"values": {}}, 3),
])
def test_should_exit_with_the_code_of_the_contract_and_print_no_value(make_app, answer, code):
    app, _ = make_app(answer)

    assert run(app, "get", "A") == code
    assert app.stdout.getvalue() == ""


@pytest.mark.spec("docs/updating.md")
@pytest.mark.parametrize("argv,stdin", [(("get", "A"), ""), (("list",), ""), (("set", "A"), "new"), (("rm", "A"), "")])
def test_should_stop_at_a_newer_store_format_and_name_the_release_that_reads_it(make_app, argv, stdin):
    app, _ = make_app({"newer": {"format": 2, "reader": "0.9.0"}}, stdin=stdin)

    assert run(app, *argv) == 1
    assert "format 2" in app.stderr.getvalue()
    assert "release 0.9.0 or later" in app.stderr.getvalue()
    assert app.stdout.getvalue() == ""


@pytest.mark.spec("docs/updating.md")
def test_should_not_print_a_reader_that_is_not_a_release_number(make_app):
    app, _ = make_app({"newer": {"format": 2, "reader": "\x1b[31m9.9.9"}})

    assert run(app, "get", "A") == 1
    assert "a newer release" in app.stderr.getvalue()
    assert "\x1b" not in app.stderr.getvalue()


@pytest.mark.spec("README.md")
def test_should_not_open_the_browser_when_no_store_exists(make_app):
    app, asked = make_app(with_store=False)

    assert run(app, "get", "A") == 3
    assert asked.requests == []
    assert "init" in app.stderr.getvalue()


@pytest.mark.spec("README.md")
def test_should_show_the_asking_session_on_the_page(make_app):
    app, asked = make_app({"values": {"A": "one"}}, environ={"KEYGUARD_SESSION": "0f5e2a1c", "KEYGUARD_TITLE": "Fix CI"})

    run(app, "get", "A")

    assert (asked.requests[0].session, asked.requests[0].title) == ("0f5e2a1c", "Fix CI")


@pytest.mark.spec("docs/provider.md")
def test_should_list_the_names_sorted(make_app):
    app, _ = make_app({"names": ["B", "A"]})

    assert run(app, "list") == 0
    assert app.stdout.getvalue() == "A\nB\n"


@pytest.mark.spec("docs/provider.md")
def test_should_refuse_a_listing_that_holds_something_other_than_names(make_app):
    app, _ = make_app({"names": ["A", "\x1b[31mred"]})

    assert run(app, "list") == 3
    assert app.stdout.getvalue() == ""


@pytest.mark.spec("docs/provider.md")
def test_should_store_the_value_from_standard_input(make_app):
    app, asked = make_app({"written": ["A"]}, stdin="new value\n")

    assert run(app, "set", "A") == 0
    assert asked.requests[0].op is Op.SET
    assert asked.requests[0].values == {"A": "new value"}
    assert asked.requests[0].force is True


@pytest.mark.spec("docs/provider.md")
def test_should_not_ask_when_there_is_no_value_to_store(make_app):
    app, asked = make_app(stdin="")

    assert run(app, "set", "A") == 1
    assert asked.requests == []


@pytest.mark.spec("docs/provider.md")
def test_should_ask_to_remove_one_name(make_app):
    app, asked = make_app({"removed": "A"})

    assert run(app, "rm", "A") == 0
    assert (asked.requests[0].op, asked.requests[0].names) == (Op.RM, ("A",))


@pytest.mark.spec("README.md")
def test_should_import_every_line_and_keep_the_plaintext_file(make_app, tmp_path):
    source = tmp_path / "secrets.env"
    source.write_text("export A=one\nB='two'\n", encoding="utf-8")
    app, asked = make_app({"written": ["A"], "skipped": ["B"], "verified": "A"})

    code = run(app, "import", str(source))

    assert code == 0
    assert asked.requests[0].values == {"A": "one", "B": "two"}
    assert asked.requests[0].force is False
    assert source.exists()
    assert "Stored 1 of 2" in app.stderr.getvalue()
    assert "Kept the store's value for: B" in app.stderr.getvalue()


@pytest.mark.spec("README.md")
def test_should_import_the_lines_of_standard_input(make_app):
    app, asked = make_app({"written": ["A", "KEY"], "skipped": [], "verified": "A"},
                          stdin="A=one\nKEY=base64:bGluZSBvbmUKbGluZSB0d28=\n")

    assert run(app, "import", "-", "--force") == 0
    assert asked.requests[0].values == {"A": "one", "KEY": "line one\nline two"}
    assert asked.requests[0].force is True
    assert "still holds the plaintext" not in app.stderr.getvalue()


@pytest.mark.spec("README.md")
def test_should_not_ask_when_standard_input_holds_no_secret(make_app):
    app, asked = make_app(stdin="# nothing\n")

    assert run(app, "import", "-") == 1
    assert asked.requests == []


@pytest.mark.spec("README.md")
def test_should_fail_an_import_whose_values_were_not_read_back(make_app, tmp_path):
    source = tmp_path / "secrets.env"
    source.write_text("A=one\n", encoding="utf-8")
    app, _ = make_app({"written": ["A"], "skipped": [], "verified": None})

    assert run(app, "import", str(source)) == 1
    assert "keep" in app.stderr.getvalue()


@pytest.mark.spec("README.md")
def test_should_not_ask_for_an_unreadable_env_file(make_app, tmp_path):
    app, asked = make_app()

    assert run(app, "import", str(tmp_path / "missing.env")) == 1
    assert asked.requests == []


@pytest.mark.spec("README.md")
def test_should_refuse_to_create_a_store_over_an_existing_one(make_app):
    app, asked = make_app()

    assert run(app, "init") == 1
    assert asked.requests == []


@pytest.mark.spec("README.md")
def test_should_ask_the_page_to_create_the_store(make_app):
    app, asked = make_app({"created": True}, with_store=False)

    assert run(app, "init") == 0
    assert asked.requests[0].op is Op.INIT


EXIT_WITH_SECRET_CHECK = "import os, sys; sys.exit(0 if os.environ.get('A') == sys.argv[1] else 9)"


@pytest.mark.spec("README.md")
def test_should_give_the_released_value_to_the_command(make_app):
    app, asked = make_app({"values": {"A": "secret"}})

    code = run(app, "exec", "A", "--", sys.executable, "-c", EXIT_WITH_SECRET_CHECK, "secret")

    assert code == 0
    assert asked.requests[0].names == ("A",)


@pytest.mark.spec("README.md")
def test_should_let_a_value_in_the_environment_win_without_an_approval(make_app):
    app, asked = make_app({"values": {"A": "from the store"}}, environ={"A": "from the shell"})

    code = run(app, "exec", "A", "--", sys.executable, "-c", EXIT_WITH_SECRET_CHECK, "from the shell")

    assert code == 0
    assert asked.requests == []


@pytest.mark.spec("README.md")
def test_should_return_the_exit_code_of_the_command(make_app):
    app, _ = make_app({"values": {"A": "secret"}})

    assert run(app, "exec", "A", "--", sys.executable, "-c", "import sys; sys.exit(7)") == 7


@pytest.mark.spec("README.md")
def test_should_not_run_anything_when_the_release_was_refused(make_app, tmp_path):
    marker = tmp_path / "ran"
    app, _ = make_app({"refused": True})

    code = run(app, "exec", "A", "--", sys.executable, "-c", f"open({str(marker)!r}, 'w')")

    assert code == 2
    assert not marker.exists()


@pytest.mark.spec("README.md")
def test_should_report_a_command_that_does_not_exist(make_app):
    app, _ = make_app({"values": {"A": "secret"}})

    assert run(app, "exec", "A", "--", "no-such-command-keyguard-passkey") == 127


@pytest.mark.spec("README.md")
def test_should_ask_for_a_command_after_the_separator(make_app):
    app, asked = make_app()

    assert run(app, "exec", "A") == 64
    assert asked.requests == []


@pytest.mark.spec("docs/provider.md")
@pytest.mark.parametrize("argv", [["get"], ["get", "not-a-name"], ["unknown"], []])
def test_should_exit_64_for_a_usage_error(argv):
    with pytest.raises(SystemExit) as raised:
        cli.build_parser().parse_args(argv)

    assert raised.value.code == 64


@pytest.mark.spec("README.md")
def test_should_keep_a_separator_inside_the_command(make_app):
    assert cli.split_command(["exec", "A", "--", "git", "log", "--", "file"]) == (["exec", "A"], ["git", "log", "--", "file"])
    assert cli.split_command(["get", "A"]) == (["get", "A"], [])


@pytest.mark.spec("README.md")
def test_should_answer_from_the_hold_without_an_approval(make_app):
    app, asked = make_app(hold=FakeHold({"A": "held"}))

    assert run(app, "get", "A") == 0
    assert app.stdout.getvalue() == "A=held\n"
    assert asked.requests == []
    assert "still held" in app.stderr.getvalue()


@pytest.mark.spec("README.md")
def test_should_ask_only_for_the_names_the_hold_lacks_and_keep_them(make_app):
    hold = FakeHold({"A": "held"})
    app, asked = make_app({"values": {"B": "fresh"}}, hold=hold)

    assert run(app, "get", "A", "B") == 0
    assert app.stdout.getvalue() == "A=held\nB=fresh\n"
    assert asked.requests[0].names == ("B",)
    assert hold.kept == [{"B": "fresh"}]


@pytest.mark.spec("README.md")
def test_should_still_release_through_an_approval_when_the_hold_fails(make_app):
    app, asked = make_app({"values": {"A": "fresh"}}, hold=FakeHold(broken=True))

    assert run(app, "get", "A") == 0
    assert app.stdout.getvalue() == "A=fresh\n"
    assert len(asked.requests) == 1


@pytest.mark.spec("README.md")
@pytest.mark.parametrize("argv,answer,stdin", [
    (("set", "A"), {"written": ["A"]}, "new"),
    (("rm", "A"), {"removed": "A"}, ""),
])
def test_should_forget_the_held_value_of_a_changed_secret(make_app, argv, answer, stdin):
    hold = FakeHold({"A": "old"})
    app, _ = make_app(answer, stdin=stdin, hold=hold)

    assert run(app, *argv) == 0
    assert hold.forgotten == [["A"]]


@pytest.mark.spec("README.md")
def test_should_forget_the_held_values_an_import_replaced(make_app, tmp_path):
    source = tmp_path / "secrets.env"
    source.write_text("A=one\nB=two\n", encoding="utf-8")
    hold = FakeHold()
    app, _ = make_app({"written": ["A"], "skipped": ["B"], "verified": "A"}, hold=hold)

    assert run(app, "import", str(source)) == 0
    assert hold.forgotten == [["A"]]


@pytest.mark.spec("README.md")
def test_should_drop_every_held_value_on_lock(make_app):
    hold = FakeHold({"A": "held"})
    app, asked = make_app(hold=hold)

    assert run(app, "lock") == 0
    assert hold.forgotten == [None]
    assert asked.requests == []


@pytest.mark.spec("README.md")
@pytest.mark.parametrize("setting,seconds", [(None, 600), ("0", 0), ("120", 120), ("99999", 600), ("-5", 0)])
def test_should_read_the_hold_time_and_cap_it_at_ten_minutes(setting, seconds):
    environ = {} if setting is None else {"KEYGUARD_PASSKEY_HOLD_SECONDS": setting}

    assert cli.hold_seconds(environ) == seconds


@pytest.fixture
def vendored(tmp_path, monkeypatch):
    root = tmp_path / "bin"
    (root / "keyguard_passkey").mkdir(parents=True)
    (root / "keyguard_passkey" / "cli.py").write_text("def main():\n    return 0\n", encoding="utf-8")
    manifest = install.Manifest(tag="v1.2.3", commit="c0ffee", files=install.digests(root))
    install.manifest_path(root).write_text(json.dumps(manifest.as_json()), encoding="utf-8")
    monkeypatch.setattr(install, "ROOT", root)
    return root


@pytest.mark.spec("docs/updating.md")
def test_should_confirm_a_vendored_copy_that_matches_its_manifest(make_app, vendored):
    app, asked = make_app(with_store=False)

    assert run(app, "verify-install") == 0
    assert app.stdout.getvalue() == "1 files match v1.2.3 (c0ffee)\n"
    assert asked.requests == []


@pytest.mark.spec("docs/updating.md")
def test_should_name_each_file_that_differs_from_the_manifest_and_exit_1(make_app, vendored):
    (vendored / "keyguard_passkey" / "cli.py").write_text("def main():\n    return 1\n", encoding="utf-8")
    app, _ = make_app(with_store=False)

    assert run(app, "verify-install") == 1
    assert "changed: keyguard_passkey/cli.py" in app.stderr.getvalue()
    assert app.stdout.getvalue() == ""


@pytest.mark.spec("docs/updating.md")
def test_should_exit_1_when_there_is_no_manifest_to_check_against(make_app, vendored):
    install.manifest_path(vendored).unlink()
    app, _ = make_app(with_store=False)

    assert run(app, "verify-install") == 1
    assert "not a vendored copy" in app.stderr.getvalue()


class HeldByService:
    def __init__(self) -> None:
        self.held_requests = []

    def held(self, request):
        self.held_requests.append(request)


def through_service(app):
    service = HeldByService()
    return dataclasses.replace(app, service=service), service


@pytest.mark.spec("docs/service.md")
def test_should_skip_the_store_check_when_the_service_holds_the_store(make_app):
    app, asked = make_app({"values": {"A": "one"}}, with_store=False)
    app, _ = through_service(app)

    assert run(app, "get", "A") == 0
    assert asked.requests[0].names == ("A",)


@pytest.mark.spec("docs/service.md")
def test_should_tell_the_service_when_the_hold_answers_everything(make_app):
    app, asked = make_app(with_store=False, hold=FakeHold({"A": "held"}))
    app, service = through_service(app)

    assert run(app, "get", "A") == 0
    assert app.stdout.getvalue() == "A=held\n"
    assert asked.requests == []
    assert [request.names for request in service.held_requests] == [("A",)]


@pytest.mark.spec("docs/service.md")
def test_should_refuse_import_through_the_service_before_reading_the_file(make_app, tmp_path):
    app, asked = make_app()
    app, _ = through_service(app)

    assert run(app, "import", str(tmp_path / "missing.env")) == 64
    assert "store each name with set" in app.stderr.getvalue()
    assert asked.requests == []


@pytest.mark.spec("docs/service.md")
@pytest.mark.parametrize("error, code", [(UsageError("needs --requester"), 64),
                                         (client.Cancelled("cancelled before the approver answered"), 2),
                                         (client.UnlockTimeout("no answer within 300 s"), 3),
                                         (client.ServiceRefused(500), 3)])
def test_should_map_service_failures_to_the_provider_exit_codes(error, code):
    def service(_request):
        raise error

    with pytest.raises(cli.Failure) as raised:
        cli.service_ask(service)(cli.Request(op=Op.GET, names=("A",)))

    assert (raised.value.code, str(raised.value)) == (code, str(error))


def parsed_args(*argv):
    args = cli.build_parser().parse_args(list(argv))
    args.command = []
    return args


@pytest.mark.spec("docs/service.md")
def test_should_hold_a_services_values_under_the_service_scope(tmp_path):
    app = cli.build_app(parsed_args("--service", f"{SERVICE_URL}/", "get", "A"), {"XDG_CONFIG_HOME": str(tmp_path)})

    assert app.service is not None and app.service.url == SERVICE_URL
    assert app.hold.scope == scope_of_service(SERVICE_URL)


@pytest.mark.spec("README.md")
def test_should_hold_a_local_stores_values_under_its_folder_scope(tmp_path):
    app = cli.build_app(parsed_args("get", "A"), {"XDG_CONFIG_HOME": str(tmp_path)})

    assert app.service is None
    assert app.hold.scope == scope_of(tmp_path / "keyguard-passkey" / "store")


@pytest.mark.spec("docs/service.md")
@pytest.mark.parametrize("environ, message", [({"KEYGUARD_PASSKEY_HOLD_SECONDS": "abc"}, "whole number"),
                                              ({"KEYGUARD_PASSKEY_SERVICE": SERVICE_URL,
                                                "KEYGUARD_PASSKEY_REQUEST_TOKEN_FILE": "."}, "cannot read")])
def test_should_refuse_settings_it_cannot_use_as_a_usage_error(tmp_path, environ, message):
    with pytest.raises(cli.Failure, match=message) as raised:
        cli.build_app(parsed_args("get", "A"), {"XDG_CONFIG_HOME": str(tmp_path), **environ})

    assert raised.value.code == 64


@pytest.fixture
def clean_environment(tmp_path, monkeypatch):
    for name in [name for name in os.environ if name.startswith("KEYGUARD")]:
        monkeypatch.delenv(name)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("KEYGUARD_PASSKEY_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("KEYGUARD_PASSKEY_HOLD_SECONDS", "0")


@pytest.fixture
def service_cli(clean_environment, unlock_service, phone, monkeypatch):
    connect = connection.connect
    monkeypatch.setattr(connection, "connect", lambda *args: dataclasses.replace(connect(*args),
                                                                                timeout=MISSED_ANSWER_TIMEOUT))

    def _run(decide, *argv, stdin=""):
        stderr = phone(decide)
        monkeypatch.setattr(sys, "stderr", stderr)
        monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
        code = cli.main(["--service", unlock_service.base, "--requester", REQUESTER, "--signing-key",
                         str(unlock_service.signing_key), "--token-file", str(unlock_service.token_file),
                         "--remote-recipient", unlock_service.remote.recipient, *argv])
        return code, stderr
    return _run


@pytest.mark.spec("docs/service.md")
@pytest.mark.parametrize("argv, printed", [(["get", "--format", "shell", "A"], "export A='it'\\''s'\n"),
                                           (["get", "A"], "A=it's\n")])
def test_should_print_what_the_approver_releases_through_the_service(service_cli, capsys, argv, printed):
    code, stderr = service_cli(lambda record: {"A": "it's"}, *argv)

    assert (code, capsys.readouterr().out) == (0, printed)
    assert stderr.seen[0]["host"] == REQUESTER


@pytest.mark.spec("docs/service.md")
def test_should_list_the_names_through_the_service(service_cli, capsys):
    code, _ = service_cli(lambda record: {"list": ["B", "A"]}, "list")

    assert (code, capsys.readouterr().out) == (0, "A\nB\n")


@pytest.mark.spec("docs/service.md")
def test_should_propose_a_value_from_standard_input_through_the_service(service_cli, unlock_service):
    proposed = []

    def approve(record):
        proposed.append(opened(unlock_service.remote.identity, str(record["value"])))
        return {"set": "A"}

    code, stderr = service_cli(approve, "set", "A", stdin="rotated\n")

    assert (code, proposed) == (0, ["rotated"])
    assert "Stored A" in stderr.getvalue()


@pytest.mark.spec("docs/service.md")
def test_should_print_the_token_the_approver_signs_through_the_service(service_cli, capsys):
    code, stderr = service_cli(signs_the_shown_message, "sign", "--target", TARGET, "--box", "macbook", "--title",
                               "Mazzotta-bot", stdin="yes, push\npersonal-infra 06be524c\n")

    assert (code, capsys.readouterr().out) == (0, owner_token("yes, push\npersonal-infra 06be524c", TARGET,
                                                              "macbook") + "\n")
    assert (stderr.seen[0]["op"], stderr.seen[0]["target_title"]) == ("sign", "Mazzotta-bot")


@pytest.mark.spec("docs/service.md")
@pytest.mark.parametrize("argv, stdin, message", [
    (["--target", "212df38e", "--box", "macbook"], "yes", "full conversation id"),
    (["--target", TARGET, "--box", "mac book"], "yes", "letters, digits or hyphens"),
    (["--target", TARGET, "--box", "macbook"], "", "1 to 1000 characters"),
], ids=["short target", "bad box", "no text"])
def test_should_refuse_a_message_the_service_would_refuse_before_asking(service_cli, argv, stdin, message):
    code, stderr = service_cli(signs_the_shown_message, "sign", *argv, stdin=stdin)

    assert code == 64
    assert message in stderr.getvalue()
    assert stderr.seen == []


@pytest.mark.spec("docs/service.md")
def test_should_report_a_store_without_the_owner_key(service_cli):
    code, stderr = service_cli(lambda record: {"absent": ["KEYGUARD_OWNER_SIGNING_KEY"]}, "sign", "--target", TARGET,
                               "--box", "macbook", stdin="yes")

    assert code == 4
    assert "not in the store: KEYGUARD_OWNER_SIGNING_KEY" in stderr.getvalue()


@pytest.mark.spec("docs/service.md")
def test_should_refuse_to_sign_without_the_service(make_app):
    app, asked = make_app()

    assert run(app, "sign", "--target", TARGET, "--box", "macbook") == 64
    assert "through the unlock service" in app.stderr.getvalue()
    assert asked.requests == []


@pytest.mark.spec("docs/service.md")
def test_should_remove_a_name_through_the_service(service_cli):
    code, stderr = service_cli(lambda record: {"rm": "A"}, "rm", "A")

    assert code == 0
    assert "Removed A" in stderr.getvalue()


@pytest.mark.spec("docs/service.md")
@pytest.mark.parametrize("decide, code, message", [
    (lambda record: {"absent": ["A"]}, 4, "not in the store: A"),
    (lambda record: os.kill(os.getpid(), signal.SIGTERM), 2, "cancelled"),
], ids=["absent", "cancelled"])
def test_should_exit_with_the_provider_code_when_no_value_comes_back(service_cli, decide, code, message):
    exit_code, stderr = service_cli(decide, "get", "A")

    assert exit_code == code
    assert message in stderr.getvalue()


@pytest.mark.spec("docs/service.md")
def test_should_exit_unavailable_when_the_service_drops_the_request(service_cli, unlock_service):
    code, stderr = service_cli(lambda record: unlock_service.call(f"/r/{record['id']}", "DELETE"), "get", "A")

    assert code == 3
    assert "no longer holds the request" in stderr.getvalue()


@pytest.mark.spec("docs/service.md")
def test_should_point_init_at_the_enrol_page_of_the_service(service_cli, unlock_service):
    code, stderr = service_cli(lambda record: None, "init")

    assert code == 64
    assert f"{unlock_service.base}/enroll" in stderr.getvalue()
    assert stderr.seen == []


def waiting_record(folder, request_id=WAITING_ID):
    folder.mkdir(parents=True, exist_ok=True)
    record = {"id": request_id, "vars": ["A"], "op": "get", "state": "waiting", "host": REQUESTER,
              "link": f"{SERVICE_URL}/approve?id={request_id}", "created": time.time(), "ended": None,
              "pid": os.getpid()}
    (folder / f"{request_id}.json").write_text(json.dumps(record))
    return record


@pytest.mark.spec("docs/service.md")
def test_should_print_the_waiting_requests_as_one_json_line(clean_environment, tmp_path, capsys):
    record = waiting_record(tmp_path / "pending")

    assert cli.main(["pending", "--dir", str(tmp_path / "pending")]) == 0

    [line] = capsys.readouterr().out.splitlines()
    assert [(listed["id"], listed["link"]) for listed in json.loads(line)["pending"]] == [(record["id"],
                                                                                          record["link"])]


@pytest.mark.spec("docs/service.md")
def test_should_read_the_pending_folder_from_the_state_folder_by_default(clean_environment, tmp_path, capsys):
    waiting_record(tmp_path / "state" / "pending")

    assert cli.main(["pending"]) == 0

    assert len(json.loads(capsys.readouterr().out)["pending"]) == 1


@pytest.mark.spec("docs/service.md")
def test_should_cancel_a_waiting_request_even_when_its_id_starts_with_a_dash(clean_environment, tmp_path):
    waiting_record(tmp_path / "pending")

    assert cli.main(["pending", "--dir", str(tmp_path / "pending"), f"--cancel={WAITING_ID}"]) == 0

    assert (tmp_path / "pending" / f"{WAITING_ID}.cancel").exists()


@pytest.mark.spec("docs/service.md")
def test_should_name_the_request_it_could_not_cancel(clean_environment, tmp_path, capsys):
    assert cli.main(["pending", "--dir", str(tmp_path / "pending"), "--cancel", "Unknown000000001"]) == 1

    assert "no request 'Unknown000000001' waits for an answer" in capsys.readouterr().err
