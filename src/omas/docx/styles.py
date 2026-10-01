"""Effective-style resolution over direct formatting → paragraph pPr →
``w:pStyle`` basedOn chain → docDefaults (v1.1 §3.3, probe 4).

python-docx's high-level ``run.font`` only reflects direct formatting, so the
merge is implemented here directly on lxml. Theme-only font references
(``asciiTheme``/``eastAsiaTheme`` without literal ``w:ascii``/``w:eastAsia``)
are deliberately returned as ``None`` (unknown) with a resolution note — never
guessed (probe 4: the default template's docDefaults is theme-indirect).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ._xml import (
    XmlElement,
    find_child,
    find_children,
    get_attr,
    local_name,
    namespace_of,
    parse_rpr_fragment,
    parse_xml,
    w,
)
from ._zipio import open_zip, read_capped
from .extract import DocumentDetail, ExtractedPart, extract_details

__all__ = ["ResolvedStyle", "StyleResolver"]

#: w:jc values mapped onto the contract's alignment vocabulary.
_ALIGNMENT_MAP: dict[str, str] = {
    "left": "left",
    "start": "left",
    "center": "center",
    "right": "right",
    "end": "right",
    "both": "both",
}

_FALSE_VALUES = frozenset({"false", "0", "off"})


@dataclass(frozen=True, slots=True)
class ResolvedStyle:
    """Merged effective style; ``None`` per dimension means "cannot decide"."""

    font_latin: str | None = None
    font_east_asia: str | None = None
    font_size_half_points: int | None = None
    bold: bool | None = None
    paragraph_style: str | None = None
    #: one of left/center/right/both; None = unset or undecidable
    alignment: str | None = None
    numbering_id: str | None = None
    numbering_level: int | None = None
    #: why a dimension stayed unknown (theme-indirect fonts, broken chains,
    #: unresolvable numbering references, ...)
    resolution_notes: tuple[str, ...] = field(default=())


@dataclass(frozen=True, slots=True)
class _StyleDef:
    style_id: str
    based_on: str | None
    rpr: XmlElement | None
    ppr: XmlElement | None


@dataclass(frozen=True, slots=True)
class _NumberingIndex:
    num_to_abstract: dict[str, str]
    abstract_levels: dict[str, frozenset[int]]


@dataclass(frozen=True, slots=True)
class _MergeLevel:
    """One precedence level contributing run and/or paragraph properties."""

    label: str
    rpr: XmlElement | None = None
    ppr: XmlElement | None = None


class StyleResolver:
    """Resolves effective styles for paragraphs of one DOCX package."""

    def __init__(self, docx_bytes: bytes) -> None:
        self._docx_bytes = docx_bytes
        self._details: DocumentDetail | None = None
        defaults = _parse_styles(docx_bytes)
        self._styles, self._doc_default_rpr, self._doc_default_ppr, self._default_para_style = (
            defaults
        )
        self._numbering = _parse_numbering(docx_bytes)

    # ------------------------------------------------------------ public API

    def resolve_paragraph(self, part: ExtractedPart, para_index: int) -> ResolvedStyle:
        """Resolve the paragraph at *para_index* of *part* (0-based)."""
        details = self._document()
        part_detail = details.part(part.name)
        if part_detail is None:
            raise IndexError(f"unknown part {part.name!r} in this document")
        if para_index < 0 or para_index >= len(part_detail.paragraphs):
            raise IndexError(
                f"paragraph index {para_index} out of range for part {part.name!r} "
                f"({len(part_detail.paragraphs)} paragraphs)"
            )
        paragraph_el = part_detail.paragraphs[para_index].element
        notes: list[str] = []

        ppr = find_child(paragraph_el, w("pPr"))
        p_style_id = self._style_id_of(ppr)
        run_rpr = self._first_text_run_rpr(paragraph_el, notes)

        run_chain = [_MergeLevel("direct-run", rpr=run_rpr)]
        para_chain = [_MergeLevel("direct-paragraph", ppr=ppr)]
        style_chain, chain_style_id = self._style_chain(p_style_id, notes)
        run_chain.extend(style_chain)
        para_chain.extend(style_chain)
        run_chain.append(_MergeLevel("docDefaults", rpr=self._doc_default_rpr))
        para_chain.append(_MergeLevel("docDefaults", ppr=self._doc_default_ppr))

        return self._merge(run_chain, para_chain, chain_style_id, notes)

    def resolve_paragraph_by_id(
        self, style_id: str | None, direct_rpr: bytes | None = None
    ) -> ResolvedStyle:
        """Resolve from a style id plus optional serialized direct ``w:rPr``."""
        notes: list[str] = []
        fragment: XmlElement | None = None
        if direct_rpr is not None:
            fragment = parse_rpr_fragment(direct_rpr)
            if direct_rpr.strip() and fragment is None:
                notes.append("direct rPr fragment could not be parsed and was ignored")
        run_chain = [_MergeLevel("direct-run", rpr=fragment)]
        para_chain: list[_MergeLevel] = []
        style_chain, chain_style_id = self._style_chain(style_id, notes)
        run_chain.extend(style_chain)
        para_chain.extend(style_chain)
        run_chain.append(_MergeLevel("docDefaults", rpr=self._doc_default_rpr))
        para_chain.append(_MergeLevel("docDefaults", ppr=self._doc_default_ppr))
        return self._merge(run_chain, para_chain, chain_style_id, notes)

    # ----------------------------------------------------------- internals

    def _document(self) -> DocumentDetail:
        if self._details is None:
            self._details = extract_details(self._docx_bytes)
        return self._details

    def _style_id_of(self, ppr: XmlElement | None) -> str | None:
        if ppr is None:
            return None
        pstyle = find_child(ppr, w("pStyle"))
        if pstyle is None:
            return None
        return get_attr(pstyle, w("val"))

    def _first_text_run_rpr(
        self, paragraph_el: XmlElement, notes: list[str]
    ) -> XmlElement | None:
        """rPr of the first run carrying text; notes differing sibling rPrs."""
        chosen: XmlElement | None = None
        chosen_text: str | None = None
        differing = False
        for run in paragraph_el.iter(w("r")):
            parts: list[str] = []
            for child in run:
                if local_name(child) == "t":
                    parts.append(child.text or "")
            text = "".join(parts)
            if not text:
                continue
            rpr = find_child(run, w("rPr"))
            if chosen is None:
                chosen, chosen_text = rpr, text
            elif chosen_text is not None and rpr is not chosen and not _same_rpr(chosen, rpr):
                differing = True
        if differing:
            notes.append("paragraph has multiple text runs with differing direct formatting")
        return chosen

    def _style_chain(
        self, style_id: str | None, notes: list[str]
    ) -> tuple[list[_MergeLevel], str | None]:
        """basedOn chain from *style_id* (or the default paragraph style).

        Cycle-safe: each style is visited at most once. Returns the levels and
        the style id that anchored the chain.
        """
        anchor = style_id if style_id is not None else self._default_para_style
        if anchor is None:
            notes.append("no paragraph style and no default paragraph style in styles.xml")
            return [], None
        levels: list[_MergeLevel] = []
        seen: set[str] = set()
        current: str | None = anchor
        while current is not None:
            if current in seen:
                notes.append(f"style basedOn cycle detected at {current!r}; chain truncated")
                break
            seen.add(current)
            definition = self._styles.get(current)
            if definition is None:
                notes.append(f"style {current!r} not found in styles.xml")
                break
            levels.append(_MergeLevel(f"style:{current}", rpr=definition.rpr, ppr=definition.ppr))
            current = definition.based_on
        return levels, anchor

    def _merge(
        self,
        run_chain: list[_MergeLevel],
        para_chain: list[_MergeLevel],
        chain_style_id: str | None,
        notes: list[str],
    ) -> ResolvedStyle:
        font_latin, latin_note = _resolve_font(run_chain, "ascii")
        font_east_asia, east_note = _resolve_font(run_chain, "eastAsia")
        size, size_note = _resolve_size(run_chain)
        bold, bold_note = _resolve_bool(run_chain, "b")
        alignment, align_note = _resolve_alignment(para_chain)
        numbering_id, numbering_level = self._resolve_numbering(para_chain, notes)
        if latin_note:
            notes.append(latin_note)
        if east_note:
            notes.append(east_note)
        if size_note:
            notes.append(size_note)
        if bold_note:
            notes.append(bold_note)
        if align_note:
            notes.append(align_note)
        return ResolvedStyle(
            font_latin=font_latin,
            font_east_asia=font_east_asia,
            font_size_half_points=size,
            bold=bold,
            paragraph_style=chain_style_id,
            alignment=alignment,
            numbering_id=numbering_id,
            numbering_level=numbering_level,
            resolution_notes=tuple(notes),
        )

    def _resolve_numbering(
        self, para_chain: list[_MergeLevel], notes: list[str]
    ) -> tuple[str | None, int | None]:
        """First numPr in the chain wins; numId=0 explicitly disables."""
        for level in para_chain:
            if level.ppr is None:
                continue
            numpr = find_child(level.ppr, w("numPr"))
            if numpr is None:
                continue
            numid_el = find_child(numpr, w("numId"))
            ilvl_el = find_child(numpr, w("ilvl"))
            num_id = get_attr(numid_el, w("val")) if numid_el is not None else None
            if num_id is None:
                continue
            ilvl_raw = get_attr(ilvl_el, w("val")) if ilvl_el is not None else None
            ilvl = 0 if ilvl_raw is None else _to_int(ilvl_raw, 0)
            if num_id == "0":
                return None, None
            return self._verify_numbering(num_id, ilvl, notes)
        return None, None

    def _verify_numbering(
        self, num_id: str, ilvl: int, notes: list[str]
    ) -> tuple[str | None, int | None]:
        """numId → abstractNumId → level existence check (v1.1 §3.3)."""
        if not self._numbering.num_to_abstract:
            notes.append("numbering.xml absent or empty; numPr cannot be verified")
            return None, None
        abstract = self._numbering.num_to_abstract.get(num_id)
        if abstract is None:
            notes.append(f"numId {num_id!r} has no w:num definition in numbering.xml")
            return None, None
        levels = self._numbering.abstract_levels.get(abstract)
        if levels is None:
            notes.append(f"abstractNum {abstract!r} referenced by numId {num_id!r} is missing")
            return None, None
        if ilvl not in levels:
            notes.append(f"abstractNum {abstract!r} defines no level {ilvl}")
            return None, None
        return num_id, ilvl


# --------------------------------------------------------------- pure helpers


def _parse_styles(
    docx_bytes: bytes,
) -> tuple[dict[str, _StyleDef], XmlElement | None, XmlElement | None, str | None]:
    """Parse styles.xml: style table, docDefaults, default paragraph style."""
    try:
        zf = open_zip(docx_bytes)
    except ValueError:
        return {}, None, None, None
    with zf:
        if "word/styles.xml" not in zf.namelist():
            return {}, None, None, None
        try:
            root = parse_xml(read_capped(zf, "word/styles.xml"), source="word/styles.xml")
        except ValueError:
            return {}, None, None, None

    styles: dict[str, _StyleDef] = {}
    default_para: str | None = None
    for style_el in find_children(root, w("style")):
        style_id = get_attr(style_el, w("styleId"))
        if style_id is None:
            continue
        based_on_el = find_child(style_el, w("basedOn"))
        based_on = get_attr(based_on_el, w("val")) if based_on_el is not None else None
        styles[style_id] = _StyleDef(
            style_id=style_id,
            based_on=based_on,
            rpr=find_child(style_el, w("rPr")),
            ppr=find_child(style_el, w("pPr")),
        )
        if (
            default_para is None
            and get_attr(style_el, w("type")) == "paragraph"
            and get_attr(style_el, w("default")) in ("1", "true")
        ):
            default_para = style_id

    doc_default_rpr = None
    doc_default_ppr = None
    defaults = find_child(root, w("docDefaults"))
    if defaults is not None:
        rpr_default = find_child(defaults, w("rPrDefault"))
        if rpr_default is not None:
            doc_default_rpr = find_child(rpr_default, w("rPr"))
        ppr_default = find_child(defaults, w("pPrDefault"))
        if ppr_default is not None:
            doc_default_ppr = find_child(ppr_default, w("pPr"))
    return styles, doc_default_rpr, doc_default_ppr, default_para


def _parse_numbering(docx_bytes: bytes) -> _NumberingIndex:
    try:
        zf = open_zip(docx_bytes)
    except ValueError:
        return _NumberingIndex({}, {})
    with zf:
        if "word/numbering.xml" not in zf.namelist():
            return _NumberingIndex({}, {})
        try:
            root = parse_xml(read_capped(zf, "word/numbering.xml"), source="word/numbering.xml")
        except ValueError:
            return _NumberingIndex({}, {})

    num_to_abstract: dict[str, str] = {}
    for num_el in find_children(root, w("num")):
        num_id = get_attr(num_el, w("numId"))
        abstract_el = find_child(num_el, w("abstractNumId"))
        abstract = get_attr(abstract_el, w("val")) if abstract_el is not None else None
        if num_id is not None and abstract is not None:
            num_to_abstract[num_id] = abstract

    abstract_levels: dict[str, frozenset[int]] = {}
    for abstract_el in find_children(root, w("abstractNum")):
        abstract_id = get_attr(abstract_el, w("abstractNumId"))
        if abstract_id is None:
            continue
        levels: set[int] = set()
        for lvl in find_children(abstract_el, w("lvl")):
            ilvl_raw = get_attr(lvl, w("ilvl"))
            if ilvl_raw is not None:
                levels.add(_to_int(ilvl_raw, -1))
        abstract_levels[abstract_id] = frozenset(levels)
    return _NumberingIndex(num_to_abstract, abstract_levels)


def _resolve_font(
    chain: list[_MergeLevel], attribute: str
) -> tuple[str | None, str | None]:
    """First ``w:rFonts`` specifying *attribute* (or its Theme twin) wins.

    A theme-only reference is decisive in OOXML (it overrides inherited
    literal fonts) but unresolvable here, so it yields ``None`` plus a note.
    """
    theme_attr = f"{attribute}Theme"
    for level in chain:
        if level.rpr is None:
            continue
        rfonts = find_child(level.rpr, w("rFonts"))
        if rfonts is None:
            continue
        literal = get_attr(rfonts, w(attribute))
        if literal is not None:
            return literal, None
        theme = get_attr(rfonts, w(theme_attr))
        if theme is not None:
            return None, f"{level.label}: w:rFonts has only {theme_attr}={theme!r}; font unknown"
    return None, None


def _resolve_size(chain: list[_MergeLevel]) -> tuple[int | None, str | None]:
    for level in chain:
        if level.rpr is None:
            continue
        sz = find_child(level.rpr, w("sz"))
        if sz is None:
            continue
        raw = get_attr(sz, w("val"))
        if raw is None:
            continue
        try:
            return int(raw), None
        except ValueError:
            return None, f"{level.label}: non-numeric w:sz val={raw!r}"
    return None, None


def _resolve_bool(chain: list[_MergeLevel], tag: str) -> tuple[bool | None, str | None]:
    for level in chain:
        if level.rpr is None:
            continue
        el = find_child(level.rpr, w(tag))
        if el is None:
            continue
        val = get_attr(el, w("val"))
        return val not in _FALSE_VALUES, None
    return None, None


def _resolve_alignment(chain: list[_MergeLevel]) -> tuple[str | None, str | None]:
    for level in chain:
        if level.ppr is None:
            continue
        jc = find_child(level.ppr, w("jc"))
        if jc is None:
            continue
        val = get_attr(jc, w("val"))
        if val is None:
            continue
        mapped = _ALIGNMENT_MAP.get(val)
        if mapped is None:
            return None, f"{level.label}: unsupported w:jc val={val!r}"
        return mapped, None
    return None, None


def _to_int(raw: str, default: int) -> int:
    try:
        return int(raw)
    except ValueError:
        return default


def _same_rpr(a: XmlElement | None, b: XmlElement | None) -> bool:
    """Structural equality of two rPr elements (both None counts as equal)."""
    if a is None or b is None:
        return a is b
    if len(a) != len(b):
        return False
    for ca, cb in zip(a, b, strict=True):
        if namespace_of(ca) != namespace_of(cb) or local_name(ca) != local_name(cb):
            return False
        if dict(ca.attrib) != dict(cb.attrib):
            return False
    return dict(a.attrib) == dict(b.attrib)
