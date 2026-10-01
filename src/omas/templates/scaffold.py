"""Template scaffold: prepare an uploaded DOCX into a registered template.

The mechanical prep that used to be manual (ADR 0002 amendment):

1. discover ``{{ slot }}`` placeholders that stand alone in their paragraph;
2. inject same-name bookmarks around those paragraphs (stable anchors);
3. auto-generate the semantic sidecar (per-slot semantics come from the
   uploader — the program never invents them), the static map (every
   non-slot paragraph is a static region) and a minimal styles spec;
4. register through :class:`TemplateRegistry` (immutable version) and upsert
   display metadata for the library UI.

Unsupported structures are NOT silently fixed: they surface as findings and
the template is not activatable until the document is corrected.
"""

from __future__ import annotations

import io
import zipfile
from dataclasses import dataclass

from omas.artifacts.store import ArtifactStore
from omas.storage.db import Ledger

_W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


@dataclass(frozen=True, slots=True)
class ScaffoldResult:
    template_id: str
    version: int
    version_id: str
    activatable: bool
    findings: tuple[str, ...]
    slots: tuple[str, ...]


def discover_placeholders(docx_bytes: bytes) -> list[str]:
    """Slot names of ``{{ name }}`` placeholders, in document order.

    A cheap pre-flight for the upload form: shows the uploader which slots
    the document declares before they write semantics.
    """
    with zipfile.ZipFile(io.BytesIO(docx_bytes)) as zf:
        if "word/document.xml" not in zf.namelist():
            return []
        document = zf.read("word/document.xml")
    from omas.docx._xml import parse_xml

    root = parse_xml(document, source="document.xml")
    found: list[str] = []
    for paragraph in root.findall(f".//{{{_W_NS}}}body/{{{_W_NS}}}p"):
        texts = [t.text or "" for t in paragraph.findall(f".//{{{_W_NS}}}t")]
        joined = "".join(texts).strip()
        if joined.startswith("{{") and joined.endswith("}}"):
            name = joined[2:-2].strip()
            if name and name.replace("_", "").isalnum():
                found.append(name)
    return found


def _inject_bookmarks(docx_bytes: bytes, slot_names: set[str]) -> bytes:
    """Wrap placeholder paragraphs with same-name bookmarks (idempotent)."""
    from omas.docx._xml import parse_xml

    with zipfile.ZipFile(io.BytesIO(docx_bytes)) as zf:
        names = zf.namelist()
        payload = {name: zf.read(name) for name in names}
    root = parse_xml(payload["word/document.xml"], source="document.xml")
    existing = {b.get(f"{{{_W_NS}}}name") for b in root.iter(f"{{{_W_NS}}}bookmarkStart")}
    next_id = 1000
    for paragraph in root.findall(f".//{{{_W_NS}}}body/{{{_W_NS}}}p"):
        joined = "".join(
            t.text or "" for t in paragraph.findall(f".//{{{_W_NS}}}t")
        ).strip()
        if not (joined.startswith("{{") and joined.endswith("}}")):
            continue
        name = joined[2:-2].strip()
        if name not in slot_names or name in existing:
            continue
        start = paragraph.makeelement(
            f"{{{_W_NS}}}bookmarkStart",
            {f"{{{_W_NS}}}id": str(next_id), f"{{{_W_NS}}}name": name},
        )
        end = paragraph.makeelement(f"{{{_W_NS}}}bookmarkEnd", {f"{{{_W_NS}}}id": str(next_id)})
        paragraph.insert(0, start)
        paragraph.append(end)
        next_id += 1
    from omas.docx._xml import canonical_bytes

    payload["word/document.xml"] = canonical_bytes(root)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as target:
        for name in names:
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            target.writestr(info, payload[name])
    return buffer.getvalue()


def _static_map_for(docx_bytes: bytes, slot_names: set[str]) -> dict[str, object]:
    """Every non-placeholder document paragraph becomes a static region."""
    from omas.docx._xml import parse_xml

    with zipfile.ZipFile(io.BytesIO(docx_bytes)) as zf:
        document = zf.read("word/document.xml")
    root = parse_xml(document, source="document.xml")
    regions: list[dict[str, object]] = []
    body = root.find(f"{{{_W_NS}}}body")
    if body is None:
        return {"regions": regions}
    index = 0
    for child in body:
        if child.tag != f"{{{_W_NS}}}p":
            continue
        joined = "".join(
            t.text or "" for t in child.findall(f".//{{{_W_NS}}}t")
        ).strip()
        is_slot = joined.startswith("{{") and joined.endswith("}}")
        if not is_slot and joined:
            regions.append(
                {
                    "region_id": f"static_{index}",
                    "part": "document",
                    "paragraph_range": [index, index + 1],
                }
            )
        index += 1
    return {"regions": regions}


