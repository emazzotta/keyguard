from __future__ import annotations

import os
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from keyguard_passkey import hold
from keyguard_passkey.hold import (
    ADDRESS_FILE,
    MAX_HOLD_SECONDS,
    Held,
    HoldClient,
    HoldError,
    agent_environment,
    answer,
    scope_of,
    scope_of_service,
)

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "the-agent-token"
START_WAIT_SECONDS = 5.0


class Clock:
    def __init__(self) -> None:
        self.time = 1000.0

    def __call__(self) -> float:
        return self.time


@pytest.mark.spec("README.md")
def test_should_return_only_the_names_it_holds():
    held = Held()
    held.put("store", {"A": "one", "B": "two"}, 60)

    assert held.values_for("store", ["A", "MISSING"]) == {"A": "one"}


@pytest.mark.spec("README.md")
def test_should_drop_a_value_once_its_time_is_over():
    clock = Clock()
    held = Held(now=clock)
    held.put("store", {"A": "one"}, 60)

    clock.time += 61

    assert held.values_for("store", ["A"]) == {}
    assert held.empty


@pytest.mark.spec("README.md")
def test_should_never_hold_longer_than_five_minutes():
    clock = Clock()
    held = Held(now=clock)
    held.put("store", {"A": "one"}, 24 * 60 * 60)

    clock.time += MAX_HOLD_SECONDS + 1

    assert held.values_for("store", ["A"]) == {}


@pytest.mark.spec("README.md")
def test_should_keep_the_values_of_two_stores_apart():
    held = Held()
    held.put("first", {"A": "one"}, 60)

    assert held.values_for("second", ["A"]) == {}


@pytest.mark.spec("README.md")
@pytest.mark.parametrize("names,left", [(["A"], {"B": "two"}), (None, {})])
def test_should_forget_the_named_values_or_all_of_one_store(names, left):
    held = Held()
    held.put("store", {"A": "one", "B": "two"}, 60)
    held.put("other", {"A": "kept"}, 60)

    held.forget("store", names)

    assert held.values_for("store", ["A", "B"]) == left
    assert held.values_for("other", ["A"]) == {"A": "kept"}


@pytest.mark.spec("README.md")
@pytest.mark.parametrize("message", [
    {"token": "wrong", "op": "get", "scope": "store", "names": ["A"]},
    {"op": "get", "scope": "store", "names": ["A"]},
    "not an object",
])
def test_should_refuse_a_caller_without_the_token(message):
    held = Held()
    held.put("store", {"A": "one"}, 60)

    assert answer(held, TOKEN, message) == {"error": "refused"}


@pytest.mark.spec("README.md")
def test_should_answer_put_get_and_forget_for_the_token_holder():
    held = Held()
    put = {"token": TOKEN, "op": "put", "scope": "store", "values": {"A": "one"}, "seconds": 60}
    get = {"token": TOKEN, "op": "get", "scope": "store", "names": ["A"]}
    forget = {"token": TOKEN, "op": "forget", "scope": "store", "names": ["A"]}

    assert answer(held, TOKEN, put) == {"ok": True}
    assert answer(held, TOKEN, get) == {"values": {"A": "one"}}
    assert answer(held, TOKEN, forget) == {"ok": True}
    assert answer(held, TOKEN, get) == {"values": {}}


@pytest.mark.spec("README.md")
def test_should_derive_one_scope_per_store_folder_without_its_path(tmp_path):
    first, second = scope_of(tmp_path / "a"), scope_of(tmp_path / "b")

    assert first != second
    assert first == scope_of(tmp_path / "a")
    assert str(tmp_path) not in first


@pytest.mark.spec("docs/service.md")
def test_should_keep_a_services_values_apart_from_every_store_folder(tmp_path):
    url = "https://unlock.example.test"

    assert scope_of_service(url) == scope_of_service(url)
    assert scope_of_service(url) != scope_of_service("https://other.example.test")
    assert scope_of_service(url) != scope_of(tmp_path / url)


@pytest.mark.spec("docs/service.md")
@pytest.mark.parametrize("existing, expected", [(None, "/pkg"), ("", "/pkg"), ("/lib", f"/pkg{os.pathsep}/lib")])
def test_should_start_the_agent_with_the_package_on_the_import_path(existing, expected):
    environ = {"HOME": "/srv/someone"} if existing is None else {"HOME": "/srv/someone", "PYTHONPATH": existing}

    assert agent_environment(environ, Path("/pkg")) == {"HOME": "/srv/someone", "PYTHONPATH": expected}


