"""Gate A (provenance precheck) unit tests: T03/T11/T18 flavours."""

from __future__ import annotations

from omas.core import resolve_span
from omas.domain.findings import GateStatus
from omas.domain.ids import ArtifactId, TaskId, TemplateVersionId
from omas.domain.ir import BindingIR, BoundBinding, DocxRenderIR, RenderSlot, TemplateStaticRef
from omas.domain.task import Task
from omas.domain.template import (
    StaticTextRegion,
    TemplateContract,
    TemplatePackageHashes,
)
from omas.gates.provenance import ProvenancePrecheck
from tests.unit.conftest import H64, EnvBox, ingest_material, make_task, register_template

MATERIAL = "本周销售营收一百二十万元，环比增长百分之八。"


def _binding(env: EnvBox, task: Task, artifact_id: str, digest: str) -> BindingIR:
    span = resolve_span(ArtifactId(artifact_id), _canonical_of(env, artifact_id), 0, 2)
    return BindingIR(
        schema_version=1,
        task_id=TaskId(task.task_id),
        plan_artifact_id=ArtifactId(artifact_id),
        template_version_id=task.template_version_id or TemplateVersionId("tver_" + "1" * 32),
        epoch=task.epoch,
        binding_version=1,
        material_set_digest=digest,
        bindings=[
            BoundBinding(
                binding_status="bound", slot_id="sales_summary", producer="user",
                source_refs=(span,),
            )
        ],
    )


def _canonical_of(env: EnvBox, artifact_id: str):
    from omas.core import canonicalize

    artifact = env.ledger.artifacts.get(ArtifactId(artifact_id))
    assert artifact is not None
    return canonicalize(env.store.read(artifact.relative_path))


def _contract(version_id: str) -> TemplateContract:
    return TemplateContract(
        schema_version=1,
        template_id="weekly-report",
        version=1,
        extractor_version="test-1",
        hashes=TemplatePackageHashes(
            docx_sha256=H64, contract_sha256="b" * 64,
            styles_sha256="c" * 64, static_map_sha256="d" * 64,
        ),
        sections=({"section_id": "s1", "slot_ids": ("sales_summary",)},),
        slots=(
            {
                "slot_id": "sales_summary",
                "placeholder": "{{ sales_summary }}",
                "kind": "text_block",
                "required": True,
                "semantic_requirement": "本周销售情况",
                "style_key": "body",
            },
        ),
        static_regions=(
            StaticTextRegion(
                region_id="title", part="document", locator="para:0",
                text_sha256="e" * 64, token_count=3,
            ),
        ),
    )


def _ir(task: Task, artifact_id: str, spans) -> DocxRenderIR:
    return DocxRenderIR(
        schema_version=1,
        task_id=TaskId(task.task_id),
        epoch=task.epoch,
        template_version_id=TemplateVersionId(task.template_version_id or ""),
        template_docx_artifact_id=ArtifactId(artifact_id),
        plan_artifact_id=ArtifactId(artifact_id),
        binding_artifact_id=ArtifactId(artifact_id),
        static_regions=(
            TemplateStaticRef(
                template_version_id=TemplateVersionId(task.template_version_id or ""),
                region_id="title",
            ),
        ),
        slots=(RenderSlot(slot_id="sales_summary", spans=spans, style_key="body"),),
    )


def _happy_setup(env: EnvBox):
    task = make_task(env)
    version_id = register_template(env)

    env.ledger.template_versions.get(version_id)
    task = env.ledger.tasks.bind_template(TaskId(task.task_id), version_id)
    artifact, canonical = ingest_material(env, task.task_id, MATERIAL)
    span = resolve_span(artifact.artifact_id, canonical, 0, 2)
    binding = _binding(env, task, artifact.artifact_id, _material_digest(env, task))
    contract = _contract(version_id)
    ir = _ir(task, artifact.artifact_id, (span,))
    return task, artifact, binding, contract, ir


