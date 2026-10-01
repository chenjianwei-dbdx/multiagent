"""Application Service (v1.1 §5): the only door CLI/Web get to open.

Semantic contract (implemented, not aspirational):

- submit / respond / cancel / recover / export are idempotent by key;
  same key + different payload digest is IDEMPOTENCY_CONFLICT
- status / events are pure reads
- material bytes flow straight into the artifact pool; the ledger sees refs
- execution is delegated to an injected executor (deterministic driver now,
  LangGraph in P4) — this class never invents plans, bindings or text
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from omas.artifacts.store import ArtifactStore
from omas.artifacts.writers import InboxWriter
from omas.core.canonical import sha256_bytes
from omas.core.digest import payload_digest
from omas.domain.artifact import Artifact, ArtifactKind
from omas.domain.decisions import AwaitingEvent, Decision
from omas.domain.delivery import Delivery
from omas.domain.errors import (
    ArtifactNotFoundError,
    ConcurrencyError,
    IdempotencyConflictError,
    OmasError,
    TaskStateError,
)
from omas.domain.ids import (
    ArtifactId,
    AwaitingEventId,
    DecisionId,
    OperationId,
    TaskId,
    TemplateVersionId,
    new_awaiting_event_id,
    new_operation_id,
)
from omas.domain.operations import Operation
from omas.domain.task import Task, TaskStatus
from omas.storage.db import Ledger

from .commands import (
    AwaitingInfo,
    CancelTask,
    DecisionReceipt,
    EventItem,
    EventPage,
    ExecutionOutcome,
    ExportReceipt,
    ExportTask,
    RecoverTask,
    RespondTask,
    SubmitTask,
    TaskReceipt,
    TaskView,
)


class TaskExecutor(Protocol):
    """Executes one task run; P4 wraps the LangGraph graph in this shape."""

    def execute(self, task: Task) -> ExecutionOutcome:
        ...  # pragma: no cover - protocol shape


class TaskService:
    def __init__(self, home: Path, store: ArtifactStore, ledger: Ledger) -> None:
        self._home = home
        self._store = store
        self._ledger = ledger

    # ------------------------------------------------------------------ submit

    def submit(self, command: SubmitTask) -> TaskReceipt:
        digest = payload_digest(
            command.request_id,
            command.template_version_id,
            command.intent,
            command.data_policy.value,
            *(f"{m.filename}:{sha256_bytes(m.content)}" for m in command.materials),
        )
        existing = self._ledger.tasks.get_by_request_id(command.request_id)
        if existing is not None:
            row = self._ledger.connection.execute(
                "SELECT request_payload_digest FROM tasks WHERE request_id = ?",
                (command.request_id,),
            ).fetchone()
            if row is not None and row["request_payload_digest"] == digest:
                return TaskReceipt(
                    task_id=existing.task_id,
                    request_id=command.request_id,
                    replayed=True,
                    created_at=existing.created_at,
                )
            raise IdempotencyConflictError(
                f"request_id {command.request_id} already used with a different payload"
            )
        if self._ledger.template_versions.get(command.template_version_id) is None:
            raise ArtifactNotFoundError(
                f"template version {command.template_version_id} is not registered;"
                " run `omas template extract` first"
            )
        now = datetime.now(UTC)
        task = Task(
            task_id=self._new_task_id(),
            request_id=command.request_id,
            data_policy=command.data_policy,
            created_at=now,
            updated_at=now,
        )
        created = self._ledger.tasks.create(task, digest)
        bound = self._ledger.tasks.bind_template(created.task_id, command.template_version_id)
        intent_artifact = self._ingest_text(
            bound.task_id, "intent.txt", command.intent.encode("utf-8"),
            kind=ArtifactKind.INTENT,
        )
        for material in command.materials:
            self._ingest_text(bound.task_id, material.filename, material.content)
        self._ledger.events.append(
            bound.task_id, "task_submitted",
            refs={"intent": intent_artifact.artifact_id},
            counts={"materials": len(command.materials)},
        )
        return TaskReceipt(
            task_id=bound.task_id,
            request_id=command.request_id,
            replayed=False,
            created_at=bound.created_at,
        )

    # ----------------------------------------------------------------- respond

    def respond(self, command: RespondTask) -> DecisionReceipt:
        task = self._ledger.tasks.get(command.task_id)
        if task is None:
            raise ArtifactNotFoundError(f"task {command.task_id} not found")
        if command.action not in ("provide_material", "omit_slot"):
            raise OmasError(f"unknown respond action: {command.action}")
        digest = payload_digest(
            command.decision_id, command.task_id, str(command.expected_epoch),
            command.awaiting_event_id, command.action, command.slot_id or "",
            *(f"{m.filename}:{sha256_bytes(m.content)}" for m in command.materials),
        )
        existing = self._ledger.decisions.get(command.decision_id)
        if existing is not None:
            if existing.payload_digest != digest:
                raise IdempotencyConflictError(
                    f"decision_id {command.decision_id} reused with a different payload"
                )
            return DecisionReceipt(
                decision_id=DecisionId(existing.decision_id),
                task_id=task.task_id,
                epoch_after=task.epoch,
                replayed=True,
                accepted_at=existing.created_at,
            )

        event = self._ledger.awaiting_events.get(command.awaiting_event_id)
        if event is None or event.task_id != task.task_id:
            raise ConcurrencyError("awaiting event does not belong to this task")
        if event.is_resolved():
            raise ConcurrencyError("awaiting event already resolved")
        if event.epoch != command.expected_epoch or task.epoch != command.expected_epoch:
            raise ConcurrencyError(
                f"stale decision: expected epoch {command.expected_epoch}, task at {task.epoch}"
            )
        if task.status is not TaskStatus.AWAITING_USER:
            raise TaskStateError(f"task is {task.status.value}, not awaiting_user")

        now = datetime.now(UTC)
        decision = Decision(
            decision_id=command.decision_id,
            task_id=task.task_id,
            expected_epoch=command.expected_epoch,
            awaiting_event_id=command.awaiting_event_id,
            action=command.action,  # type: ignore[arg-type]
            attachment_artifact_ids=(),
            payload_digest=digest,
            created_at=now,
        )
        self._ledger.decisions.accept(decision)

        epoch_after = task.epoch
        if command.action == "provide_material":
            if not command.materials:
                raise OmasError("provide_material requires at least one material")
            for material in command.materials:
                self._ingest_text(task.task_id, material.filename, material.content)
            epoch_after = self._ledger.tasks.advance_epoch(task.task_id, task.epoch)
        else:
            if command.slot_id is None:
                raise OmasError("omit_slot requires slot_id")
            if command.slot_id not in event.missing_slot_ids:
                raise TaskStateError(
                    f"slot {command.slot_id} is not among the event's missing slots"
                )
            # D5/ADR: only slots the template declares allow_user_omit=true may
            # be left empty by an explicit user decision — never any missing slot.
            if task.template_version_id is not None:
                from omas.templates.registry import TemplateRegistry

                contract = TemplateRegistry(self._store, self._ledger).get_contract(
                    TemplateVersionId(task.template_version_id)
                )
                spec = contract.slot(command.slot_id)
                if spec is None or not spec.allow_user_omit:
                    raise TaskStateError(
                        f"slot {command.slot_id} does not allow user omission"
                    )
            import json as _json

            from omas.artifacts.writers import NodeArtifactWriter
            from omas.domain.decisions import SlotOverride
            from omas.pipeline.recorders import LedgerRecorder

            # The omission must be backed by a program-verified ref: the render
            # IR carries override_artifact_id for every user-omitted slot, so
            # persist the decision record and link it (v1.1 §8, RenderSlot).
            record = _json.dumps(
                {
                    "decision_id": command.decision_id,
                    "slot_id": command.slot_id,
                    "epoch": command.expected_epoch,
                    "action": "omit_slot",
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            writer = NodeArtifactWriter(
                self._store, task.task_id, "decision", recorder=LedgerRecorder(self._ledger)
            )
            reason = writer.commit_out(
                writer.stage("omit.json", record),
                kind=ArtifactKind.RAW_TEXT,
                final_name=f"{sha256_bytes(record)}.json",
                content_type="application/json",
            )
            self._ledger.slot_overrides.insert(
                f"ovr_{command.decision_id.removeprefix('dec_')}",
                SlotOverride(
                    task_id=task.task_id,
                    slot_id=command.slot_id,
                    decision_id=command.decision_id,
                    epoch=task.epoch,
                    reason_artifact_id=reason.artifact_id,
                ),
            )
        self._ledger.awaiting_events.resolve(command.awaiting_event_id, now)
        self._ledger.tasks.update_status(task.task_id, TaskStatus.RUNNING)
        self._ledger.events.append(
            task.task_id, f"decision_{command.action}",
            refs={"decision": command.decision_id}, counts={"epoch": epoch_after},
        )
        return DecisionReceipt(
            decision_id=command.decision_id,
            task_id=task.task_id,
            epoch_after=epoch_after,
            replayed=False,
            accepted_at=now,
        )

    # ------------------------------------------------------------------ cancel

    def cancel(self, command: CancelTask) -> TaskView:
        task = self._require_task(command.task_id)
        self._keyed_operation(
            key=f"cancel:{command.task_id}:{command.request_id}",
            digest=payload_digest(command.task_id, command.request_id),
            task_id=task.task_id,
            node="cancel",
        )
        if task.status not in (TaskStatus.COMPLETED, TaskStatus.CANCELLED, TaskStatus.FAILED):
            self._ledger.tasks.update_status(task.task_id, TaskStatus.CANCELLED)
        self._ledger.events.append(task.task_id, "task_cancelled", refs={}, counts={})
        return self.status(task.task_id)

    # ----------------------------------------------------------------- recover

    def recover(self, command: RecoverTask) -> TaskView:
        """Best-effort state repair; full graph reconcile arrives in P4."""
        task = self._require_task(command.task_id)
        self._keyed_operation(
            key=f"recover:{command.task_id}:{command.request_id}",
            digest=payload_digest(command.task_id, command.request_id),
            task_id=task.task_id,
            node="recover",
        )
        delivery = self._delivery_of(task.task_id)
        if delivery is not None and task.status is TaskStatus.RUNNING:
            self._ledger.tasks.update_status(task.task_id, TaskStatus.COMPLETED)
            task = self._require_task(task.task_id)
        self._ledger.events.append(task.task_id, "task_recovered", refs={}, counts={})
        return self.status(task.task_id)

    # -------------------------------------------------------------- run/status

    def run(self, task_id: TaskId, executor: TaskExecutor) -> TaskView:
        """Drive one execution round through the injected executor."""
        task = self._require_task(task_id)
        if task.status is TaskStatus.COMPLETED:
            return self.status(task_id)
        if task.status is TaskStatus.CANCELLED:
            raise TaskStateError("cannot run a cancelled task")
        if task.status is TaskStatus.CREATED:
            task = self._ledger.tasks.update_status(
                task_id, TaskStatus.RUNNING, expected=TaskStatus.CREATED
            )
        outcome = executor.execute(task)
        if outcome.status.value == "completed":
            if self._delivery_of(task_id) is None:
                raise OmasError("executor reported completed without a delivery record")
            fresh = self._require_task(task_id)
            if fresh.status is TaskStatus.RUNNING:
                self._ledger.tasks.update_status(task_id, TaskStatus.COMPLETED)
        elif outcome.status.value == "awaiting_user":
            now = datetime.now(UTC)
            # The graph's gap_check already commits one AwaitingEvent per epoch
            # (upstream, idempotent); reuse it instead of duplicating, so the
            # event the caller resolves is the same one gap_check will find
            # resolved on resume — otherwise omit_slot loops forever (v1.1 §8).
            existing = self._active_awaiting(task_id)
            if existing is not None and existing.epoch == task.epoch:
                event_id = existing.awaiting_event_id
                missing = existing.missing_slot_ids
                # gap_check already audited this event; appending again would
                # duplicate the awaiting_user line in the event stream
            else:
                event_id = new_awaiting_event_id()
                missing = outcome.missing_slot_ids or ("unknown",)
                self._ledger.awaiting_events.create(
                    AwaitingEvent(
                        awaiting_event_id=event_id,
                        task_id=task_id,
                        epoch=task.epoch,
                        missing_slot_ids=missing,
                        created_at=now,
                    )
                )
                self._ledger.events.append(
                    task_id, "awaiting_user",
                    refs={"awaiting_event": event_id},
                    counts={"missing": len(missing)},
                )
            fresh = self._require_task(task_id)
            if fresh.status is TaskStatus.RUNNING:
                self._ledger.tasks.update_status(task_id, TaskStatus.AWAITING_USER)
        else:
            fresh = self._require_task(task_id)
            if fresh.status is TaskStatus.RUNNING:
                self._ledger.tasks.update_status(task_id, TaskStatus.FAILED)
            self._ledger.events.append(
                task_id, "execution_failed",
                refs={}, counts={"code": 1},
            )
        return self.status(task_id)

    def omissible_missing_slots(self, view: TaskView) -> frozenset[str]:
        """缺槽中允许用户省略的槽位（模板契约 ``allow_user_omit``，不臆测）。

        CLI 与 Web 共用的单一来源（ADR 0002）：界面提示与服务器侧校验用同一
        份规则；``respond`` 的 omit_slot 分支仍会独立再验一次。
        """
        if view.awaiting is None or view.task.template_version_id is None:
            return frozenset()
        from omas.templates.registry import TemplateRegistry

        registry = TemplateRegistry(self._store, self._ledger)
        try:
            contract = registry.get_contract(
                TemplateVersionId(view.task.template_version_id)
            )
        except OmasError:
            return frozenset()
        missing = set(view.awaiting.missing_slot_ids)
        return frozenset(
            spec.slot_id
            for spec in contract.slots
            if spec.allow_user_omit and spec.slot_id in missing
        )

    def status(self, task_id: TaskId) -> TaskView:
        task = self._require_task(task_id)
        awaiting = self._active_awaiting(task_id)
        return TaskView(
            task=task,
            awaiting=awaiting,
            delivery=self._delivery_of(task_id),
            material_count=self._material_count(task_id),
        )

    def events(self, task_id: TaskId, after: int = 0) -> EventPage:
        self._require_task(task_id)
        rows = self._ledger.events.list_after(task_id, after)
        items = [
            EventItem(
                seq=row[0],
                event_code=row[1],
                refs=row[2],
                counts=row[3],
                created_at=row[4],
            )
            for row in rows
        ]
        return EventPage(items=tuple(items), last_seq=items[-1].seq if items else after)

    # ------------------------------------------------------------------ export

    def export(self, command: ExportTask) -> ExportReceipt:
        task = self._require_task(command.task_id)
        operation = self._keyed_operation(
            key=f"export:{command.task_id}:{command.request_id}",
            digest=payload_digest(command.task_id, command.request_id, command.output_path),
            task_id=task.task_id,
            node="export",
        )
        delivery = self._delivery_of(task.task_id)
        if delivery is None:
            raise OmasError("no committed delivery to export")
        docx_path = self._store.delivery_relative(task.task_id, f"{task.task_id}.docx")
        self._store.verify(docx_path, delivery.final_sha256)
        target = Path(command.output_path).expanduser().resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        replayed = False
        if target.exists():
            if target.read_bytes() == self._store.read(docx_path):
                replayed = True  # same bytes: idempotent re-export
            else:
                raise OmasError(
                    f"refusing to overwrite existing different content at {target}"
                )
        else:
            import shutil

            shutil.copyfile(self._store.resolve_path(docx_path), target)
        if operation.state.value != "committed":
            self._ledger.operations.commit(
                OperationId(operation.operation_id), (), datetime.now(UTC)
            )
        self._ledger.events.append(
            task.task_id, "exported",
            refs={"delivery": delivery.delivery_id}, counts={},
        )
        return ExportReceipt(
            task_id=task.task_id,
            delivery_id=delivery.delivery_id,
            exported_to=str(target),
            sha256=delivery.final_sha256,
            replayed=replayed,
        )

    # ----------------------------------------------------------------- helpers

    def _require_task(self, task_id: TaskId) -> Task:
        task = self._ledger.tasks.get(task_id)
        if task is None:
            raise ArtifactNotFoundError(f"task {task_id} not found")
        return task

    def _new_task_id(self) -> TaskId:
        from omas.domain.ids import new_task_id

        return new_task_id()

    def _ingest_text(
        self,
        task_id: TaskId,
        filename: str,
        content: bytes,
        *,
        kind: ArtifactKind = ArtifactKind.CANONICAL_TEXT,
    ) -> Artifact:
        from omas.domain.ids import new_artifact_id

        _raw, cfile, canonical = InboxWriter(self._store).ingest_text(task_id, filename, content)
        artifact = Artifact(
            artifact_id=new_artifact_id(),
            task_id=task_id,
            kind=kind,
            relative_path=cfile.relative_path,
            sha256=canonical.sha256,
            size=canonical.size_bytes,
            source_refs=(),
            created_at=datetime.now(UTC),
        )
        return self._ledger.artifacts.register(artifact)

    def _keyed_operation(self, *, key: str, digest: str, task_id: TaskId, node: str) -> Operation:
        return self._ledger.operations.ensure_intent(
            Operation(
                operation_id=new_operation_id(),
                operation_key=key,
                task_id=task_id,
                node_name=node,
                payload_digest=digest,
            )
        )

    def _active_awaiting(self, task_id: TaskId) -> AwaitingInfo | None:
        rows = self._ledger.connection.execute(
            "SELECT * FROM awaiting_events WHERE task_id = ? AND resolved_at IS NULL"
            " ORDER BY created_at DESC LIMIT 1",
            (task_id,),
        ).fetchall()
        if not rows:
            return None
        row = rows[0]
        import json as _json

        return AwaitingInfo(
            awaiting_event_id=AwaitingEventId(row["awaiting_event_id"]),
            epoch=int(row["epoch"]),
            missing_slot_ids=tuple(_json.loads(row["missing_slot_ids_json"])),
        )

    def _delivery_of(self, task_id: TaskId) -> Delivery | None:
        row = self._ledger.deliveries.get_by_task(task_id)
        if row is None:
            return None
        from omas.storage.db import from_db_datetime

        return Delivery(
            delivery_id=row["delivery_id"],
            task_id=TaskId(row["task_id"]),
            operation_id=OperationId(row["operation_id"]),
            candidate_artifact_id=ArtifactId(row["candidate_artifact_id"]),
            final_sha256=row["final_sha256"],
            manifest_artifact_id=ArtifactId(row["manifest_artifact_id"]),
            created_at=from_db_datetime(row["created_at"]),
        )

    def _material_count(self, task_id: TaskId) -> int:
        row = self._ledger.connection.execute(
            "SELECT COUNT(*) AS n FROM artifacts WHERE task_id = ? AND kind = 'canonical_text'",
            (task_id,),
        ).fetchone()
        return int(row["n"])