@pytest.fixture
def state_dir(tmp_path):
    folder = tmp_path / "state"
    yield folder
    for scope in ("store", "another store"):
        HoldClient(state_dir=folder, scope=scope).forget(None)


def wait_for(condition, seconds: float = START_WAIT_SECONDS) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.05)
    return False


@pytest.mark.spec("README.md")
def test_should_hold_nothing_and_start_no_agent_for_a_lookup(state_dir):
    client = HoldClient(state_dir=state_dir, scope="store")

    assert client.lookup(["A"]) == {}
    assert not (state_dir / ADDRESS_FILE).exists()


@pytest.mark.spec("README.md")
def test_should_start_an_agent_that_gives_the_kept_values_back_to_its_store_only(state_dir):
    client = HoldClient(state_dir=state_dir, scope="store")

    client.keep({"A": "one", "B": "two"})

    assert client.lookup(["A", "B", "C"]) == {"A": "one", "B": "two"}
    assert HoldClient(state_dir=state_dir, scope="another store").lookup(["A"]) == {}


@pytest.mark.spec("README.md")
@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")
def test_should_keep_the_agents_address_readable_by_the_user_only(state_dir):
    HoldClient(state_dir=state_dir, scope="store").keep({"A": "one"})

    assert stat.S_IMODE((state_dir / ADDRESS_FILE).stat().st_mode) == 0o600


@pytest.mark.spec("README.md")
def test_should_forget_one_name_and_then_everything(state_dir):
    client = HoldClient(state_dir=state_dir, scope="store")
    client.keep({"A": "one", "B": "two"})

    client.forget(["A"])
    after_one = client.lookup(["A", "B"])
    client.forget()

    assert after_one == {"B": "two"}
    assert client.lookup(["A", "B"]) == {}


@pytest.mark.spec("README.md")
def test_should_release_a_kept_value_only_until_its_time_is_over(state_dir):
    client = HoldClient(state_dir=state_dir, scope="store", seconds=1)
    client.keep({"A": "one"})

    assert client.lookup(["A"]) == {"A": "one"}
    assert wait_for(lambda: client.lookup(["A"]) == {}, seconds=3)


@pytest.mark.spec("README.md")
def test_should_neither_keep_nor_look_up_with_a_hold_of_zero_seconds(state_dir):
    HoldClient(state_dir=state_dir, scope="store").keep({"A": "one"})
    off = HoldClient(state_dir=state_dir, scope="store", seconds=0)

    off.keep({"B": "two"})

    assert off.lookup(["A", "B"]) == {}
    assert HoldClient(state_dir=state_dir, scope="store").lookup(["B"]) == {}


@pytest.mark.spec("README.md")
def test_should_report_a_hold_that_refuses_the_token(state_dir):
    client = HoldClient(state_dir=state_dir, scope="store")
    client.keep({"A": "one"})
    address = state_dir / ADDRESS_FILE
    genuine = address.read_text()
    address.write_text(genuine.replace('"token": "', '"token": "x'))

    try:
        with pytest.raises(HoldError, match="refused"):
            client.lookup(["A"])
    finally:
        address.write_text(genuine)


@pytest.mark.spec("README.md")
def test_should_not_keep_the_callers_output_open(state_dir):
    script = ("from pathlib import Path; from keyguard_passkey.hold import HoldClient; "
              f"HoldClient(state_dir=Path({str(state_dir)!r}), scope='store').keep({{'A': 'one'}}); print('kept')")

    done = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=15,
                          env={**os.environ, "PYTHONPATH": str(ROOT)}, check=True)

    assert done.stdout == "kept\n"
    assert HoldClient(state_dir=state_dir, scope="store").lookup(["A"]) == {"A": "one"}


@pytest.mark.spec("README.md")
def test_should_end_the_agent_and_withdraw_its_address_once_it_holds_nothing(tmp_path):
    state = tmp_path / "state"
    agent = threading.Thread(target=hold.serve, args=(state,), kwargs={"grace": 1.5}, daemon=True)
    agent.start()
    assert wait_for(lambda: (state / ADDRESS_FILE).is_file())

    agent.join(timeout=6)

    assert not agent.is_alive()
    assert not (state / ADDRESS_FILE).exists()