def _material_digest(env: EnvBox, task: Task) -> str:
    from omas.core import material_set_digest

    rows = env.ledger.connection.execute(
        "SELECT artifact_id, sha256 FROM artifacts WHERE task_id = ? AND kind = 'canonical_text'",
        (task.task_id,),
    ).fetchall()
    return material_set_digest((r["artifact_id"], r["sha256"]) for r in rows)


def test_gate_a_passes_happy_path(env: EnvBox) -> None:
    task, artifact, binding, contract, ir = _happy_setup(env)
    report = ProvenancePrecheck(env.ledger, env.store).run(
        task=task, ir=ir, binding=binding, contract=contract,
        live_materials=(artifact,),
    )
    assert report.overall_status() is GateStatus.PASS, report.results


def test_gate_a_blocks_cross_task_span(env: EnvBox) -> None:
    """T18: a span artifact owned by another task must fail scope checks."""
    task, _artifact, binding, contract, ir = _happy_setup(env)
    other = make_task(env, request_id="req-other")
    foreign, foreign_canonical = ingest_material(env, other.task_id, "别的任务的材料内容。")
    from omas.core import resolve_span as rs

    foreign_span = rs(foreign.artifact_id, foreign_canonical, 0, 2)
    ir = ir.model_copy(
        update={
            "slots": (
                RenderSlot(slot_id="sales_summary", spans=(foreign_span,), style_key="body"),
            )
        }
    )
    report = ProvenancePrecheck(env.ledger, env.store).run(
        task=task, ir=ir, binding=binding, contract=contract,
        live_materials=(_artifact,),
    )
    assert report.overall_status() is GateStatus.FAIL
    scope = next(r for r in report.results if r.check_id == "scope:sales_summary")
    assert scope.status is GateStatus.FAIL
    assert "belongs to task" in (scope.actual or "")


def test_gate_a_blocks_tampered_file(env: EnvBox) -> None:
    """T03/T11: file bytes changed after registration must fail."""
    task, artifact, binding, contract, ir = _happy_setup(env)
    target = env.store.resolve_path(artifact.relative_path)
    target.write_bytes("被篡改的原料内容。".encode("utf-8"))
    report = ProvenancePrecheck(env.ledger, env.store).run(
        task=task, ir=ir, binding=binding, contract=contract, live_materials=(artifact,)
    )
    assert report.overall_status() is GateStatus.FAIL


def test_gate_a_blocks_stale_binding(env: EnvBox) -> None:
    """Material re-supplied after assembly: digest changed, binding is stale."""
    task, artifact, binding, contract, ir = _happy_setup(env)
    extra, _ = ingest_material(env, task.task_id, "补充材料。", name="extra.md")
    report = ProvenancePrecheck(env.ledger, env.store).run(
        task=task, ir=ir, binding=binding, contract=contract,
        live_materials=(artifact, extra),
    )
    freshness = next(r for r in report.results if r.check_id == "binding_freshness")
    assert freshness.status is GateStatus.FAIL


def test_gate_a_blocks_foreign_static_ref(env: EnvBox) -> None:
    task, artifact, binding, contract, ir = _happy_setup(env)
    bad_ref = TemplateStaticRef(
        template_version_id=TemplateVersionId("tver_" + "9" * 32), region_id="title"
    )
    ir = ir.model_copy(update={"static_regions": (bad_ref,)})
    report = ProvenancePrecheck(env.ledger, env.store).run(
        task=task, ir=ir, binding=binding, contract=contract, live_materials=(artifact,)
    )
    static = next(r for r in report.results if r.check_id == "static_regions")
    assert static.status is GateStatus.FAIL
    assert "foreign template version" in (static.actual or "")


def test_gate_a_blocks_epoch_mismatch(env: EnvBox) -> None:
    task, artifact, binding, contract, ir = _happy_setup(env)
    env.ledger.tasks.advance_epoch(TaskId(task.task_id), task.epoch)
    report = ProvenancePrecheck(env.ledger, env.store).run(
        task=env.ledger.tasks.get(TaskId(task.task_id)) or task,
        ir=ir, binding=binding, contract=contract, live_materials=(artifact,),
    )
    epoch_check = next(r for r in report.results if r.check_id == "epoch_match")
    assert epoch_check.status is GateStatus.FAIL
