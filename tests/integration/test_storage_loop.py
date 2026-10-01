"""P0 storage closed loop: ledger + artifact pool + canonical spans wired.

This is the P0 acceptance "存储闭环真实落盘": create a task idempotently,
ingest text into the immutable pool, resolve a span against the canonical
text, run a node operation with intent→commit, register artifacts through the
recorder seam, replay the operation idempotently, detect tampering, and hold
the one-delivery-per-task invariant.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path

import pytest

from omas.artifacts.store import ArtifactStore
from omas.artifacts.writers import InboxWriter, NodeArtifactWriter
from omas.core import canonicalize, resolve_span, verify_span
from omas.domain.artifact import Artifact, ArtifactKind
from omas.domain.errors import (
    ArtifactHashMismatchError,
    ConcurrencyError,
    IdempotencyConflictError,
    LedgerConflictError,
)
from omas.domain.ids import (
    ArtifactId,
    OperationId,
    TaskId,
    new_delivery_id,
    new_node_run_id,
    new_operation_id,
    new_task_id,
)
from omas.domain.operations import IMPLEMENTATION_VERSION, Operation
from omas.domain.task import DataPolicy, Task
from omas.storage.db import Ledger


def _digest(payload: str) -> str:
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class LedgerRecorder:
    """The glue between the artifact pool and the ledger (services own it later)."""

    def __init__(self, ledger: Ledger) -> None:
        self._ledger = ledger

    def register(self, artifact: Artifact) -> Artifact:
        return self._ledger.artifacts.register(artifact)


@pytest.fixture()
def home(tmp_path: Path) -> Path:
    return tmp_path / "omas_home"


@pytest.fixture()
def ledger(home: Path) -> Ledger:
    home.mkdir(parents=True, exist_ok=True)
    lg = Ledger.open(home / "ledger.sqlite3")
    yield lg
    lg.close()


@pytest.fixture()
def store(home: Path) -> ArtifactStore:
    return ArtifactStore(home)


def _make_task(request_id: str = "req-001") -> Task:
    now = datetime.now(UTC)
    return Task(
        task_id=new_task_id(),
        request_id=request_id,
        data_policy=DataPolicy.LOCAL_ONLY,
        created_at=now,
        updated_at=now,
    )


def test_storage_closed_loop(ledger: Ledger, store: ArtifactStore) -> None:
    recorder = LedgerRecorder(ledger)

    # 1. task creation + submit idempotency (T07)
    task = _make_task()
    digest = _digest("template=weekly;policy=local_only")
    created = ledger.tasks.create(task, digest)
    assert created.task_id == task.task_id
    replayed = ledger.tasks.create(_make_task(), digest)
    assert replayed.task_id == task.task_id
    with pytest.raises(IdempotencyConflictError):
        ledger.tasks.create(_make_task(), _digest("template=weekly;policy=llm_allowed"))

    # 2. ingest text into the immutable pool (CRLF + Chinese)
    raw = "## 本周销售\r\n营收 120 万，环比 +8%。\r\n## 下周计划\r\n推进华东渠道。".encode("utf-8")
    inbox = InboxWriter(store)
    _raw_file, canonical_file, canonical = inbox.ingest_text(task.task_id, "week.md", raw)
    assert "\r" not in canonical.text
    assert canonical_file.illegal_positions == ()

    canonical_artifact = Artifact(
        artifact_id=ArtifactId(canonical_file.name.removesuffix(".txt")),
        task_id=TaskId(task.task_id),
        kind=ArtifactKind.CANONICAL_TEXT,
        relative_path=canonical_file.relative_path,
        sha256=canonical.sha256,
        size=canonical.size_bytes,
        source_refs=(),
        created_at=datetime.now(UTC),
    )
    recorded = recorder.register(canonical_artifact)
    assert ledger.artifacts.get(recorded.artifact_id) is not None

    # 3. span resolution against the persisted canonical file
    persisted = store.read_verified(canonical_file.relative_path, canonical.sha256)
    reloaded = canonicalize(persisted)
    assert reloaded.sha256 == canonical.sha256
    title_start = canonical.text.index("本周销售")
    span = resolve_span(recorded.artifact_id, reloaded, title_start, title_start + 4)
    verify_span(reloaded, span)

    # 4. node run + operation intent -> stage IR json -> commit out -> commit op
    run_id = new_node_run_id()
    writer = NodeArtifactWriter(store, task.task_id, run_id, recorder)
    operation_key = (
        f"{task.task_id}:1:assemble_bind:{_digest(canonical.sha256)}:{IMPLEMENTATION_VERSION}"
    )
    op = Operation(
        operation_id=new_operation_id(),
        operation_key=operation_key,
        task_id=TaskId(task.task_id),
        node_name="assemble_bind",
        epoch=1,
        payload_digest=_digest(canonical.sha256),
    )
    stored_op = ledger.operations.ensure_intent(op)

    ir_json = b'{"schema_version": 1, "task_id": "' + task.task_id.encode() + b'"}'
    staged = writer.stage("binding.json", ir_json)
    ir_artifact = writer.commit_out(
        staged,
        kind=ArtifactKind.BINDING_IR,
        final_name=f"{staged.sha256}.json",
        operation_id=OperationId(stored_op.operation_id),
        source_refs=(ArtifactId(recorded.artifact_id),),
    )
    committed = ledger.operations.commit(
        OperationId(stored_op.operation_id), (ir_artifact.artifact_id,), datetime.now(UTC)
    )
    assert committed.output_artifact_ids == (ir_artifact.artifact_id,)

    # ledger row points at the real file; bytes verify (verify raises on mismatch)
    store.verify(ir_artifact.relative_path, ir_artifact.sha256)
    assert store.read(ir_artifact.relative_path) == ir_json

    # 5. operation replay after "crash": same key returns committed receipt,
    #    no second file, no second registration
    replay = ledger.operations.ensure_intent(op)
    assert replay.operation_id == stored_op.operation_id
    assert replay.output_artifact_ids == (ir_artifact.artifact_id,)
    assert store.exists(ir_artifact.relative_path)
    by_path = ledger.artifacts.get_by_path(ir_artifact.relative_path)
    assert by_path is not None and by_path.artifact_id == ir_artifact.artifact_id

    # 6. epoch CAS
    new_epoch = ledger.tasks.advance_epoch(TaskId(task.task_id), 1)
    assert new_epoch == 2
    with pytest.raises(ConcurrencyError):
        ledger.tasks.advance_epoch(TaskId(task.task_id), 1)

    # 7. single delivery per task (T06/T23 foundation)
    ledger.deliveries.record(
        delivery_id=new_delivery_id(),
        task_id=TaskId(task.task_id),
        operation_id=OperationId(stored_op.operation_id),
        candidate_artifact_id=ArtifactId(ir_artifact.artifact_id),
        final_sha256=ir_artifact.sha256,
        manifest_artifact_id=ArtifactId(ir_artifact.artifact_id),
        created_at=datetime.now(UTC),
    )
    with pytest.raises(LedgerConflictError):
        ledger.deliveries.record(
            delivery_id=new_delivery_id(),
            task_id=TaskId(task.task_id),
            operation_id=OperationId(new_operation_id()),
            candidate_artifact_id=ArtifactId(recorded.artifact_id),
            final_sha256=recorded.sha256,
            manifest_artifact_id=ArtifactId(recorded.artifact_id),
            created_at=datetime.now(UTC),
        )


def test_tampered_artifact_detected_and_marked(ledger: Ledger, store: ArtifactStore) -> None:
    """T11: a registered artifact whose bytes changed must be caught."""
    task = ledger.tasks.create(_make_task(request_id="req-t11"), _digest("p"))
    inbox = InboxWriter(store)
    _, canonical_file, canonical = inbox.ingest_text(
        task.task_id, "m.md", "原始内容".encode("utf-8")
    )
    artifact = Artifact(
        artifact_id=ArtifactId(canonical_file.name.removesuffix(".txt")),
        task_id=TaskId(task.task_id),
        kind=ArtifactKind.CANONICAL_TEXT,
        relative_path=canonical_file.relative_path,
        sha256=canonical.sha256,
        size=canonical.size_bytes,
        created_at=datetime.now(UTC),
    )
    ledger.artifacts.register(artifact)

    # overwrite the bytes behind the ledger's back (bypassing the store API)
    target = store.resolve_path(canonical_file.relative_path)
    target.write_bytes("篡改内容".encode("utf-8"))

    with pytest.raises(ArtifactHashMismatchError):
        store.verify(canonical_file.relative_path, canonical.sha256)
    corrupt_at = ledger.artifacts.mark_corrupt(ArtifactId(artifact.artifact_id), datetime.now(UTC))
    # marking is idempotent and keeps the first timestamp
    again = ledger.artifacts.mark_corrupt(ArtifactId(artifact.artifact_id), datetime.now(UTC))
    assert again == corrupt_at


def test_raw_and_canonical_both_persisted(ledger: Ledger, store: ArtifactStore) -> None:
    task = ledger.tasks.create(_make_task(request_id="req-raw"), _digest("p"))
    inbox = InboxWriter(store)
    raw_bytes = "a\r\nb".encode("utf-8")
    raw_file, canonical_file, _ = inbox.ingest_text(task.task_id, "note.txt", raw_bytes)
    # raw artifact is byte-identical to the input (line endings untouched)
    assert store.read(raw_file.relative_path) == raw_bytes
    # canonical is normalised
    assert store.read(canonical_file.relative_path).decode("utf-8") == "a\nb"
