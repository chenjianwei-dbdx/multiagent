"""Plan / Binding / Render IRs (Master §5; two-layer design, no universal IR).

ContentPlanIR — Planner's semantic mapping within the template's own slot set.
BindingIR    — slot → source span facts; discriminated union per slot status.
DocxRenderIR — render instructions built only from verified refs; carries no
               free body text at all (static content arrives as TemplateStaticRef).
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .common import Epoch, Sha256Hex
from .ids import ArtifactId, TaskId, TemplateVersionId
from .spans import SourceSpanRef

#: Producer of bound material. ``viz`` is retained for future phases; the MVP
#: profile rejects it at binding-commit time (v1.1 §4.1).
Producer = Literal["user", "upstream", "viz"]

SlotKind = Literal["text_block", "data_table", "chart", "table_asset", "image_asset"]

SCHEMA_VERSION = 1


# ---------------------------------------------------------------- ContentPlanIR


class PlanSlot(BaseModel):
    """A planned slot. Every field except semantics must equal TemplateContract.

    The PlanValidator (P3) enforces slot set membership and that required /
    kind / style_key are untouched; the DTO itself only carries them.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    slot_id: str = Field(min_length=1)
    kind: SlotKind
    required: bool
    semantic_requirement: str = Field(min_length=1)
    preferred_source_kind: tuple[Producer, ...] = ()
    depends_on: tuple[str, ...] = ()
    notes: str | None = None


class PlanSection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    section_id: str = Field(min_length=1)
    slot_ids: tuple[str, ...] = Field(min_length=1)


class ContentPlanIR(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = SCHEMA_VERSION
    task_id: TaskId
    template_version_id: TemplateVersionId
    sections: tuple[PlanSection, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _check_unique_slots(self) -> ContentPlanIR:
        seen: list[str] = []
        for section in self.sections:
            for slot_id in section.slot_ids:
                seen.append(slot_id)
        duplicates = {s for s in seen if seen.count(s) > 1}
        if duplicates:
            raise ValueError(f"duplicate slot_ids in plan: {sorted(duplicates)}")
        return self

    def slot_ids(self) -> tuple[str, ...]:
        return tuple(sid for section in self.sections for sid in section.slot_ids)


# -------------------------------------------------------------------- BindingIR


class BoundBinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    binding_status: Literal["bound"]
    slot_id: str = Field(min_length=1)
    producer: Producer
    #: ordered spans; each span renders as its own paragraph (v1.1 §3.2)
    source_refs: tuple[SourceSpanRef, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _check_refs_nonempty(self) -> BoundBinding:
        if not self.source_refs:
            raise ValueError("bound binding must have at least one source ref")
        return self


class MissingBinding(BaseModel):
    """Required material absent. Deliberately has NO producer and NO refs."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    binding_status: Literal["missing"]
    slot_id: str = Field(min_length=1)
    reason_code: Literal["no_material", "ambiguous", "budget_exceeded", "policy_blocked"]

    @model_validator(mode="after")
    def _check_no_extras(self) -> MissingBinding:
        # extra="forbid" already rejects unknown fields; this hook documents
        # the invariant: missing bindings carry no producer and no refs.
        return self


class InvalidBinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    binding_status: Literal["invalid"]
    slot_id: str = Field(min_length=1)
    error_code: str = Field(min_length=1)


SlotBinding = Annotated[
    BoundBinding | MissingBinding | InvalidBinding,
    Field(discriminator="binding_status"),
]


class BindingIR(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = SCHEMA_VERSION
    task_id: TaskId
    plan_artifact_id: ArtifactId
    template_version_id: TemplateVersionId
    epoch: Epoch
    binding_version: int = Field(ge=1)
    #: sha256 over the sorted (artifact_id, sha256) of the material set this
    #: binding was assembled against; guards stale bindings after re-supply.
    material_set_digest: Sha256Hex
    bindings: tuple[SlotBinding, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _check_unique_slots(self) -> BindingIR:
        slot_ids = [b.slot_id for b in self.bindings]
        duplicates = {s for s in slot_ids if slot_ids.count(s) > 1}
        if duplicates:
            raise ValueError(f"duplicate slot bindings: {sorted(duplicates)}")
        return self

    def binding_for(self, slot_id: str) -> SlotBinding | None:
        return next((b for b in self.bindings if b.slot_id == slot_id), None)


# ------------------------------------------------------------------ DocxRenderIR


class TemplateStaticRef(BaseModel):
    """Reference to a static text region of a *specific* template version.

    Static content never enters the IR as a string; it is resolved from the
    versioned template package by the renderer/gates (v1.1 §7).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    template_version_id: TemplateVersionId
    region_id: str = Field(min_length=1)


class RenderSlot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    slot_id: str = Field(min_length=1)
    #: ordered spans; output order is this order. Empty only for a user-omitted
    #: slot (renders as an empty paragraph — no filler text, v1.1 §8).
    spans: tuple[SourceSpanRef, ...] = ()
    style_key: str = Field(min_length=1)
    #: set when the slot is user-omitted via a valid decision
    override_artifact_id: ArtifactId | None = None

    @model_validator(mode="after")
    def _check_spans_or_override(self) -> RenderSlot:
        if self.override_artifact_id is None and not self.spans:
            raise ValueError(f"slot {self.slot_id} needs spans unless omitted via override")
        if self.override_artifact_id is not None and self.spans:
            raise ValueError(f"omitted slot {self.slot_id} must not carry spans")
        return self


class DocxRenderIR(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = SCHEMA_VERSION
    task_id: TaskId
    epoch: Epoch
    template_version_id: TemplateVersionId
    template_docx_artifact_id: ArtifactId
    plan_artifact_id: ArtifactId
    binding_artifact_id: ArtifactId
    #: every static region the render must preserve, by id from static-map
    static_regions: tuple[TemplateStaticRef, ...] = ()
    slots: tuple[RenderSlot, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _check_unique_slots(self) -> DocxRenderIR:
        slot_ids = [s.slot_id for s in self.slots]
        duplicates = {s for s in slot_ids if slot_ids.count(s) > 1}
        if duplicates:
            raise ValueError(f"duplicate render slots: {sorted(duplicates)}")
        return self
