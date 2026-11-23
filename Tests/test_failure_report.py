"""Tests for the failure line that the Mac server posts to the unlock service, against a stand-in service."""
from __future__ import annotations

import time
from types import SimpleNamespace

import pytest
from conftest import eventually

from keyguard_server import failure_report
from keyguard_server.failure_report import ReadFailure, Reason

FAILED = ReadFailure(Reason.NOT_REACHED, variables=2, session="48f92bcb")
NOT_SENT = "failure report not sent"


def stderr_says(capsys, text: str) -> bool:
    seen: list[str] = []

    def found() -> bool:
        seen.append(capsys.readouterr().err)
        return text in "".join(seen)

    return eventually(found)


# ---- The notice ----


def test_should_post_one_keyguard_failed_notice_that_names_the_mac_keyguard(unlock_service):
    failure_report.send(FAILED, unlock_service.url)

    assert len(unlock_service.posts) == 1
    post = unlock_service.posts[0]
    assert post.path == "/notify"
    assert post.headers["content-type"] == "application/json"
    assert post.body == {
        "kind": "keyguard_failed",
        "title": "Mac keyguard",
        "text": "Keyguard read of 2 variables failed at the Mac, and the phone request never reached the "
                "unlock service.",
        "session": "48f92bcb",
    }


def test_should_send_no_credential(unlock_service):
    failure_report.send(FAILED, unlock_service.url)

    assert {"authorization", "tailscale-user-login"}.isdisjoint(unlock_service.posts[0].headers)


def test_should_leave_the_session_out_when_the_caller_has_none(unlock_service):
    failure_report.send(ReadFailure(Reason.PHONE_OFF, variables=1), unlock_service.url)

    assert "session" not in unlock_service.posts[0].body


def test_should_post_to_the_store_url_whatever_its_trailing_slash(unlock_service):
    failure_report.send(FAILED, unlock_service.url + "/")

    assert [post.path for post in unlock_service.posts] == ["/notify"]


@pytest.mark.parametrize("variables,counted", [(1, "1 variable "), (3, "3 variables ")])
def test_should_count_the_variables_without_naming_one(variables, counted):
    text = ReadFailure(Reason.PHONE_OFF, variables).text

    assert counted in text


@pytest.mark.parametrize("reason", list(Reason))
def test_should_open_with_a_sentence_that_fits_the_300_characters_the_service_shows(reason):
    text = ReadFailure(reason, variables=12).text

    assert text.startswith("Keyguard read of 12 variables failed at the Mac, and ")
    assert len(text) <= 300


# ---- A post that fails ----


@pytest.mark.parametrize("status", [400, 429, 500])
def test_should_log_the_status_without_the_text_and_post_only_once_when_the_service_refuses(
        unlock_service, capsys, status):
    unlock_service.status = status

    failure_report.send(FAILED, unlock_service.url)

    err = capsys.readouterr().err
    assert f"{NOT_SENT} (HTTP {status})" in err
    assert FAILED.text not in err
    assert len(unlock_service.posts) == 1


def test_should_log_and_return_when_the_service_cannot_be_reached(dead_service_url, capsys):
    failure_report.send(FAILED, dead_service_url)

    err = capsys.readouterr().err
    assert f"{NOT_SENT} (URLError)" in err
    assert FAILED.text not in err


@pytest.mark.parametrize("address", ["file:///etc/hosts", "ftp://unlock.example.ts.net", "unlock.example.ts.net"])
def test_should_send_nothing_to_an_address_that_is_not_http_or_https(capsys, address):
    failure_report.send(FAILED, address)

    assert "not an http or https address" in capsys.readouterr().err


@pytest.mark.parametrize("address", ["http://[::1", "http://unlock.example.ts.net:notaport"])
def test_should_log_and_return_when_the_address_does_not_parse(capsys, address):
    failure_report.send(FAILED, address)

    err = capsys.readouterr().err
    assert NOT_SENT in err
    assert FAILED.text not in err


# ---- Reporting from the read ----


def test_should_post_from_a_background_thread_so_the_read_does_not_wait_for_the_service(unlock_service, monkeypatch):
    monkeypatch.setattr(failure_report, "PHONE_SERVICE", unlock_service.url)
    unlock_service.gate.clear()
    started = time.monotonic()

    failure_report.report(FAILED)

    assert time.monotonic() - started < 1
    assert unlock_service.arrived()
    unlock_service.gate.set()


def test_should_post_once_when_a_store_service_is_set(unlock_service, monkeypatch):
    monkeypatch.setattr(failure_report, "PHONE_SERVICE", unlock_service.url)

    failure_report.report(FAILED)

    assert unlock_service.arrived()
    time.sleep(0.3)
    assert len(unlock_service.posts) == 1


def test_should_post_nothing_without_a_store_service(monkeypatch):
    sent: list[ReadFailure] = []
    monkeypatch.setattr(failure_report, "send", lambda failure, service: sent.append(failure))
    monkeypatch.setattr(failure_report, "threading", SimpleNamespace(Thread=_Immediately))

    failure_report.report(FAILED)

    assert sent == []


def test_should_post_nothing_for_a_read_of_no_variables(monkeypatch):
    sent: list[ReadFailure] = []
    monkeypatch.setattr(failure_report, "PHONE_SERVICE", "https://unlock.example.ts.net")
    monkeypatch.setattr(failure_report, "send", lambda failure, service: sent.append(failure))
    monkeypatch.setattr(failure_report, "threading", SimpleNamespace(Thread=_Immediately))

    failure_report.report(ReadFailure(Reason.PHONE_OFF, variables=0))

    assert sent == []


def test_should_log_a_failed_background_post_and_not_raise(dead_service_url, monkeypatch, capsys):
    monkeypatch.setattr(failure_report, "PHONE_SERVICE", dead_service_url)

    failure_report.report(FAILED)

    assert stderr_says(capsys, NOT_SENT)


def test_should_log_and_not_raise_when_no_thread_can_start(monkeypatch, capsys):
    monkeypatch.setattr(failure_report, "PHONE_SERVICE", "https://unlock.example.ts.net")
    monkeypatch.setattr(failure_report, "threading", SimpleNamespace(Thread=_CannotStart))

    failure_report.report(FAILED)

    err = capsys.readouterr().err
    assert f"{NOT_SENT} (RuntimeError)" in err
    assert FAILED.text not in err


class _Immediately:
    def __init__(self, target, args=(), daemon=None):
        self._target, self._args = target, args

    def start(self) -> None:
        self._target(*self._args)


class _CannotStart:
    def __init__(self, target, args=(), daemon=None):
        pass

    def start(self) -> None:
        raise RuntimeError("can't start new thread")
