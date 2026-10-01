"""Explicit short-transaction repositories over the OMAS ledger schema.

Ledger rules implemented here (AGENTS.md, v1.1 §4.3 and §8):

- SQLite stores refs and finite metadata only; never body text or file bytes.
- Every user-visible write is idempotent: the same key with the same payload
  digest replays the stored record, the same key with a different payload
  digest raises :class:`IdempotencyConflictError`.
- Each repository method runs in exactly one short ``BEGIN IMMEDIATE``
  transaction; no LLM call or render waits inside a transaction.
- Datetimes are stored as UTC ISO-8601 strings with an explicit ``Z`` and
  restored as timezone-aware datetimes (the domain DTOs require tz-aware).

Minimal repositories (bindings, slot_overrides, gate_reports, llm_calls,
resolved_spans) provide insert + get only; later phases own their semantics.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import Enum, auto
from typing import Final, NamedTuple, cast

from omas.domain.artifact import Artifact, ArtifactKind
from omas.domain.decisions import (
    AwaitingEvent,
    AwaitingEventKind,
    Decision,
    DecisionAction,
    SlotOverride,
)
from omas.domain.errors import (
    ArtifactExistsError,
    ArtifactNotFoundError,
    ConcurrencyError,
    IdempotencyConflictError,
    LedgerConflictError,
    TaskStateError,
)
from omas.domain.ids import (
    ArtifactId,
    AwaitingEventId,
    DecisionId,
    NodeRunId,
    OperationId,
    SpanHandle,
    TaskId,
    TemplateVersionId,
)
from omas.domain.ir import Producer
from omas.domain.operations import Operation, OperationState
from omas.domain.spans import SourceSpanRef
from omas.domain.task import (
    TASK_STATUS_TRANSITIONS,
    DataPolicy,
    NodeRun,
    NodeStatus,
    Task,
    TaskStatus,
)

from .db import from_db_datetime, immediate, reading, to_db_datetime, utc_now

type Row = sqlite3.Row

# --------------------------------------------------------------------- helpers


class _Unset(Enum):
    TOKEN = auto()


UNSET: Final[_Unset] = _Unset.TOKEN
"""Sentinel distinguishing "column not provided" from an explicit ``None``."""


def _optional[T](value: str | None, wrap: Callable[[str], T]) -> T | None:
    return None if value is None else wrap(value)


def _dumps(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True)


def _insert_or_conflict(
    conn: sqlite3.Connection, sql: str, params: Sequence[object], what: str
) -> None:
    """INSERT, surfacing IntegrityError (unique/CHECK/FK) as LedgerConflictError."""
    try:
        conn.execute(sql, params)
    except sqlite3.IntegrityError as exc:
        raise LedgerConflictError(f"{what} insert rejected: {exc}") from exc


class _Repository:
    """Base: shared connection in autocommit mode (see db.immediate)."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn


# ------------------------------------------------------------------ row mappers


def _row_to_task(row: sqlite3.Row) -> Task:
    return Task(
        task_id=TaskId(row["task_id"]),
        request_id=str(row["request_id"]),
        status=TaskStatus(row["status"]),
        data_policy=DataPolicy(row["data_policy"]),
        epoch=int(row["epoch"]),
        template_version_id=_optional(row["template_version_id"], TemplateVersionId),
        active_plan_artifact_id=_optional(row["active_plan_artifact_id"], ArtifactId),
        active_binding_artifact_id=_optional(row["active_binding_artifact_id"], ArtifactId),
        active_render_ir_artifact_id=_optional(
            row["active_render_ir_artifact_id"], ArtifactId
        ),
        active_candidate_artifact_id=_optional(
            row["active_candidate_artifact_id"], ArtifactId
        ),
        created_at=from_db_datetime(row["created_at"]),
        updated_at=from_db_datetime(row["updated_at"]),
    )


def _row_to_operation(row: sqlite3.Row) -> Operation:
    outputs = cast(
        tuple[str, ...], tuple(json.loads(row["output_artifact_ids_json"]))
    )
    return Operation(
        operation_id=OperationId(row["operation_id"]),
        operation_key=str(row["operation_key"]),
        state=OperationState(row["state"]),
        task_id=_optional(row["task_id"], TaskId),
        node_name=_optional(row["node_name"], str),
        epoch=row["epoch"],
        payload_digest=str(row["payload_digest"]),
        created_at=from_db_datetime(row["created_at"]),
        committed_at=_optional(row["committed_at"], from_db_datetime),
        output_artifact_ids=outputs,
    )


def _row_to_artifact(row: sqlite3.Row) -> Artifact:
    lineage = json.loads(row["lineage_json"])
    return Artifact(
        artifact_id=ArtifactId(row["artifact_id"]),
        task_id=_optional(row["task_id"], TaskId),
        kind=ArtifactKind(row["kind"]),
        relative_path=str(row["relative_path"]),
        sha256=str(row["sha256"]),
        size=int(row["size"]),
        created_by_operation_id=_optional(row["created_by_operation_id"], OperationId),
        source_refs=tuple(ArtifactId(ref) for ref in lineage),
        content_type=_optional(row["content_type"], str),
        created_at=from_db_datetime(row["created_at"]),
    )


def _row_to_node_run(row: sqlite3.Row) -> NodeRun:
    return NodeRun(
        node_run_id=NodeRunId(row["node_run_id"]),
        task_id=TaskId(row["task_id"]),
        node_name=str(row["node_name"]),
        epoch=int(row["epoch"]),
        attempt=int(row["attempt"]),
        status=NodeStatus(row["status"]),
        operation_id=_optional(row["operation_id"], OperationId),
        error_code=_optional(row["error_code"], str),
        started_at=_optional(row["started_at"], from_db_datetime),
        finished_at=_optional(row["finished_at"], from_db_datetime),
    )


def _row_to_awaiting_event(row: sqlite3.Row) -> AwaitingEvent:
    slots = cast(tuple[str, ...], tuple(json.loads(row["missing_slot_ids_json"])))
    return AwaitingEvent(
        awaiting_event_id=AwaitingEventId(row["awaiting_event_id"]),
        task_id=TaskId(row["task_id"]),
        epoch=int(row["epoch"]),
        kind=AwaitingEventKind(row["kind"]),
        missing_slot_ids=slots,
        created_at=from_db_datetime(row["created_at"]),
        resolved_at=_optional(row["resolved_at"], from_db_datetime),
    )


