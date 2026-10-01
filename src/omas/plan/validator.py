"""PlanValidator — the programmatic safety boundary for Planner output (P3).

Master §6 / v1.1 §2: the Planner may only map user intent onto the section and
slot set that the bound :class:`~omas.domain.template.TemplateContract` already
declares. It must not invent slots, drop required slots, or alter
``required`` / ``kind`` / ``style_key``; it *may* refine the semantic fields
(``semantic_requirement`` / ``preferred_source_kind`` / ``notes``) and express
ordering via ``depends_on``.

This module is pure code — no LLM, no I/O. The persisted
:class:`~omas.domain.ir.ContentPlanIR` carries only section structure
(``section_id`` + ``slot_ids``); the per-slot detail the Planner proposes lives
in :class:`PlannedSlot` / :class:`PlannedSection` /
:class:`PlannedContentPlan` below, so the validator can prove attribute
tampering *at the trust boundary* (the model's output form). A plain
``ContentPlanIR`` without slot detail is accepted too — the attribute and
dependency checks are then vacuous because that form simply cannot express
them; it cannot smuggle them past the renderer either (its DTO has no such
fields, ``extra='forbid'``).

Which contract a ``template_version_id`` resolves to is the caller's
responsibility (the Service resolves version id -> contract); the validator
checks the plan against the contract it is handed.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from pydantic import ConfigDict, Field, model_validator

from omas.domain.ir import ContentPlanIR, PlanSection, PlanSlot
from omas.domain.template import TemplateContract

__all__ = [
    "PlanValidationReport",
    "PlanValidator",
    "PlannedContentPlan",
    "PlannedSection",
    "PlannedSlot",
]


@dataclass(frozen=True, slots=True)
class PlanValidationReport:
    """Outcome of validating one plan against one contract.

    ``violations`` is a human-readable list (slot ids, attribute names) and
    never contains body text or raw model output.
    """

    ok: bool
    violations: tuple[str, ...]


class PlannedSlot(PlanSlot):
    """A :class:`~omas.domain.ir.PlanSlot` plus the ``style_key`` echo.

    The domain ``PlanSlot`` carries kind/required but not style_key, because
    the persisted IR never stores slot attributes at all. While a plan is
    being produced, the Planner must echo every contract-owned attribute —
    ``kind``, ``required``, ``style_key`` — so the validator can compare them
    against the :class:`~omas.domain.template.SlotSpec` and detect tampering.
    The semantic fields may differ (refinement is allowed).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    style_key: str = Field(min_length=1)


class PlannedSection(PlanSection):
    """A :class:`~omas.domain.ir.PlanSection` carrying full slot proposals.

    ``slot_ids`` is derived from ``slots`` when not supplied explicitly; when
    both are supplied they must agree, otherwise construction fails (which the
    Planner's repair loop treats like any other invalid output).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    slots: tuple[PlannedSlot, ...] = Field(min_length=1)

    @model_validator(mode="before")
    @classmethod
    def _derive_slot_ids(cls, data: Any) -> Any:
        if isinstance(data, dict) and "slots" in data and "slot_ids" not in data:
            ids: list[str] = []
            for slot in data["slots"]:
                if isinstance(slot, dict):
                    slot_id = slot.get("slot_id")
                else:
                    slot_id = getattr(slot, "slot_id", None)
                ids.append(slot_id if isinstance(slot_id, str) else "")
            data = {**data, "slot_ids": ids}
        return data

    @model_validator(mode="after")
    def _check_ids_match_slots(self) -> PlannedSection:
        if tuple(slot.slot_id for slot in self.slots) != self.slot_ids:
            raise ValueError(
                f"slot_ids must list the planned slots in order for section {self.section_id}"
            )
        return self


class PlannedContentPlan(ContentPlanIR):
    """A :class:`~omas.domain.ir.ContentPlanIR` that still carries slot detail.

    isinstance-compatible with ``ContentPlanIR`` (same fields plus
    ``PlannedSection.slots``), so :meth:`PlanValidator.validate` accepts both
    the rich planner form and the plain persisted IR.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    sections: tuple[PlannedSection, ...] = Field(min_length=1)


