"""Template contract extraction (v1.1 §3): deterministic structure reader +
human-maintained semantic sidecar → :class:`TemplateContract`.

The extractor aggregates every structural objection as an
:class:`omas.domain.template.UnsupportedFinding`; a contract carrying any
finding must not be activated (:meth:`TemplateContract.is_activatable`).
Template extraction never calls an LLM.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Literal

from pydantic import ValidationError

from omas.docx.extract import (
    TAB,
    TEXT,
    DocumentDetail,
    ParagraphDetail,
    extract_details,
    find_unsupported_text_containers,
    paragraph_plain_text,
)
from omas.docx.zipcheck import check_zip_safety
from omas.domain.template import (
    AnchorInfo,
    PlanSectionTemplate,
    SlotSpec,
    StaticTextRegion,
    StyleSpec,
    TemplateContract,
    TemplatePackageHashes,
    UnsupportedFinding,
)

__all__ = ["EXTRACTOR_VERSION", "build_contract", "canonical_json"]

#: Version recorded with every template registration; bump when the
#: extraction rules below change (same DOCX then re-registers as a new
#: version, never an overwrite — v1.1 §3.1).
EXTRACTOR_VERSION = "omas-template-extractor/1"

#: sha256 of the empty string — placeholder while self-referential digesting
_EMPTY_SHA = hashlib.sha256(b"").hexdigest()

_SIDECAR_SCHEMA_VERSION = "1.1"

_PLACEHOLDER_RE = re.compile(r"\{\{[^{}]*\}\}")
_VARIABLE_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_RICH_TAG_RE = re.compile(r"\{\{\s*[rpP]\b")
_CONTROL_TAG_RE = re.compile(r"\{%")
_FILTER_BAR_RE = re.compile(r"\{\{[^{}]*\|[^{}]*\}\}")

#: token stream separators (keep unambiguous against real text content)
_TOK_SEP = "\x1f"
_TOK_END = "\x1e"
_PARA_SEP = "\x1d"


def canonical_json(payload: Any) -> bytes:
    """Deterministic serialization used for every persisted template file."""
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def build_contract(
    *,
    docx_bytes: bytes,
    sidecar: dict[str, Any],
    styles_spec: dict[str, Any],
    static_map: dict[str, Any],
    template_id: str,
    version: int,
    extractor_version: str,
) -> TemplateContract:
    """Extract and validate one template version into a TemplateContract.

    Structural objections are collected, not raised: the contract is still
    returned (and registrable) with ``unsupported_findings`` so the caller can
    report precisely why activation is blocked. Only a sidecar that cannot be
    represented by the domain DTO at all (empty slots/sections, inconsistent
    section coverage, duplicate placeholders) raises :class:`ValueError`.
    """
    findings: list[UnsupportedFinding] = []
    findings.extend(check_zip_safety(docx_bytes))
    findings.extend(find_unsupported_text_containers(docx_bytes))

    slots, sections, sidecar_findings = _build_sidecar_slots(sidecar, template_id)
    findings.extend(sidecar_findings)

    details: DocumentDetail | None = None
    try:
        details = extract_details(docx_bytes)
    except ValueError as exc:
        findings.append(
            UnsupportedFinding(
                code="DOCX_UNEXTRACTABLE", locator="package", detail=str(exc)
            )
        )

    anchors: dict[str, AnchorInfo] = {}
    static_regions: list[StaticTextRegion] = []
    if details is not None:
        placeholder_findings, anchors = _check_placeholders(details, slots)
        findings.extend(placeholder_findings)
        static_findings, static_regions = _build_static_regions(details, static_map)
        findings.extend(static_findings)

    findings.extend(_check_styles_spec(styles_spec, slots))

    slots = [
        slot.model_copy(update={"anchor": anchors.get(slot.slot_id)}) for slot in slots
    ]

    styles_sha = hashlib.sha256(canonical_json(styles_spec)).hexdigest()
    static_sha = hashlib.sha256(canonical_json(static_map)).hexdigest()
    docx_sha = hashlib.sha256(docx_bytes).hexdigest()

    try:
        contract = TemplateContract(
            template_id=template_id,
            version=version,
            extractor_version=extractor_version,
            hashes=TemplatePackageHashes(
                docx_sha256=docx_sha,
                contract_sha256=_EMPTY_SHA,
                styles_sha256=styles_sha,
                static_map_sha256=static_sha,
            ),
            sections=tuple(sections),
            slots=tuple(slots),
            static_regions=tuple(static_regions),
            unsupported_findings=tuple(findings),
        )
    except ValidationError as exc:
        raise ValueError(f"SIDECAR_CONTRACT_INVALID: {_first_error(exc)}") from exc

    # contract_sha256 covers the full contract minus the field itself
    payload = contract.model_dump(mode="json")
    payload["hashes"].pop("contract_sha256")
    contract_sha = hashlib.sha256(canonical_json(payload)).hexdigest()
    return contract.model_copy(
        update={"hashes": contract.hashes.model_copy(update={"contract_sha256": contract_sha})}
    )


# ------------------------------------------------------------------- sidecar


def _build_sidecar_slots(
    sidecar: dict[str, Any], template_id: str
) -> tuple[list[SlotSpec], list[PlanSectionTemplate], list[UnsupportedFinding]]:
    findings: list[UnsupportedFinding] = []
    if sidecar.get("schema_version") != _SIDECAR_SCHEMA_VERSION:
        findings.append(
            UnsupportedFinding(
                code="SIDECAR_SCHEMA_VERSION",
                locator="sidecar.schema_version",
                detail=f"expected {_SIDECAR_SCHEMA_VERSION!r}, "
                f"got {sidecar.get('schema_version')!r}",
            )
        )
    sidecar_template_id = sidecar.get("template_id")
    if sidecar_template_id is not None and sidecar_template_id != template_id:
        findings.append(
            UnsupportedFinding(
                code="SIDECAR_TEMPLATE_ID_MISMATCH",
                locator="sidecar.template_id",
                detail=f"sidecar declares {sidecar_template_id!r}, "
                f"registration is {template_id!r}",
            )
        )

    slots: list[SlotSpec] = []
    for index, raw in enumerate(sidecar.get("slots", [])):
        try:
            slots.append(SlotSpec.model_validate(raw))
        except ValidationError as exc:
            findings.append(
                UnsupportedFinding(
                    code="SIDECAR_SLOT_INVALID",
                    locator=f"sidecar.slots[{index}]",
                    detail=_first_error(exc),
                )
            )

    sections: list[PlanSectionTemplate] = []
    for index, raw in enumerate(sidecar.get("sections", [])):
        try:
            sections.append(PlanSectionTemplate.model_validate(raw))
        except ValidationError as exc:
            findings.append(
                UnsupportedFinding(
                    code="SIDECAR_SECTION_INVALID",
                    locator=f"sidecar.sections[{index}]",
                    detail=_first_error(exc),
                )
            )

    missing_fields = [
        name
        for name, value in (
            ("slots", sidecar.get("slots")),
            ("sections", sidecar.get("sections")),
        )
        if not value
    ]
    for name in missing_fields:
        findings.append(
            UnsupportedFinding(
                code="SIDECAR_INCOMPLETE", locator=f"sidecar.{name}", detail="must not be empty"
            )
        )
    if not slots or not sections:
        raise ValueError(f"SIDECAR_CONTRACT_INVALID: sidecar needs slots and sections: {findings}")
    return slots, sections, findings


# --------------------------------------------------------------- placeholders


def _check_placeholders(
    details: DocumentDetail, slots: list[SlotSpec]
) -> tuple[list[UnsupportedFinding], dict[str, AnchorInfo]]:
    findings: list[UnsupportedFinding] = []
    document = details.part("document")
    if document is None:
        findings.append(
            UnsupportedFinding(
                code="DOCX_UNEXTRACTABLE", locator="document", detail="no document part"
            )
        )
        return findings, {}

    for part in details.parts:
        if part.name == "document":
            continue
        part_text = "\n".join(paragraph_plain_text(d.paragraph) for d in part.paragraphs)
        if "{{" in part_text:
            findings.append(
                UnsupportedFinding(
                    code="PLACEHOLDER_IN_HEADER_FOOTER",
                    locator=part.name,
                    detail="dynamic placeholders are only allowed in the document part",
                )
            )

    full_text = "".join(
        token.text or ""
        for d in document.paragraphs
        for token in d.paragraph.tokens
        if token.kind is TEXT
    )

    for match in _PLACEHOLDER_RE.finditer(full_text):
        token = match.group(0).strip()
        if token in {slot.placeholder.strip() for slot in slots}:
            continue
        inner = token[2:-2].strip()
        if _VARIABLE_NAME_RE.match(inner):
            findings.append(
                UnsupportedFinding(
                    code="PLACEHOLDER_UNDECLARED",
                    locator=_locator(document.paragraphs, match.start()),
                    detail=f"document contains placeholder {token!r} not declared in sidecar",
                )
            )
        else:
            findings.append(
                UnsupportedFinding(
                    code="PLACEHOLDER_NOT_VARIABLE",
                    locator=_locator(document.paragraphs, match.start()),
                    detail=f"placeholder {token!r} is not a plain whitelisted variable node",
                )
            )

    if _CONTROL_TAG_RE.search(full_text):
        findings.append(
            UnsupportedFinding(
                code="JINJA_CONTROL_SYNTAX",
                locator="document",
                detail="Jinja control syntax ({% ... %}) is outside the supported subset",
            )
        )
    if _RICH_TAG_RE.search(full_text):
        findings.append(
            UnsupportedFinding(
                code="JINJA_RICH_TEXT_TAG",
                locator="document",
                detail="rich-text tags ({{r / {{p) are outside the supported subset",
            )
        )
    if _FILTER_BAR_RE.search(full_text):
        findings.append(
            UnsupportedFinding(
                code="JINJA_FILTER",
                locator="document",
                detail="filters ('|') in placeholders are outside the supported subset",
            )
        )

    anchors: dict[str, AnchorInfo] = {}
    anchored: list[tuple[int, str, AnchorInfo]] = []
    for slot in slots:
        placeholder = slot.placeholder.strip()
        occurrences = full_text.count(placeholder) if placeholder else 0
        if occurrences == 0:
            findings.append(
                UnsupportedFinding(
                    code="PLACEHOLDER_MISSING",
                    locator=f"document:{slot.slot_id}",
                    detail=f"placeholder {placeholder!r} not found in document part",
                )
            )
            continue
        if occurrences > 1:
            findings.append(
                UnsupportedFinding(
                    code="PLACEHOLDER_DUPLICATE",
                    locator=f"document:{slot.slot_id}",
                    detail=(
                        f"placeholder {placeholder!r} occurs {occurrences} times; "
                        "exactly once required"
                    ),
                )
            )
        owner = _find_owner(document.paragraphs, placeholder)
        if owner is None:
            findings.append(
                UnsupportedFinding(
                    code="PLACEHOLDER_FRAGMENTED",
                    locator=f"document:{slot.slot_id}",
                    detail=f"placeholder {placeholder!r} is not fully contained in one paragraph",
                )
            )
            continue
        paragraph = owner.paragraph
        text = paragraph_plain_text(paragraph)
        if text.strip() != placeholder:
            findings.append(
                UnsupportedFinding(
                    code="PLACEHOLDER_NOT_ALONE",
                    locator=f"document:para[{owner.index}]",
                    detail=f"placeholder paragraph must contain only {placeholder!r}; "
                    f"found {text!r}",
                )
            )
        elif not any(run.strip() == placeholder for run in owner.run_texts):
            findings.append(
                UnsupportedFinding(
                    code="PLACEHOLDER_SPLIT_RUN",
                    locator=f"document:para[{owner.index}]",
                    detail=f"placeholder spans multiple runs; run boundaries: "
                    f"{[run[:40] for run in owner.run_texts]}",
                )
            )
        anchor = _anchor_for(owner, slot.slot_id)
        if anchor is None:
            findings.append(
                UnsupportedFinding(
                    code="PLACEHOLDER_MISSING_ANCHOR",
                    locator=f"document:para[{owner.index}]",
                    detail=f"no complete bookmark named {slot.slot_id!r} covers the paragraph",
                )
            )
        else:
            anchored.append((owner.index, slot.slot_id, anchor))

    for slot_order, (_index, slot_id, anchor) in enumerate(
        sorted(anchored, key=lambda item: item[0])
    ):
        anchors[slot_id] = anchor.model_copy(update={"slot_order": slot_order})
    return findings, anchors


def _find_owner(
    paragraphs: tuple[ParagraphDetail, ...], placeholder: str
) -> ParagraphDetail | None:
    for detail in paragraphs:
        if placeholder in paragraph_plain_text(detail.paragraph):
            return detail
    return None


def _anchor_for(detail: ParagraphDetail, slot_id: str) -> AnchorInfo | None:
    for bookmark in detail.bookmarks:
        if bookmark.name != slot_id:
            continue
        if not bookmark.has_matching_end:
            continue
        return AnchorInfo(
            part="document",
            bookmark_id=bookmark.id,
            slot_order=0,
            in_table=detail.paragraph.in_table,
            cell_locator=detail.paragraph.cell_locator,
            structure_signature=detail.signature,
        )
    return None


def _locator(
    paragraphs: tuple[ParagraphDetail, ...], offset: int
) -> str:
    """Map a character offset in the concatenated w:t stream to a paragraph."""
    consumed = 0
    for index, detail in enumerate(paragraphs):
        length = sum(
            len(token.text or "") for token in detail.paragraph.tokens if token.kind is TEXT
        )
        if consumed <= offset < consumed + length:
            return f"document:para[{index}]"
        consumed += length
    return "document"


# ------------------------------------------------------------- static regions


def _build_static_regions(
    details: DocumentDetail, static_map: dict[str, Any]
) -> tuple[list[UnsupportedFinding], list[StaticTextRegion]]:
    findings: list[UnsupportedFinding] = []
    regions: list[StaticTextRegion] = []
    raw_regions = static_map.get("regions")
    if not isinstance(raw_regions, list):
        findings.append(
            UnsupportedFinding(
                code="STATIC_MAP_INVALID",
                locator="static_map.regions",
                detail="static_map must contain a non-empty 'regions' list",
            )
        )
        return findings, regions

    seen_ids: set[str] = set()
    for index, raw in enumerate(raw_regions):
        if not isinstance(raw, dict):
            findings.append(
                UnsupportedFinding(
                    code="STATIC_REGION_INVALID",
                    locator=f"static_map.regions[{index}]",
                    detail="region entry must be an object",
                )
            )
            continue
        region_id = str(raw.get("region_id", ""))
        if not region_id:
            findings.append(
                UnsupportedFinding(
                    code="STATIC_REGION_INVALID",
                    locator=f"static_map.regions[{index}]",
                    detail="missing region_id",
                )
            )
            continue
        if region_id in seen_ids:
            findings.append(
                UnsupportedFinding(
                    code="STATIC_REGION_INVALID",
                    locator=f"static_map.regions[{index}]",
                    detail=f"duplicate region_id {region_id!r}",
                )
            )
            continue
        seen_ids.add(region_id)
        part_name = str(raw.get("part", "document"))
        part = details.part(part_name)
        if part is None:
            findings.append(
                UnsupportedFinding(
                    code="STATIC_REGION_LOCATOR_INVALID",
                    locator=f"static:{region_id}",
                    detail=f"unknown part {part_name!r}",
                )
            )
            continue
        paragraph_range = raw.get("paragraph_range")
        table_ref = raw.get("table_ref")
        if paragraph_range is not None:
            paragraphs = _region_by_range(part.paragraphs, paragraph_range, region_id, findings)
        elif table_ref is not None:
            paragraphs = _region_by_table(part.paragraphs, str(table_ref), region_id, findings)
        else:
            findings.append(
                UnsupportedFinding(
                    code="STATIC_REGION_LOCATOR_INVALID",
                    locator=f"static:{region_id}",
                    detail="region needs either paragraph_range or table_ref",
                )
            )
            continue
        if paragraphs is None:
            continue
        digest, token_count = _region_digest(paragraphs)
        regions.append(
            StaticTextRegion(
                region_id=region_id,
                part=_part_kind(part_name),
                locator=_region_locator(part_name, raw, paragraph_range, table_ref),
                text_sha256=digest,
                token_count=token_count,
            )
        )
    return findings, regions


def recompute_static_regions(
    docx_bytes: bytes, static_map: dict[str, Any]
) -> tuple[tuple[UnsupportedFinding, ...], tuple[StaticTextRegion, ...]]:
    """Re-derive static regions from *any* DOCX (template or rendered output).

    Public seam for Gate B: recomputes the canonical region digests with the
    exact same token-stream format used at contract build time.
    """
    details = extract_details(docx_bytes)
    findings, regions = _build_static_regions(details, static_map)
    return tuple(findings), tuple(regions)


def _part_kind(part_name: str) -> Literal["document", "header", "footer"]:
    """The domain DTO carries the part *kind*; the concrete index (header1,
    footer2) stays visible in the region locator."""
    if part_name.startswith("header"):
        return "header"
    if part_name.startswith("footer"):
        return "footer"
    return "document"


def _region_locator(
    part_name: str, raw: dict[str, Any], paragraph_range: Any, table_ref: str | None
) -> str:
    """Prefer an explicit sidecar locator; default to a structural one."""
    explicit = raw.get("locator")
    if explicit:
        return str(explicit)
    structural = (
        table_ref
        if table_ref is not None
        else _range_locator(paragraph_range)
    )
    return structural if part_name == "document" else f"{part_name}:{structural}"


def _range_locator(paragraph_range: Any) -> str:
    if isinstance(paragraph_range, list) and len(paragraph_range) == 2:
        return f"para[{paragraph_range[0]}:{paragraph_range[1]}]"
    return "para[?]"


def _region_by_range(
    paragraphs: tuple[ParagraphDetail, ...],
    paragraph_range: Any,
    region_id: str,
    findings: list[UnsupportedFinding],
) -> list[ParagraphDetail] | None:
    if (
        not isinstance(paragraph_range, list)
        or len(paragraph_range) != 2
        or not all(isinstance(v, int) for v in paragraph_range)
    ):
        findings.append(
            UnsupportedFinding(
                code="STATIC_REGION_LOCATOR_INVALID",
                locator=f"static:{region_id}",
                detail=f"paragraph_range must be [start, end] integers: {paragraph_range!r}",
            )
        )
        return None
    start, end = paragraph_range
    if start < 0 or end < start or end >= len(paragraphs):
        findings.append(
            UnsupportedFinding(
                code="STATIC_REGION_LOCATOR_INVALID",
                locator=f"static:{region_id}",
                detail=f"paragraph_range [{start}, {end}] out of part bounds "
                f"(0..{len(paragraphs) - 1})",
            )
        )
        return None
    return list(paragraphs[start : end + 1])


def _region_by_table(
    paragraphs: tuple[ParagraphDetail, ...],
    table_ref: str,
    region_id: str,
    findings: list[UnsupportedFinding],
) -> list[ParagraphDetail] | None:
    prefix = f"{table_ref}/"
    matched = [
        d
        for d in paragraphs
        if d.paragraph.cell_locator is not None and d.paragraph.cell_locator.startswith(prefix)
    ]
    if not matched:
        findings.append(
            UnsupportedFinding(
                code="STATIC_REGION_LOCATOR_INVALID",
                locator=f"static:{region_id}",
                detail=f"no table cells match table_ref {table_ref!r}",
            )
        )
        return None
    return matched


def _region_digest(paragraphs: list[ParagraphDetail]) -> tuple[str, int]:
    """sha256 + token count over the region's ordered token stream.

    Canonical stream per paragraph: TEXT → ``t\\x1f<text>\\x1e``, TAB →
    ``tab\\x1e``, BREAK → ``br\\x1e``; paragraphs joined with ``\\x1d``. Gate B
    must reproduce this exact stream when comparing static regions.
    """
    chunks: list[str] = []
    token_count = 0
    for position, detail in enumerate(paragraphs):
        if position:
            chunks.append(_PARA_SEP)
        for token in detail.paragraph.tokens:
            token_count += 1
            if token.kind is TEXT:
                chunks.append(f"t{_TOK_SEP}{token.text or ''}{_TOK_END}")
            elif token.kind is TAB:
                chunks.append(f"tab{_TOK_END}")
            else:
                chunks.append(f"br{_TOK_END}")
    digest = hashlib.sha256("".join(chunks).encode("utf-8")).hexdigest()
    return digest, token_count


# ----------------------------------------------------------------- styles spec


def _check_styles_spec(
    styles_spec: dict[str, Any], slots: list[SlotSpec]
) -> list[UnsupportedFinding]:
    findings: list[UnsupportedFinding] = []
    for style_key, raw in styles_spec.items():
        try:
            payload = dict(raw) if isinstance(raw, dict) else None
            if payload is None:
                raise ValueError("style entry must be an object")
            payload["style_key"] = style_key
            StyleSpec.model_validate(payload)
        except (ValidationError, ValueError) as exc:
            detail = _first_error(exc) if isinstance(exc, ValidationError) else str(exc)
            findings.append(
                UnsupportedFinding(
                    code="STYLES_SPEC_INVALID",
                    locator=f"styles_spec.{style_key}",
                    detail=detail,
                )
            )
    for slot in slots:
        if slot.style_key not in styles_spec:
            findings.append(
                UnsupportedFinding(
                    code="STYLE_KEY_UNKNOWN",
                    locator=f"slot:{slot.slot_id}",
                    detail=f"style_key {slot.style_key!r} missing from styles_spec",
                )
            )
    return findings


def _first_error(exc: ValidationError) -> str:
    errors = exc.errors()
    if not errors:
        return str(exc)
    first = errors[0]
    loc = ".".join(str(part) for part in first.get("loc", ()))
    message = str(first.get("msg", ""))
    return f"{loc}: {message}" if loc else message
