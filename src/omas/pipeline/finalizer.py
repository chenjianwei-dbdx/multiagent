"""Finalizer: the only writer of deliverables (Master §17; v1.1 §8.2).

Sequence (inside the executor lock):

1. re-verify task state / epoch / gate subjects against the live candidate
2. idempotent operation intent (task+epoch+node+input digest+impl version)
3. already committed → verify the delivery file hash and return the receipt
4. crash window "promoted, not recorded" → adopt_existing by expected hash
5. stage delivery copy + manifest → atomic promote → single delivery record
6. commit the operation with the output artifact refs

No cross-resource exactly-once is claimed: the committed ledger record is the
single business truth (v1.1 §1.1 hard boundary).
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from omas import __version__
from omas.artifacts.store import ArtifactStore
from omas.artifacts.writers import DeliveryWriter
from omas.core.canonical import sha256_bytes
from omas.core.digest import canonical_model_sha, operation_key, payload_digest
from omas.domain.artifact import Artifact, ArtifactKind
from omas.domain.delivery import Delivery
from omas.domain.errors import FinalizeRejectedError
from omas.domain.findings import GateName, GateReport, GateStatus
from omas.domain.ids import (
    ArtifactId,
    DeliveryId,
    OperationId,
    TaskId,
    new_artifact_id,
    new_node_run_id,
    new_operation_id,
)
from omas.domain.ir import DocxRenderIR
from omas.domain.operations import IMPLEMENTATION_VERSION, Operation
from omas.domain.task import Task
from omas.domain.template import TemplateContract
from omas.pipeline.locking import executor_lock
from omas.pipeline.recorders import LedgerRecorder
from omas.storage.db import Ledger, from_db_datetime

_REQUIRED_GATES = (GateName.PROVENANCE_PRECHECK, GateName.PROVENANCE_GATE, GateName.FORMAT_GATE)
_DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


@dataclass(frozen=True, slots=True)
class FinalizeContext:
    """Everything the manifest must be able to trace back (I9)."""

    intent_artifact_id: ArtifactId | None = None
    materials: tuple[Artifact, ...] = ()
    plan_artifact_id: ArtifactId | None = None
    binding_artifact_id: ArtifactId | None = None
    render_ir_artifact_id: ArtifactId | None = None
    gate_report_artifact_ids: tuple[ArtifactId, ...] = ()
    overrides: tuple[tuple[str, str], ...] = ()  # (slot_id, decision_id)
    operation_ids: tuple[str, ...] = ()
    llm_call_ids: tuple[str, ...] = ()
    extra: dict[str, str] = field(default_factory=dict)


class Finalizer:
    def __init__(self, home: Path, store: ArtifactStore, ledger: Ledger) -> None:
        self._home = home
        self._store = store
        self._ledger = ledger
        self._recorder = LedgerRecorder(ledger)

    def finalize(
        self,
        *,
        task: Task,
        candidate: Artifact,
        ir: DocxRenderIR,
        gate_reports: Sequence[GateReport],
        contract: TemplateContract,
        context: FinalizeContext,
    ) -> Delivery:
        with executor_lock(self._home):
            return self._finalize_locked(
                task=task,
                candidate=candidate,
                ir=ir,
                gate_reports=gate_reports,
                contract=contract,
                context=context,
            )

    # ----------------------------------------------------------------- locked

    def _finalize_locked(
        self,
        *,
        task: Task,
        candidate: Artifact,
        ir: DocxRenderIR,
        gate_reports: Sequence[GateReport],
        contract: TemplateContract,
        context: FinalizeContext,
    ) -> Delivery:
        fresh = self._ledger.tasks.get(task.task_id)
        if fresh is None:
            raise FinalizeRejectedError(f"task {task.task_id} vanished")
        if fresh.status.value == "cancelled":
            raise FinalizeRejectedError("task cancelled; no delivery may be committed")
        if fresh.epoch != ir.epoch:
            raise FinalizeRejectedError(
                f"stale render IR: epoch {ir.epoch}, task is at {fresh.epoch}"
            )
        if fresh.template_version_id != ir.template_version_id:
            raise FinalizeRejectedError("task template version changed under finalize")
        self._verify_gates(ir=ir, candidate=candidate, gate_reports=gate_reports)
        # candidate bytes must still match the registered hash (T11)
        self._store.verify(candidate.relative_path, candidate.sha256)

        input_digest = payload_digest(candidate.sha256, canonical_model_sha(ir))
        op_key = operation_key(
            task_id=fresh.task_id,
            epoch=fresh.epoch,
            node_name="finalize",
            input_digest=input_digest,
            implementation_version=IMPLEMENTATION_VERSION,
        )
        operation = self._ledger.operations.ensure_intent(
            Operation(
                operation_id=new_operation_id(),
                operation_key=op_key,
                task_id=TaskId(fresh.task_id),
                node_name="finalize",
                epoch=fresh.epoch,
                payload_digest=input_digest,
            )
        )
        existing = self._ledger.deliveries.get_by_task(TaskId(fresh.task_id))
        if existing is not None or operation.state.value == "committed":
            return self._return_existing(existing)

        docx_artifact = self._write_delivery_docx(fresh, candidate, operation.operation_id)
        manifest_artifact = self._write_manifest(
            fresh, candidate, ir, contract, gate_reports, context, docx_artifact
        )
        now = datetime.now(UTC)
        delivery_id = DeliveryId(f"dlv_{operation.operation_id.removeprefix('op_')}")
        self._ledger.deliveries.record(
            delivery_id=delivery_id,
            task_id=TaskId(fresh.task_id),
            operation_id=OperationId(operation.operation_id),
            candidate_artifact_id=ArtifactId(candidate.artifact_id),
            final_sha256=docx_artifact.sha256,
            manifest_artifact_id=ArtifactId(manifest_artifact.artifact_id),
            created_at=now,
        )
        self._ledger.operations.commit(
            OperationId(operation.operation_id),
            (docx_artifact.artifact_id, manifest_artifact.artifact_id),
            now,
        )
        return Delivery(
            delivery_id=delivery_id,
            task_id=TaskId(fresh.task_id),
            operation_id=OperationId(operation.operation_id),
            candidate_artifact_id=ArtifactId(candidate.artifact_id),
            final_sha256=docx_artifact.sha256,
            manifest_artifact_id=ArtifactId(manifest_artifact.artifact_id),
            created_at=now,
        )

    def _verify_gates(
        self, *, ir: DocxRenderIR, candidate: Artifact, gate_reports: Sequence[GateReport]
    ) -> None:
        by_gate = {report.gate: report for report in gate_reports}
        problems: list[str] = []
        ir_sha = canonical_model_sha(ir)
        for gate in _REQUIRED_GATES:
            report = by_gate.get(gate)
            if report is None:
                problems.append(f"{gate.value}: report missing")
                continue
            if report.overall_status() is not GateStatus.PASS:
                problems.append(f"{gate.value}: {report.overall_status().value}")
            if report.render_ir_sha256 != ir_sha:
                problems.append(f"{gate.value}: report bound to a different render IR")
            if report.template_version_id != ir.template_version_id:
                problems.append(f"{gate.value}: report bound to a different template version")
            subject_checks_candidate = gate is not GateName.PROVENANCE_PRECHECK
            if subject_checks_candidate and report.candidate_sha256 != candidate.sha256:
                problems.append(f"{gate.value}: report bound to a different candidate")
        if problems:
            raise FinalizeRejectedError("; ".join(problems))

    def _return_existing(self, row: sqlite3.Row | None) -> Delivery:
        if row is None:
            raise FinalizeRejectedError(
                "operation committed but no delivery record found; "
                "manual reconciliation required"
            )
        task_id = str(row["task_id"])
        # the committed bytes must still match the recorded hashes (T21)
        self._store.verify(
            self._store.delivery_relative(task_id, f"{task_id}.docx"), str(row["final_sha256"])
        )
        manifest = self._ledger.artifacts.get(ArtifactId(str(row["manifest_artifact_id"])))
        if manifest is None:
            raise FinalizeRejectedError("manifest artifact missing from ledger")
        self._store.verify(self._store.delivery_relative(task_id, "manifest.json"), manifest.sha256)
        return Delivery(
            delivery_id=DeliveryId(str(row["delivery_id"])),
            task_id=TaskId(task_id),
            operation_id=OperationId(str(row["operation_id"])),
            candidate_artifact_id=ArtifactId(str(row["candidate_artifact_id"])),
            final_sha256=str(row["final_sha256"]),
            manifest_artifact_id=ArtifactId(str(row["manifest_artifact_id"])),
            created_at=from_db_datetime(str(row["created_at"])),
        )

    # ---------------------------------------------------------------- writing

    def _write_delivery_docx(
        self, task: Task, candidate: Artifact, operation_id: str
    ) -> Artifact:
        """Copy the candidate into deliverables/ — adopt orphans by hash first."""
        target = self._store.delivery_relative(task.task_id, f"{task.task_id}.docx")
        if self._store.exists(target):
            promoted = self._store.adopt_existing(target, candidate.sha256)
            return self._register_or_reuse(
                promoted.relative_path,
                promoted.sha256,
                promoted.size,
                kind=ArtifactKind.DELIVERY_DOCX,
                task_id=task.task_id,
                operation_id=operation_id,
                source_refs=(ArtifactId(candidate.artifact_id),),
                content_type=_DOCX_MIME,
            )
        writer = DeliveryWriter(self._store, task.task_id, self._recorder)
        run_id = new_node_run_id()
        payload = self._store.read_verified(candidate.relative_path, candidate.sha256)
        staged = self._store.stage(task.task_id, run_id, f"{candidate.sha256}.docx", payload)
        return writer.deliver(
            staged,
            final_name=f"{task.task_id}.docx",
            kind=ArtifactKind.DELIVERY_DOCX,
            operation_id=OperationId(operation_id),
            source_refs=(ArtifactId(candidate.artifact_id),),
            content_type=_DOCX_MIME,
        )

    def _write_manifest(
        self,
        task: Task,
        candidate: Artifact,
        ir: DocxRenderIR,
        contract: TemplateContract,
        gate_reports: Sequence[GateReport],
        context: FinalizeContext,
        docx_artifact: Artifact,
    ) -> Artifact:
        manifest_bytes = self._build_manifest(
            task, candidate, ir, contract, gate_reports, context, docx_artifact
        )
        target = self._store.delivery_relative(task.task_id, "manifest.json")
        if self._store.exists(target):
            promoted = self._store.adopt_existing(target, sha256_bytes(manifest_bytes))
            return self._register_or_reuse(
                promoted.relative_path,
                promoted.sha256,
                promoted.size,
                kind=ArtifactKind.DELIVERY_MANIFEST,
                task_id=task.task_id,
                operation_id=None,
                source_refs=(),
                content_type="application/json",
            )
        writer = DeliveryWriter(self._store, task.task_id, self._recorder)
        run_id = new_node_run_id()
        staged = self._store.stage(task.task_id, run_id, "manifest.staged.json", manifest_bytes)
        return writer.deliver(
            staged,
            final_name="manifest.json",
            kind=ArtifactKind.DELIVERY_MANIFEST,
            content_type="application/json",
        )

    def _register_or_reuse(
        self,
        relative_path: str,
        sha256: str,
        size: int,
        *,
        kind: ArtifactKind,
        task_id: str,
        operation_id: str | None,
        source_refs: tuple[ArtifactId, ...],
        content_type: str | None,
    ) -> Artifact:
        """An adopted orphan is reused if already registered, else registered now."""
        registered = self._ledger.artifacts.get_by_path(relative_path)
        if registered is not None:
            return registered
        return self._recorder.register(
            Artifact(
                artifact_id=new_artifact_id(),
                task_id=TaskId(task_id),
                kind=kind,
                relative_path=relative_path,
                sha256=sha256,
                size=size,
                created_by_operation_id=(
                    OperationId(operation_id) if operation_id is not None else None
                ),
                source_refs=source_refs,
                content_type=content_type,
                created_at=datetime.now(UTC),
            )
        )

    def _build_manifest(
        self,
        task: Task,
        candidate: Artifact,
        ir: DocxRenderIR,
        contract: TemplateContract,
        gate_reports: Sequence[GateReport],
        context: FinalizeContext,
        docx_artifact: Artifact,
    ) -> bytes:
        manifest = {
            "schema_version": 1,
            "task_id": task.task_id,
            "epoch": task.epoch,
            "created_at": datetime.now(UTC).isoformat(),
            "software": {
                "omas_version": __version__,
                "implementation_version": IMPLEMENTATION_VERSION,
            },
            "template": {
                "template_id": contract.template_id,
                "version": contract.version,
                "template_version_id": ir.template_version_id,
                "hashes": contract.hashes.model_dump(),
            },
            "intent_artifact_id": context.intent_artifact_id,
            "materials": [
                {"artifact_id": m.artifact_id, "sha256": m.sha256, "kind": m.kind.value}
                for m in context.materials
            ],
            "plan_artifact_id": context.plan_artifact_id,
            "binding_artifact_id": context.binding_artifact_id,
            "render_ir_artifact_id": context.render_ir_artifact_id,
            "candidate": {"artifact_id": candidate.artifact_id, "sha256": candidate.sha256},
            "render_ir_sha256": canonical_model_sha(ir),
            "gates": [
                {
                    "gate": r.gate.value,
                    "overall": r.overall_status().value,
                    "rule_version": r.rule_version,
                }
                for r in gate_reports
            ],
            "gate_report_artifact_ids": list(context.gate_report_artifact_ids),
            "overrides": [
                {"slot_id": slot, "decision_id": decision} for slot, decision in context.overrides
            ],
            "operation_ids": list(context.operation_ids),
            "llm_call_ids": list(context.llm_call_ids),
            "delivery": {
                "docx_artifact_id": docx_artifact.artifact_id,
                "final_sha256": docx_artifact.sha256,
            },
        }
        return json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")
