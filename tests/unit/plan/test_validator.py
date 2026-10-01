"""PlanValidator checks against the weekly-report fixture contract (P3).

Covers the v1.1 §2 boundary: no new slots, no dropped required slots, no
attribute tampering (kind/required/style_key), dependencies exist and are
acyclic; semantic refinement stays allowed.
"""

from __future__ import annotations

from typing import Any

from omas.domain.ids import TaskId, TemplateVersionId
from omas.domain.ir import ContentPlanIR, PlanSection
from omas.domain.template import TemplateContract
from omas.plan.validator import (
    PlannedContentPlan,
    PlannedSection,
    PlannedSlot,
    PlanValidator,
)

TASK = TaskId("task_" + "1" * 32)
TVER = TemplateVersionId("tver_" + "2" * 32)


def _slot_data(
    contract: TemplateContract, slot_id: str, **overrides: Any
) -> dict[str, Any]:
    spec = contract.slot(slot_id)
    assert spec is not None, f"fixture contract lacks slot {slot_id}"
    data: dict[str, Any] = {
        "slot_id": spec.slot_id,
        "kind": spec.kind,
        "required": spec.required,
        "style_key": spec.style_key,
        "semantic_requirement": spec.semantic_requirement,
        "preferred_source_kind": ["user"],
        "depends_on": [],
        "notes": None,
    }
    data.update(overrides)
    return data


def _make_plan(
    contract: TemplateContract,
    *,
    drop_slots: frozenset[str] = frozenset(),
    extra_sections: tuple[dict[str, Any], ...] = (),
    slot_overrides: dict[str, dict[str, Any]] | None = None,
) -> PlannedContentPlan:
    """A plan echoing the contract, with the requested deviations applied."""
    overrides = slot_overrides or {}
    sections: list[dict[str, Any]] = []
    for section in contract.sections:
        slots = [
            _slot_data(contract, slot_id, **overrides.get(slot_id, {}))
            for slot_id in section.slot_ids
            if slot_id not in drop_slots
        ]
        if slots:
            sections.append({"section_id": section.section_id, "slots": slots})
    sections.extend(extra_sections)
    return PlannedContentPlan(
        schema_version=1,
        task_id=TASK,
        template_version_id=TVER,
        sections=tuple(PlannedSection.model_validate(section) for section in sections),
    )


def _extra_section(contract: TemplateContract, section_id: str, slot: dict[str, Any]):
    return {"section_id": section_id, "slots": [slot]}


def test_valid_plan_passes(contract: TemplateContract) -> None:
    report = PlanValidator().validate(_make_plan(contract), contract)
    assert report.ok is True
    assert report.violations == ()


def test_semantic_refinement_is_allowed(contract: TemplateContract) -> None:
    plan = _make_plan(
        contract,
        slot_overrides={
            "sales_summary": {
                "semantic_requirement": "聚焦销售额环比变化与结构贡献",
                "preferred_source_kind": ["upstream"],
                "notes": "用户强调华东区",
                "depends_on": ["risks"],
            }
        },
    )
    report = PlanValidator().validate(plan, contract)
    assert report.ok is True
    assert report.violations == ()


def test_unknown_slot_is_rejected(contract: TemplateContract) -> None:
    rogue = _slot_data(contract, "sales_summary") | {"slot_id": "bonus"}
    plan = _make_plan(
        contract,
        extra_sections=(_extra_section(contract, "sales", rogue),),
    )
    report = PlanValidator().validate(plan, contract)
    assert report.ok is False
    assert "unknown slot: bonus" in report.violations


def test_unknown_section_is_rejected(contract: TemplateContract) -> None:
    plan = _make_plan(
        contract,
        drop_slots=frozenset({"sales_summary"}),
        extra_sections=(
            _extra_section(contract, "rogue", _slot_data(contract, "sales_summary")),
        ),
    )
    report = PlanValidator().validate(plan, contract)
    assert report.ok is False
    assert "unknown section: rogue" in report.violations
    # the slot itself is contract-known and required coverage still holds
    assert not [v for v in report.violations if v.startswith("unknown slot")]
    assert not [v for v in report.violations if v.startswith("required slot missing")]


