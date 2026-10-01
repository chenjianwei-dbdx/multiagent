"""P1 closed loop acceptance: fixture template + program-built BindingIR
→ deterministic pipeline → real, manifest-traceable DOCX delivery. No LLM.
"""

from __future__ import annotations

import io
import zipfile

import pytest

from omas.domain.errors import MissingRequiredSlotsError
from omas.domain.ids import TaskId
from omas.domain.ir import MissingBinding
from omas.domain.task import TaskStatus
from omas.pipeline.deterministic import DeterministicRenderPipeline
from omas.templates.registry import TemplateRegistry
from tests.integration.conftest import (
    binding_for_material,
    ingest,
    make_task,
    sections_of,
    styles_spec_typed,
)


def register_template(env, weekly):
    registry = TemplateRegistry(env.store, env.ledger)
    version_id, contract = registry.register(
        docx_bytes=weekly.docx_bytes,
        sidecar=weekly.sidecar,
        styles_spec=weekly.styles_spec,
        static_map=weekly.static_map,
        template_id="weekly-report",
    )
    assert contract.is_activatable(), [f.code for f in contract.unsupported_findings]
    return version_id, contract


def build_case(env, weekly, drop_slots=(), extra_bindings=()):
    """Register template, create+bind task, ingest material, build binding."""
    version_id, contract = register_template(env, weekly)
    task = make_task(env)
    task = env.ledger.tasks.bind_template(TaskId(task.task_id), version_id)
    material, canonical = ingest(env, task.task_id, weekly.material_markdown)
    spans_by_slot = {
        slot: texts
        for slot, texts in sections_of(weekly.material_markdown).items()
        if slot not in drop_slots
    }
    binding, binding_artifact = binding_for_material(
        env, task, version_id, material, canonical, spans_by_slot, list(extra_bindings)
    )
    bound_texts = spans_by_slot
    return task, version_id, contract, material, binding, binding_artifact, bound_texts


def run_pipeline(env, weekly, case):
    task, version_id, contract, material, binding, binding_artifact, _texts = case
    pipeline = DeterministicRenderPipeline(env.home, env.store, env.ledger)
    return pipeline.run(
        task=task,
        binding=binding,
        contract=contract,
        template_version_id=version_id,
        template_docx=weekly.docx_bytes,
        template_relative_path=env.store.template_relative(
            "weekly-report", "1", "template.docx"
        ),
        static_map=weekly.static_map,
        styles_spec=styles_spec_typed(weekly.styles_spec),
        plan_artifact_id=binding_artifact.artifact_id,
        binding_artifact_id=binding_artifact.artifact_id,
        materials=(material,),
    )


def test_full_closed_loop_produces_traceable_docx(loop_env, weekly) -> None:
    case = build_case(loop_env, weekly)
    delivery = run_pipeline(loop_env, weekly, case)
    task = case[0]

    docx_path = loop_env.store.delivery_relative(task.task_id, f"{task.task_id}.docx")
    manifest_path = loop_env.store.delivery_relative(task.task_id, "manifest.json")
    assert loop_env.store.exists(docx_path)
    assert loop_env.store.exists(manifest_path)

    docx_bytes = loop_env.store.read_verified(docx_path, delivery.final_sha256)
    with zipfile.ZipFile(io.BytesIO(docx_bytes)) as zf:
        names = zf.namelist()
        assert "word/document.xml" in names
        document = zf.read("word/document.xml").decode("utf-8")
    for texts in case[6].values():
        for text in texts:
            assert text in document

    manifest_artifact = loop_env.ledger.artifacts.get(delivery.manifest_artifact_id)
    assert manifest_artifact is not None
    manifest = loop_env.store.read_verified(
        manifest_artifact.relative_path, manifest_artifact.sha256
    )
    assert task.task_id.encode() in manifest
    assert b'"final_sha256"' in manifest and delivery.final_sha256.encode() in manifest
    assert b'"gates"' in manifest

    fresh = loop_env.ledger.tasks.get(TaskId(task.task_id))
    assert fresh is not None and fresh.status is TaskStatus.COMPLETED


def test_pipeline_replay_yields_single_delivery(loop_env, weekly) -> None:
    case = build_case(loop_env, weekly)
    first = run_pipeline(loop_env, weekly, case)
    second = run_pipeline(loop_env, weekly, case)
    assert second.delivery_id == first.delivery_id
    count = loop_env.ledger.connection.execute(
        "SELECT COUNT(*) AS n FROM deliveries"
    ).fetchone()["n"]
    assert count == 1


def test_missing_required_slot_stops_before_render(loop_env, weekly) -> None:
    extra = [MissingBinding(binding_status="missing", slot_id="risks", reason_code="no_material")]
    case = build_case(loop_env, weekly, drop_slots={"risks"}, extra_bindings=extra)
    task = case[0]
    pipeline = DeterministicRenderPipeline(loop_env.home, loop_env.store, loop_env.ledger)
    with pytest.raises(MissingRequiredSlotsError, match="risks"):
        run_pipeline(loop_env, weekly, case)
    assert loop_env.ledger.deliveries.get_by_task(TaskId(task.task_id)) is None
    _ = pipeline
