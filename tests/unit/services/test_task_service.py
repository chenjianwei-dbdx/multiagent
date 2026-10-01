"""Application Service behaviour: idempotency, epochs, awaiting flow, export."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest

from omas.artifacts.store import ArtifactStore
from omas.domain.errors import (
    ConcurrencyError,
    IdempotencyConflictError,
    OmasError,
    TaskStateError,
)
from omas.domain.ids import TaskId, new_awaiting_event_id, new_decision_id
from omas.domain.task import TaskStatus
from omas.pipeline.recorders import LedgerRecorder
from omas.services import (
    CancelTask,
    ExecutionOutcome,
    ExecutionStatus,
    ExportTask,
    MaterialInput,
    RespondTask,
    SubmitTask,
    TaskService,
)
from omas.storage.db import Ledger
from tests.unit.conftest import register_template


@dataclass
class SvcEnv:
    home: Path
    ledger: Ledger
    store: ArtifactStore
    service: TaskService


@pytest.fixture()
def svc(tmp_path: Path) -> SvcEnv:
    home = tmp_path / "home"
    home.mkdir()
    ledger = Ledger.open(home / "ledger.sqlite3")
    store = ArtifactStore(home)
    _ = LedgerRecorder(ledger)
    yield SvcEnv(home=home, ledger=ledger, store=store, service=TaskService(home, store, ledger))
    ledger.close()


_LAST_VERSION: dict[str, str] = {}


def _submit(svc: SvcEnv, request_id: str = "req-1", materials: int = 1) -> object:
    version_id = register_template(svc)
    _LAST_VERSION["v"] = version_id
    return svc.service.submit(
        SubmitTask(
            request_id=request_id,
            template_version_id=version_id,
            intent="生成本周项目周报",
            materials=tuple(
                MaterialInput(filename=f"m{i}.md", content=f"材料 {i} 内容".encode("utf-8"))
                for i in range(materials)
            ),
        )
    )


@dataclass
class FakeExecutor:
    status: ExecutionStatus
    missing: tuple[str, ...] = ()

    def execute(self, task) -> ExecutionOutcome:
        return ExecutionOutcome(
            status=self.status, missing_slot_ids=self.missing, error_code=None
        )


def _awaiting_event_id(ledger: Ledger, task_id: str) -> str:
    row = ledger.connection.execute(
        "SELECT awaiting_event_id FROM awaiting_events WHERE task_id = ?"
        " AND resolved_at IS NULL ORDER BY created_at DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    assert row is not None
    return row["awaiting_event_id"]


def test_submit_idempotent_and_conflicting(svc: SvcEnv) -> None:
    first = _submit(svc)
    replay = _submit(svc)
    assert replay.replayed is True and replay.task_id == first.task_id
    version_id = _LAST_VERSION["v"]
    with pytest.raises(IdempotencyConflictError):
        svc.service.submit(
            SubmitTask(
                request_id="req-1",
                template_version_id=version_id,
                intent="不同的意图",
                materials=(MaterialInput(filename="m.md", content=b"x"),),
            )
        )
    view = svc.service.status(TaskId(first.task_id))
    assert view.task.status is TaskStatus.CREATED
    assert view.material_count == 1


def test_run_awaiting_then_provide_material(svc: SvcEnv) -> None:
    receipt = _submit(svc)
    task_id = TaskId(receipt.task_id)
    view = svc.service.run(task_id, FakeExecutor(ExecutionStatus.AWAITING_USER, ("risks",)))
    assert view.task.status is TaskStatus.AWAITING_USER
    assert view.awaiting is not None and "risks" in view.awaiting.missing_slot_ids

    event_id = _awaiting_event_id(svc.ledger, receipt.task_id)
    decision_id = new_decision_id()
    answer = svc.service.respond(
        RespondTask(
            task_id=task_id,
            decision_id=decision_id,
            expected_epoch=1,
            awaiting_event_id=event_id,
            action="provide_material",
            materials=(MaterialInput(filename="more.md", content="补充材料".encode("utf-8")),),
        )
    )
    assert answer.epoch_after == 2 and answer.replayed is False
    view = svc.service.status(task_id)
    assert view.task.status is TaskStatus.RUNNING and view.material_count == 2

    replay = svc.service.respond(
        RespondTask(
            task_id=task_id,
            decision_id=decision_id,
            expected_epoch=1,
            awaiting_event_id=event_id,
            action="provide_material",
            materials=(MaterialInput(filename="more.md", content="补充材料".encode("utf-8")),),
        )
    )
    assert replay.replayed is True

    done = svc.service.run(task_id, _CompletingExecutor(svc))
    assert done.task.status is TaskStatus.COMPLETED


@dataclass
class _CompletingExecutor:
    svc: SvcEnv

    def execute(self, task) -> ExecutionOutcome:
        # fabricate a committed delivery so "completed" is honest
        from omas.domain.ids import new_artifact_id, new_operation_id

        op = self.svc.ledger.operations.ensure_intent(
            __import__("omas").domain.operations.Operation(
                operation_id=new_operation_id(),
                operation_key=f"fake:{task.task_id}",
                task_id=TaskId(task.task_id),
                payload_digest="f" * 64,
            )
        )
        artifact = self.svc.ledger.artifacts.register(
            __import__("omas").domain.artifact.Artifact(
                artifact_id=new_artifact_id(),
                task_id=TaskId(task.task_id),
                kind="raw_text",
                relative_path=f"tasks/{task.task_id}/inbox/x.txt",
                sha256="a" * 64,
                size=1,
                created_at=datetime.now(UTC),
            )
        )
        payload = b"fake delivery docx bytes"
        docx_sha = __import__("hashlib").sha256(payload).hexdigest()
        self.svc.store.write_immutable(
            self.svc.store.delivery_relative(task.task_id, f"{task.task_id}.docx"), payload
        )
        self.svc.store.write_immutable(
            self.svc.store.delivery_relative(task.task_id, "manifest.json"), b"{}"
        )
        self.svc.ledger.deliveries.record(
            delivery_id=f"dlv_{op.operation_id.removeprefix('op_')}",
            task_id=TaskId(task.task_id),
            operation_id=op.operation_id,
            candidate_artifact_id=artifact.artifact_id,
            final_sha256=docx_sha,
            manifest_artifact_id=artifact.artifact_id,
            created_at=datetime.now(UTC),
        )
        return ExecutionOutcome(status=ExecutionStatus.COMPLETED)


def test_respond_stale_epoch_rejected(svc: SvcEnv) -> None:
    receipt = _submit(svc)
    task_id = TaskId(receipt.task_id)
    svc.service.run(task_id, FakeExecutor(ExecutionStatus.AWAITING_USER, ("risks",)))
    event_id = _awaiting_event_id(svc.ledger, receipt.task_id)
    with pytest.raises(ConcurrencyError):
        svc.service.respond(
            RespondTask(
                task_id=task_id,
                decision_id=new_decision_id(),
                expected_epoch=99,
                awaiting_event_id=event_id,
                action="provide_material",
                materials=(MaterialInput(filename="m.md", content=b"x"),),
            )
        )


def test_omit_slot_requires_listed_slot(svc: SvcEnv) -> None:
    receipt = _submit(svc)
    task_id = TaskId(receipt.task_id)
    svc.service.run(task_id, FakeExecutor(ExecutionStatus.AWAITING_USER, ("risks",)))
    event_id = _awaiting_event_id(svc.ledger, receipt.task_id)
    with pytest.raises(TaskStateError):
        svc.service.respond(
            RespondTask(
                task_id=task_id,
                decision_id=new_decision_id(),
                expected_epoch=1,
                awaiting_event_id=event_id,
                action="omit_slot",
                slot_id="not_missing",
            )
        )


def test_run_completed_without_delivery_is_rejected(svc: SvcEnv) -> None:
    receipt = _submit(svc)
    with pytest.raises(OmasError, match="without a delivery"):
        svc.service.run(TaskId(receipt.task_id), FakeExecutor(ExecutionStatus.COMPLETED))


def test_cancel_then_finalize_refused(svc: SvcEnv) -> None:
    receipt = _submit(svc)
    task_id = TaskId(receipt.task_id)
    view = svc.service.cancel(CancelTask(task_id=task_id, request_id="cancel-1"))
    assert view.task.status is TaskStatus.CANCELLED
    again = svc.service.cancel(CancelTask(task_id=task_id, request_id="cancel-1"))
    assert again.task.status is TaskStatus.CANCELLED
    with pytest.raises(OmasError):
        svc.service.run(task_id, FakeExecutor(ExecutionStatus.COMPLETED))


def test_events_paging(svc: SvcEnv) -> None:
    receipt = _submit(svc)
    page = svc.service.events(TaskId(receipt.task_id))
    codes = [item.event_code for item in page.items]
    assert "task_submitted" in codes
    later = svc.service.events(TaskId(receipt.task_id), after=page.last_seq)
    assert later.items == ()


def test_export_requires_delivery_and_is_idempotent(svc: SvcEnv) -> None:
    receipt = _submit(svc)
    task_id = TaskId(receipt.task_id)
    with pytest.raises(OmasError, match="no committed delivery"):
        svc.service.export(
            ExportTask(task_id=task_id, request_id="exp-0", output_path="/tmp/x.docx")
        )
    _ = new_awaiting_event_id()
    view = svc.service.run(task_id, _CompletingExecutor(svc))
    assert view.delivery is not None
    target = svc.home / "out" / "report.docx"
    result = svc.service.export(
        ExportTask(task_id=task_id, request_id="exp-1", output_path=str(target))
    )
    assert target.exists() and result.replayed is False
    again = svc.service.export(
        ExportTask(task_id=task_id, request_id="exp-1", output_path=str(target))
    )
    assert again.replayed is True
    target.write_bytes(b"tampered")
    with pytest.raises(OmasError, match="overwrite"):
        svc.service.export(
            ExportTask(task_id=task_id, request_id="exp-1", output_path=str(target))
        )


def test_submit_rejects_unknown_template_version(svc: SvcEnv) -> None:
    """Template must be registered before submit, not discovered at run time."""
    from omas.domain.ids import TemplateVersionId

    with pytest.raises(OmasError, match="not registered"):
        svc.service.submit(
            SubmitTask(
                request_id="req-badtpl",
                template_version_id=TemplateVersionId("tver_" + "9" * 32),
                intent="x",
                materials=(MaterialInput(filename="m.md", content=b"x"),),
            )
        )


def test_awaiting_user_audited_once_per_event(svc: SvcEnv) -> None:
    """gap_check already audits its event; service.run must not duplicate."""
    receipt = _submit(svc)
    task_id = TaskId(receipt.task_id)
    FakeExecutor(ExecutionStatus.AWAITING_USER, ("risks",))
    svc.service.run(task_id, FakeExecutor(ExecutionStatus.AWAITING_USER, ("risks",)))
    # a second run reporting awaiting again (same epoch) reuses the event
    svc.service.run(task_id, _NoopAwaitingExecutor())
    rows = svc.ledger.connection.execute(
        "SELECT COUNT(*) AS n FROM events WHERE task_id = ? AND event_code = 'awaiting_user'",
        (receipt.task_id,),
    ).fetchone()
    assert rows["n"] == 1


@dataclass
class _NoopAwaitingExecutor:
    def execute(self, task) -> ExecutionOutcome:
        return ExecutionOutcome(
            status=ExecutionStatus.AWAITING_USER, missing_slot_ids=("risks",)
        )
