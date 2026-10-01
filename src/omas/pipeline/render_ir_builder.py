"""RenderIRBuilder: BindingIR (+ contract + overrides) → DocxRenderIR (v1.1 §7).

The IR carries no body text — only span refs, static template refs and style
keys. Missing required slots without a recorded user omission are a gap, not
an IR: the builder refuses so gap_check/interrupt happen upstream (I6).
"""

from __future__ import annotations

from omas.domain.errors import MissingRequiredSlotsError
from omas.domain.ids import ArtifactId, TaskId, TemplateVersionId
from omas.domain.ir import (
    BindingIR,
    BoundBinding,
    DocxRenderIR,
    MissingBinding,
    RenderSlot,
    TemplateStaticRef,
)
from omas.domain.template import TemplateContract
from omas.storage.db import Ledger


class RenderIRBuilder:
    def __init__(self, ledger: Ledger) -> None:
        self._ledger = ledger

    def build(
        self,
        *,
        task_id: TaskId,
        epoch: int,
        contract: TemplateContract,
        template_version_id: TemplateVersionId,
        template_docx_artifact_id: ArtifactId,
        binding: BindingIR,
        plan_artifact_id: ArtifactId,
        binding_artifact_id: ArtifactId,
    ) -> DocxRenderIR:
        overrides = self._ledger.slot_overrides.active_for_task(task_id, epoch)
        slots: list[RenderSlot] = []
        missing: list[str] = []
        for binding_slot in binding.bindings:
            spec = contract.slot(binding_slot.slot_id)
            if spec is None:
                raise MissingRequiredSlotsError(
                    f"binding references slot unknown to the template: {binding_slot.slot_id}"
                )
            if isinstance(binding_slot, BoundBinding):
                slots.append(
                    RenderSlot(
                        slot_id=binding_slot.slot_id,
                        spans=binding_slot.source_refs,
                        style_key=spec.style_key,
                    )
                )
            elif isinstance(binding_slot, MissingBinding):
                override = overrides.get(binding_slot.slot_id)
                if override is None:
                    missing.append(binding_slot.slot_id)
                    continue
                slots.append(
                    RenderSlot(
                        slot_id=binding_slot.slot_id,
                        spans=(),
                        style_key=spec.style_key,
                        override_artifact_id=override.reason_artifact_id,
                    )
                )
            else:  # InvalidBinding
                raise MissingRequiredSlotsError(
                    f"slot {binding_slot.slot_id} has an invalid binding "
                    f"({binding_slot.error_code}); fix or rebind before rendering"
                )
        if missing:
            raise MissingRequiredSlotsError(
                f"required slots still missing without user omission: {sorted(missing)}"
            )
        return DocxRenderIR(
            schema_version=1,
            task_id=task_id,
            epoch=epoch,
            template_version_id=template_version_id,
            template_docx_artifact_id=template_docx_artifact_id,
            plan_artifact_id=plan_artifact_id,
            binding_artifact_id=binding_artifact_id,
            static_regions=tuple(
                TemplateStaticRef(
                    template_version_id=template_version_id, region_id=region.region_id
                )
                for region in contract.static_regions
            ),
            slots=tuple(slots),
        )
