"""TemplateContract and StyleSpec (Master §6; v1.1 §3).

The contract is produced by the deterministic extractor plus a human-maintained
semantic sidecar. Template extraction never calls an LLM. Any re-extraction of
the same DOCX yields a NEW version; versions are immutable.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .common import Sha256Hex
from .ir import SlotKind

__all__ = [
    "AnchorInfo",
    "PlanSectionTemplate",
    "SlotSpec",
    "StaticTextRegion",
    "StyleSpec",
    "TemplateContract",
    "TemplatePackageHashes",
    "UnsupportedFinding",
]

OoxmlTextPart = Literal["document", "header", "footer"]


class TemplatePackageHashes(BaseModel):
    """Hashes binding a template version to its full file package."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    docx_sha256: Sha256Hex
    contract_sha256: Sha256Hex
    styles_sha256: Sha256Hex
    static_map_sha256: Sha256Hex


class AnchorInfo(BaseModel):
    """Stable structural anchor for a slot (survives multi-paragraph inserts).

    Absolute paragraph indexes are recorded only as diagnostics; identity is
    the bookmark id plus the structure signature.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    part: OoxmlTextPart = "document"
    bookmark_id: str = Field(min_length=1)
    #: 0-based order of the slot within its part
    slot_order: int = Field(ge=0)
    in_table: bool = False
    #: locator for the owning cell, e.g. "tbl[0]/tr[1]/tc[2]"; None in body
    cell_locator: str | None = None
    #: digest over the owning paragraph/cell structure so gate B can detect
    #: structural tampering around the anchor
    structure_signature: Sha256Hex


class SlotSpec(BaseModel):
    """One dynamic slot as declared by the semantic sidecar (v1.1 §3.1)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    slot_id: str = Field(min_length=1)
    placeholder: str = Field(min_length=1)
    kind: SlotKind
    required: bool
    allow_user_omit: bool = False
    semantic_requirement: str = Field(min_length=1)
    style_key: str = Field(min_length=1)
    anchor: AnchorInfo | None = None

    @model_validator(mode="after")
    def _check_placeholder_shape(self) -> SlotSpec:
        token = self.placeholder.strip()
        if not (token.startswith("{{") and token.endswith("}}")):
            raise ValueError(f"placeholder must be a {{ ... }} variable node: {self.placeholder!r}")
        inner = token[2:-2].strip()
        if not inner or not inner.replace("_", "").isidentifier():
            raise ValueError(
                f"placeholder must contain a plain variable name: {self.placeholder!r}"
            )
        return self


class PlanSectionTemplate(BaseModel):
    """Section grouping declared by the sidecar."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    section_id: str = Field(min_length=1)
    title: str | None = None
    slot_ids: tuple[str, ...] = Field(min_length=1)


class StaticTextRegion(BaseModel):
    """A static text area that gate B must verify byte-for-byte (v1.1 §7).

    ``text_sha256`` is the digest over the region's extracted token stream
    (w:t / tab / newline tokens in order), as produced by the versioned
    extractor — never recomputed from caller input.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    region_id: str = Field(min_length=1)
    part: OoxmlTextPart = "document"
    locator: str = Field(min_length=1)
    text_sha256: Sha256Hex
    token_count: int = Field(ge=0)


class UnsupportedFinding(BaseModel):
    """Structure outside the supported subset; blocks template activation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str = Field(min_length=1)
    locator: str | None = None
    detail: str | None = None


class StyleSpec(BaseModel):
    """How content of a style region should look (structure-level, not visual).

    Font sizes are half-points, indents/margins are twips (v1.1 §3.3).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    style_key: str = Field(min_length=1)
    paragraph_style: str | None = None
    font_latin: str | None = None
    font_east_asia: str | None = None
    font_size_half_points: int | None = Field(default=None, ge=1)
    bold: bool | None = None
    alignment: Literal["left", "center", "right", "both", "unknown"] | None = None
    line_spacing: float | None = Field(default=None, gt=0)
    indent_left_twips: int | None = None
    indent_first_line_twips: int | None = None
    margin_top_twips: int | None = None
    margin_bottom_twips: int | None = None
    numbering_id: str | None = None
    numbering_level: int | None = Field(default=None, ge=0)
    #: "unknown" must be representable everywhere a resolver cannot decide
    font_size_tolerance_half_points: int = Field(default=2, ge=0)
    indent_tolerance_twips: int = Field(default=20, ge=0)


class TemplateContract(BaseModel):
    """Versioned, immutable template contract (extractor + sidecar)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = 1
    template_id: str = Field(min_length=1)
    version: int = Field(ge=1)
    extractor_version: str = Field(min_length=1)
    hashes: TemplatePackageHashes
    sections: tuple[PlanSectionTemplate, ...] = Field(min_length=1)
    slots: tuple[SlotSpec, ...] = Field(min_length=1)
    static_regions: tuple[StaticTextRegion, ...] = ()
    #: non-empty means the template must NOT be activated
    unsupported_findings: tuple[UnsupportedFinding, ...] = ()

    @model_validator(mode="after")
    def _check_slot_sets(self) -> TemplateContract:
        slot_ids = [s.slot_id for s in self.slots]
        duplicates = {s for s in slot_ids if slot_ids.count(s) > 1}
        if duplicates:
            raise ValueError(f"duplicate slot ids: {sorted(duplicates)}")
        known = set(slot_ids)
        for section in self.sections:
            unknown = set(section.slot_ids) - known
            if unknown:
                raise ValueError(
                    f"section {section.section_id} references unknown slots: {sorted(unknown)}"
                )
        referenced = {sid for section in self.sections for sid in section.slot_ids}
        orphan = known - referenced
        if orphan:
            raise ValueError(f"slots not covered by any section: {sorted(orphan)}")
        placeholders = [s.placeholder.strip() for s in self.slots]
        if len(set(placeholders)) != len(placeholders):
            raise ValueError("placeholder text must be unique per slot")
        return self

    def slot_ids(self) -> tuple[str, ...]:
        return tuple(s.slot_id for s in self.slots)

    def slot(self, slot_id: str) -> SlotSpec | None:
        return next((s for s in self.slots if s.slot_id == slot_id), None)

    def is_activatable(self) -> bool:
        return not self.unsupported_findings