def _row_to_decision(row: sqlite3.Row) -> Decision:
    attachments = cast(
        tuple[ArtifactId, ...], tuple(json.loads(row["attachment_refs_json"]))
    )
    return Decision(
        decision_id=DecisionId(row["decision_id"]),
        task_id=TaskId(row["task_id"]),
        expected_epoch=int(row["expected_epoch"]),
        awaiting_event_id=AwaitingEventId(row["awaiting_event_id"]),
        action=DecisionAction(row["action"]),
        attachment_artifact_ids=attachments,
        payload_digest=str(row["payload_digest"]),
        created_at=from_db_datetime(row["created_at"]),
    )


def _row_to_slot_override(row: sqlite3.Row) -> SlotOverride:
    return SlotOverride(
        task_id=TaskId(row["task_id"]),
        slot_id=str(row["slot_id"]),
        decision_id=DecisionId(row["decision_id"]),
        epoch=int(row["epoch"]),
        action="omit",
        reason_artifact_id=(
            ArtifactId(row["reason_artifact_id"]) if row["reason_artifact_id"] else None
        ),
        created_at=from_db_datetime(row["created_at"]),
    )


def _same_artifact(a: Artifact, b: Artifact) -> bool:
    """Content identity for idempotent re-registration (created_at excluded)."""
    return (
        a.artifact_id == b.artifact_id
        and a.task_id == b.task_id
        and a.kind == b.kind
        and a.relative_path == b.relative_path
        and a.sha256 == b.sha256
        and a.size == b.size
        and a.created_by_operation_id == b.created_by_operation_id
        and a.source_refs == b.source_refs
        and a.content_type == b.content_type
    )


def _same_node_run(a: NodeRun, b: NodeRun) -> bool:
    return (
        a.node_run_id == b.node_run_id
        and a.task_id == b.task_id
        and a.node_name == b.node_name
        and a.epoch == b.epoch
        and a.attempt == b.attempt
        and a.status == b.status
        and a.operation_id == b.operation_id
        and a.error_code == b.error_code
        and a.started_at == b.started_at
        and a.finished_at == b.finished_at
    )


def _same_awaiting_event(a: AwaitingEvent, b: AwaitingEvent) -> bool:
    return (
        a.awaiting_event_id == b.awaiting_event_id
        and a.task_id == b.task_id
        and a.epoch == b.epoch
        and a.kind == b.kind
        and a.missing_slot_ids == b.missing_slot_ids
        and a.created_at == b.created_at
    )


# ------------------------------------------------------------ TaskRepository

_TASK_INSERT = (
    "INSERT INTO tasks (task_id, request_id, request_payload_digest, status,"
    " data_policy, epoch, template_version_id, active_plan_artifact_id,"
    " active_binding_artifact_id, active_render_ir_artifact_id,"
    " active_candidate_artifact_id, created_at, updated_at)"
    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)


