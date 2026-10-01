"""Domain contract invariants: discriminated bindings, gate semantics,
template contract validation, task transitions, artifact paths.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from omas.domain.artifact import Artifact, ArtifactKind
from omas.domain.findings import CheckResult, GateName, GateReport, GateStatus
from omas.domain.ir import (
    BindingIR,
    BoundBinding,
    ContentPlanIR,
    DocxRenderIR,
    MissingBinding,
    PlanSection,
    RenderSlot,
    SourceSpanRef,
    TemplateStaticRef,
)
from omas.domain.spans import SourceSpanRef as SpanRef
from omas.domain.task import (
    TASK_STATUS_TRANSITIONS,
    DataPolicy,
    Task,
    TaskStatus,
)
from omas.domain.template import (
    TemplateContract,
    TemplatePackageHashes,
)

NOW = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)
H64 = "a" * 64
ART1 = "art_" + "1" * 32
TASK1 = "task_" + "2" * 32
TVER1 = "tver_" + "3" * 32


def make_span(start: int = 0, end: int = 3) -> SpanRef:
    return SpanRef(
        artifact_id=ART1,
        canonical_sha256=H64,
        start=start,
        end=end,
        span_sha256=H64,
    )


# ------------------------------------------------------------------- bindings


def test_bound_binding_requires_refs() -> None:
    with pytest.raises(ValidationError):
        BoundBinding(
            binding_status="bound", slot_id="s1", producer="user", source_refs=()
        )


def test_missing_binding_has_no_producer_field() -> None:
    binding = MissingBinding(binding_status="missing", slot_id="s1", reason_code="no_material")
    assert "producer" not in type(binding).model_fields


def test_missing_binding_rejects_producer_injection() -> None:
    with pytest.raises(ValidationError, match="Extra inputs"):
        MissingBinding(
            binding_status="missing",
            slot_id="s1",
            reason_code="no_material",
            producer="user",
        )


def test_binding_union_discriminates() -> None:
    payload = {
        "binding_status": "bound",
        "slot_id": "s1",
        "producer": "user",
        "source_refs": [make_span().model_dump()],
    }
    ir = BindingIR(
        schema_version=1,
        task_id=TASK1,
        plan_artifact_id=ART1,
        template_version_id=TVER1,
        epoch=1,
        binding_version=1,
        material_set_digest=H64,
        bindings=[payload],  # type: ignore[list-item]
    )
    assert isinstance(ir.bindings[0], BoundBinding)


def test_binding_ir_rejects_duplicate_slots() -> None:
    with pytest.raises(ValidationError, match="duplicate"):
        BindingIR(
            schema_version=1,
            task_id=TASK1,
            plan_artifact_id=ART1,
            template_version_id=TVER1,
            epoch=1,
            binding_version=1,
            material_set_digest=H64,
            bindings=[
                MissingBinding(binding_status="missing", slot_id="s1", reason_code="no_material"),
                MissingBinding(binding_status="missing", slot_id="s1", reason_code="no_material"),
            ],
        )


def test_span_rejects_empty_and_inverted() -> None:
    with pytest.raises(ValidationError):
        SourceSpanRef(
            artifact_id=ART1, canonical_sha256=H64, start=2, end=2, span_sha256=H64
        )
    with pytest.raises(ValidationError):
        SourceSpanRef(
            artifact_id=ART1, canonical_sha256=H64, start=5, end=3, span_sha256=H64
        )


# ----------------------------------------------------------------------- gates


def make_report(
    gate: GateName,
    statuses: list[GateStatus],
    required: bool = True,
    candidate: str | None = H64,
) -> GateReport:
    return GateReport(
        gate=gate,
        candidate_sha256=candidate,
        render_ir_sha256=H64,
        template_version_id=TVER1,
        rule_version="1",
        results=[
            CheckResult(check_id=f"c{i}", rule_id="r", status=s, required=required)
            for i, s in enumerate(statuses)
        ],
    )


def test_all_pass_is_finalizable() -> None:
    report = make_report(GateName.PROVENANCE_GATE, [GateStatus.PASS, GateStatus.PASS])
    assert report.overall_status() is GateStatus.PASS
    assert report.is_finalizable()


def test_required_unknown_blocks_finalize() -> None:
    """T14: required unknown is NOT pass."""
    report = make_report(GateName.FORMAT_GATE, [GateStatus.PASS, GateStatus.UNKNOWN])
    assert report.overall_status() is GateStatus.UNKNOWN
    assert not report.is_finalizable()


def test_fail_blocks_even_with_unknown() -> None:
    report = make_report(GateName.FORMAT_GATE, [GateStatus.FAIL, GateStatus.UNKNOWN])
    assert report.overall_status() is GateStatus.FAIL


def test_optional_unknown_does_not_block() -> None:
    report = make_report(
        GateName.FORMAT_GATE, [GateStatus.PASS, GateStatus.UNKNOWN], required=False
    )
    assert report.overall_status() is GateStatus.PASS


def test_gate_b_requires_candidate_hash() -> None:
    with pytest.raises(ValidationError, match="candidate_sha256"):
        make_report(GateName.PROVENANCE_GATE, [GateStatus.PASS], candidate=None)


def test_gate_a_allows_missing_candidate_hash() -> None:
    report = make_report(GateName.PROVENANCE_PRECHECK, [GateStatus.PASS], candidate=None)
    assert report.is_finalizable()


# ------------------------------------------------------------------- templates


def make_contract(**overrides: object) -> TemplateContract:
    data: dict[str, object] = {
        "schema_version": 1,
        "template_id": "weekly-report",
        "version": 1,
        "extractor_version": "0.1",
        "hashes": TemplatePackageHashes(
            docx_sha256=H64,
            contract_sha256=H64,
            styles_sha256=H64,
            static_map_sha256=H64,
        ),
        "sections": [{"section_id": "sec1", "slot_ids": ["sales_summary"]}],
        "slots": [
            {
                "slot_id": "sales_summary",
                "placeholder": "{{ sales_summary }}",
                "kind": "text_block",
                "required": True,
                "semantic_requirement": "本周销售情况",
                "style_key": "body",
            }
        ],
    }
    data.update(overrides)
    return TemplateContract.model_validate(data)


def test_contract_accepts_sidecar_shape() -> None:
    contract = make_contract()
    assert contract.slot_ids() == ("sales_summary",)
    assert contract.is_activatable()


def test_contract_rejects_unknown_slot_reference() -> None:
    with pytest.raises(ValidationError, match="unknown slots"):
        make_contract(sections=[{"section_id": "sec1", "slot_ids": ["ghost"]}])
    # a slot declared but covered by no section is equally invalid
    with pytest.raises(ValidationError, match="not covered"):
        make_contract(
            slots=[
                {
                    "slot_id": "sales_summary",
                    "placeholder": "{{ sales_summary }}",
                    "kind": "text_block",
                    "required": True,
                    "semantic_requirement": "本周销售情况",
                    "style_key": "body",
                },
                {
                    "slot_id": "orphan_slot",
                    "placeholder": "{{ orphan_slot }}",
                    "kind": "text_block",
                    "required": False,
                    "semantic_requirement": "x",
                    "style_key": "body",
                },
            ],
            sections=[{"section_id": "sec1", "slot_ids": ["sales_summary"]}],
        )


def test_contract_rejects_bad_placeholder() -> None:
    with pytest.raises(ValidationError, match="placeholder"):
        make_contract(
            slots=[
                {
                    "slot_id": "s",
                    "placeholder": "{{ sales_summary | upper }}",
                    "kind": "text_block",
                    "required": True,
                    "semantic_requirement": "x",
                    "style_key": "body",
                }
            ],
            sections=[{"section_id": "sec1", "slot_ids": ["s"]}],
        )


def test_unsupported_findings_block_activation() -> None:
    contract = make_contract(
        unsupported_findings=[{"code": "split_run_placeholder", "locator": "doc:p3"}]
    )
    assert not contract.is_activatable()


def test_duplicate_placeholders_rejected() -> None:
    slot = {
        "slot_id": "s",
        "placeholder": "{{ x }}",
        "kind": "text_block",
        "required": True,
        "semantic_requirement": "x",
        "style_key": "body",
    }
    with pytest.raises(ValidationError, match="placeholder"):
        make_contract(
            slots=[slot, {**slot, "slot_id": "s2"}],
            sections=[{"section_id": "sec1", "slot_ids": ["s", "s2"]}],
        )


# ----------------------------------------------------------------------- tasks


def make_task(**overrides: object) -> Task:
    data: dict[str, object] = {
        "task_id": TASK1,
        "request_id": "req-1",
        "data_policy": DataPolicy.LOCAL_ONLY,
        "created_at": NOW,
        "updated_at": NOW,
    }
    data.update(overrides)
    return Task.model_validate(data)


def test_task_is_frozen() -> None:
    task = make_task()
    with pytest.raises(ValidationError):
        task.status = TaskStatus.RUNNING  # type: ignore[misc]


def test_task_rejects_naive_datetime() -> None:
    with pytest.raises(ValidationError, match="timezone-aware"):
        make_task(created_at=datetime(2026, 9, 30, 12, 0))


def test_task_terminal_states_have_no_exits() -> None:
    for status in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED):
        assert TASK_STATUS_TRANSITIONS[status] == frozenset()


def test_task_epoch_must_be_positive() -> None:
    with pytest.raises(ValidationError):
        make_task(epoch=0)


# ------------------------------------------------------------------- artifacts


def make_artifact(**overrides: object) -> Artifact:
    data: dict[str, object] = {
        "artifact_id": ART1,
        "task_id": TASK1,
        "kind": ArtifactKind.CANONICAL_TEXT,
        "relative_path": "tasks/x/canonical/y.txt",
        "sha256": H64,
        "size": 10,
        "created_at": NOW,
    }
    data.update(overrides)
    return Artifact.model_validate(data)


@pytest.mark.parametrize(
    "bad_path",
    [
        "/etc/passwd",
        "tasks/../../etc/passwd",
        "tasks\\x.txt",
        "tasks/./x.txt",
        "",
    ],
)
def test_artifact_rejects_bad_paths(bad_path: str) -> None:
    with pytest.raises(ValidationError):
        make_artifact(relative_path=bad_path)


def test_artifact_rejects_bad_sha() -> None:
    with pytest.raises(ValidationError):
        make_artifact(sha256="XYZ")


def test_artifact_extra_fields_rejected() -> None:
    with pytest.raises(ValidationError, match="Extra inputs"):
        make_artifact(text="llm should not be able to add free text here")


# ------------------------------------------------------------------ render IR


def test_render_ir_has_no_text_fields() -> None:
    """I2 foundation: the render IR schema simply has no slot for free text."""
    DocxRenderIR(
        schema_version=1,
        task_id=TASK1,
        epoch=1,
        template_version_id=TVER1,
        template_docx_artifact_id=ART1,
        plan_artifact_id=ART1,
        binding_artifact_id=ART1,
        static_regions=[TemplateStaticRef(template_version_id=TVER1, region_id="header")],
        slots=[RenderSlot(slot_id="s1", spans=(make_span(),), style_key="body")],
    )
    assert "text" not in RenderSlot.model_fields
    with pytest.raises(ValidationError, match="Extra inputs"):
        RenderSlot.model_validate(
            {
                "slot_id": "s1",
                "spans": [make_span().model_dump()],
                "style_key": "body",
                "text": "模型自己写的一句话",
            }
        )


def test_plan_ir_rejects_duplicate_slots() -> None:
    with pytest.raises(ValidationError, match="duplicate"):
        ContentPlanIR(
            schema_version=1,
            task_id=TASK1,
            template_version_id=TVER1,
            sections=[
                PlanSection(section_id="a", slot_ids=("s1",)),
                PlanSection(section_id="b", slot_ids=("s1",)),
            ],
        )
