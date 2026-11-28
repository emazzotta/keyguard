from __future__ import annotations

import argparse
import io
from pathlib import Path

import pytest

from keyguard_passkey.service import connection
from keyguard_passkey.service.asking import UsageError

URL = "https://unlock.example.test"

pytestmark = pytest.mark.spec("docs/service.md")


def parsed(*argv: str) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    connection.add_arguments(parser)
    return parser.parse_args(list(argv))


def connected(tmp_path: Path, environ: dict[str, str], *argv: str):
    return connection.connect(parsed(*argv), environ, tmp_path / "config", tmp_path / "state", io.StringIO())


@pytest.fixture
def config(tmp_path: Path) -> Path:
    folder = tmp_path / "config"
    folder.mkdir()
    (folder / connection.TOKEN_FILE).write_text("token-from-config\n")
    (folder / connection.REMOTE_RECIPIENT_FILE).write_text("age1fromconfig\n")
    return folder


@pytest.mark.parametrize("argv, environ, url", [
    (["--service", f"  {URL}/ "], {}, URL),
    ([], {connection.SERVICE_ENV: f"{URL}/"}, URL),
    (["--service", URL], {connection.SERVICE_ENV: "https://other.example.test"}, URL),
    ([], {}, ""),
], ids=["flag", "variable", "flag over variable", "unset"])
def test_should_take_the_service_from_the_flag_else_the_variable(argv, environ, url):
    assert connection.service_url(parsed(*argv), environ) == url


def test_should_read_the_files_in_the_config_folder_by_default(tmp_path, config):
    ask = connected(tmp_path, {}, "--service", URL)

    assert ask.signing_key == config / connection.SIGNING_KEY_FILE
    assert (ask.token, ask.remote_recipient) == ("token-from-config", "age1fromconfig")
    assert ask.pending_dir == tmp_path / "state" / connection.PENDING_FOLDER


def test_should_prefer_the_variables_over_the_config_folder(tmp_path, config):
    token = tmp_path / "elsewhere-token"
    token.write_text("token-from-variable")
    environ = {connection.SIGNING_KEY_ENV: str(tmp_path / "env.key"), connection.TOKEN_FILE_ENV: str(token),
               connection.REMOTE_RECIPIENT_ENV: "age1fromvariable", connection.REQUESTER_ENV: " devbox ",
               connection.PENDING_ENV: str(tmp_path / "queue")}

    ask = connected(tmp_path, environ, "--service", URL)

    assert (ask.signing_key, ask.token, ask.remote_recipient) == (tmp_path / "env.key", "token-from-variable",
                                                                  "age1fromvariable")
    assert (ask.requester, ask.pending_dir) == ("devbox", tmp_path / "queue")


def test_should_prefer_the_flags_over_the_variables(tmp_path, config):
    token = tmp_path / "flag-token"
    token.write_text("token-from-flag")
    environ = {connection.SIGNING_KEY_ENV: str(tmp_path / "env.key"), connection.REQUESTER_ENV: "devbox",
               connection.REMOTE_RECIPIENT_ENV: "age1fromvariable"}

    ask = connected(tmp_path, environ, "--service", URL, "--signing-key", str(tmp_path / "flag.key"), "--token-file",
                    str(token), "--requester", "mac", "--remote-recipient", "age1fromflag")

    assert (ask.signing_key, ask.token, ask.requester, ask.remote_recipient) == (
        tmp_path / "flag.key", "token-from-flag", "mac", "age1fromflag")


def test_should_never_take_the_token_itself_from_a_variable(tmp_path):
    ask = connected(tmp_path, {"KEYGUARD_PASSKEY_REQUEST_TOKEN": "leaked"}, "--service", URL)

    assert (ask.token, ask.remote_recipient, ask.requester) == ("", "", "")


def test_should_name_the_asking_tmux_pane(tmp_path):
    assert connected(tmp_path, {"TMUX_PANE": "%3"}, "--service", URL).pane == "%3"


def test_should_refuse_a_token_file_it_cannot_read(tmp_path):
    folder = tmp_path / "token-is-a-folder"
    folder.mkdir()

    with pytest.raises(UsageError, match="cannot read"):
        connected(tmp_path, {}, "--service", URL, "--token-file", str(folder))


def test_should_read_the_presence_folder_from_the_variable_else_the_claude_folder_of_the_home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")

    default = connected(tmp_path, {}, "--service", URL).presence_dir
    chosen = connected(tmp_path, {connection.PRESENCE_ENV: str(tmp_path / "elsewhere")}, "--service", URL).presence_dir

    assert (default, chosen) == (tmp_path / "home" / ".claude" / "presence", tmp_path / "elsewhere")


def test_should_take_the_container_label_from_its_variable_and_none_without_it(tmp_path):
    named = connected(tmp_path, {connection.CONTAINER_ENV: " devbox-7f7d78d989-tgd9t "}, "--service", URL)

    assert (named.container, connected(tmp_path, {}, "--service", URL).container) == ("devbox-7f7d78d989-tgd9t", "")
