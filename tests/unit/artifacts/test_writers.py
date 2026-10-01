"""Capability-separated writers: NodeArtifactWriter / DeliveryWriter / InboxWriter."""

from __future__ import annotations

import hashlib
import inspect
from pathlib import Path

import pytest

from omas.artifacts.store import ArtifactStore
from omas.artifacts.writers import DeliveryWriter, InboxWriter, NodeArtifactWriter
from omas.core import canonicalize
from omas.domain.artifact import Artifact, ArtifactKind
from omas.domain.errors import (
    ArtifactExistsError,
    CapabilityError,
    DecodeError,
    PathEscapeError,
)
from omas.domain.ids import ArtifactId, OperationId, new_operation_id


class FakeRecorder:
    """Minimal in-memory ArtifactRecorder — idempotent per artifact id."""

    def __init__(self) -> None:
        self._by_id: dict[str, Artifact] = {}
        self.calls: list[Artifact] = []

    def register(self, artifact: Artifact) -> Artifact:
        self.calls.append(artifact)
        existing = self._by_id.get(artifact.artifact_id)
        if existing is not None:
            return existing
        self._by_id[artifact.artifact_id] = artifact
        return artifact

    def get(self, artifact_id: str) -> Artifact | None:
        return self._by_id.get(artifact_id)


@pytest.fixture()
def store(tmp_path: Path) -> ArtifactStore:
    return ArtifactStore(tmp_path)


# —— NodeArtifactWriter ————————————————————————————————————————————————


def test_node_writer_commit_out_registers_artifact(store: ArtifactStore) -> None:
    recorder = FakeRecorder()
    writer = NodeArtifactWriter(store, "task_1", "run_1", recorder=recorder)
    payload = b'{"plan": []}'
    staged = writer.stage("plan.json", payload)

    operation_id = new_operation_id()
    source = ArtifactId("art_src")
    artifact = writer.commit_out(
        staged,
        ArtifactKind.PLAN_IR,
        "plan.json",
        operation_id=operation_id,
        source_refs=(source,),
        content_type="application/json",
    )

    assert artifact.artifact_id.startswith("art_")
    assert artifact.task_id == "task_1"
    assert artifact.kind is ArtifactKind.PLAN_IR
    assert artifact.relative_path == "tasks/task_1/nodes/run_1/out/plan.json"
    assert artifact.sha256 == hashlib.sha256(payload).hexdigest()
    assert artifact.size == len(payload)
    assert artifact.created_by_operation_id == operation_id
    assert artifact.source_refs == (source,)
    assert artifact.content_type == "application/json"
    assert artifact.created_at is not None
    assert store.read(artifact.relative_path) == payload
    assert recorder.get(artifact.artifact_id) is artifact


def test_node_writer_scopes_staging_to_own_run(store: ArtifactStore) -> None:
    writer = NodeArtifactWriter(store, "task_1", "run_1")
    staged = writer.stage("f.bin", b"123")
    assert staged.relative_path == "tasks/task_1/nodes/run_1/work/f.bin"
    assert staged.path.parent == store.root / "tasks" / "task_1" / "nodes" / "run_1" / "work"


def test_node_writer_rejects_pathed_final_name(store: ArtifactStore) -> None:
    writer = NodeArtifactWriter(store, "task_1", "run_1")
    staged = writer.stage("x.txt", b"x")
    for bad in ("../evil.txt", "a/b.txt", ".", "", "a\\b.txt"):
        with pytest.raises(CapabilityError):
            writer.commit_out(staged, ArtifactKind.NODE_LOG, bad)


def test_node_writer_rejects_foreign_staged_file(store: ArtifactStore) -> None:
    writer = NodeArtifactWriter(store, "task_1", "run_1")
    other_run = NodeArtifactWriter(store, "task_1", "run_2")
    foreign = other_run.stage("x.txt", b"x")
    with pytest.raises(CapabilityError):
        writer.commit_out(foreign, ArtifactKind.NODE_LOG, "x.txt")
    # Nothing was consumed: the foreign staging file is untouched.
    assert foreign.path.read_bytes() == b"x"


def test_node_writer_without_recorder_still_returns_artifact(store: ArtifactStore) -> None:
    writer = NodeArtifactWriter(store, "task_1", "run_1")
    staged = writer.stage("log.txt", b"line\n")
    artifact = writer.commit_out(staged, ArtifactKind.NODE_LOG, "log.txt")
    assert artifact.kind is ArtifactKind.NODE_LOG
    assert artifact.created_by_operation_id is None
    assert artifact.source_refs == ()
    assert store.read_verified(artifact.relative_path, artifact.sha256) == b"line\n"


def test_node_writer_has_no_api_to_address_other_scopes(store: ArtifactStore) -> None:
    """Scope is bound at construction; no method accepts a task/run/path override."""
    writer = NodeArtifactWriter(store, "task_1", "run_1")
    public = {name for name in vars(type(writer)) if not name.startswith("_")}
    assert public == {"stage", "commit_out"}
    for name in public:
        params = inspect.signature(getattr(type(writer), name)).parameters
        assert "task_id" not in params
        assert "run_id" not in params
        assert not any(param.endswith("path") for param in params)


