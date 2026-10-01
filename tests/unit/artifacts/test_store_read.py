"""Read / verify semantics for ArtifactStore."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from omas.artifacts.store import ArtifactStore
from omas.domain.errors import (
    ArtifactHashMismatchError,
    ArtifactNotFoundError,
    PathEscapeError,
)

DATA = b"verified read payload\n"


@pytest.fixture()
def store(tmp_path: Path) -> ArtifactStore:
    return ArtifactStore(tmp_path)


@pytest.fixture()
def committed(store: ArtifactStore) -> tuple[str, str]:
    staged = store.stage("task_1", "run_1", "a.txt", DATA)
    relative = store.node_out_relative("task_1", "run_1", "a.txt")
    promoted = store.promote(staged, relative)
    return relative, promoted.sha256


def test_read_returns_bytes(store: ArtifactStore, committed: tuple[str, str]) -> None:
    relative, _ = committed
    assert store.read(relative) == DATA
    assert store.exists(relative)


def test_read_missing_raises_not_found(store: ArtifactStore) -> None:
    with pytest.raises(ArtifactNotFoundError):
        store.read("tasks/task_1/inbox/nope.txt")
    assert not store.exists("tasks/task_1/inbox/nope.txt")


def test_read_verified_and_verify_pass(store: ArtifactStore, committed: tuple[str, str]) -> None:
    relative, sha = committed
    assert store.read_verified(relative, sha) == DATA
    store.verify(relative, sha)  # no exception
    assert store.verify(relative, sha) is None


def test_verify_detects_tampering(store: ArtifactStore, committed: tuple[str, str]) -> None:
    relative, sha = committed
    # Fault injection: rewrite the file behind the store's back.
    (store.root / relative).write_bytes(b"tampered")
    with pytest.raises(ArtifactHashMismatchError):
        store.verify(relative, sha)
    with pytest.raises(ArtifactHashMismatchError):
        store.read_verified(relative, sha)
    # Plain read is hash-agnostic by contract; callers verify separately.
    assert store.read(relative) == b"tampered"


def test_read_refuses_symlink(store: ArtifactStore, tmp_path: Path) -> None:
    target = tmp_path / "outside.bin"
    target.write_bytes(b"secret bytes")
    inbox = store.root / "tasks" / "t1" / "inbox"
    inbox.mkdir(parents=True)
    os.symlink(target, inbox / "ln.bin")
    with pytest.raises(PathEscapeError):
        store.read("tasks/t1/inbox/ln.bin")


def test_exists_false_for_directory(store: ArtifactStore) -> None:
    store.inbox_dir("task_1")
    assert store.exists("tasks/task_1/inbox") is False


def test_read_back_large_payload_roundtrip(store: ArtifactStore) -> None:
    payload = os.urandom(3 * 1024 * 1024)  # forces the chunked read loop
    staged = store.stage("task_1", "run_1", "big.bin", payload)
    relative = store.node_out_relative("task_1", "run_1", "big.bin")
    store.promote(staged, relative)
    assert store.read_verified(relative, hashlib.sha256(payload).hexdigest()) == payload