class PlanValidator:
    """Checks a plan against a template contract (v1.1 §2, §6).

    Rules, in order:

    1. Membership — every ``section_id`` / ``slot_id`` the plan references
       exists in the contract ("unknown section/slot: X").
    2. Coverage — every contract slot with ``required=True`` is referenced by
       the plan ("required slot missing from plan: X").
    3. Attribute integrity — for every slot the plan describes in detail
       (:class:`PlannedSlot`), ``kind`` / ``required`` / ``style_key`` must
       equal the contract's :class:`~omas.domain.template.SlotSpec`.
       ``semantic_requirement`` / ``preferred_source_kind`` / ``notes`` are
       Planner refinements and are deliberately not compared.
    4. Dependencies — ``depends_on`` entries must reference slots present in
       the plan, and the dependency graph over planned slots must be acyclic
       (topological check).
    """

    def validate(
        self, plan: ContentPlanIR, contract: TemplateContract
    ) -> PlanValidationReport:
        violations: list[str] = []

        contract_sections = {section.section_id for section in contract.sections}
        contract_slots = set(contract.slot_ids())
        plan_slot_ids = plan.slot_ids()
        plan_slots = set(plan_slot_ids)

        # 1. membership: the plan stays inside the contract's section/slot set
        for section in plan.sections:
            if section.section_id not in contract_sections:
                violations.append(f"unknown section: {section.section_id}")
        for slot_id in plan_slot_ids:
            if slot_id not in contract_slots:
                violations.append(f"unknown slot: {slot_id}")

        # 2. required slots must all be planned
        for spec in contract.slots:
            if spec.required and spec.slot_id not in plan_slots:
                violations.append(f"required slot missing from plan: {spec.slot_id}")

        # 3. contract-owned attributes must be echoed verbatim
        planned: dict[str, PlannedSlot] = {}
        for section in plan.sections:
            if not isinstance(section, PlannedSection):
                continue
            for slot in section.slots:
                planned[slot.slot_id] = slot
        for slot_id, slot in planned.items():
            slot_spec = contract.slot(slot_id)
            if slot_spec is None:
                continue  # already reported as an unknown slot
            if slot.kind != slot_spec.kind:
                violations.append(
                    f"slot {slot_id} kind mismatch: plan={slot.kind} contract={slot_spec.kind}"
                )
            if slot.required != slot_spec.required:
                violations.append(
                    f"slot {slot_id} required mismatch: "
                    f"plan={slot.required} contract={slot_spec.required}"
                )
            if slot.style_key != slot_spec.style_key:
                violations.append(
                    f"slot {slot_id} style_key mismatch: "
                    f"plan={slot.style_key} contract={slot_spec.style_key}"
                )

        # 4. depends_on: known inside the plan, and acyclic
        for slot_id, slot in planned.items():
            for dep in slot.depends_on:
                if dep not in plan_slots:
                    violations.append(f"slot {slot_id} depends_on unknown slot: {dep}")
        cycle = _find_dependency_cycle(planned)
        if cycle is not None:
            violations.append("depends_on cycle: " + " -> ".join(cycle))

        return PlanValidationReport(
            ok=not violations,
            violations=tuple(violations),
        )


def _find_dependency_cycle(slots: Mapping[str, PlannedSlot]) -> tuple[str, ...] | None:
    """Topological check over in-plan ``depends_on`` edges.

    Returns the cycle as ``a -> b -> a`` for the violation message, or ``None``
    when the graph is a DAG. Edges to slots outside the plan are ignored here
    (they are reported separately as unknown dependencies).
    """
    WHITE, GRAY, BLACK = 0, 1, 2
    color: dict[str, int] = dict.fromkeys(slots, WHITE)
    path: list[str] = []

    def visit(node: str) -> tuple[str, ...] | None:
        color[node] = GRAY
        path.append(node)
        for dep in slots[node].depends_on:
            if dep not in slots:
                continue
            if color[dep] == GRAY:
                return tuple([*path[path.index(dep) :], dep])
            if color[dep] == WHITE:
                found = visit(dep)
                if found is not None:
                    return found
        path.pop()
        color[node] = BLACK
        return None

    for node in slots:
        if color[node] == WHITE:
            found = visit(node)
            if found is not None:
                return found
    return None
