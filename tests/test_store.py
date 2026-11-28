from __future__ import annotations

import hashlib
import json
import threading
import time

import pytest

from keyguard_passkey import store as store_module
from keyguard_passkey.store import META_FILE, StaleStore, Store, StoreError, is_store_path

VARIABLE = "vars/" + "ab" * 32 + ".age"


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "store")


@pytest.mark.spec("README.md")
def test_should_report_version_zero_and_no_store_before_the_first_commit(store):
    assert store.meta().version == 0
    assert not store.exists


@pytest.mark.spec("README.md")
def test_should_write_files_and_a_manifest_with_their_sha256(store):
    meta = store.commit({"index.age": b"index", VARIABLE: b"secret"}, [], expected_version=0)

    assert store.exists
    assert store.read(VARIABLE) == b"secret"
    assert meta.version == 1
    assert meta.files == {"index.age": hashlib.sha256(b"index").hexdigest(),
                          VARIABLE: hashlib.sha256(b"secret").hexdigest()}
    assert json.loads((store.root / META_FILE).read_text()) == {"version": 1, "files": meta.files, "meta_version": 1}


@pytest.mark.spec("README.md")
def test_should_refuse_a_commit_based_on_an_older_version(store):
    store.commit({"index.age": b"one"}, [], expected_version=0)

    with pytest.raises(StaleStore):
        store.commit({"index.age": b"two"}, [], expected_version=0)

    assert store.read("index.age") == b"one"


@pytest.mark.spec("README.md")
def test_should_delete_a_file_and_drop_it_from_the_manifest(store):
    store.commit({"index.age": b"index", VARIABLE: b"secret"}, [], expected_version=0)

    meta = store.commit({}, [VARIABLE], expected_version=1)

    assert VARIABLE not in meta.files
    assert not (store.root / VARIABLE).exists()


@pytest.mark.spec("README.md")
@pytest.mark.parametrize("path", ["../outside.age", "meta.json", "vars/short.age", "/etc/passwd", "index.age.tmp"])
def test_should_refuse_a_path_that_is_not_a_store_file(store, path):
    with pytest.raises(StoreError):
        store.commit({path: b"x"}, [], expected_version=0)

    assert not store.root.exists() or not any(store.root.iterdir())


@pytest.mark.spec("README.md")
@pytest.mark.parametrize("path", ["identity-remote.local.age", "identity-remote.local.passkey", "identity-remote.paper.age"])
def test_should_accept_the_wrapped_identity_files(store, path):
    store.commit({path: b"wrapped"}, [], expected_version=0)

    assert store.read(path) == b"wrapped"


@pytest.mark.spec("docs/service.md")
@pytest.mark.parametrize("path", [
    "", "vars/../x", f"vars/{'a' * 64}.txt", "vars/AAAA.age", f"vars/{'a' * 63}.age", f"vars/{'a' * 65}.age",
    f"vars/{'a' * 64}.age/../evil", "identity-remote.age", "identity-remote..age", "identity-remote.PHONE.age",
    "identity-remote.phone.txt", "identity-remote.phone", "identity-remote/x.age", "identity-remote.a b.age",
    "identity-remote.passkey", "identity-remote.phone.passkey.tmp", "passkey.phone",
])
def test_should_refuse_every_other_path(store, path):
    assert not is_store_path(path)
    with pytest.raises(StoreError):
        store.read(path)


@pytest.mark.spec("docs/service.md")
def test_should_leave_no_staged_files_behind(store):
    store.commit({"index.age": b"x", VARIABLE: b"y"}, [], expected_version=0)

    assert not list(store.root.rglob("*.tmp"))


@pytest.mark.spec("docs/service.md")
def test_should_keep_what_it_committed_when_reopened(store):
    store.commit({"index.age": b"kept"}, [], expected_version=0)

    assert Store(store.root).read("index.age") == b"kept"


@pytest.mark.spec("docs/service.md")
def test_should_name_the_current_version_when_a_commit_is_stale(store):
    store.commit({"index.age": b"one"}, [], expected_version=0)

    with pytest.raises(StaleStore) as stale:
        store.commit({"index.age": b"two"}, [], expected_version=0)

    assert (stale.value.current, str(stale.value)) == (1, "version is 1")


@pytest.mark.spec("docs/service.md")
def test_should_flush_every_file_to_disk_before_the_manifest(store, monkeypatch):
    synced: list[int] = []
    monkeypatch.setattr(store_module.os, "fsync", synced.append)

    store.commit({"index.age": b"x", VARIABLE: b"y"}, [], expected_version=0)

    assert len(synced) == 3


@pytest.mark.spec("docs/service.md")
def test_should_let_only_one_of_two_racing_commits_through(store, monkeypatch):
    real_fsync = store_module.os.fsync

    def slow_fsync(descriptor: int) -> None:
        time.sleep(0.2)
        real_fsync(descriptor)

    monkeypatch.setattr(store_module.os, "fsync", slow_fsync)
    outcomes: list[object] = []

    def commit(data: bytes) -> None:
        try:
            outcomes.append(store.commit({"index.age": data}, [], expected_version=0).version)
        except StaleStore:
            outcomes.append("stale")

    racers = [threading.Thread(target=commit, args=(data,)) for data in (b"one", b"two")]
    for racer in racers:
        racer.start()
    for racer in racers:
        racer.join()

    assert sorted(map(str, outcomes)) == ["1", "stale"]
    assert store.meta().version == 1


@pytest.mark.spec("docs/service.md")
def test_should_report_a_manifest_that_is_not_json(store):
    store.root.mkdir(parents=True)
    (store.root / META_FILE).write_text("{nope")

    with pytest.raises(StoreError):
        store.meta()