# —— DeliveryWriter —————————————————————————————————————————————————————


def test_delivery_writer_writes_deliverables(store: ArtifactStore) -> None:
    recorder = FakeRecorder()
    node = NodeArtifactWriter(store, "task_1", "run_1")
    staged = node.stage("final.docx", b"PK\x03\x04docx")
    finalizer = DeliveryWriter(store, "task_1", recorder=recorder)
    operation_id: OperationId = new_operation_id()
    artifact = finalizer.deliver(staged, "final.docx", operation_id=operation_id)

    assert artifact.kind is ArtifactKind.DELIVERY_DOCX
    assert artifact.relative_path == "tasks/task_1/deliverables/final.docx"
    assert artifact.created_by_operation_id == operation_id
    assert store.read(artifact.relative_path) == b"PK\x03\x04docx"
    assert recorder.get(artifact.artifact_id) is artifact


def test_delivery_writer_rejects_pathed_name(store: ArtifactStore) -> None:
    node = NodeArtifactWriter(store, "task_1", "run_1")
    staged = node.stage("f.docx", b"PK")
    finalizer = DeliveryWriter(store, "task_1")
    with pytest.raises(CapabilityError):
        finalizer.deliver(staged, "../final.docx")
    with pytest.raises(CapabilityError):
        finalizer.deliver(staged, "sub/final.docx")


def test_delivery_writer_target_is_immutable(store: ArtifactStore) -> None:
    node_a = NodeArtifactWriter(store, "task_1", "run_1")
    node_b = NodeArtifactWriter(store, "task_1", "run_2")
    finalizer = DeliveryWriter(store, "task_1")
    finalizer.deliver(node_a.stage("f.docx", b"first"), "final.docx")
    with pytest.raises(ArtifactExistsError):
        finalizer.deliver(node_b.stage("f.docx", b"second"), "final.docx")
    assert store.read("tasks/task_1/deliverables/final.docx") == b"first"


# —— InboxWriter ————————————————————————————————————————————————————————


def test_ingest_text_canonicalises_and_preserves_raw(store: ArtifactStore) -> None:
    raw_text = "e\u0301\r\nx\ry\n"  # NFD acute + CRLF + lone CR
    raw = raw_text.encode("utf-8")
    writer = InboxWriter(store)

    raw_file, canonical_file, canonical = writer.ingest_text("task_1", "note.txt", raw)

    # Raw bytes land in inbox/ byte-for-byte (provenance copy).
    assert raw_file.relative_path == "tasks/task_1/inbox/note.txt"
    assert store.read(raw_file.relative_path) == raw
    assert raw_file.sha256 == hashlib.sha256(raw).hexdigest()

    # Canonical text: NFC + LF only.
    expected = canonicalize(raw)
    assert canonical.text == "é\nx\ny\n"
    assert canonical.sha256 == expected.sha256
    assert canonical_file.relative_path.startswith("tasks/task_1/canonical/")
    assert canonical_file.relative_path.endswith(".txt")
    assert canonical_file.name == canonical_file.relative_path.rsplit("/", 1)[1]
    assert canonical_file.sha256 == canonical.sha256
    assert canonical_file.illegal_positions == ()
    assert store.read(canonical_file.relative_path) == canonical.text.encode("utf-8")


def test_ingest_reports_illegal_control_positions(store: ArtifactStore) -> None:
    raw = "a\u0000b\u0001c".encode("utf-8")
    _, canonical_file, canonical = InboxWriter(store).ingest_text("task_1", "n.txt", raw)
    # Not filtered — written as-is, positions reported for the caller to reject.
    assert canonical.text == "a\x00b\x01c"
    assert canonical_file.illegal_positions == (1, 3)
    assert store.read(canonical_file.relative_path) == canonical.text.encode("utf-8")


def test_ingest_propagates_decode_error(store: ArtifactStore) -> None:
    with pytest.raises(DecodeError):
        InboxWriter(store).ingest_text("task_1", "n.txt", b"a\xffb\xfe")


def test_ingest_filename_must_be_single_component(store: ArtifactStore) -> None:
    writer = InboxWriter(store)
    with pytest.raises(PathEscapeError):
        writer.ingest_text("task_1", "../evil.txt", b"x")
    with pytest.raises(PathEscapeError):
        writer.ingest_text("task_1", "sub/n.txt", b"x")
    with pytest.raises(PathEscapeError):
        writer.ingest_text("../task_1", "n.txt", b"x")


def test_ingest_raw_is_immutable(store: ArtifactStore) -> None:
    writer = InboxWriter(store)
    writer.ingest_text("task_1", "a.txt", b"one")
    with pytest.raises(ArtifactExistsError):
        writer.ingest_text("task_1", "a.txt", b"two")
    assert store.read("tasks/task_1/inbox/a.txt") == b"one"


def test_ingest_produces_distinct_canonical_files(store: ArtifactStore) -> None:
    writer = InboxWriter(store)
    _, first, _ = writer.ingest_text("task_1", "a.txt", b"same bytes")
    _, second, _ = writer.ingest_text("task_1", "b.txt", b"same bytes")
    assert first.relative_path != second.relative_path
    assert first.sha256 == second.sha256
