"""Deterministic DOCX renderer (P1; Master §7, v1.1 §3.2 & §7).

Security boundary (hard constraints):

- No free text enters through the public API: slot content is produced only
  by the ``resolve_span`` callback, which the caller implements on top of
  ArtifactStore + ``verify_span``. Nothing here accepts body-text strings.
- ``template_docx`` must hash (SHA-256) to ``contract.hashes.docx_sha256``
  before anything is rendered; otherwise ``ArtifactHashMismatchError``.
  The hash check is also what binds the IR to this contract/template
  version (the contract DTO carries no template_version_id field).
- The Jinja environment is built here with ``StrictUndefined`` and
  ``autoescape=True``; the context contains only variables derived from the
  contract's slot placeholders plus generated multi-span variables. Caller
  dictionaries are never passed through.
- IR slots unknown to the contract raise ``ValueError``. Required slots
  missing from the IR are NOT this module's concern (GapCheck owns that).

Multi-span rendering (v1.1 §3.2): every span of a slot becomes its own
paragraph; the paragraph boundary is structure, no connector text is added.
Implementation: before handing the template to docxtpl, ``word/document.xml``
is preprocessed with lxml — the paragraph holding the slot placeholder is
deep-copied N-1 times and inserted right after it, and each copy's
placeholder text is rewritten to a generated variable ``{{ <slot_id>__i }}``
(the contract guarantees the placeholder occupies exactly one paragraph and
one run; matching is exact full-``w:t`` text).

Control-character policy: resolved span text is re-checked with
``validate_renderable_text`` so characters docxtpl would give special
meaning (e.g. ``\\a``, ``\\f``) fail loudly instead of being interpreted.

Proven behavior this builds on (dependency-baseline probe 3, docxtpl 0.20.2):
LF inside a value becomes ``<w:br/>`` and Tab becomes ``<w:tab/>`` via
``resolve_listing()``; with ``autoescape=True`` the characters ``<>&"'``
round-trip exactly (with autoescape off they are silently corrupted by the
internal ``XMLParser(recover=True)``).

The whole render is in-memory: no files are written and no store is touched;
persisting the returned bytes is the pipeline's job.

``RenderError`` is the domain error (``omas.domain.errors.RenderError``,
code ``RENDER_FAILED``); this module raises and re-exports that single class.
"""

from __future__ import annotations

import copy
import hashlib
import io
import zipfile
from collections.abc import Callable, Mapping
from typing import Any

from docxtpl import DocxTemplate  # type: ignore[import-untyped]
from jinja2 import Environment, StrictUndefined, TemplateError
from lxml import etree  # type: ignore[import-untyped]

from omas.core.canonical import validate_renderable_text
from omas.domain.errors import ArtifactHashMismatchError, RenderError
from omas.domain.ir import DocxRenderIR
from omas.domain.spans import SourceSpanRef
from omas.domain.template import SlotSpec, TemplateContract

__all__ = ["DocxRenderer", "RenderError", "SpanTextResolver", "render_docx"]

SpanTextResolver = Callable[[SourceSpanRef], str]
"""Produces the canonical text behind a verified span reference.

Implemented by the upper layer with ArtifactStore + ``verify_span``; the
renderer itself never touches storage. Exceptions from the resolver (span
verification failures) propagate unchanged.
"""

_W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_W = f"{{{_W_NS}}}"
_DOCUMENT_XML = "word/document.xml"
_LEFTOVER_MARK = "{{"


# --------------------------------------------------------------- template prep


def _placeholder_var(spec: SlotSpec) -> str:
    """The plain Jinja variable name inside the slot's placeholder."""
    var = spec.placeholder.strip()[2:-2].strip()
    if not var.isidentifier():
        raise RenderError(
            f"slot {spec.slot_id}: placeholder {spec.placeholder!r} is not a plain variable node"
        )
    return var


def _find_unique_placeholder_t(root: Any, spec: SlotSpec) -> Any:
    """The single ``w:t`` whose full text is exactly this slot's placeholder."""
    wanted = {spec.placeholder, spec.placeholder.strip()}
    matches = [t for t in root.iter(f"{_W}t") if t.text in wanted]
    if len(matches) != 1:
        raise RenderError(
            f"slot {spec.slot_id}: its placeholder must occur exactly once in "
            f"{_DOCUMENT_XML} (contract guarantee), found {len(matches)}"
        )
    return matches[0]


