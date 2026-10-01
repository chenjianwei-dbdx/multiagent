"""Finalizer tests: gate-subject binding, idempotent replay, cancel refusal.

T14 (unknown blocks), T20 (stale report blocks), T21 (at most one delivery),
T23 (cancel before commit refuses).
"""

from __future__ import annotations

import pytest

from omas.core.digest import canonical_model_sha
from omas.domain.artifact import Artifact, ArtifactKind
from omas.domain.errors import FinalizeRejectedError
from omas.domain.findings import CheckResult, GateName, GateReport, GateStatus
from omas.domain.ids import ArtifactId, TaskId
from omas.domain.ir import DocxRenderIR, RenderSlot, SourceSpanRef, TemplateStaticRef
from omas.domain.task import TaskStatus
from omas.pipeline import FinalizeContext, Finalizer
from tests.unit.conftest import H64, EnvBox, ingest_material, make_task, register_template

CANDIDATE_BYTES = b"PK\x03\x04 fake docx bytes for finalize tests"


def _span(artifact_id: str) -> SourceSpanRef:
    return SourceSpanRef(
        artifact_id=ArtifactId(artifact_id),
        canonical_sha256=H64,
        start=0,
        end=2,
        span_sha256=H64,
    )


def _make_candidate(env: EnvBox, task_id: str, payload: bytes = CANDIDATE_BYTES) -> Artifact:

    from omas.artifacts.writers import NodeArtifactWriter

    writer = NodeArtifactWriter(env.store, task_id, "run_" + "1" * 32, env.recorder)
    staged = writer.stage("candidate.docx", payload)
    return writer.commit_out(
        staged,
        kind=ArtifactKind.DOCX_CANDIDATE,
        final_name=f"{staged.sha256}.docx",
        content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )


def _ir(task_id: str, template_version_id: str, artifact_id: str) -> DocxRenderIR:
    return DocxRenderIR(
        schema_version=1,
        task_id=TaskId(task_id),
        epoch=1,
        template_version_id=template_version_id,
        template_docx_artifact_id=ArtifactId(artifact_id),
        plan_artifact_id=ArtifactId(artifact_id),
        binding_artifact_id=ArtifactId(artifact_id),
        static_regions=(
            TemplateStaticRef(template_version_id=template_version_id, region_id="title"),
        ),
        slots=(RenderSlot(slot_id="sales_summary", spans=(_span(artifact_id),), style_key="body"),),
    )


def _contract():
    from omas.domain.template import TemplateContract, TemplatePackageHashes

    return TemplateContract(
        schema_version=1,
        template_id="weekly-report",
        version=1,
        extractor_version="test-1",
        hashes=TemplatePackageHashes(
            docx_sha256=H64,
            contract_sha256="b" * 64,
            styles_sha256="c" * 64,
            static_map_sha256="d" * 64,
        ),
        sections=({"section_id": "s1", "slot_ids": ("sales_summary",)},),
        slots=(
            {
                "slot_id": "sales_summary",
                "placeholder": "{{ sales_summary }}",
                "kind": "text_block",
                "required": True,
                "semantic_requirement": "x",
                "style_key": "body",
            },
        ),
    )


def _reports(ir: DocxRenderIR, candidate_sha: str, statuses: dict[str, GateStatus] | None = None):
    statuses = statuses or {}
    reports = []
    for gate in (GateName.PROVENANCE_PRECHECK, GateName.PROVENANCE_GATE, GateName.FORMAT_GATE):
        status = statuses.get(gate.value, GateStatus.PASS)
        reports.append(
            GateReport(
                gate=gate,
                candidate_sha256=None if gate is GateName.PROVENANCE_PRECHECK else candidate_sha,
                render_ir_sha256=canonical_model_sha(ir),
                template_version_id=ir.template_version_id,
                rule_version="t",
                results=(CheckResult(check_id="c", rule_id="r", status=status),),
            )
        )
    return reports


def _setup(env: EnvBox):
    task = make_task(env)
    version_id = register_template(env)
    task = env.ledger.tasks.bind_template(TaskId(task.task_id), version_id)
    material, _ = ingest_material(env, task.task_id, "材料内容。")
    candidate = _make_candidate(env, task.task_id)
    ir = _ir(task.task_id, version_id, material.artifact_id)
    context = FinalizeContext(
        materials=(material,),
        plan_artifact_id=material.artifact_id,
        binding_artifact_id=material.artifact_id,
        render_ir_artifact_id=material.artifact_id,
    )
    return env, task, material, candidate, ir, context


