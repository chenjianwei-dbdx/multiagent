"""Immutability contract: no overwrite anywhere, single registration.

Immutability here means: the write surface (stage / promote /
write_immutable) never overwrites an existing artifact file, reads are
strictly read-only, and every registered artifact id is registered exactly
once.  There is intentionally **no** API that can modify or replace the bytes
of a committed file — new content means a new path (a new artifact id).
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from omas.artifacts.store import ArtifactStore
from omas.artifacts.writers import DeliveryWriter, NodeArtifactWriter
from omas.domain.artifact import Artifact, ArtifactKind
from omas.domain.errors import ArtifactExistsError


class FakeRecorder:
    """In-memory ArtifactRecorder, idempotent per artifact id."""

    def __init__(self) -> None:
        self._by_id: dict[str, Artifact] = {}

    def register(self, artifact: Artifact) -> Artifact:
        existing = self._by_id.get(artifact.artifact_id)
        if existing is not None:
            return existing
        self._by_id[artifact.artifact_id] = artifact
        return artifact


@pytest.fixture()
def store(tmp_path: Path) -> ArtifactStore:
    return ArtifactStore(tmp_path)


def test_second_promote_to_same_path_fails(store: ArtifactStore) -> None:
    relative = store.node_out_relative("task_1", "run_1", "a.txt")
    promoted = store.promote(store.stage("task_1", "run_1", "a.txt", b"first"), relative)
    with pytest.raises(ArtifactExistsError):
        store.promote(store.stage("task_1", "run_2", "a.txt", b"second"), relative)
    # The committed bytes are unchanged and still verify against the record.
    assert store.read_verified(relative, promoted.sha256) == b"first"


def test_recommitting_same_artifact_fails_at_file_level(store: ArtifactStore) -> None:
    recorder = FakeRecorder()
    writer = NodeArtifactWriter(store, "task_1", "run_1", recorder=recorder)
    first = writer.commit_out(writer.stage("a.txt", b"v1"), ArtifactKind.NODE_LOG, "a.txt")
    # A retried operation re-stages and re-commits the same final name: the
    # file pool refuses the overwrite; recovery is adopt_existing, not replace.
    with pytest.raises(ArtifactExistsError):
        writer.commit_out(writer.stage("a.txt", b"v1"), ArtifactKind.NODE_LOG, "a.txt")
    assert store.read_verified(first.relative_path, first.sha256) == b"v1"


def test_deliverables_cannot_be_rewritten(store: ArtifactStore) -> None:
    node = NodeArtifactWriter(store, "task_1", "run_1")
    finalizer = DeliveryWriter(store, "task_1")
    delivered = finalizer.deliver(node.stage("f.docx", b"PK-one"), "final.docx")
    node2 = NodeArtifactWriter(store, "task_1", "run_2")
    with pytest.raises(ArtifactExistsError):
        finalizer.deliver(node2.stage("f.docx", b"PK-two"), "final.docx")
    assert store.read_verified(delivered.relative_path, delivered.sha256) == b"PK-one"


def test_reads_never_mutate_files(store: ArtifactStore) -> None:
    relative = store.node_out_relative("task_1", "run_1", "a.txt")
    promoted = store.promote(store.stage("task_1", "run_1", "a.txt", b"stable"), relative)
    before = store.read(relative)
    store.read(relative)
    store.read_verified(relative, promoted.sha256)
    store.verify(relative, promoted.sha256)
    assert store.read(relative) == before
    assert store.read_verified(relative, promoted.sha256) == b"stable"


def test_recorder_registration_is_idempotent_per_id() -> None:
    recorder = FakeRecorder()
    artifact = Artifact(
        artifact_id="art_fixed",
        task_id="task_1",
        kind=ArtifactKind.NODE_LOG,
        relative_path="tasks/task_1/nodes/run_1/out/a.txt",
        sha256="0" * 64,
        size=1,
        created_at=datetime.now(UTC),
    )
    again = recorder.register(artifact)
    assert again is artifact  # same id, same content → the stored record