def _ancestor_paragraph(element: Any, slot_id: str) -> Any:
    node = element.getparent()
    while node is not None and node.tag != f"{_W}p":
        node = node.getparent()
    if node is None:
        raise RenderError(f"slot {slot_id}: placeholder w:t has no enclosing paragraph")
    return node


def _rewrite_placeholder_text(paragraph: Any, old: str, new: str, slot_id: str) -> None:
    """Replace the placeholder's full w:t text inside one (copied) paragraph."""
    matches = [t for t in paragraph.iter(f"{_W}t") if t.text == old]
    if len(matches) != 1:
        raise RenderError(
            f"slot {slot_id}: placeholder paragraph must hold exactly one "
            f"placeholder run text, found {len(matches)}"
        )
    matches[0].text = new


def _rebuild_zip_with_document_xml(docx_bytes: bytes, document_xml: bytes) -> bytes:
    """Copy the template package, swapping only word/document.xml."""
    out = io.BytesIO()
    with (
        zipfile.ZipFile(io.BytesIO(docx_bytes)) as src,
        zipfile.ZipFile(out, "w") as dst,
    ):
        for info in src.infolist():
            data = document_xml if info.filename == _DOCUMENT_XML else src.read(info.filename)
            dst.writestr(info, data)
    return out.getvalue()


def _expand_multispan_slots(
    template_docx: bytes, contract: TemplateContract, ir: DocxRenderIR
) -> bytes:
    """Give every span of multi-span slots its own placeholder paragraph.

    Slots with a single span (or an override) keep the original placeholder
    untouched. When nothing needs expanding the original bytes are returned
    as-is (zip untouched, byte-for-byte).
    """
    multi = [slot for slot in ir.slots if len(slot.spans) > 1]
    if not multi:
        return template_docx

    try:
        with zipfile.ZipFile(io.BytesIO(template_docx)) as zf:
            root = etree.fromstring(zf.read(_DOCUMENT_XML))  # strict parse
    except KeyError as exc:
        raise RenderError(f"template package is missing {_DOCUMENT_XML}") from exc

    for slot in multi:
        spec = contract.slot(slot.slot_id)
        if spec is None:
            # Unreachable behind _checked_specs; kept defensive.
            raise RenderError(f"slot {slot.slot_id} is not in the template contract")
        if not slot.slot_id.isidentifier():
            raise RenderError(
                f"slot id {slot.slot_id!r} cannot form template variables for multi-span rendering"
            )
        wt = _find_unique_placeholder_t(root, spec)
        paragraph = _ancestor_paragraph(wt, slot.slot_id)
        original_text = wt.text
        copies = [copy.deepcopy(paragraph) for _ in range(len(slot.spans) - 1)]
        prev = paragraph
        for duplicate in copies:
            prev.addnext(duplicate)
            prev = duplicate
        _rewrite_placeholder_text(
            paragraph, original_text, f"{{{{ {slot.slot_id}__0 }}}}", slot.slot_id
        )
        for index, duplicate in enumerate(copies, start=1):
            _rewrite_placeholder_text(
                duplicate, original_text, f"{{{{ {slot.slot_id}__{index} }}}}", slot.slot_id
            )

    document_xml = etree.tostring(
        root, xml_declaration=True, encoding="UTF-8", standalone=True
    )
    return _rebuild_zip_with_document_xml(template_docx, document_xml)


# ------------------------------------------------------------------- context


def _checked_specs(ir: DocxRenderIR, contract: TemplateContract) -> Mapping[str, SlotSpec]:
    """Contract slot specs keyed by slot_id; ValueError on unknown IR slots."""
    specs = {spec.slot_id: spec for spec in contract.slots}
    unknown = sorted({slot.slot_id for slot in ir.slots} - specs.keys())
    if unknown:
        raise ValueError(f"render IR references slots not in the template contract: {unknown}")
    return specs