class TaskRepository(_Repository):
    def create(self, task: Task, request_payload_digest: str) -> Task:
        """Insert a task; idempotent on (request_id, request_payload_digest).

        A unique request_id conflict with the same digest replays the stored
        task; with a different digest it is IDEMPOTENCY_CONFLICT.
        """
        params = (
            task.task_id,
            task.request_id,
            request_payload_digest,
            task.status.value,
            task.data_policy.value,
            task.epoch,
            task.template_version_id,
            task.active_plan_artifact_id,
            task.active_binding_artifact_id,
            task.active_render_ir_artifact_id,
            task.active_candidate_artifact_id,
            to_db_datetime(task.created_at),
            to_db_datetime(task.updated_at),
        )
        with immediate(self._conn):
            try:
                self._conn.execute(_TASK_INSERT, params)
                return task
            except sqlite3.IntegrityError as exc:
                row = self._conn.execute(
                    "SELECT * FROM tasks WHERE request_id = ?", (task.request_id,)
                ).fetchone()
                if row is not None:
                    if row["request_payload_digest"] == request_payload_digest:
                        return _row_to_task(row)
                    raise IdempotencyConflictError(
                        f"request_id {task.request_id} already recorded with a "
                        "different request payload digest"
                    ) from exc
                duplicate_id = self._conn.execute(
                    "SELECT task_id FROM tasks WHERE task_id = ?", (task.task_id,)
                ).fetchone()
                if duplicate_id is not None:
                    raise LedgerConflictError(
                        f"task_id {task.task_id} already exists under another "
                        "request_id"
                    ) from exc
                raise LedgerConflictError(f"task insert rejected: {exc}") from exc

    def get(self, task_id: TaskId) -> Task | None:
        row = self._conn.execute(
            "SELECT * FROM tasks WHERE task_id = ?", (task_id,)
        ).fetchone()
        return None if row is None else _row_to_task(row)

    def get_by_request_id(self, request_id: str) -> Task | None:
        row = self._conn.execute(
            "SELECT * FROM tasks WHERE request_id = ?", (request_id,)
        ).fetchone()
        return None if row is None else _row_to_task(row)

    def list_recent(self, limit: int = 50) -> list[Task]:
        """Recent tasks, newest first (pure read; web console index/列表)."""
        rows = self._conn.execute(
            "SELECT * FROM tasks ORDER BY created_at DESC, task_id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [_row_to_task(row) for row in rows]

    def update_status(
        self,
        task_id: TaskId,
        new_status: TaskStatus,
        expected: TaskStatus | None = None,
    ) -> Task:
        """Transition a task's status under the domain transition table.

        Raises ConcurrencyError when ``expected`` does not match the stored
        status (stale writer) and TaskStateError for an illegal transition or
        a missing task.
        """
        with immediate(self._conn):
            row = self._conn.execute(
                "SELECT * FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
            if row is None:
                raise TaskStateError(f"task {task_id} not found")
            current = TaskStatus(row["status"])
            if expected is not None and current != expected:
                raise ConcurrencyError(
                    f"expected task {task_id} in status {expected.value}, "
                    f"found {current.value}"
                )
            if new_status not in TASK_STATUS_TRANSITIONS[current]:
                raise TaskStateError(
                    f"illegal task transition {current.value} -> {new_status.value}"
                )
            updated = self._conn.execute(
                "UPDATE tasks SET status = ?, updated_at = ? WHERE task_id = ?",
                (new_status.value, to_db_datetime(utc_now()), task_id),
            )
            if updated.rowcount != 1:
                raise ConcurrencyError(f"task {task_id} changed concurrently")
            fresh = self._conn.execute(
                "SELECT * FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
            if fresh is None:  # pragma: no cover - deleted mid-transaction
                raise ConcurrencyError(f"task {task_id} vanished mid-transaction")
            return _row_to_task(fresh)

    def bind_template(self, task_id: TaskId, template_version_id: TemplateVersionId) -> Task:
        """Bind the task to a template version, once and for its lifetime.

        Rebinding a different version raises TaskStateError (Master §6.3);
        binding the same version again is idempotent.
        """
        with immediate(self._conn):
            row = self._conn.execute(
                "SELECT template_version_id FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
            if row is None:
                raise TaskStateError(f"task {task_id} not found")
            current = row["template_version_id"]
            if current is not None and current != template_version_id:
                raise TaskStateError(
                    f"task {task_id} already bound to template version {current}"
                )
            if current is None:
                self._conn.execute(
                    "UPDATE tasks SET template_version_id = ?, updated_at = ?"
                    " WHERE task_id = ?",
                    (template_version_id, to_db_datetime(utc_now()), task_id),
                )
            fresh = self._conn.execute(
                "SELECT * FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
            assert fresh is not None
            return _row_to_task(fresh)

    def advance_epoch(self, task_id: TaskId, expected_epoch: int) -> int:
        """CAS bump of the task epoch; returns the new epoch.

        A stale or unknown expected_epoch (including a missing task) is a
        ConcurrencyError.
        """
        with immediate(self._conn):
            cursor = self._conn.execute(
                "UPDATE tasks SET epoch = epoch + 1, updated_at = ? "
                "WHERE task_id = ? AND epoch = ?",
                (to_db_datetime(utc_now()), task_id, expected_epoch),
            )
            if cursor.rowcount != 1:
                raise ConcurrencyError(
                    f"epoch CAS failed for task {task_id}: expected {expected_epoch}"
                )
            return expected_epoch + 1

    def set_active_refs(
        self,
        task_id: TaskId,
        *,
        plan: ArtifactId | _Unset | None = UNSET,
        binding: ArtifactId | _Unset | None = UNSET,
        render_ir: ArtifactId | _Unset | None = UNSET,
        candidate: ArtifactId | _Unset | None = UNSET,
    ) -> Task:
        """Update the task's active business refs.

        Only columns explicitly provided are written: an id sets it, an explicit
        ``None`` clears it, and an omitted argument (``UNSET``) leaves it alone.
        """
        columns: dict[str, ArtifactId | None] = {}
        if not isinstance(plan, _Unset):
            columns["active_plan_artifact_id"] = plan
        if not isinstance(binding, _Unset):
            columns["active_binding_artifact_id"] = binding
        if not isinstance(render_ir, _Unset):
            columns["active_render_ir_artifact_id"] = render_ir
        if not isinstance(candidate, _Unset):
            columns["active_candidate_artifact_id"] = candidate
        with immediate(self._conn):
            if columns:
                assignments = ", ".join(f"{name} = ?" for name in columns)
                cursor = self._conn.execute(
                    f"UPDATE tasks SET {assignments}, updated_at = ? WHERE task_id = ?",
                    [*columns.values(), to_db_datetime(utc_now()), task_id],
                )
                if cursor.rowcount != 1:
                    raise TaskStateError(f"task {task_id} not found")
            row = self._conn.execute(
                "SELECT * FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
            if row is None:
                raise TaskStateError(f"task {task_id} not found")
            return _row_to_task(row)


# ------------------------------------------------------- OperationRepository

_OPERATION_INSERT = (
    "INSERT INTO operations (operation_id, operation_key, state, task_id, node_name,"
    " epoch, payload_digest, created_at, committed_at, output_artifact_ids_json)"
    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)


class OperationRepository(_Repository):
    def ensure_intent(self, operation: Operation) -> Operation:
        """Insert an intent keyed by operation_key, idempotently.

        An existing row with the same payload_digest is returned as-is
        (whether intent or committed); a different digest is
        IDEMPOTENCY_CONFLICT.
        """
        params = (
            operation.operation_id,
            operation.operation_key,
            operation.state.value,
            operation.task_id,
            operation.node_name,
            operation.epoch,
            operation.payload_digest,
            to_db_datetime(operation.created_at),
            None if operation.committed_at is None else to_db_datetime(operation.committed_at),
            _dumps(list(operation.output_artifact_ids)),
        )
        with immediate(self._conn):
            row = self._conn.execute(
                "SELECT * FROM operations WHERE operation_key = ?",
                (operation.operation_key,),
            ).fetchone()
            if row is not None:
                stored = _row_to_operation(row)
                if stored.payload_digest != operation.payload_digest:
                    raise IdempotencyConflictError(
                        f"operation_key {operation.operation_key} already recorded "
                        "with a different payload digest"
                    )
                return stored
            try:
                self._conn.execute(_OPERATION_INSERT, params)
                return operation
            except sqlite3.IntegrityError as exc:
                raise LedgerConflictError(
                    f"operation insert rejected: {exc}"
                ) from exc

    def commit(
        self,
        operation_id: OperationId,
        output_artifact_ids: Sequence[str],
        committed_at: datetime,
    ) -> Operation:
        """intent -> committed, recording the output artifact refs.

        Committing an already committed operation with the same outputs is an
        idempotent replay of the stored receipt; different outputs are an
        IDEMPOTENCY_CONFLICT. Committing an abandoned operation is a
        LedgerConflictError.
        """
        outputs = tuple(output_artifact_ids)
        with immediate(self._conn):
            row = self._select(operation_id)
            if row is None:
                raise LedgerConflictError(f"operation {operation_id} not found")
            stored = _row_to_operation(row)
            if stored.state is OperationState.COMMITTED:
                if stored.output_artifact_ids != outputs:
                    raise IdempotencyConflictError(
                        f"operation {operation_id} already committed with "
                        "different output artifacts"
                    )
                return stored
            if stored.state is OperationState.ABANDONED:
                raise LedgerConflictError(
                    f"cannot commit abandoned operation {operation_id}"
                )
            self._conn.execute(
                "UPDATE operations SET state = 'committed', committed_at = ?,"
                " output_artifact_ids_json = ? WHERE operation_id = ?",
                (to_db_datetime(committed_at), _dumps(list(outputs)), operation_id),
            )
            fresh = self._select(operation_id)
            if fresh is None:  # pragma: no cover - row was just updated
                raise LedgerConflictError(f"operation {operation_id} vanished")
            return _row_to_operation(fresh)

    def abandon(self, operation_id: OperationId) -> Operation:
        """intent -> abandoned (idempotent); committed cannot be abandoned."""
        with immediate(self._conn):
            row = self._select(operation_id)
            if row is None:
                raise LedgerConflictError(f"operation {operation_id} not found")
            stored = _row_to_operation(row)
            if stored.state is OperationState.COMMITTED:
                raise LedgerConflictError(
                    f"cannot abandon committed operation {operation_id}"
                )
            if stored.state is OperationState.ABANDONED:
                return stored
            self._conn.execute(
                "UPDATE operations SET state = 'abandoned' WHERE operation_id = ?",
                (operation_id,),
            )
            fresh = self._select(operation_id)
            if fresh is None:  # pragma: no cover - row was just updated
                raise LedgerConflictError(f"operation {operation_id} vanished")
            return _row_to_operation(fresh)

    def get(self, operation_id: OperationId) -> Operation | None:
        row = self._select(operation_id)
        return None if row is None else _row_to_operation(row)

    def get_by_key(self, operation_key: str) -> Operation | None:
        row = self._conn.execute(
            "SELECT * FROM operations WHERE operation_key = ?", (operation_key,)
        ).fetchone()
        return None if row is None else _row_to_operation(row)

    def _select(self, operation_id: OperationId) -> sqlite3.Row | None:
        row: sqlite3.Row | None = self._conn.execute(
            "SELECT * FROM operations WHERE operation_id = ?", (operation_id,)
        ).fetchone()
        return row


# -------------------------------------------------------- ArtifactRepository

_ARTIFACT_INSERT = (
    "INSERT INTO artifacts (artifact_id, task_id, kind, relative_path, sha256, size,"
    " created_by_operation_id, lineage_json, content_type, created_at)"
    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)


class ArtifactRepository(_Repository):
    def register(self, artifact: Artifact) -> Artifact:
        """Insert an artifact record (the bytes live in the file pool).

        - same artifact_id + same content: idempotent replay of the stored row
        - same artifact_id + different content: IDEMPOTENCY_CONFLICT
        - different artifact_id + same relative_path: ARTIFACT_EXISTS
        - dangling task/operation reference: LedgerConflictError (FK)
        """
        params = (
            artifact.artifact_id,
            artifact.task_id,
            artifact.kind.value,
            artifact.relative_path,
            artifact.sha256,
            artifact.size,
            artifact.created_by_operation_id,
            _dumps(list(artifact.source_refs)),
            artifact.content_type,
            to_db_datetime(artifact.created_at),
        )
        with immediate(self._conn):
            try:
                self._conn.execute(_ARTIFACT_INSERT, params)
                return artifact
            except sqlite3.IntegrityError as exc:
                by_id = self._select(artifact.artifact_id)
                if by_id is not None:
                    stored = _row_to_artifact(by_id)
                    if _same_artifact(stored, artifact):
                        return stored
                    raise IdempotencyConflictError(
                        f"artifact {artifact.artifact_id} already registered with "
                        "different content"
                    ) from exc
                by_path = self._conn.execute(
                    "SELECT artifact_id FROM artifacts WHERE relative_path = ?",
                    (artifact.relative_path,),
                ).fetchone()
                if by_path is not None:
                    raise ArtifactExistsError(
                        f"relative_path {artifact.relative_path} already registered "
                        f"as {by_path['artifact_id']}"
                    ) from exc
                raise LedgerConflictError(
                    f"artifact insert rejected (foreign key or check): {exc}"
                ) from exc

    def get(self, artifact_id: ArtifactId) -> Artifact | None:
        row = self._select(artifact_id)
        return None if row is None else _row_to_artifact(row)

    def get_by_path(self, relative_path: str) -> Artifact | None:
        row = self._conn.execute(
            "SELECT * FROM artifacts WHERE relative_path = ?", (relative_path,)
        ).fetchone()
        return None if row is None else _row_to_artifact(row)

    def mark_corrupt(self, artifact_id: ArtifactId, at: datetime) -> datetime:
        """Set corrupt_at once; replays keep the first recorded time."""
        with immediate(self._conn):
            row = self._select(artifact_id)
            if row is None:
                raise ArtifactNotFoundError(f"artifact {artifact_id} not found")
            if row["corrupt_at"] is not None:
                return from_db_datetime(row["corrupt_at"])
            self._conn.execute(
                "UPDATE artifacts SET corrupt_at = ? WHERE artifact_id = ?"
                " AND corrupt_at IS NULL",
                (to_db_datetime(at), artifact_id),
            )
            return at

    def _select(self, artifact_id: ArtifactId) -> sqlite3.Row | None:
        row: sqlite3.Row | None = self._conn.execute(
            "SELECT * FROM artifacts WHERE artifact_id = ?", (artifact_id,)
        ).fetchone()
        return row


# ---------------------------------------------------------- EventRepository


class EventRecord(NamedTuple):
    """One appended ledger event (refs/counts only, never body text)."""

    seq: int
    event_code: str
    refs: dict[str, str]
    counts: dict[str, int]
    created_at: datetime


class EventRepository(_Repository):
    def append(
        self,
        task_id: TaskId,
        event_code: str,
        refs: dict[str, str],
        counts: dict[str, int],
    ) -> int:
        """Append one event; returns its monotonically increasing seq."""
        with immediate(self._conn):
            try:
                cursor = self._conn.execute(
                    "INSERT INTO events (task_id, event_code, refs_json, counts_json,"
                    " created_at) VALUES (?, ?, ?, ?, ?)",
                    (task_id, event_code, _dumps(refs), _dumps(counts),
                     to_db_datetime(utc_now())),
                )
            except sqlite3.IntegrityError as exc:
                raise LedgerConflictError(f"event insert rejected: {exc}") from exc
            seq = cursor.lastrowid
            if seq is None:  # pragma: no cover - AUTOINCREMENT always yields one
                raise LedgerConflictError("event seq not allocated")
            return seq

    def list_after(self, task_id: TaskId, after_seq: int) -> list[EventRecord]:
        """Events with seq > after_seq, in ascending seq order."""
        rows = self._conn.execute(
            "SELECT seq, event_code, refs_json, counts_json, created_at FROM events"
            " WHERE task_id = ? AND seq > ? ORDER BY seq",
            (task_id, after_seq),
        ).fetchall()
        return [
            EventRecord(
                seq=int(row["seq"]),
                event_code=str(row["event_code"]),
                refs=cast(dict[str, str], json.loads(row["refs_json"])),
                counts=cast(dict[str, int], json.loads(row["counts_json"])),
                created_at=from_db_datetime(row["created_at"]),
            )
            for row in rows
        ]


# -------------------------------------------------------- NodeRunRepository


class NodeRunRepository(_Repository):
    def create(self, node_run: NodeRun) -> NodeRun:
        """Insert one attempt row; identical re-insert replays idempotently."""
        params = (
            node_run.node_run_id,
            node_run.task_id,
            node_run.node_name,
            node_run.epoch,
            node_run.attempt,
            node_run.status.value,
            node_run.operation_id,
            node_run.error_code,
            None if node_run.started_at is None else to_db_datetime(node_run.started_at),
            None if node_run.finished_at is None
            else to_db_datetime(node_run.finished_at),
        )
        with immediate(self._conn):
            try:
                self._conn.execute(
                    "INSERT INTO node_runs (node_run_id, task_id, node_name, epoch,"
                    " attempt, status, operation_id, error_code, started_at,"
                    " finished_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    params,
                )
                return node_run
            except sqlite3.IntegrityError as exc:
                row = self._conn.execute(
                    "SELECT * FROM node_runs WHERE node_run_id = ?",
                    (node_run.node_run_id,),
                ).fetchone()
                if row is not None:
                    stored = _row_to_node_run(row)
                    if _same_node_run(stored, node_run):
                        return stored
                    raise LedgerConflictError(
                        f"node_run {node_run.node_run_id} already exists with "
                        "different content"
                    ) from exc
                raise LedgerConflictError(
                    f"node_run insert rejected: {exc}"
                ) from exc

    def mark_status(
        self,
        node_run_id: NodeRunId,
        status: NodeStatus,
        error_code: str | None = None,
        finished_at: datetime | None = None,
    ) -> NodeRun:
        """Overwrite status/error_code/finished_at (None clears the column)."""
        with immediate(self._conn):
            cursor = self._conn.execute(
                "UPDATE node_runs SET status = ?, error_code = ?, finished_at = ?"
                " WHERE node_run_id = ?",
                (status.value, error_code,
                 None if finished_at is None else to_db_datetime(finished_at),
                 node_run_id),
            )
            if cursor.rowcount != 1:
                raise LedgerConflictError(f"node_run {node_run_id} not found")
            row = self._conn.execute(
                "SELECT * FROM node_runs WHERE node_run_id = ?", (node_run_id,)
            ).fetchone()
            if row is None:  # pragma: no cover - row was just updated
                raise LedgerConflictError(f"node_run {node_run_id} vanished")
            return _row_to_node_run(row)

    def next_attempt(self, task_id: TaskId, node_name: str, epoch: int) -> int:
        """max(attempt) + 1 for the node in this epoch (1 when absent)."""
        row = self._conn.execute(
            "SELECT COALESCE(MAX(attempt), 0) AS max_attempt FROM node_runs"
            " WHERE task_id = ? AND node_name = ? AND epoch = ?",
            (task_id, node_name, epoch),
        ).fetchone()
        return int(row["max_attempt"]) + 1


# ------------------------------------------------ TemplateVersionRepository


@dataclass(frozen=True)
class TemplateVersionRow:
    """Row shape of template_versions (no domain DTO exists for it yet)."""

    template_version_id: str
    template_id: str
    version: int
    docx_sha256: str
    contract_sha256: str
    styles_sha256: str
    static_map_sha256: str
    extractor_version: str
    created_at: datetime


_TEMPLATE_VERSION_INSERT = (
    "INSERT INTO template_versions (template_version_id, template_id, version,"
    " docx_sha256, contract_sha256, styles_sha256, static_map_sha256,"
    " extractor_version, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
)


class TemplateVersionRepository(_Repository):
    def register(
        self,
        *,
        template_version_id: str,
        template_id: str,
        version: int,
        docx_sha256: str,
        contract_sha256: str,
        styles_sha256: str,
        static_map_sha256: str,
        extractor_version: str,
        created_at: datetime,
    ) -> TemplateVersionRow:
        """Register one template version.

        Re-registering the exact same row is idempotent; a different row for
        the same (template_id, version) or the same id is a LedgerConflictError
        (template re-extraction produces a new version, never an overwrite).
        """
        params = (
            template_version_id,
            template_id,
            version,
            docx_sha256,
            contract_sha256,
            styles_sha256,
            static_map_sha256,
            extractor_version,
            to_db_datetime(created_at),
        )
        with immediate(self._conn):
            normalized_at = to_db_datetime(created_at)
            try:
                self._conn.execute(_TEMPLATE_VERSION_INSERT, params)
                return TemplateVersionRow(
                    template_version_id=template_version_id,
                    template_id=template_id,
                    version=version,
                    docx_sha256=docx_sha256,
                    contract_sha256=contract_sha256,
                    styles_sha256=styles_sha256,
                    static_map_sha256=static_map_sha256,
                    extractor_version=extractor_version,
                    created_at=from_db_datetime(normalized_at),
                )
            except sqlite3.IntegrityError as exc:
                row = self._select(template_version_id)
                if row is not None:
                    stored = _row_to_template_version(row)
                    identical = (
                        stored.template_id == template_id
                        and stored.version == version
                        and stored.docx_sha256 == docx_sha256
                        and stored.contract_sha256 == contract_sha256
                        and stored.styles_sha256 == styles_sha256
                        and stored.static_map_sha256 == static_map_sha256
                        and stored.extractor_version == extractor_version
                        and to_db_datetime(stored.created_at) == normalized_at
                    )
                    if identical:
                        return stored
                    raise LedgerConflictError(
                        f"template_version {template_version_id} already registered "
                        "with different content"
                    ) from exc
                raise LedgerConflictError(
                    f"template version ({template_id}, {version}) already registered: "
                    f"{exc}"
                ) from exc

    def get(self, template_version_id: str) -> TemplateVersionRow | None:
        row = self._select(template_version_id)
        return None if row is None else _row_to_template_version(row)

    def latest_for(self, template_id: str) -> TemplateVersionRow | None:
        """ Highest registered version of *template_id*, or None (pure read).

        The idempotency probe for built-in seeding: the caller rebuilds the
        contract digests from its own assets and compares them against this
        row before touching the immutable pool (:meth:`TemplateRegistry.ensure_registered`).
        """
        row: sqlite3.Row | None = self._conn.execute(
            "SELECT * FROM template_versions WHERE template_id = ?"
            " ORDER BY version DESC LIMIT 1",
            (template_id,),
        ).fetchone()
        return None if row is None else _row_to_template_version(row)

    def list_recent(self, limit: int = 50) -> list[TemplateVersionRow]:
        """Recent template versions, newest first (pure read; web 下拉源)."""
        rows = self._conn.execute(
            "SELECT * FROM template_versions ORDER BY created_at DESC,"
            " template_version_id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [_row_to_template_version(row) for row in rows]

    def _select(self, template_version_id: str) -> sqlite3.Row | None:
        row: sqlite3.Row | None = self._conn.execute(
            "SELECT * FROM template_versions WHERE template_version_id = ?",
            (template_version_id,),
        ).fetchone()
        return row


def _row_to_template_version(row: sqlite3.Row) -> TemplateVersionRow:
    return TemplateVersionRow(
        template_version_id=str(row["template_version_id"]),
        template_id=str(row["template_id"]),
        version=int(row["version"]),
        docx_sha256=str(row["docx_sha256"]),
        contract_sha256=str(row["contract_sha256"]),
        styles_sha256=str(row["styles_sha256"]),
        static_map_sha256=str(row["static_map_sha256"]),
        extractor_version=str(row["extractor_version"]),
        created_at=from_db_datetime(row["created_at"]),
    )


# ---------------------------------------------------- AwaitingEventRepository


class AwaitingEventRepository(_Repository):
    def create(self, event: AwaitingEvent) -> AwaitingEvent:
        """Insert an awaiting event; identical re-insert replays idempotently."""
        params = (
            event.awaiting_event_id,
            event.task_id,
            event.epoch,
            event.kind.value,
            _dumps(list(event.missing_slot_ids)),
            to_db_datetime(event.created_at),
        )
        with immediate(self._conn):
            try:
                self._conn.execute(
                    "INSERT INTO awaiting_events (awaiting_event_id, task_id, epoch,"
                    " kind, missing_slot_ids_json, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    params,
                )
                return event
            except sqlite3.IntegrityError as exc:
                row = self._select(event.awaiting_event_id)
                if row is not None:
                    stored = _row_to_awaiting_event(row)
                    if _same_awaiting_event(stored, event):
                        return stored
                    raise LedgerConflictError(
                        f"awaiting event {event.awaiting_event_id} already exists "
                        "with different content"
                    ) from exc
                raise LedgerConflictError(
                    f"awaiting event insert rejected: {exc}"
                ) from exc

    def get(self, awaiting_event_id: AwaitingEventId) -> AwaitingEvent | None:
        row = self._select(awaiting_event_id)
        return None if row is None else _row_to_awaiting_event(row)

    def resolve(self, awaiting_event_id: AwaitingEventId, at: datetime) -> AwaitingEvent:
        """Set resolved_at once; resolving again replays the stored row."""
        with immediate(self._conn):
            row = self._select(awaiting_event_id)
            if row is None:
                raise LedgerConflictError(
                    f"awaiting event {awaiting_event_id} not found"
                )
            if row["resolved_at"] is not None:
                return _row_to_awaiting_event(row)
            self._conn.execute(
                "UPDATE awaiting_events SET resolved_at = ? WHERE awaiting_event_id = ?"
                " AND resolved_at IS NULL",
                (to_db_datetime(at), awaiting_event_id),
            )
            fresh = self._select(awaiting_event_id)
            if fresh is None:  # pragma: no cover - row was just updated
                raise LedgerConflictError(
                    f"awaiting event {awaiting_event_id} vanished"
                )
            return _row_to_awaiting_event(fresh)

    def _select(self, awaiting_event_id: AwaitingEventId) -> sqlite3.Row | None:
        row: sqlite3.Row | None = self._conn.execute(
            "SELECT * FROM awaiting_events WHERE awaiting_event_id = ?",
            (awaiting_event_id,),
        ).fetchone()
        return row


# -------------------------------------------------------- DecisionRepository


class DecisionRepository(_Repository):
    def accept(self, decision: Decision) -> Decision:
        """Insert a decision; idempotent on (decision_id, payload_digest).

        The referenced awaiting event must exist (enforced by FK as well).
        """
        params = (
            decision.decision_id,
            decision.task_id,
            decision.expected_epoch,
            decision.awaiting_event_id,
            decision.action.value,
            decision.payload_digest,
            _dumps(list(decision.attachment_artifact_ids)),
            to_db_datetime(decision.created_at),
        )
        with immediate(self._conn):
            try:
                self._conn.execute(
                    "INSERT INTO user_decisions (decision_id, task_id, expected_epoch,"
                    " awaiting_event_id, action, payload_digest, attachment_refs_json,"
                    " created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    params,
                )
                return decision
            except sqlite3.IntegrityError as exc:
                row = self._conn.execute(
                    "SELECT * FROM user_decisions WHERE decision_id = ?",
                    (decision.decision_id,),
                ).fetchone()
                if row is not None:
                    stored = _row_to_decision(row)
                    if stored.payload_digest == decision.payload_digest:
                        return stored
                    raise IdempotencyConflictError(
                        f"decision {decision.decision_id} already accepted with a "
                        "different payload digest"
                    ) from exc
                raise LedgerConflictError(
                    f"decision insert rejected (unknown awaiting event or task): {exc}"
                ) from exc

    def get(self, decision_id: DecisionId) -> Decision | None:
        row = self._conn.execute(
            "SELECT * FROM user_decisions WHERE decision_id = ?", (decision_id,)
        ).fetchone()
        return None if row is None else _row_to_decision(row)


# -------------------------------------------------------- DeliveryRepository


class DeliveryRepository(_Repository):
    def record(
        self,
        *,
        delivery_id: str,
        task_id: TaskId,
        operation_id: OperationId,
        candidate_artifact_id: ArtifactId,
        final_sha256: str,
        manifest_artifact_id: ArtifactId,
        created_at: datetime,
    ) -> None:
        """Record the single final delivery of a task (one per task, MVP)."""
        params = (
            delivery_id,
            task_id,
            operation_id,
            candidate_artifact_id,
            final_sha256,
            manifest_artifact_id,
            to_db_datetime(created_at),
        )
        with immediate(self._conn):
            try:
                self._conn.execute(
                    "INSERT INTO deliveries (delivery_id, task_id, operation_id,"
                    " candidate_artifact_id, final_sha256, manifest_artifact_id,"
                    " created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    params,
                )
            except sqlite3.IntegrityError as exc:
                existing = self._conn.execute(
                    "SELECT delivery_id FROM deliveries WHERE task_id = ?", (task_id,)
                ).fetchone()
                if existing is not None:
                    raise LedgerConflictError(
                        f"task {task_id} already delivered as {existing['delivery_id']}"
                    ) from exc
                raise LedgerConflictError(f"delivery insert rejected: {exc}") from exc

    def get_by_task(self, task_id: TaskId) -> sqlite3.Row | None:
        row: sqlite3.Row | None = self._conn.execute(
            "SELECT * FROM deliveries WHERE task_id = ?", (task_id,)
        ).fetchone()
        return row


# --------------------------------------------------- minimal repositories
# bindings / slot_overrides / gate_reports / llm_calls / resolved_spans:
# insert + get only; later phases own their full semantics.


class BindingRepository(_Repository):
    def insert(
        self,
        *,
        binding_id: str,
        task_id: TaskId,
        epoch: int,
        binding_version: int,
        slot_id: str,
        binding_status: str,
        producer: Producer | None,
        source_refs: Sequence[SourceSpanRef],
        created_at: datetime,
    ) -> None:
        """Insert one slot binding row (source_refs stored as ref JSON)."""
        with immediate(self._conn):
            _insert_or_conflict(
                self._conn,
                "INSERT INTO bindings (binding_id, task_id, epoch, binding_version,"
                " slot_id, binding_status, producer, source_refs_json, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    binding_id,
                    task_id,
                    epoch,
                    binding_version,
                    slot_id,
                    binding_status,
                    producer,
                    _dumps([ref.model_dump() for ref in source_refs]),
                    to_db_datetime(created_at),
                ),
                "binding",
            )

    def get(self, binding_id: str) -> sqlite3.Row | None:
        row: sqlite3.Row | None = self._conn.execute(
            "SELECT * FROM bindings WHERE binding_id = ?", (binding_id,)
        ).fetchone()
        return row


class SlotOverrideRepository(_Repository):
    def insert(self, override_id: str, override: SlotOverride) -> None:
        """Insert one user omission of a slot (action is always 'omit')."""
        with immediate(self._conn):
            _insert_or_conflict(
                self._conn,
                "INSERT INTO slot_overrides (override_id, task_id, slot_id,"
                " decision_id, epoch, action, reason_artifact_id, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    override_id,
                    override.task_id,
                    override.slot_id,
                    override.decision_id,
                    override.epoch,
                    override.action,
                    override.reason_artifact_id,
                    to_db_datetime(override.created_at),
                ),
                "slot_override",
            )

    def get(self, override_id: str) -> sqlite3.Row | None:
        row: sqlite3.Row | None = self._conn.execute(
            "SELECT * FROM slot_overrides WHERE override_id = ?", (override_id,)
        ).fetchone()
        return row

    def active_for_task(self, task_id: TaskId, epoch: int) -> dict[str, SlotOverride]:
        """Active user omissions for *task_id* at *epoch*, keyed by slot_id."""
        rows = self._conn.execute(
            "SELECT * FROM slot_overrides WHERE task_id = ? AND epoch = ?",
            (task_id, epoch),
        ).fetchall()
        return {row["slot_id"]: _row_to_slot_override(row) for row in rows}


class GateReportRepository(_Repository):
    def insert(
        self,
        *,
        gate_report_id: str,
        task_id: TaskId,
        gate: str,
        candidate_sha256: str | None,
        render_ir_sha256: str,
        template_version_id: str,
        rule_version: str,
        overall_status: str,
        summary_json: str,
        report_artifact_id: ArtifactId,
        created_at: datetime,
    ) -> None:
        """Insert one gate report summary row (evidence lives in artifacts)."""
        with immediate(self._conn):
            _insert_or_conflict(
                self._conn,
                "INSERT INTO gate_reports (gate_report_id, task_id, gate,"
                " candidate_sha256, render_ir_sha256, template_version_id,"
                " rule_version, overall_status, summary_json, report_artifact_id,"
                " created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    gate_report_id,
                    task_id,
                    gate,
                    candidate_sha256,
                    render_ir_sha256,
                    template_version_id,
                    rule_version,
                    overall_status,
                    summary_json,
                    report_artifact_id,
                    to_db_datetime(created_at),
                ),
                "gate_report",
            )

    def get(self, gate_report_id: str) -> sqlite3.Row | None:
        row: sqlite3.Row | None = self._conn.execute(
            "SELECT * FROM gate_reports WHERE gate_report_id = ?", (gate_report_id,)
        ).fetchone()
        return row


class LlmCallRepository(_Repository):
    def insert(
        self,
        *,
        call_id: str,
        provider: str,
        model_id: str,
        created_at: datetime,
        task_id: TaskId | None = None,
        node_name: str | None = None,
        model_revision: str | None = None,
        prompt_template_digest: str | None = None,
        system_prompt_digest: str | None = None,
        tool_schema_version: str | None = None,
        model_config_json: str | None = None,
        input_artifact_refs_json: str | None = None,
        raw_output_artifact_id: ArtifactId | None = None,
        parsed_output_artifact_id: ArtifactId | None = None,
        tokens_in: int | None = None,
        tokens_out: int | None = None,
        cost: float | None = None,
    ) -> None:
        """Insert one LLM call metadata row (outputs referenced, not stored)."""
        with immediate(self._conn):
            _insert_or_conflict(
                self._conn,
                "INSERT INTO llm_calls (call_id, task_id, node_name, provider,"
                " model_id, model_revision, prompt_template_digest,"
                " system_prompt_digest, tool_schema_version, model_config_json,"
                " input_artifact_refs_json, raw_output_artifact_id,"
                " parsed_output_artifact_id, tokens_in, tokens_out, cost, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    call_id,
                    task_id,
                    node_name,
                    provider,
                    model_id,
                    model_revision,
                    prompt_template_digest,
                    system_prompt_digest,
                    tool_schema_version,
                    model_config_json,
                    input_artifact_refs_json,
                    raw_output_artifact_id,
                    parsed_output_artifact_id,
                    tokens_in,
                    tokens_out,
                    cost,
                    to_db_datetime(created_at),
                ),
                "llm_call",
            )

    def get(self, call_id: str) -> sqlite3.Row | None:
        row: sqlite3.Row | None = self._conn.execute(
            "SELECT * FROM llm_calls WHERE call_id = ?", (call_id,)
        ).fetchone()
        return row


class ResolvedSpanRepository(_Repository):
    def insert(
        self,
        *,
        span_handle: SpanHandle,
        task_id: TaskId,
        run_id: NodeRunId | None,
        span: SourceSpanRef,
        created_at: datetime,
    ) -> None:
        """Insert one resolved span index row (handle -> verified SourceSpanRef)."""
        with immediate(self._conn):
            _insert_or_conflict(
                self._conn,
                "INSERT INTO resolved_spans (span_handle, task_id, run_id,"
                " artifact_id, canonical_sha256, start, end, span_sha256, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    span_handle,
                    task_id,
                    run_id,
                    span.artifact_id,
                    span.canonical_sha256,
                    span.start,
                    span.end,
                    span.span_sha256,
                    to_db_datetime(created_at),
                ),
                "resolved_span",
            )

    def get(self, span_handle: SpanHandle) -> sqlite3.Row | None:
        row: sqlite3.Row | None = self._conn.execute(
            "SELECT * FROM resolved_spans WHERE span_handle = ?", (span_handle,)
        ).fetchone()
        return row


# ------------------------------------------------------------ web console rows


class ConversationRepository(_Repository):
    """Chat conversations (ADR 0002 amendment): UI-level grouping only."""

    def create(
        self, *, conversation_id: str, title: str, template_version_id: str | None,
        data_policy: str, created_at: datetime,
    ) -> None:
        with immediate(self._conn):
            _insert_or_conflict(
                self._conn,
                "INSERT INTO conversations (conversation_id, title, template_version_id,"
                " data_policy, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                (conversation_id, title, template_version_id, data_policy,
                 to_db_datetime(created_at), to_db_datetime(created_at)),
                "conversation",
            )

    def list(self) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM conversations ORDER BY updated_at DESC"
        ).fetchall()

    def get(self, conversation_id: str) -> sqlite3.Row | None:
        row: sqlite3.Row | None = self._conn.execute(
            "SELECT * FROM conversations WHERE conversation_id = ?", (conversation_id,)
        ).fetchone()
        return row

    def touch(self, conversation_id: str, at: datetime) -> None:
        with immediate(self._conn):
            self._conn.execute(
                "UPDATE conversations SET updated_at = ? WHERE conversation_id = ?",
                (to_db_datetime(at), conversation_id),
            )


