"""stage → promote commit pipeline for ArtifactStore."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from omas.artifacts.store import ArtifactStore
from omas.domain.errors import (
    ArtifactExistsError,
    ArtifactHashMismatchError,
    ArtifactNotFoundError,
    PathEscapeError,
)

DATA = b"hello artifacts\n"


@pytest.fixture()
def store(tmp_path: Path) -> ArtifactStore:
    return ArtifactStore(tmp_path)


def test_stage_then_promote_lands_in_out(store: ArtifactStore) -> None:
    staged = store.stage("task_1", "run_1", "draft.txt", DATA)
    assert staged.sha256 == hashlib.sha256(DATA).hexdigest()
    assert staged.size == len(DATA)
    assert staged.name == "draft.txt"
    assert staged.relative_path == "tasks/task_1/nodes/run_1/work/draft.txt"
    assert staged.path.read_bytes() == DATA

    promoted = store.promote(staged, store.node_out_relative("task_1", "run_1", "draft.txt"))
    assert promoted.relative_path == "tasks/task_1/nodes/run_1/out/draft.txt"
    assert promoted.sha256 == staged.sha256
    assert promoted.size == len(DATA)
    final = store.root / "tasks/task_1/nodes/run_1/out/draft.txt"
    assert final.read_bytes() == DATA
    # The staging file was moved away, not copied.
    assert not staged.path.exists()


def test_promote_refuses_existing_target_and_keeps_original(store: ArtifactStore) -> None:
    first = store.stage("task_1", "run_1", "a.txt", b"original bytes")
    relative = store.node_out_relative("task_1", "run_1", "same.txt")
    store.promote(first, relative)

    second = store.stage("task_1", "run_2", "same.txt", b"new content")
    with pytest.raises(ArtifactExistsError):
        store.promote(second, relative)

    # The original file's bytes are untouched and the contender stayed staged.
    assert store.read(relative) == b"original bytes"
    assert second.path.read_bytes() == b"new content"


def test_promote_expected_sha_mismatch(store: ArtifactStore) -> None:
    staged = store.stage("task_1", "run_1", "a.txt", DATA)
    relative = store.node_out_relative("task_1", "run_1", "a.txt")
    with pytest.raises(ArtifactHashMismatchError):
        store.promote(staged, relative, expected_sha256="0" * 64)
    # Nothing was committed; the staging file is still there.
    assert staged.path.exists()
    assert not store.exists(relative)


def test_promote_detects_tampered_staging(store: ArtifactStore) -> None:
    staged = store.stage("task_1", "run_1", "a.txt", DATA)
    # Fault injection: bypass the store and rewrite the work file in place.
    staged.path.write_bytes(b"tampered")
    with pytest.raises(ArtifactHashMismatchError):
        store.promote(staged, store.node_out_relative("task_1", "run_1", "a.txt"))
    assert staged.path.read_bytes() == b"tampered"  # left as-is for inspection


def test_promote_with_missing_staging_raises_not_found(store: ArtifactStore) -> None:
    staged = store.stage("task_1", "run_1", "a.txt", DATA)
    staged.path.unlink()  # fault injection: staging vanished before promote
    with pytest.raises(ArtifactNotFoundError):
        store.promote(staged, store.node_out_relative("task_1", "run_1", "a.txt"))


def test_promote_accepts_matching_expected_sha(store: ArtifactStore) -> None:
    staged = store.stage("task_1", "run_1", "a.txt", DATA)
    expected = hashlib.sha256(DATA).hexdigest()
    promoted = store.promote(
        staged, store.node_out_relative("task_1", "run_1", "a.txt"), expected_sha256=expected
    )
    assert promoted.sha256 == expected


def test_stage_never_overwrites_work_file(store: ArtifactStore) -> None:
    store.stage("task_1", "run_1", "s.txt", b"one")
    with pytest.raises(ArtifactExistsError):
        store.stage("task_1", "run_1", "s.txt", b"two")
    work = store.root / "tasks/task_1/nodes/run_1/work/s.txt"
    assert work.read_bytes() == b"one"


def test_stage_reuses_name_after_promote(store: ArtifactStore) -> None:
    staged = store.stage("task_1", "run_1", "s.txt", b"v1")
    store.promote(staged, store.node_out_relative("task_1", "run_1", "s.txt"))
    # The work slot is free again after the file was promoted away.
    again = store.stage("task_1", "run_1", "s.txt", b"v2")
    assert again.path.read_bytes() == b"v2"


def test_write_immutable_creates_once(store: ArtifactStore) -> None:
    relative = store.inbox_relative("task_1", "a.txt")
    promoted = store.write_immutable(relative, b"abc")
    assert store.read(relative) == b"abc"
    assert promoted.sha256 == hashlib.sha256(b"abc").hexdigest()
    with pytest.raises(ArtifactExistsError):
        store.write_immutable(relative, b"xyz")
    assert store.read(relative) == b"abc"


def test_promote_rejects_escaping_target(store: ArtifactStore) -> None:
    staged = store.stage("task_1", "run_1", "a.txt", DATA)
    with pytest.raises(PathEscapeError):
        store.promote(staged, "../escape.txt")
    # The staged file was not consumed by the failed attempt.
    assert store.exists(staged.relative_path)