def test_finalize_happy_path(env: EnvBox) -> None:
    env, task, _material, candidate, ir, context = _setup(env)
    delivery = Finalizer(env.home, env.store, env.ledger).finalize(
        task=task,
        candidate=candidate,
        ir=ir,
        gate_reports=_reports(ir, candidate.sha256),
        contract=_contract(),
        context=context,
    )
    assert env.store.exists(env.store.delivery_relative(task.task_id, f"{task.task_id}.docx"))
    assert env.store.exists(env.store.delivery_relative(task.task_id, "manifest.json"))
    assert delivery.final_sha256 == candidate.sha256
    manifest = env.ledger.artifacts.get(ArtifactId(delivery.manifest_artifact_id))
    assert manifest is not None
    payload = env.store.read_verified(manifest.relative_path, manifest.sha256)
    assert b'"task_id"' in payload and task.task_id.encode() in payload
    assert b'"final_sha256"' in payload


def test_finalize_replay_is_idempotent(env: EnvBox) -> None:
    """T21: second finalize returns the same receipt, no second files."""
    env, task, _material, candidate, ir, context = _setup(env)
    finalizer = Finalizer(env.home, env.store, env.ledger)
    first = finalizer.finalize(
        task=task, candidate=candidate, ir=ir,
        gate_reports=_reports(ir, candidate.sha256), contract=_contract(), context=context,
    )
    second = finalizer.finalize(
        task=task, candidate=candidate, ir=ir,
        gate_reports=_reports(ir, candidate.sha256), contract=_contract(), context=context,
    )
    assert second.delivery_id == first.delivery_id
    assert second.final_sha256 == first.final_sha256
    rows = env.ledger.connection.execute(
        "SELECT COUNT(*) AS n FROM deliveries WHERE task_id = ?", (task.task_id,)
    ).fetchone()
    assert rows["n"] == 1


def test_finalize_rejects_unknown_gate(env: EnvBox) -> None:
    """T14: required gate unknown → no finalize, no deliverable."""
    env, task, _material, candidate, ir, context = _setup(env)
    reports = _reports(ir, candidate.sha256, {"format_gate": GateStatus.UNKNOWN})
    with pytest.raises(FinalizeRejectedError):
        Finalizer(env.home, env.store, env.ledger).finalize(
            task=task, candidate=candidate, ir=ir,
            gate_reports=reports, contract=_contract(), context=context,
        )
    assert not env.store.exists(env.store.delivery_relative(task.task_id, f"{task.task_id}.docx"))


def test_finalize_rejects_stale_report(env: EnvBox) -> None:
    """T20: report bound to a different candidate sha must refuse."""
    env, task, _material, candidate, ir, context = _setup(env)
    reports = _reports(ir, "f" * 64)  # wrong candidate sha
    with pytest.raises(FinalizeRejectedError, match="different candidate"):
        Finalizer(env.home, env.store, env.ledger).finalize(
            task=task, candidate=candidate, ir=ir,
            gate_reports=reports, contract=_contract(), context=context,
        )


def test_finalize_rejects_missing_report(env: EnvBox) -> None:
    env, task, _material, candidate, ir, context = _setup(env)
    reports = _reports(ir, candidate.sha256)[:2]  # format gate missing
    with pytest.raises(FinalizeRejectedError, match="format_gate"):
        Finalizer(env.home, env.store, env.ledger).finalize(
            task=task, candidate=candidate, ir=ir,
            gate_reports=reports, contract=_contract(), context=context,
        )


def test_finalize_refuses_cancelled_task(env: EnvBox) -> None:
    """T23: cancel before finalize commit → no delivery."""
    env, task, _material, candidate, ir, context = _setup(env)
    env.ledger.tasks.update_status(TaskId(task.task_id), TaskStatus.CANCELLED)
    with pytest.raises(FinalizeRejectedError, match="cancelled"):
        Finalizer(env.home, env.store, env.ledger).finalize(
            task=task, candidate=candidate, ir=ir,
            gate_reports=_reports(ir, candidate.sha256), contract=_contract(), context=context,
        )
    assert env.ledger.deliveries.get_by_task(TaskId(task.task_id)) is None
