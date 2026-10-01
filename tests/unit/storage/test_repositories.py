"""Repository semantics: idempotency, conflicts, CAS, FK enforcement."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from omas.domain.artifact import Artifact, ArtifactKind
from omas.domain.decisions import (
    AwaitingEvent,
    AwaitingEventKind,
    Decision,
    DecisionAction,
)
from omas.domain.errors import (
    ArtifactExistsError,
    ConcurrencyError,
    IdempotencyConflictError,
    LedgerConflictError,
    TaskStateError,
)
from omas.domain.ids import (
    ArtifactId,
    AwaitingEventId,
    OperationId,
    TaskId,
    new_artifact_id,
    new_awaiting_event_id,
    new_decision_id,
    new_delivery_id,
    new_node_run_id,
    new_operation_id,
    new_task_id,
    new_template_version_id,
)
from omas.domain.operations import Operation, OperationState
from omas.domain.task import DataPolicy, NodeRun, NodeStatus, Task, TaskStatus
from omas.storage import Ledger

DIGEST_A = "a" * 64
DIGEST_B = "b" * 64
DIGEST_C = "c" * 64


def _now() -> datetime:
    return datetime.now(UTC)


def _make_task(request_id: str = "req-1") -> Task:
    now = _now()
    return Task(
        task_id=new_task_id(),
        request_id=request_id,
        status=TaskStatus.CREATED,
        data_policy=DataPolicy.LOCAL_ONLY,
        epoch=1,
        created_at=now,
        updated_at=now,
    )


def _make_operation(
    task_id: TaskId | None = None,
    *,
    payload_digest: str = DIGEST_A,
    state: OperationState = OperationState.INTENT,
) -> Operation:
    return Operation(
        operation_id=new_operation_id(),
        operation_key=f"opkey-{new_operation_id()}",
        state=state,
        task_id=task_id,
        node_name="extract",
        epoch=1,
        payload_digest=payload_digest,
        created_at=_now(),
    )


def _make_artifact(
    task_id: TaskId | None,
    relative_path: str,
    *,
    sha256: str = DIGEST_C,
    operation_id: OperationId | None = None,
    kind: ArtifactKind = ArtifactKind.RAW_TEXT,
) -> Artifact:
    return Artifact(
        artifact_id=new_artifact_id(),
        task_id=task_id,
        kind=kind,
        relative_path=relative_path,
        sha256=sha256,
        size=3,
        created_by_operation_id=operation_id,
        source_refs=(),
        created_at=_now(),
    )


@pytest.fixture()
def ledger(tmp_path):  # type: ignore[no-untyped-def]
    led = Ledger.open(tmp_path / "ledger.db")
    yield led
    led.close()


def _create_task(ledger: Ledger, request_id: str = "req-1") -> Task:
    return ledger.tasks.create(_make_task(request_id), DIGEST_A)


def _create_task_at(ledger: Ledger, request_id: str, created_at: datetime) -> Task:
    task = _make_task(request_id)
    task = task.model_copy(update={"created_at": created_at, "updated_at": created_at})
    return ledger.tasks.create(task, DIGEST_A)


# ------------------------------------------------------------------- tasks


def test_task_roundtrip_and_request_payload_digest(ledger: Ledger) -> None:
    task = _create_task(ledger)
    fetched = ledger.tasks.get(task.task_id)
    assert fetched == task
    by_request = ledger.tasks.get_by_request_id("req-1")
    assert by_request == task
    row = ledger.connection.execute(
        "SELECT request_payload_digest FROM tasks WHERE task_id = ?", (task.task_id,)
    ).fetchone()
    assert row["request_payload_digest"] == DIGEST_A


def test_task_create_idempotent_same_request_digest(ledger: Ledger) -> None:
    task = _make_task("req-1")
    first = ledger.tasks.create(task, DIGEST_A)
    second = ledger.tasks.create(task, DIGEST_A)  # full replay
    assert second == first
    count = ledger.connection.execute("SELECT COUNT(*) AS n FROM tasks").fetchone()["n"]
    assert count == 1


def test_task_create_conflict_different_request_digest(ledger: Ledger) -> None:
    _create_task(ledger, "req-1")
    with pytest.raises(IdempotencyConflictError):
        ledger.tasks.create(_make_task("req-1"), DIGEST_B)


def test_update_status_legal_transitions(ledger: Ledger) -> None:
    task = _create_task(ledger)
    task = ledger.tasks.update_status(task.task_id, TaskStatus.RUNNING)
    assert task.status is TaskStatus.RUNNING
    task = ledger.tasks.update_status(task.task_id, TaskStatus.AWAITING_USER)
    task = ledger.tasks.update_status(task.task_id, TaskStatus.RUNNING)
    task = ledger.tasks.update_status(task.task_id, TaskStatus.COMPLETED)
    assert task.is_terminal()


def test_update_status_rejects_illegal_transition(ledger: Ledger) -> None:
    task = _create_task(ledger)
    with pytest.raises(TaskStateError):  # created -> completed is not allowed
        ledger.tasks.update_status(task.task_id, TaskStatus.COMPLETED)
    with pytest.raises(TaskStateError):  # same status is not a transition
        ledger.tasks.update_status(task.task_id, TaskStatus.CREATED)
    running = ledger.tasks.update_status(task.task_id, TaskStatus.RUNNING)
    terminal = ledger.tasks.update_status(running.task_id, TaskStatus.FAILED)
    with pytest.raises(TaskStateError):  # failed is terminal
        ledger.tasks.update_status(terminal.task_id, TaskStatus.RUNNING)


def test_update_status_expected_mismatch_is_concurrency_error(ledger: Ledger) -> None:
    task = _create_task(ledger)
    ledger.tasks.update_status(task.task_id, TaskStatus.RUNNING)
    with pytest.raises(ConcurrencyError):
        ledger.tasks.update_status(
            task.task_id, TaskStatus.PARKED, expected=TaskStatus.CREATED
        )


def test_advance_epoch_cas(ledger: Ledger) -> None:
    task = _create_task(ledger)
    assert ledger.tasks.advance_epoch(task.task_id, 1) == 2
    with pytest.raises(ConcurrencyError):  # stale epoch
        ledger.tasks.advance_epoch(task.task_id, 1)
    with pytest.raises(ConcurrencyError):  # unknown task
        ledger.tasks.advance_epoch(new_task_id(), 1)
    assert ledger.tasks.advance_epoch(task.task_id, 2) == 3
    assert ledger.tasks.get(task.task_id).epoch == 3


def test_set_active_refs_set_and_clear(ledger: Ledger) -> None:
    task = _create_task(ledger)
    plan_a = ArtifactId(new_artifact_id())
    binding_a = ArtifactId(new_artifact_id())
    updated = ledger.tasks.set_active_refs(task.task_id, plan=plan_a, binding=binding_a)
    assert updated.active_plan_artifact_id == plan_a
    assert updated.active_binding_artifact_id == binding_a
    # explicit None clears only the provided column
    cleared = ledger.tasks.set_active_refs(task.task_id, plan=None)
    assert cleared.active_plan_artifact_id is None
    assert cleared.active_binding_artifact_id == binding_a


# -------------------------------------------------------------- operations


def test_operation_ensure_intent_idempotent_same_digest(ledger: Ledger) -> None:
    operation = _make_operation()
    first = ledger.operations.ensure_intent(operation)
    assert first.state is OperationState.INTENT
    replay = ledger.operations.ensure_intent(operation)
    assert replay == first
    count = ledger.connection.execute(
        "SELECT COUNT(*) AS n FROM operations"
    ).fetchone()["n"]
    assert count == 1


def test_operation_ensure_intent_conflict_different_digest(ledger: Ledger) -> None:
    operation = _make_operation(payload_digest=DIGEST_A)
    ledger.operations.ensure_intent(operation)
    same_key_other_payload = operation.model_copy(update={"payload_digest": DIGEST_B})
    with pytest.raises(IdempotencyConflictError):
        ledger.operations.ensure_intent(same_key_other_payload)


def test_operation_commit_is_idempotent_replay(ledger: Ledger) -> None:
    operation = ledger.operations.ensure_intent(_make_operation())
    outputs = [new_artifact_id(), new_artifact_id()]
    committed_at = _now()
    committed = ledger.operations.commit(operation.operation_id, outputs, committed_at)
    assert committed.state is OperationState.COMMITTED
    assert committed.committed_at == committed_at
    assert committed.output_artifact_ids == tuple(outputs)

    # replay with the same outputs returns the stored receipt unchanged
    replay = ledger.operations.commit(operation.operation_id, outputs, _now() + timedelta(1))
    assert replay == committed

    stored = ledger.operations.get(operation.operation_id)
    assert stored is not None and stored.output_artifact_ids == tuple(outputs)
    by_key = ledger.operations.get_by_key(operation.operation_key)
    assert by_key is not None and by_key.state is OperationState.COMMITTED


def test_operation_commit_replay_with_other_outputs_conflicts(ledger: Ledger) -> None:
    operation = ledger.operations.ensure_intent(_make_operation())
    ledger.operations.commit(operation.operation_id, [new_artifact_id()], _now())
    with pytest.raises(IdempotencyConflictError):
        ledger.operations.commit(operation.operation_id, [new_artifact_id()], _now())


def test_operation_abandon_semantics(ledger: Ledger) -> None:
    operation = ledger.operations.ensure_intent(_make_operation())
    abandoned = ledger.operations.abandon(operation.operation_id)
    assert abandoned.state is OperationState.ABANDONED
    assert ledger.operations.abandon(operation.operation_id) == abandoned  # idempotent
    with pytest.raises(LedgerConflictError):  # abandoned cannot be committed
        ledger.operations.commit(operation.operation_id, [], _now())

    committed = ledger.operations.ensure_intent(_make_operation())
    ledger.operations.commit(committed.operation_id, [new_artifact_id()], _now())
    with pytest.raises(LedgerConflictError):  # committed cannot be abandoned
        ledger.operations.abandon(committed.operation_id)


# --------------------------------------------------------------- artifacts


def test_artifact_register_fk_blocks_missing_task(ledger: Ledger) -> None:
    ghost_task = new_task_id()
    artifact = _make_artifact(TaskId(ghost_task), f"tasks/{ghost_task}/raw.txt")
    with pytest.raises(LedgerConflictError):
        ledger.artifacts.register(artifact)


def test_artifact_register_idempotent_same_id_same_content(ledger: Ledger) -> None:
    task = _create_task(ledger)
    artifact = _make_artifact(task.task_id, f"tasks/{task.task_id}/raw.txt")
    first = ledger.artifacts.register(artifact)
    replay = ledger.artifacts.register(artifact)
    assert replay == first
    by_path = ledger.artifacts.get_by_path(artifact.relative_path)
    assert by_path is not None and by_path.artifact_id == artifact.artifact_id


def test_artifact_same_id_different_content_conflicts(ledger: Ledger) -> None:
    task = _create_task(ledger)
    artifact = _make_artifact(task.task_id, f"tasks/{task.task_id}/raw.txt")
    ledger.artifacts.register(artifact)
    altered = artifact.model_copy(update={"sha256": DIGEST_B})
    with pytest.raises(IdempotencyConflictError):
        ledger.artifacts.register(altered)


def test_artifact_different_id_same_path_is_artifact_exists(ledger: Ledger) -> None:
    task = _create_task(ledger)
    path = f"tasks/{task.task_id}/raw.txt"
    ledger.artifacts.register(_make_artifact(task.task_id, path))
    with pytest.raises(ArtifactExistsError):
        ledger.artifacts.register(_make_artifact(task.task_id, path, sha256=DIGEST_B))


# ------------------------------------------------------------------ events


def test_events_seq_increasing_and_list_after(ledger: Ledger) -> None:
    task = _create_task(ledger)
    seqs = [
        ledger.events.append(
            task.task_id, "task.created", {"task_id": task.task_id}, {"artifacts": 0}
        ),
        ledger.events.append(task.task_id, "material.added", {}, {"files": 2}),
        ledger.events.append(task.task_id, "task.completed", {}, {}),
    ]
    assert seqs == sorted(seqs)
    assert len(set(seqs)) == 3

    records = ledger.events.list_after(task.task_id, 0)
    assert [record.seq for record in records] == seqs
    assert records[0].event_code == "task.created"
    assert records[0].refs == {"task_id": task.task_id}
    assert records[0].counts == {"artifacts": 0}
    assert records[1].counts == {"files": 2}
    assert records[0].created_at.tzinfo is not None

    tail = ledger.events.list_after(task.task_id, seqs[0])
    assert [record.seq for record in tail] == seqs[1:]


def test_events_append_fk_blocks_missing_task(ledger: Ledger) -> None:
    with pytest.raises(LedgerConflictError):
        ledger.events.append(new_task_id(), "task.created", {}, {})


# --------------------------------------------------------------- node runs


def test_node_run_create_mark_and_next_attempt(ledger: Ledger) -> None:
    task = _create_task(ledger)
    assert ledger.node_runs.next_attempt(task.task_id, "extract", 1) == 1
    first = NodeRun(
        node_run_id=new_node_run_id(),
        task_id=task.task_id,
        node_name="extract",
        epoch=1,
        attempt=1,
    )
    stored = ledger.node_runs.create(first)
    assert stored == first
    assert ledger.node_runs.next_attempt(task.task_id, "extract", 1) == 2
    # other epoch / node do not influence the attempt counter
    assert ledger.node_runs.next_attempt(task.task_id, "extract", 2) == 1
    assert ledger.node_runs.next_attempt(task.task_id, "render", 1) == 1

    finished_at = _now()
    done = ledger.node_runs.mark_status(
        first.node_run_id, NodeStatus.DONE, finished_at=finished_at
    )
    assert done.status is NodeStatus.DONE
    assert done.finished_at == finished_at
    assert done.error_code is None

    failed = ledger.node_runs.create(
        NodeRun(
            node_run_id=new_node_run_id(),
            task_id=task.task_id,
            node_name="extract",
            epoch=1,
            attempt=2,
        )
    )
    marked = ledger.node_runs.mark_status(
        failed.node_run_id, NodeStatus.FAILED_FINAL, error_code="DECODE_INVALID"
    )
    assert marked.error_code == "DECODE_INVALID"
    assert marked.finished_at is None
    with pytest.raises(LedgerConflictError):
        ledger.node_runs.mark_status(new_node_run_id(), NodeStatus.DONE)


# ------------------------------------------------------- template versions


def test_template_version_register_conflicts(ledger: Ledger) -> None:
    common = {
        "template_id": "tpl-contract",
        "version": 1,
        "docx_sha256": DIGEST_A,
        "contract_sha256": DIGEST_B,
        "styles_sha256": DIGEST_C,
        "static_map_sha256": DIGEST_A,
        "extractor_version": "p0.1",
        "created_at": _now(),
    }
    first = ledger.template_versions.register(
        template_version_id=new_template_version_id(), **common
    )
    # identical replay is idempotent
    replay = ledger.template_versions.register(
        template_version_id=first.template_version_id, **common
    )
    assert replay == first
    # same (template_id, version) with a different id/hash is a conflict
    with pytest.raises(LedgerConflictError):
        ledger.template_versions.register(
            template_version_id=new_template_version_id(),
            **{**common, "docx_sha256": DIGEST_B},
        )
    # next version is fine
    second = ledger.template_versions.register(
        template_version_id=new_template_version_id(),
        **{**common, "version": 2},
    )
    assert second.version == 2
    got = ledger.template_versions.get(second.template_version_id)
    assert got == second


def test_template_version_list_recent_newest_first(ledger: Ledger) -> None:
    assert ledger.template_versions.list_recent() == []
    common = {
        "template_id": "tpl-list",
        "docx_sha256": DIGEST_A,
        "contract_sha256": DIGEST_B,
        "styles_sha256": DIGEST_C,
        "static_map_sha256": DIGEST_A,
        "extractor_version": "p0.1",
    }
    older = ledger.template_versions.register(
        template_version_id=new_template_version_id(),
        created_at=_now() - timedelta(seconds=2),
        version=1,
        **common,
    )
    newer = ledger.template_versions.register(
        template_version_id=new_template_version_id(),
        created_at=_now(),
        version=2,
        **common,
    )
    assert ledger.template_versions.list_recent(limit=10) == [newer, older]
    assert ledger.template_versions.list_recent(limit=1) == [newer]


def test_task_list_recent_newest_first_and_limit(ledger: Ledger) -> None:
    assert ledger.tasks.list_recent() == []
    older = _create_task_at(ledger, "req-old", _now() - timedelta(seconds=2))
    newer = _create_task_at(ledger, "req-new", _now())
    listed = ledger.tasks.list_recent(limit=10)
    assert listed == [newer, older]
    assert ledger.tasks.list_recent(limit=1) == [newer]


# ------------------------------------------------- awaiting events/decisions


def _create_awaiting_event(ledger: Ledger, task: Task) -> AwaitingEvent:
    event = AwaitingEvent(
        awaiting_event_id=new_awaiting_event_id(),
        task_id=task.task_id,
        epoch=task.epoch,
        kind=AwaitingEventKind.MISSING_MATERIAL,
        missing_slot_ids=("slot_a",),
        created_at=_now(),
    )
    return ledger.awaiting_events.create(event)


def test_awaiting_event_create_get_resolve(ledger: Ledger) -> None:
    task = _create_task(ledger)
    event = _create_awaiting_event(ledger, task)
    fetched = ledger.awaiting_events.get(event.awaiting_event_id)
    assert fetched == event
    assert not fetched.is_resolved()

    resolved_at = _now()
    resolved = ledger.awaiting_events.resolve(event.awaiting_event_id, resolved_at)
    assert resolved.is_resolved() and resolved.resolved_at == resolved_at
    # second resolve is idempotent and keeps the first timestamp
    again = ledger.awaiting_events.resolve(
        event.awaiting_event_id, resolved_at + timedelta(days=1)
    )
    assert again.resolved_at == resolved_at


def test_decision_accept_idempotent_and_conflict(ledger: Ledger) -> None:
    task = _create_task(ledger)
    event = _create_awaiting_event(ledger, task)
    decision = Decision(
        decision_id=new_decision_id(),
        task_id=task.task_id,
        expected_epoch=task.epoch,
        awaiting_event_id=event.awaiting_event_id,
        action=DecisionAction.PROVIDE_MATERIAL,
        payload_digest=DIGEST_A,
        created_at=_now(),
    )
    first = ledger.decisions.accept(decision)
    replay = ledger.decisions.accept(decision)
    assert replay == first
    assert ledger.decisions.get(decision.decision_id) == first
    with pytest.raises(IdempotencyConflictError):
        ledger.decisions.accept(decision.model_copy(update={"payload_digest": DIGEST_B}))


def test_decision_fk_requires_existing_awaiting_event(ledger: Ledger) -> None:
    task = _create_task(ledger)
    decision = Decision(
        decision_id=new_decision_id(),
        task_id=task.task_id,
        expected_epoch=1,
        awaiting_event_id=AwaitingEventId(new_awaiting_event_id()),
        action=DecisionAction.OMIT_SLOT,
        payload_digest=DIGEST_A,
        created_at=_now(),
    )
    with pytest.raises(LedgerConflictError):
        ledger.decisions.accept(decision)


# -------------------------------------------------------------- deliveries


def test_delivery_record_and_per_task_uniqueness(ledger: Ledger) -> None:
    task = _create_task(ledger)
    operation = ledger.operations.ensure_intent(_make_operation(task.task_id))
    candidate = ledger.artifacts.register(
        _make_artifact(
            task.task_id,
            f"tasks/{task.task_id}/candidate.docx",
            operation_id=operation.operation_id,
            kind=ArtifactKind.DOCX_CANDIDATE,
        )
    )
    manifest = ledger.artifacts.register(
        _make_artifact(
            task.task_id,
            f"tasks/{task.task_id}/manifest.json",
            operation_id=operation.operation_id,
            kind=ArtifactKind.DELIVERY_MANIFEST,
        )
    )
    ledger.deliveries.record(
        delivery_id=new_delivery_id(),
        task_id=task.task_id,
        operation_id=operation.operation_id,
        candidate_artifact_id=candidate.artifact_id,
        final_sha256=DIGEST_C,
        manifest_artifact_id=manifest.artifact_id,
        created_at=_now(),
    )
    row = ledger.deliveries.get_by_task(task.task_id)
    assert row is not None
    assert row["final_sha256"] == DIGEST_C
    assert row["candidate_artifact_id"] == candidate.artifact_id

    # a second delivery for the same task is rejected (MVP: one per task)
    with pytest.raises(LedgerConflictError):
        ledger.deliveries.record(
            delivery_id=new_delivery_id(),
            task_id=task.task_id,
            operation_id=operation.operation_id,
            candidate_artifact_id=candidate.artifact_id,
            final_sha256=DIGEST_B,
            manifest_artifact_id=manifest.artifact_id,
            created_at=_now(),
        )