def _build_context(
    ir: DocxRenderIR,
    specs: Mapping[str, SlotSpec],
    resolve_span: SpanTextResolver,
) -> dict[str, str]:
    """Context with only whitelisted variable names; values from resolve_span.

    resolve_span failures (span verification errors raised by the caller's
    implementation) propagate unchanged — the renderer never swallows them.
    """
    context: dict[str, str] = {}
    ordered_keys: list[str] = []

    def put(key: str, value: str) -> None:
        ordered_keys.append(key)
        context[key] = value

    for slot in ir.slots:
        spec = specs[slot.slot_id]
        if slot.override_artifact_id is not None:
            # User-omitted slot: renders an empty paragraph, no filler text.
            put(_placeholder_var(spec), "")
            continue
        if len(slot.spans) == 1:
            put(_placeholder_var(spec), _resolve(slot.spans[0], resolve_span))
        else:
            for index, ref in enumerate(slot.spans):
                put(f"{slot.slot_id}__{index}", _resolve(ref, resolve_span))

    duplicates = sorted({key for key in ordered_keys if ordered_keys.count(key) > 1})
    if duplicates:
        raise RenderError(f"template variable name collision between slots: {duplicates}")
    return context


def _resolve(ref: SourceSpanRef, resolve_span: SpanTextResolver) -> str:
    text = resolve_span(ref)
    # Reject, never filter: control chars docxtpl would reinterpret (\a, \f, ...).
    validate_renderable_text(text)
    return text


# --------------------------------------------------------------------- render


def _assert_no_leftover_placeholders(rendered: bytes) -> None:
    """Minimal post-render defense: document.xml must contain no '{{'."""
    try:
        with zipfile.ZipFile(io.BytesIO(rendered)) as zf:
            xml = zf.read(_DOCUMENT_XML).decode("utf-8")
    except KeyError as exc:
        raise RenderError(f"rendered package is missing {_DOCUMENT_XML}") from exc
    if _LEFTOVER_MARK in xml:
        raise RenderError(
            "unrendered '{{' remains in document.xml "
            "(missing template variable or anomalous template)"
        )


def render_docx(
    *,
    ir: DocxRenderIR,
    contract: TemplateContract,
    template_docx: bytes,
    resolve_span: SpanTextResolver,
) -> bytes:
    """Deterministically render ``ir`` into ``template_docx``; return DOCX bytes.

    The result is not persisted here — the calling pipeline owns all IO.
    Raises ``ArtifactHashMismatchError`` (template bytes vs contract hash),
    ``ValueError`` (IR slot outside the contract), the resolver's own errors
    unchanged, and ``RenderError`` for template/render anomalies.
    """
    actual_sha = hashlib.sha256(template_docx).hexdigest()
    if actual_sha != contract.hashes.docx_sha256:
        raise ArtifactHashMismatchError(
            "template docx sha256 mismatch: "
            f"contract={contract.hashes.docx_sha256} actual={actual_sha}"
        )

    specs = _checked_specs(ir, contract)
    prepared = _expand_multispan_slots(template_docx, contract, ir)
    context = _build_context(ir, specs, resolve_span)

    template = DocxTemplate(io.BytesIO(prepared))
    # Fixed recipe (probe 3): autoescape=True is required for <>& fidelity;
    # StrictUndefined makes any non-whitelisted template variable fail loudly
    # instead of rendering empty. docxtpl's render() then re-asserts
    # autoescape on our environment (a no-op here).
    jinja_env: Environment = Environment(undefined=StrictUndefined, autoescape=True)
    try:
        template.render(context, jinja_env=jinja_env, autoescape=True)
    except TemplateError as exc:
        raise RenderError(f"template render failed: {type(exc).__name__}: {exc}") from exc

    out = io.BytesIO()
    template.save(out)
    rendered = out.getvalue()
    _assert_no_leftover_placeholders(rendered)
    return rendered


class DocxRenderer:
    """Injectable thin wrapper around :func:`render_docx`.

    Stateless by design: the class exists so the upper pipeline can inject
    or replace the renderer without importing the function directly.
    """

    def __init__(self) -> None:
        """Nothing to configure; all inputs arrive per render call."""

    def render(
        self,
        *,
        ir: DocxRenderIR,
        contract: TemplateContract,
        template_docx: bytes,
        resolve_span: SpanTextResolver,
    ) -> bytes:
        """Same contract as :func:`render_docx`."""
        return render_docx(
            ir=ir,
            contract=contract,
            template_docx=template_docx,
            resolve_span=resolve_span,
        )
