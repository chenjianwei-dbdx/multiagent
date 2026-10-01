"""Deterministic render pipeline (P1): no LLM anywhere in this loop.

    binding → render IR → Gate A → render → Gate B → FormatGate → finalize

Every persisted step is content-addressed (sha-named files under a
task+node-stable run directory), so re-running the pipeline with the same
inputs reuses existing artifacts instead of duplicating them; business
uniqueness of the delivery is the Finalizer's job.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

from omas.artifacts.store import ArtifactStore, StagedFile
from omas.artifacts.writers import NodeArtifactWriter
from omas.core.canonical import canonicalize, sha256_bytes
from omas.core.digest import canonical_model_sha
from omas.core.spans import verify_span
from omas.domain.artifact import Artifact, ArtifactKind
from omas.domain.delivery import Delivery
from omas.domain.errors import (
    ArtifactExistsError,
    ArtifactNotFoundError,
    GateBlockedError,
)
from omas.domain.findings import GateReport, GateStatus
from omas.domain.ids import ArtifactId, TaskId, TemplateVersionId, new_artifact_id
from omas.domain.ir import BindingIR
from omas.domain.spans import SourceSpanRef
from omas.domain.task import Task, TaskStatus
from omas.domain.template import StyleSpec, TemplateContract
from omas.gates.format import FormatGate
from omas.gates.gate_b import ProvenanceGate, SlotLocations, classify_output
from omas.gates.provenance import ProvenancePrecheck
from omas.pipeline.finalizer import FinalizeContext, Finalizer
from omas.pipeline.recorders import LedgerRecorder
from omas.pipeline.render_ir_builder import RenderIRBuilder
from omas.renderers.docx_renderer import SpanTextResolver, render_docx
from omas.storage.db import Ledger

_DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def _stable_run_id(task_id: str, node: str) -> str:
    digest = hashlib.sha256(f"{task_id}:{node}".encode("utf-8")).hexdigest()[:32]
    return f"run_{digest}"


class DeterministicRenderPipeline:
    def __init__(self, home: Path, store: ArtifactStore, ledger: Ledger) -> None:
        self._home = home
        self._store = store
        self._ledger = ledger
        self._recorder = LedgerRecorder(ledger)
        self._gate_a = ProvenancePrecheck(ledger, store)
        self._gate_b = ProvenanceGate()
        self._format = FormatGate()
        self._builder = RenderIRBuilder(ledger)
        self._finalizer = Finalizer(home, store, ledger)

    def run(
        self,
        *,
        task: Task,
        binding: BindingIR,
        contract: TemplateContract,
        template_version_id: TemplateVersionId,
        template_docx: bytes,
        template_relative_path: str,
        static_map: dict[str, object] | None = None,
        styles_spec: dict[str, StyleSpec] | None = None,
        plan_artifact_id: ArtifactId,
        binding_artifact_id: ArtifactId,
        materials: tuple[Artifact, ...] = (),
    ) -> Delivery:
        fresh = self._ledger.tasks.get(task.task_id)
        if fresh is None:
            raise ArtifactNotFoundError(f"task {task.task_id} not found")
        if fresh.status is TaskStatus.CANCELLED:
            raise GateBlockedError("task cancelled")
        if fresh.status is TaskStatus.CREATED:
            fresh = self._ledger.tasks.update_status(
                TaskId(fresh.task_id), TaskStatus.RUNNING, expected=TaskStatus.CREATED
            )

        template_artifact = self._ensure_template_artifact(
            fresh, template_relative_path, contract
        )
        ir = self._builder.build(
            task_id=TaskId(fresh.task_id),
            epoch=fresh.epoch,
            contract=contract,
            template_version_id=TemplateVersionId(template_version_id),
            template_docx_artifact_id=ArtifactId(template_artifact.artifact_id),
            binding=binding,
            plan_artifact_id=plan_artifact_id,
            binding_artifact_id=binding_artifact_id,
        )
        ir_artifact = self._persist_bytes(
            fresh.task_id,
            "build_render_ir",
            f"{canonical_model_sha(ir)}.json",
            ir.model_dump_json().encode("utf-8"),
            kind=ArtifactKind.RENDER_IR,
            source_refs=(binding_artifact_id, plan_artifact_id),
            content_type="application/json",
        )
        self._ledger.tasks.set_active_refs(
            TaskId(fresh.task_id), render_ir=ArtifactId(ir_artifact.artifact_id)
        )

        live_materials = materials or self._canonical_materials(fresh)
        gate_a = self._gate_a.run(
            task=fresh,
            ir=ir,
            binding=binding,
            contract=contract,
            live_materials=live_materials,
        )
        gate_a_artifact = self._persist_report(fresh.task_id, gate_a)
        self._require_pass(gate_a)

        resolver = self._make_span_resolver()
        candidate_bytes = render_docx(
            ir=ir, contract=contract, template_docx=template_docx, resolve_span=resolver
        )
        candidate_sha = sha256_bytes(candidate_bytes)
        candidate_artifact = self._persist_bytes(
            fresh.task_id,
            "render_docx",
            f"{candidate_sha}.docx",
            candidate_bytes,
            kind=ArtifactKind.DOCX_CANDIDATE,
            source_refs=(ArtifactId(ir_artifact.artifact_id),),
            content_type=_DOCX_MIME,
        )
        self._ledger.tasks.set_active_refs(
            TaskId(fresh.task_id), candidate=ArtifactId(candidate_artifact.artifact_id)
        )

        locations: SlotLocations = classify_output(
            template_docx=template_docx,
            candidate_docx=candidate_bytes,
            contract=contract,
            ir=ir,
            resolve_span=resolver,
        )[0]
        gate_b = self._gate_b.run(
            candidate_docx=candidate_bytes,
            template_docx=template_docx,
            contract=contract,
            ir=ir,
            resolve_span=resolver,
            static_map=static_map,
        )
        gate_b_artifact = self._persist_report(fresh.task_id, gate_b)
        format_gate = self._format.run(
            candidate_docx=candidate_bytes,
            candidate_sha256=candidate_sha,
            contract=contract,
            styles_spec=styles_spec or {},
            ir=ir,
            locations=locations,
        )
        format_artifact = self._persist_report(fresh.task_id, format_gate)
        self._require_pass(gate_b)
        self._require_pass(format_gate)

        overrides = self._ledger.slot_overrides.active_for_task(
            TaskId(fresh.task_id), fresh.epoch
        )
        context = FinalizeContext(
            materials=tuple(live_materials),
            plan_artifact_id=plan_artifact_id,
            binding_artifact_id=binding_artifact_id,
            render_ir_artifact_id=ArtifactId(ir_artifact.artifact_id),
            gate_report_artifact_ids=(
                ArtifactId(gate_a_artifact.artifact_id),
                ArtifactId(gate_b_artifact.artifact_id),
                ArtifactId(format_artifact.artifact_id),
            ),
            overrides=tuple((o.slot_id, o.decision_id) for o in overrides.values()),
        )
        delivery = self._finalizer.finalize(
            task=fresh,
            candidate=candidate_artifact,
            ir=ir,
            gate_reports=(gate_a, gate_b, format_gate),
            contract=contract,
            context=context,
        )
        if fresh.status is TaskStatus.RUNNING:
            self._ledger.tasks.update_status(TaskId(fresh.task_id), TaskStatus.COMPLETED)
        return delivery

    # ---------------------------------------------------------------- helpers

    def _make_span_resolver(self) -> SpanTextResolver:
        def resolve(ref: SourceSpanRef) -> str:
            artifact = self._ledger.artifacts.get(ref.artifact_id)
            if artifact is None:
                raise ArtifactNotFoundError(f"span artifact {ref.artifact_id} not registered")
            raw = self._store.read_verified(artifact.relative_path, artifact.sha256)
            canonical = canonicalize(raw)
            verify_span(canonical, ref)
            return canonical.text[ref.start : ref.end]

        return resolve

    def _canonical_materials(self, task: Task) -> tuple[Artifact, ...]:
        rows = self._ledger.connection.execute(
            "SELECT artifact_id FROM artifacts WHERE task_id = ? AND kind = 'canonical_text'",
            (task.task_id,),
        ).fetchall()
        found = []
        for row in rows:
            artifact = self._ledger.artifacts.get(ArtifactId(row["artifact_id"]))
            if artifact is not None:
                found.append(artifact)
        return tuple(found)

    def _ensure_template_artifact(
        self, task: Task, template_relative_path: str, contract: TemplateContract
    ) -> Artifact:
        existing = self._ledger.artifacts.get_by_path(template_relative_path)
        if existing is not None:
            return existing
        return self._ledger.artifacts.register(
            Artifact(
                artifact_id=new_artifact_id(),
                task_id=TaskId(task.task_id),
                kind=ArtifactKind.TEMPLATE_DOCX,
                relative_path=template_relative_path,
                sha256=contract.hashes.docx_sha256,
                size=len(self._store.read(template_relative_path)),
                source_refs=(),
                created_at=datetime.now(UTC),
            )
        )

    def _persist_bytes(
        self,
        task_id: str,
        node: str,
        name: str,
        data: bytes,
        *,
        kind: ArtifactKind,
        source_refs: tuple[ArtifactId, ...] = (),
        content_type: str | None = None,
    ) -> Artifact:
        """Content-addressed node output; replays reuse instead of duplicating."""
        run_id = _stable_run_id(task_id, node)
        out_relative = self._store.node_out_relative(task_id, run_id, name)
        if self._store.exists(out_relative):
            registered = self._ledger.artifacts.get_by_path(out_relative)
            if registered is not None:
                return registered
            promoted = self._store.adopt_existing(out_relative, sha256_bytes(data))
            return self._ledger.artifacts.register(
                Artifact(
                    artifact_id=new_artifact_id(),
                    task_id=TaskId(task_id),
                    kind=kind,
                    relative_path=promoted.relative_path,
                    sha256=promoted.sha256,
                    size=promoted.size,
                    source_refs=source_refs,
                    content_type=content_type,
                    created_at=datetime.now(UTC),
                )
            )
        writer = NodeArtifactWriter(
            self._store, task_id, run_id, recorder=self._recorder
        )
        try:
            staged = writer.stage(name, data)
        except ArtifactExistsError:
            staged = self._readopt_staged(task_id, run_id, name, sha256_bytes(data))
        return writer.commit_out(
            staged,
            kind=kind,
            final_name=name,
            source_refs=source_refs,
            content_type=content_type,
        )

    def _readopt_staged(
        self, task_id: str, run_id: str, name: str, expected_sha: str
    ) -> StagedFile:
        work_relative = self._store.node_work_relative(task_id, run_id, name)
        self._store.verify(work_relative, expected_sha)
        return StagedFile(
            path=self._store.resolve_path(work_relative),
            relative_path=work_relative,
            name=name,
            sha256=expected_sha,
            size=len(self._store.read(work_relative)),
        )

    def _persist_report(self, task_id: str, report: GateReport) -> Artifact:
        name = (
            f"{report.gate.value}_{report.render_ir_sha256[:16]}"
            f"_{(report.candidate_sha256 or 'none')[:16]}.json"
        )
        artifact = self._persist_bytes(
            task_id,
            report.gate.value,
            name,
            report.model_dump_json().encode("utf-8"),
            kind=ArtifactKind.GATE_REPORT,
            content_type="application/json",
        )
        gate_report_id = f"gr_{artifact.artifact_id.removeprefix('art_')}"
        existing = self._ledger.gate_reports.get(gate_report_id)
        if existing is not None:
            return artifact  # replay: report row already recorded
        self._ledger.gate_reports.insert(
            gate_report_id=gate_report_id,
            task_id=TaskId(task_id),
            gate=report.gate.value,
            candidate_sha256=report.candidate_sha256,
            render_ir_sha256=report.render_ir_sha256,
            template_version_id=report.template_version_id,
            rule_version=report.rule_version,
            overall_status=report.overall_status().value,
            summary_json=json.dumps({"checks": len(report.results)}, ensure_ascii=False),
            report_artifact_id=ArtifactId(artifact.artifact_id),
            created_at=datetime.now(UTC),
        )
        return artifact

    def _require_pass(self, report: GateReport) -> None:
        if report.overall_status() is not GateStatus.PASS:
            bad = [c.check_id for c in report.results if c.status is not GateStatus.PASS]
            raise GateBlockedError(
                f"{report.gate.value} did not pass "
                f"({report.overall_status().value}: {bad[:5]})"
            )
