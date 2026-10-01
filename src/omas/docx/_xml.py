"""Shared lxml plumbing for the standalone OOXML extractor (v1.1 §7 / §9).

lxml ships no inline typing for this project's mypy strict run and lxml-stubs
is not a locked dependency, so the lxml import carries an explicit
``import-untyped`` ignore and element references are typed with the opaque
:class:`XmlElement` alias. Every public function in this package remains fully
annotated.

Parser policy (v1.1 §9): entities unresolved, network and DTD loading off,
``huge_tree`` disabled, DOCTYPE declarations rejected outright.
"""

from __future__ import annotations

from typing import Any

from lxml import etree  # type: ignore[import-untyped]

#: Opaque reference to an lxml element (lxml is untyped under mypy strict).
type XmlElement = Any

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"

#: Maximum accepted XML element depth (v1.1 §9 initial engineering limit).
XML_DEPTH_LIMIT = 128

#: DOCTYPE must appear before the root element; scanning the prolog prefix is
#: sufficient for well-formed input and bounds work on hostile files.
_PROLOG_SCAN_BYTES = 64 * 1024

_W_WRAPPER = (
    b'<w:root xmlns:w="' + W_NS.encode("ascii") + b'">'
    b"{fragment}"
    b"</w:root>"
)


def w(tag: str) -> str:
    """Clark-notation qualified name in the ``w`` (wordprocessingml) namespace."""
    return f"{{{W_NS}}}{tag}"


def has_doctype(data: bytes) -> bool:
    """Whether the (prefix of an) XML document declares a DOCTYPE."""
    prefix = data[:_PROLOG_SCAN_BYTES]
    return b"<!DOCTYPE" in prefix or b"<!doctype" in prefix


def safe_parser() -> Any:
    """The mandated hardened parser configuration (v1.1 §9)."""
    return etree.XMLParser(
        resolve_entities=False,
        no_network=True,
        dtd_validation=False,
        load_dtd=False,
        huge_tree=False,
    )


def parse_xml(data: bytes, *, source: str) -> XmlElement:
    """Parse an OOXML part, rejecting DOCTYPE, broken XML and over-deep trees.

    Raises :class:`ValueError` (never a raw lxml error) so callers can treat
    every extraction failure uniformly.
    """
    if has_doctype(data):
        raise ValueError(f"{source}: DOCTYPE declarations are rejected")
    try:
        root = etree.fromstring(data, parser=safe_parser())
    except etree.XMLSyntaxError as exc:  # pragma: no cover - message varies
        raise ValueError(f"{source}: XML parse error: {exc}") from exc
    if root is None:  # pragma: no cover - empty documents fail parsing above
        raise ValueError(f"{source}: empty XML part")
    if max_depth(root) > XML_DEPTH_LIMIT:
        raise ValueError(f"{source}: XML depth exceeds {XML_DEPTH_LIMIT}")
    return root


def parse_rpr_fragment(fragment: bytes) -> XmlElement | None:
    """Parse a serialized ``w:rPr`` fragment (or any rPr-like element).

    A bare fragment without namespace declarations is wrapped in a
    namespace-carrying root. Returns ``None`` for empty input or fragments
    that are not a single element.
    """
    if not fragment.strip():
        return None
    data = fragment.strip()
    try:
        return parse_xml(data, source="direct-rPr")
    except ValueError:
        pass
    try:
        root = etree.fromstring(_W_WRAPPER.replace(b"{fragment}", data), parser=safe_parser())
    except etree.XMLSyntaxError:
        return None
    if root is None:
        return None
    return root[0] if len(root) else None


def max_depth(root: XmlElement) -> int:
    """Deepest element nesting below and including *root* (iterative walk)."""
    depth = 0
    stack: list[tuple[XmlElement, int]] = [(root, 1)]
    while stack:
        element, level = stack.pop()
        if level > depth:
            depth = level
        for child in element:
            stack.append((child, level + 1))
    return depth


def local_name(element: XmlElement) -> str:
    """Local name of *element* without its namespace (``w:p`` -> ``p``)."""
    return str(etree.QName(element).localname)


def namespace_of(element: XmlElement) -> str | None:
    """Namespace URI of *element* (None for unqualified names)."""
    namespace = etree.QName(element).namespace
    return None if namespace is None else str(namespace)


def find_child(element: XmlElement, tag: str) -> XmlElement | None:
    """First direct child matching the qualified *tag*."""
    return element.find(tag)


def find_children(element: XmlElement, tag: str) -> list[XmlElement]:
    """All direct children matching the qualified *tag*, in order."""
    return list(element.findall(tag))


def iter_tag(root: XmlElement, tag: str) -> list[XmlElement]:
    """All descendants (incl. *root*) matching *tag*, in document order."""
    return list(root.iter(tag))


def get_attr(element: XmlElement, name: str) -> str | None:
    """Attribute *name* (qualified) as ``str``, or ``None``."""
    value = element.get(name)
    return None if value is None else str(value)


def element_path(element: XmlElement) -> str:
    """Human-readable ancestor chain like ``w:body/w:p/w:r/w:t`` (diagnostics)."""
    parts: list[str] = []
    current: XmlElement | None = element
    while current is not None:
        ns = namespace_of(current)
        prefix = "w:" if ns == W_NS else ""
        parts.append(f"{prefix}{local_name(current)}")
        current = current.getparent()
    return "/".join(reversed(parts))


def canonical_bytes(element: XmlElement) -> bytes:
    """Deterministic canonical serialization used for structure signatures."""
    return bytes(etree.tostring(element, method="c14n"))