class ConversationMessageRepository(_Repository):
    def append(
        self, *, message_id: str, conversation_id: str, role: str, kind: str,
        content: str, task_id: str | None, created_at: datetime,
    ) -> None:
        with immediate(self._conn):
            _insert_or_conflict(
                self._conn,
                "INSERT INTO conversation_messages (message_id, conversation_id, role,"
                " kind, content, task_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (message_id, conversation_id, role, kind, content, task_id,
                 to_db_datetime(created_at)),
                "conversation_message",
            )

    def list(self, conversation_id: str) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM conversation_messages WHERE conversation_id = ?"
            " ORDER BY created_at, message_id",
            (conversation_id,),
        ).fetchall()


class ResearchSourceRepository(_Repository):
    """Web provenance for research-fetched materials (ADR D27).

    One row per (task, source_url): the research node may re-run on resume,
    and duplicate registration for the same URL returns the existing artifact
    instead of refetching (content-addressed idempotency, I9 spirit for
    program operations).
    """

    def register(
        self,
        *,
        source_id: str,
        task_id: str,
        artifact_id: str,
        source_url: str,
        query: str,
        http_status: int,
        fetched_at: datetime,
    ) -> Row | None:
        with immediate(self._conn):
            existing = self._conn.execute(
                "SELECT * FROM research_sources WHERE task_id = ? AND source_url = ?",
                (task_id, source_url),
            ).fetchone()
            if existing is not None:
                return cast(Row, existing)
            self._conn.execute(
                "INSERT INTO research_sources"
                " (source_id, task_id, artifact_id, source_url, query, http_status,"
                " fetched_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    source_id,
                    task_id,
                    artifact_id,
                    source_url,
                    query,
                    http_status,
                    to_db_datetime(fetched_at),
                ),
            )
        row = self._conn.execute(
            "SELECT * FROM research_sources WHERE task_id = ? AND source_url = ?",
            (task_id, source_url),
        ).fetchone()
        return cast(Row | None, row)

    def for_task(self, task_id: str) -> list[Row]:
        with reading(self._conn):
            return self._conn.execute(
                "SELECT * FROM research_sources WHERE task_id = ?"
                " ORDER BY fetched_at, source_id",
                (task_id,),
            ).fetchall()


class TemplateMetaRepository(_Repository):
    """Display-level metadata for the template library (contracts stay immutable)."""

    def upsert(
        self, *, template_id: str, display_name: str, description: str, at: datetime
    ) -> None:
        with immediate(self._conn):
            self._conn.execute(
                "INSERT INTO template_meta (template_id, display_name, description,"
                " updated_at) VALUES (?, ?, ?, ?)"
                " ON CONFLICT(template_id) DO UPDATE SET display_name = excluded.display_name,"
                " description = excluded.description, updated_at = excluded.updated_at",
                (template_id, display_name, description, to_db_datetime(at)),
            )

    def get(self, template_id: str) -> sqlite3.Row | None:
        row: sqlite3.Row | None = self._conn.execute(
            "SELECT * FROM template_meta WHERE template_id = ?", (template_id,)
        ).fetchone()
        return row

    def list(self) -> list[sqlite3.Row]:
        return self._conn.execute("SELECT * FROM template_meta").fetchall()