def test_missing_required_slot_is_rejected(contract: TemplateContract) -> None:
    plan = _make_plan(contract, drop_slots=frozenset({"risks"}))
    report = PlanValidator().validate(plan, contract)
    assert report.ok is False
    assert "required slot missing from plan: risks" in report.violations


def test_kind_tampering_is_rejected(contract: TemplateContract) -> None:
    plan = _make_plan(contract, slot_overrides={"sales_summary": {"kind": "chart"}})
    report = PlanValidator().validate(plan, contract)
    assert report.ok is False
    assert (
        "slot sales_summary kind mismatch: plan=chart contract=text_block"
        in report.violations
    )


def test_required_tampering_is_rejected(contract: TemplateContract) -> None:
    plan = _make_plan(contract, slot_overrides={"risks": {"required": False}})
    report = PlanValidator().validate(plan, contract)
    assert report.ok is False
    assert (
        "slot risks required mismatch: plan=False contract=True" in report.violations
    )


def test_style_key_tampering_is_rejected(contract: TemplateContract) -> None:
    plan = _make_plan(contract, slot_overrides={"next_plan": {"style_key": "heading"}})
    report = PlanValidator().validate(plan, contract)
    assert report.ok is False
    assert (
        "slot next_plan style_key mismatch: plan=heading contract=body"
        in report.violations
    )


def test_depends_on_unknown_slot_is_rejected(contract: TemplateContract) -> None:
    plan = _make_plan(
        contract, slot_overrides={"sales_summary": {"depends_on": ["ghost"]}}
    )
    report = PlanValidator().validate(plan, contract)
    assert report.ok is False
    assert "slot sales_summary depends_on unknown slot: ghost" in report.violations


def test_depends_on_cycle_is_rejected(contract: TemplateContract) -> None:
    plan = _make_plan(
        contract,
        slot_overrides={
            "sales_summary": {"depends_on": ["risks"]},
            "risks": {"depends_on": ["sales_summary"]},
        },
    )
    report = PlanValidator().validate(plan, contract)
    assert report.ok is False
    cycle_lines = [v for v in report.violations if v.startswith("depends_on cycle: ")]
    assert len(cycle_lines) == 1
    assert "sales_summary" in cycle_lines[0] and "risks" in cycle_lines[0]
    assert cycle_lines[0].count("sales_summary") == 2  # a -> b -> a closed path


def test_depends_on_self_cycle_is_rejected(contract: TemplateContract) -> None:
    plan = _make_plan(contract, slot_overrides={"risks": {"depends_on": ["risks"]}})
    report = PlanValidator().validate(plan, contract)
    assert report.ok is False
    assert "depends_on cycle: risks -> risks" in report.violations


def test_plain_content_plan_ir_gets_structural_checks(contract: TemplateContract) -> None:
    """The persisted (ids-only) form cannot carry attributes to tamper with;
    membership and required-coverage checks still apply to it."""
    plan = ContentPlanIR(
        schema_version=1,
        task_id=TASK,
        template_version_id=TVER,
        sections=(
            PlanSection(section_id="sales", slot_ids=("sales_summary", "bonus")),
        ),
    )
    report = PlanValidator().validate(plan, contract)
    assert report.ok is False
    assert "unknown slot: bonus" in report.violations
    assert "required slot missing from plan: risks" in report.violations
    assert "required slot missing from plan: next_plan" in report.violations
    # attribute checks are vacuous on the plain form: no mismatch entries exist
    assert not [v for v in report.violations if "mismatch" in v]

    clean = ContentPlanIR(
        schema_version=1,
        task_id=TASK,
        template_version_id=TVER,
        sections=(
            PlanSection(section_id="sales", slot_ids=("sales_summary",)),
            PlanSection(section_id="risks", slot_ids=("risks",)),
            PlanSection(section_id="next_plan", slot_ids=("next_plan",)),
        ),
    )
    assert PlanValidator().validate(clean, contract).ok is True


def test_planned_section_derives_slot_ids_from_slots(contract: TemplateContract) -> None:
    section = PlannedSection.model_validate(
        {"section_id": "sales", "slots": [_slot_data(contract, "sales_summary")]}
    )
    assert section.slot_ids == ("sales_summary",)
    assert isinstance(section.slots[0], PlannedSlot)
    assert section.slots[0].style_key == "body"