@dataclass(frozen=True, slots=True)
class ScaffoldPayloads:
    """Everything :meth:`TemplateRegistry.register` consumes for one DOCX.

    Built deterministically from the DOCX plus the uploader's slot semantics;
    shared by the web upload path (:func:`scaffold_and_register`) and the
    built-in seed path so both produce the *same* contract digests for the
    same inputs (:func:`omas.templates.seed.seed_builtin_templates`).
    """

    docx_bytes: bytes
    sidecar: dict[str, object]
    styles_spec: dict[str, object]
    static_map: dict[str, object]
    slot_names: tuple[str, ...]


def scaffold_payloads(
    docx_bytes: bytes,
    *,
    template_id: str,
    slot_semantics: dict[str, str],
    allow_omit: set[str] | None = None,
) -> ScaffoldPayloads:
    """Discover placeholders, inject anchors and derive all registration inputs.

    Raises :class:`ValueError` when the document declares no placeholder or a
    placeholder carries no semantic requirement — the uploader must fix the
    document or the semantics, the program never invents semantics (v1.1 §3.1).
    """
    slot_names = discover_placeholders(docx_bytes)
    if not slot_names:
        raise ValueError(
            "文档中未发现 {{ 槽位名 }} 占位符；请在需要填充内容的段落里写入占位符后重新上传"
        )
    missing = [s for s in slot_names if not slot_semantics.get(s, "").strip()]
    if missing:
        raise ValueError(f"以下槽位缺少语义说明: {', '.join(missing)}")
    prepared = _inject_bookmarks(docx_bytes, set(slot_names))
    sidecar: dict[str, object] = {
        "schema_version": "1.1",
        "template_id": template_id,
        "slots": [
            {
                "slot_id": slot,
                "placeholder": f"{{{{ {slot} }}}}",
                "kind": "text_block",
                "required": True,
                "allow_user_omit": slot in (allow_omit or set()),
                "semantic_requirement": slot_semantics[slot].strip(),
                "style_key": "body",
            }
            for slot in slot_names
        ],
        "sections": [{"section_id": "root", "slot_ids": slot_names}],
    }
    styles_spec: dict[str, object] = {
        "body": {"paragraph_style": "Normal"},
    }
    static_map = _static_map_for(prepared, set(slot_names))
    return ScaffoldPayloads(
        docx_bytes=prepared,
        sidecar=sidecar,
        styles_spec=styles_spec,
        static_map=static_map,
        slot_names=tuple(slot_names),
    )


def scaffold_and_register(
    *,
    store: ArtifactStore,
    ledger: Ledger,
    docx_bytes: bytes,
    template_id: str,
    display_name: str,
    description: str,
    slot_semantics: dict[str, str],
    allow_omit: set[str] | None = None,
) -> ScaffoldResult:
    """Prepare + register one template version and its display metadata."""
    payloads = scaffold_payloads(
        docx_bytes,
        template_id=template_id,
        slot_semantics=slot_semantics,
        allow_omit=allow_omit,
    )
    from omas.templates.registry import TemplateRegistry

    registry = TemplateRegistry(store, ledger)
    version_id, contract = registry.register(
        docx_bytes=payloads.docx_bytes,
        sidecar=payloads.sidecar,
        styles_spec=payloads.styles_spec,
        static_map=payloads.static_map,
        template_id=template_id,
    )
    from datetime import UTC, datetime

    ledger.template_meta.upsert(
        template_id=template_id,
        display_name=display_name or template_id,
        description=description,
        at=datetime.now(UTC),
    )
    return ScaffoldResult(
        template_id=template_id,
        version=contract.version,
        version_id=version_id,
        activatable=contract.is_activatable(),
        findings=tuple(f"{f.code}@{f.locator or ''}" for f in contract.unsupported_findings),
        slots=payloads.slot_names,
    )
