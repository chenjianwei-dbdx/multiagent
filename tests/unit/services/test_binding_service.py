"""BindingService: the program's BindingIR write path (I3), fully offline.

Environment follows tests/unit/tools/test_materials.py: a real Ledger +
ArtifactStore in tmp_path (``env`` fixture), two tasks with canonical Chinese
materials, a three-slot contract, and span handles issued for real through
MaterialToolkit.resolve_span — the only legitimate signing path.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pytest

from omas.agents.assembler import AssemblerProposal, SlotProposal
from omas.artifacts.writers import InboxWriter
from omas.core.digest import material_set_digest
from omas.domain.artifact import Artifact, ArtifactKind
from omas.domain.ids import ArtifactId, TaskId, TemplateVersionId, new_id
from omas.domain.ir import BindingIR, ContentPlanIR, PlanSection
from omas.domain.task import Task, TaskStatus
from omas.services.binding_service import BindingRejectedError, BindingService
from omas.tools.materials import MaterialToolkit, RunContext
from tests.unit.conftest import EnvBox, make_task

H64 = "a" * 64
TVER = TemplateVersionId("tver_" + "1" * 32)
PLAN_ARTIFACT = ArtifactId("art_" + "2" * 32)

SALES = (
    "销售周报\n"
    "本周销售额为一百二十三万元，同比增长百分之八。\n"
    "华东地区销售额贡献最大。\n"
    "下周计划：跟进重点客户。"
)
MINUTES = "会议纪要\n今日会议讨论了采购预算。\n华东地区物流成本上升。"
OTHER_TASK_MATERIAL = "另一个任务的机密材料：华东大区专用，禁止跨任务绑定。"


def _contract() -> Any:
    from omas.domain.template import (
        PlanSectionTemplate,
        SlotSpec,
        TemplateContract,
        TemplatePackageHashes,
    )

    return TemplateContract(
        template_id="weekly-report",
        version=1,
        extractor_version="test-1",
        hashes=TemplatePackageHashes(
            docx_sha256=H64,
            contract_sha256=H64,
            styles_sha256=H64,
            static_map_sha256=H64,
        ),
        sections=(
            PlanSectionTemplate(
                section_id="body",
                title="正文",
                slot_ids=("sales_summary", "next_plan", "optional_note"),
            ),
        ),
        slots=(
            SlotSpec(
                slot_id="sales_summary",
                placeholder="{{ sales_summary }}",
                kind="text_block",
                required=True,
                semantic_requirement="本周销售情况",
                style_key="body",
            ),
            SlotSpec(
                slot_id="next_plan",
                placeholder="{{ next_plan }}",
                kind="text_block",
                required=True,
                semantic_requirement="下周计划",
                style_key="body",
            ),
            SlotSpec(
                slot_id="optional_note",
                placeholder="{{ optional_note }}",
                kind="text_block",
                required=False,
                semantic_requirement="备注",
                style_key="body",
            ),
        ),
    )


def _ingest_canonical(
    env: EnvBox, task_id: str, text: str, name: str
) -> tuple[Artifact, str]:
    _raw, cfile, canonical = InboxWriter(env.store).ingest_text(
        task_id, name, text.encode("utf-8")
    )
    artifact = env.ledger.artifacts.register(
        Artifact(
            artifact_id=ArtifactId(cfile.name.removesuffix(".txt")),
            task_id=TaskId(task_id),
            kind=ArtifactKind.CANONICAL_TEXT,
            relative_path=cfile.relative_path,
            sha256=canonical.sha256,
            size=canonical.size_bytes,
            source_refs=(),
            created_at=datetime.now(UTC),
        )
    )
    return artifact, canonical.text


@dataclass
class Setup:
    env: EnvBox
    task: Task
    other_task: Task
    contract: Any
    toolkit: MaterialToolkit
    sales: Artifact
    sales_text: str
    minutes: Artifact
    minutes_text: str
    other_material: Artifact
    service: BindingService


@pytest.fixture()
def setup(env: EnvBox) -> Setup:
    task = env.ledger.tasks.bind_template(make_task(env).task_id, TVER)
    sales, sales_text = _ingest_canonical(env, task.task_id, SALES, "sales.md")
    minutes, minutes_text = _ingest_canonical(env, task.task_id, MINUTES, "minutes.md")
    other_task = make_task(env, request_id="req-2", payload="p2")
    other_material, _other_text = _ingest_canonical(
        env, other_task.task_id, OTHER_TASK_MATERIAL, "other.md"
    )
    toolkit = MaterialToolkit(
        env.ledger, env.store, RunContext(TaskId(task.task_id), epoch=task.epoch)
    )
    return Setup(
        env=env,
        task=task,
        other_task=other_task,
        contract=_contract(),
        toolkit=toolkit,
        sales=sales,
        sales_text=sales_text,
        minutes=minutes,
        minutes_text=minutes_text,
        other_material=other_material,
        service=BindingService(env.ledger, env.store),
    )


def _handle(setup: Setup, artifact: Artifact, text: str, needle: str, length: int) -> str:
    start = text.index(needle)
    return setup.toolkit.resolve_span(
        artifact.artifact_id, start, start + length
    ).span_handle


def _proposal(*slots: SlotProposal) -> AssemblerProposal:
    return AssemblerProposal(slots=slots)


def _commit(setup: Setup, proposal: AssemblerProposal, **kwargs: Any) -> tuple[BindingIR, Artifact]:
    return setup.service.commit_proposal(
        task=setup.task,
        proposal=proposal,
        plan=kwargs.get("plan"),
        plan_artifact_id=kwargs.get("plan_artifact_id", PLAN_ARTIFACT),
        contract=setup.contract,
    )


def _binding_rows(setup: Setup) -> list[Any]:
    return setup.env.ledger.connection.execute(
        "SELECT * FROM bindings WHERE task_id = ? ORDER BY slot_id",
        (setup.task.task_id,),
    ).fetchall()


def _events(setup: Setup) -> list[str]:
    return [
        event.event_code
        for event in setup.env.ledger.events.list_after(
            TaskId(setup.task.task_id), 0
        )
    ]


# ------------------------------------------------------------- happy path


def test_commit_valid_proposal_persists_ir_and_rows(setup: Setup) -> None:
    first = _handle(setup, setup.sales, setup.sales_text, "本周销售额", 6)
    second = _handle(setup, setup.minutes, setup.minutes_text, "华东地区", 4)
    proposal = _proposal(
        SlotProposal(
            slot_id="sales_summary", binding_status="bound", span_handles=(first,)
        ),
        SlotProposal(
            slot_id="next_plan", binding_status="bound", span_handles=(first, second)
        ),
        SlotProposal(
            slot_id="optional_note", binding_status="missing", reason_code="no_material"
        ),
    )

    ir, artifact = _commit(setup, proposal)

    # IR facts
    assert ir.task_id == setup.task.task_id
    assert ir.template_version_id == TVER
    assert ir.epoch == 1
    assert ir.binding_version == 1
    assert ir.plan_artifact_id == PLAN_ARTIFACT
    sales_binding = ir.binding_for("sales_summary")
    assert sales_binding is not None and sales_binding.binding_status == "bound"
    # producer is program-decided (MVP: user); span order is proposal order
    assert sales_binding.producer == "user"
    assert len(sales_binding.source_refs) == 1
    plan_binding = ir.binding_for("next_plan")
    assert plan_binding is not None and plan_binding.binding_status == "bound"
    assert len(plan_binding.source_refs) == 2
    assert plan_binding.source_refs[0].artifact_id == setup.sales.artifact_id
    assert plan_binding.source_refs[1].artifact_id == setup.minutes.artifact_id
    note_binding = ir.binding_for("optional_note")
    assert note_binding is not None and note_binding.binding_status == "missing"

    # material set digest binds the IR to the full canonical material set
    assert ir.material_set_digest == material_set_digest(
        [
            (setup.sales.artifact_id, setup.sales.sha256),
            (setup.minutes.artifact_id, setup.minutes.sha256),
        ]
    )

    # artifact on disk under nodes/run_assemble/out, content == IR JSON
    assert artifact.kind is ArtifactKind.BINDING_IR
    assert "/nodes/run_assemble/out/" in artifact.relative_path
    on_disk = setup.env.store.read(artifact.relative_path)
    assert json.loads(on_disk) == json.loads(ir.model_dump_json())
    registered = setup.env.ledger.artifacts.get_by_path(artifact.relative_path)
    assert registered is not None and registered.artifact_id == artifact.artifact_id

    # per-slot ledger rows (task, epoch, version, slot unique)
    rows = _binding_rows(setup)
    assert len(rows) == 3
    by_slot = {row["slot_id"]: row for row in rows}
    assert by_slot["sales_summary"]["binding_status"] == "bound"
    assert by_slot["sales_summary"]["producer"] == "user"
    refs = json.loads(by_slot["sales_summary"]["source_refs_json"])
    assert refs[0]["artifact_id"] == setup.sales.artifact_id
    assert by_slot["optional_note"]["binding_status"] == "missing"
    assert by_slot["optional_note"]["producer"] is None
    assert by_slot["optional_note"]["source_refs_json"] == "[]"

    # active ref + audit event
    fresh = setup.env.ledger.tasks.get(TaskId(setup.task.task_id))
    assert fresh is not None
    assert fresh.active_binding_artifact_id == artifact.artifact_id
    assert "binding_committed" in _events(setup)


def test_commit_is_replay_safe_for_identical_ir(setup: Setup) -> None:
    first = _handle(setup, setup.sales, setup.sales_text, "本周销售额", 6)
    proposal = _proposal(
        SlotProposal(
            slot_id="sales_summary", binding_status="bound", span_handles=(first,)
        ),
        SlotProposal(
            slot_id="next_plan", binding_status="missing", reason_code="ambiguous"
        ),
    )
    _ir1, artifact1 = _commit(setup, proposal)
    # a second commit of the same proposal yields a NEW binding_version (2)
    # with its own artifact; history is retained, nothing overwritten
    ir2, artifact2 = _commit(setup, proposal)
    assert ir2.binding_version == 2
    assert artifact2.artifact_id != artifact1.artifact_id
    assert setup.env.store.exists(artifact1.relative_path)
    assert setup.env.store.exists(artifact2.relative_path)
    versions = {
        row["binding_version"] for row in _binding_rows(setup)
    }
    assert versions == {1, 2}


# ------------------------------------------------------------- handle forgery


def test_forged_handle_is_rejected(setup: Setup) -> None:
    forged = "span_" + "f" * 32  # well-formed shape, never issued
    proposal = _proposal(
        SlotProposal(
            slot_id="sales_summary", binding_status="bound", span_handles=(forged,)
        ),
        SlotProposal(
            slot_id="next_plan", binding_status="missing", reason_code="no_material"
        ),
    )
    with pytest.raises(BindingRejectedError) as excinfo:
        _commit(setup, proposal)
    assert excinfo.value.code == "BINDING_REJECTED"
    # rejected before any write: no artifact, no rows, no event
    assert _binding_rows(setup) == []
    assert "binding_committed" not in _events(setup)
    assert (
        setup.env.ledger.connection.execute(
            "SELECT COUNT(*) AS n FROM artifacts WHERE kind = 'binding_ir'"
        ).fetchone()["n"]
        == 0
    )


def test_cross_task_handle_is_rejected(setup: Setup) -> None:
    # a REAL handle — but issued against another task's material
    other_text = OTHER_TASK_MATERIAL
    other_toolkit = MaterialToolkit(
        setup.env.ledger,
        setup.env.store,
        RunContext(TaskId(setup.other_task.task_id), epoch=setup.other_task.epoch),
    )
    start = other_text.index("机密材料")
    foreign = other_toolkit.resolve_span(
        setup.other_material.artifact_id, start, start + 4
    ).span_handle

    proposal = _proposal(
        SlotProposal(
            slot_id="sales_summary", binding_status="bound", span_handles=(foreign,)
        ),
        SlotProposal(
            slot_id="next_plan", binding_status="missing", reason_code="no_material"
        ),
    )
    with pytest.raises(BindingRejectedError, match="another task"):
        _commit(setup, proposal)
    assert _binding_rows(setup) == []


def test_tampered_material_fails_verification(setup: Setup) -> None:
    first = _handle(setup, setup.sales, setup.sales_text, "本周销售额", 6)
    # tamper the material file after the handle was issued (T03)
    path = setup.env.store.resolve_path(setup.sales.relative_path)
    path.write_bytes("篡改后的正文".encode("utf-8"))
    proposal = _proposal(
        SlotProposal(
            slot_id="sales_summary", binding_status="bound", span_handles=(first,)
        ),
        SlotProposal(
            slot_id="next_plan", binding_status="missing", reason_code="no_material"
        ),
    )
    with pytest.raises(BindingRejectedError) as excinfo:
        _commit(setup, proposal)
    assert excinfo.value.code == "BINDING_REJECTED"
    assert _binding_rows(setup) == []


# ------------------------------------------------- shape re-validation (service)


def test_missing_reason_mapping_is_re_enforced(setup: Setup) -> None:
    """The Assembler layer already rejects this; the service re-validates so a
    hand-built proposal cannot smuggle an unknown reason through."""
    proposal = _proposal(
        SlotProposal(
            slot_id="sales_summary",
            binding_status="missing",
            reason_code="made_up_reason",
        ),
        SlotProposal(
            slot_id="next_plan", binding_status="missing", reason_code="no_material"
        ),
    )
    with pytest.raises(BindingRejectedError, match="invalid reason"):
        _commit(setup, proposal)


def test_invalid_slot_requires_error_code(setup: Setup) -> None:
    proposal = _proposal(
        SlotProposal(
            slot_id="sales_summary", binding_status="invalid", error_code="SPAN_ILLEGAL"
        ),
        SlotProposal(
            slot_id="next_plan", binding_status="missing", reason_code="no_material"
        ),
    )
    ir, _artifact = _commit(setup, proposal)
    invalid = ir.binding_for("sales_summary")
    assert invalid is not None and invalid.binding_status == "invalid"
    row = {r["slot_id"]: r for r in _binding_rows(setup)}["sales_summary"]
    assert row["binding_status"] == "invalid"
    assert row["producer"] is None  # only bound rows carry a producer

    # an invalid slot without error_code is refused (Assembler would have
    # repaired it; the service does not trust that it did)
    bad = _proposal(
        SlotProposal(slot_id="sales_summary", binding_status="invalid")
    )
    with pytest.raises(BindingRejectedError, match="error_code"):
        _commit(setup, bad)


def test_proposal_cannot_carry_producer_viz(setup: Setup) -> None:
    """The DTO is structurally incapable of expressing a producer at all:
    `viz` can never enter through a proposal (MVP profile, v1.1 §4.1)."""
    fields = SlotProposal.model_fields
    assert "producer" not in fields
    assert not {"text", "summary", "content"} & set(fields)
    with pytest.raises(ValueError):
        SlotProposal(
            slot_id="sales_summary",
            binding_status="bound",
            span_handles=("span_" + "0" * 32,),
            producer="viz",  # type: ignore[call-arg]
        )


def test_slot_outside_contract_is_rejected(setup: Setup) -> None:
    proposal = _proposal(
        SlotProposal(slot_id="exec_summary", binding_status="missing", reason_code="no_material")
    )
    with pytest.raises(BindingRejectedError, match="outside the contract"):
        _commit(setup, proposal)


# --------------------------------------------------------- required-slot gaps


def test_unmentioned_required_slot_becomes_missing_entry(setup: Setup) -> None:
    """A proposal that never mentions a required slot still commits; the
    service completes it with missing(no_material) so gap_check (and only
    gap_check) decides what happens next. Optional slots are NOT added."""
    first = _handle(setup, setup.sales, setup.sales_text, "本周销售额", 6)
    proposal = _proposal(
        SlotProposal(
            slot_id="sales_summary", binding_status="bound", span_handles=(first,)
        )
    )
    ir, _artifact = _commit(setup, proposal)
    gap = ir.binding_for("next_plan")
    assert gap is not None
    assert gap.binding_status == "missing"
    assert gap.reason_code == "no_material"
    # optional slot stays absent — the IR reflects reality, not padding
    assert ir.binding_for("optional_note") is None
    assert len(ir.bindings) == 2


# ----------------------------------------------------------- task state rules


def test_terminal_task_is_rejected(setup: Setup) -> None:
    setup.env.ledger.tasks.update_status(
        TaskId(setup.task.task_id), TaskStatus.RUNNING, expected=TaskStatus.CREATED
    )
    setup.env.ledger.tasks.update_status(TaskId(setup.task.task_id), TaskStatus.COMPLETED)
    proposal = _proposal(
        SlotProposal(
            slot_id="sales_summary", binding_status="missing", reason_code="no_material"
        ),
        SlotProposal(
            slot_id="next_plan", binding_status="missing", reason_code="no_material"
        ),
    )
    with pytest.raises(BindingRejectedError, match="terminal"):
        _commit(setup, proposal)


def test_stale_epoch_proposal_is_rejected(setup: Setup) -> None:
    # the ledger moved on (re-supply bumped the epoch); the caller's Task
    # snapshot — and with it the proposal's epoch — is stale
    setup.env.ledger.tasks.advance_epoch(TaskId(setup.task.task_id), 1)
    fresh = setup.env.ledger.tasks.get(TaskId(setup.task.task_id))
    assert fresh is not None
    proposal = _proposal(
        SlotProposal(
            slot_id="sales_summary", binding_status="missing", reason_code="no_material"
        ),
        SlotProposal(
            slot_id="next_plan", binding_status="missing", reason_code="no_material"
        ),
    )
    with pytest.raises(BindingRejectedError, match="stale"):
        _commit(setup, proposal)  # setup.task still carries epoch 1


# -------------------------------------------------------------- plan checks


def test_plan_consistency_is_enforced(setup: Setup) -> None:
    first = _handle(setup, setup.sales, setup.sales_text, "本周销售额", 6)
    proposal = _proposal(
        SlotProposal(
            slot_id="sales_summary", binding_status="bound", span_handles=(first,)
        ),
        SlotProposal(
            slot_id="next_plan", binding_status="missing", reason_code="no_material"
        ),
    )
    other_task_plan = ContentPlanIR(
        schema_version=1,
        task_id=TaskId(setup.other_task.task_id),
        template_version_id=TVER,
        sections=(PlanSection(section_id="body", slot_ids=("sales_summary",)),),
    )
    with pytest.raises(BindingRejectedError, match="does not match"):
        _commit(setup, proposal, plan=other_task_plan)

    wrong_version = ContentPlanIR(
        schema_version=1,
        task_id=TaskId(setup.task.task_id),
        template_version_id=TemplateVersionId("tver_" + "9" * 32),
        sections=(PlanSection(section_id="body", slot_ids=("sales_summary",)),),
    )
    with pytest.raises(BindingRejectedError, match="template version"):
        _commit(setup, proposal, plan=wrong_version)

    # a consistent plan passes through and is recorded via plan_artifact_id
    good_plan = ContentPlanIR(
        schema_version=1,
        task_id=TaskId(setup.task.task_id),
        template_version_id=TVER,
        sections=(PlanSection(section_id="body", slot_ids=("sales_summary",)),),
    )
    plan_artifact = ArtifactId(new_id("art"))
    ir, _artifact = _commit(
        setup, proposal, plan=good_plan, plan_artifact_id=plan_artifact
    )
    assert ir.plan_artifact_id == plan_artifact
