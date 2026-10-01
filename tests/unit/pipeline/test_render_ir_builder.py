"""RenderIRBuilder tests: override handling and gap refusal (I6)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from omas.core import resolve_span
from omas.domain.errors import MissingRequiredSlotsError
from omas.domain.ids import ArtifactId, TaskId
from omas.domain.ir import BindingIR, BoundBinding, MissingBinding
from omas.pipeline import RenderIRBuilder
from tests.unit.conftest import H64, ingest_material, make_task, register_template

MATERIAL = "销售情况良好。风险可控。计划推进顺利。"


def _binding(task_id: str, artifact_id: str, digest: str, slots) -> BindingIR:
    return BindingIR(
        schema_version=1,
        task_id=TaskId(task_id),
        plan_artifact_id=ArtifactId(artifact_id),
        template_version_id=task_id and _version(),
        epoch=1,
        binding_version=1,
        material_set_digest=digest,
        bindings=slots,
    )


def _version() -> str:
    return _cached_version[0]


_cached_version: list[str] = ["tver_" + "1" * 32]


def _contract_dict():
    return {
        "schema_version": 1,
        "template_id": "weekly-report",
        "version": 1,
        "extractor_version": "test-1",
        "hashes": {
            "docx_sha256": H64,
            "contract_sha256": "b" * 64,
            "styles_sha256": "c" * 64,
            "static_map_sha256": "d" * 64,
        },
        "sections": [{"section_id": "s1", "slot_ids": ["sales_summary", "risks"]}],
        "slots": [
            {
                "slot_id": "sales_summary",
                "placeholder": "{{ sales_summary }}",
                "kind": "text_block",
                "required": True,
                "semantic_requirement": "本周销售情况",
                "style_key": "body",
            },
            {
                "slot_id": "risks",
                "placeholder": "{{ risks }}",
                "kind": "text_block",
                "required": True,
                "allow_user_omit": True,
                "semantic_requirement": "风险与依赖",
                "style_key": "body",
            },
        ],
        "static_regions": [
            {
                "region_id": "title",
                "part": "document",
                "locator": "para:0",
                "text_sha256": "e" * 64,
                "token_count": 3,
            }
        ],
    }


def _contract():
    from omas.domain.template import TemplateContract

    return TemplateContract.model_validate(_contract_dict())


@pytest.fixture()
def setup(env):
    task = make_task(env)
    version_id = register_template(env)
    task = env.ledger.tasks.bind_template(TaskId(task.task_id), version_id)
    _cached_version[0] = version_id
    artifact, canonical = ingest_material(env, task.task_id, MATERIAL)
    span = resolve_span(artifact.artifact_id, canonical, 0, 4)
    return env, task, artifact, span


def test_builder_rejects_missing_required_without_override(setup) -> None:
    env, task, artifact, span = setup
    binding = _binding(
        task.task_id,
        artifact.artifact_id,
        H64,
        [
            BoundBinding(
                binding_status="bound", slot_id="sales_summary", producer="user",
                source_refs=(span,),
            ),
            MissingBinding(binding_status="missing", slot_id="risks", reason_code="no_material"),
        ],
    )
    with pytest.raises(MissingRequiredSlotsError, match="risks"):
        RenderIRBuilder(env.ledger).build(
            task_id=TaskId(task.task_id),
            epoch=task.epoch,
            contract=_contract(),
            template_version_id=task.template_version_id or "",
            template_docx_artifact_id=artifact.artifact_id,
            binding=binding,
            plan_artifact_id=artifact.artifact_id,
            binding_artifact_id=artifact.artifact_id,
        )


def test_builder_honors_recorded_override(setup) -> None:
    env, task, artifact, span = setup
    from omas.core.digest import payload_digest
    from omas.domain.decisions import AwaitingEvent, Decision, SlotOverride
    from omas.domain.ids import AwaitingEventId, DecisionId, new_awaiting_event_id, new_decision_id

    awaiting = AwaitingEvent(
        awaiting_event_id=new_awaiting_event_id(),
        task_id=TaskId(task.task_id),
        epoch=task.epoch,
        missing_slot_ids=("risks",),
        created_at=datetime.now(UTC),
    )
    env.ledger.awaiting_events.create(awaiting)
    decision = Decision(
        decision_id=new_decision_id(),
        task_id=TaskId(task.task_id),
        expected_epoch=task.epoch,
        awaiting_event_id=AwaitingEventId(awaiting.awaiting_event_id),
        action="omit_slot",
        payload_digest=payload_digest("omit", "risks"),
        created_at=datetime.now(UTC),
    )
    env.ledger.decisions.accept(decision)
    override = SlotOverride(
        task_id=TaskId(task.task_id),
        slot_id="risks",
        decision_id=DecisionId(decision.decision_id),
        epoch=task.epoch,
        reason_artifact_id=artifact.artifact_id,
    )
    env.ledger.slot_overrides.insert(f"ovr_{decision.decision_id.removeprefix('dec_')}", override)

    binding = _binding(
        task.task_id,
        artifact.artifact_id,
        H64,
        [
            BoundBinding(
                binding_status="bound", slot_id="sales_summary", producer="user",
                source_refs=(span,),
            ),
            MissingBinding(binding_status="missing", slot_id="risks", reason_code="no_material"),
        ],
    )
    ir = RenderIRBuilder(env.ledger).build(
        task_id=TaskId(task.task_id),
        epoch=task.epoch,
        contract=_contract(),
        template_version_id=task.template_version_id or "",
        template_docx_artifact_id=artifact.artifact_id,
        binding=binding,
        plan_artifact_id=artifact.artifact_id,
        binding_artifact_id=artifact.artifact_id,
    )
    risks = next(s for s in ir.slots if s.slot_id == "risks")
    assert risks.spans == ()
    assert risks.override_artifact_id == artifact.artifact_id
    sales = next(s for s in ir.slots if s.slot_id == "sales_summary")
    assert sales.spans == (span,)
    assert {r.region_id for r in ir.static_regions} == {"title"}
