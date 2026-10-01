"""Streaming ZIP/OOXML input validation (v1.1 §9) — findings, never exceptions.

Every rule is reported as a :class:`omas.domain.template.UnsupportedFinding`
so the template extractor can aggregate the results into the contract and
block activation. Initial engineering limits (not measured capacity):

- at most 2,000 entries per archive
- at most 20 MiB per uploaded DOCX
- at most 100 MiB declared decompressed total (plus capped real reads)
"""

from __future__ import annotations

import stat
import zipfile
from collections import Counter

from omas.domain.template import UnsupportedFinding

from ._xml import has_doctype
from ._zipio import DEFAULT_READ_CAP, iter_xml_parts, open_zip, read_capped

MAX_ENTRIES = 2_000
MAX_DOCX_BYTES = 20 * 1024 * 1024
MAX_EXPANDED_BYTES = 100 * 1024 * 1024

#: A normal OOXML package never contains these; macros are rejected outright.
_MACRO_MARKERS = ("vbaproject.bin",)


def check_zip_safety(docx_bytes: bytes) -> tuple[UnsupportedFinding, ...]:
    """Validate the ZIP container and package structure of a DOCX.

    Returns every violation found (deduplicated by code + locator). Never
    raises: unreadable input is itself reported as ``ZIP_INVALID``.
    """
    findings: list[UnsupportedFinding] = []

    if len(docx_bytes) > MAX_DOCX_BYTES:
        findings.append(
            UnsupportedFinding(
                code="ZIP_OVERSIZED",
                locator="package",
                detail=f"docx is {len(docx_bytes)} bytes; limit is {MAX_DOCX_BYTES}",
            )
        )

    try:
        zf = open_zip(docx_bytes)
    except ValueError as exc:
        findings.append(UnsupportedFinding(code="ZIP_INVALID", locator="package", detail=str(exc)))
        return tuple(findings)

    with zf:
        return tuple(findings + _check_entries(zf) + _check_package(zf))


def _check_entries(zf: zipfile.ZipFile) -> list[UnsupportedFinding]:
    findings: list[UnsupportedFinding] = []
    infos = zf.infolist()

    if len(infos) > MAX_ENTRIES:
        findings.append(
            UnsupportedFinding(
                code="ZIP_TOO_MANY_ENTRIES",
                locator="package",
                detail=f"{len(infos)} entries; limit is {MAX_ENTRIES}",
            )
        )

    counts = Counter(info.filename for info in infos)
    for name, count in sorted(counts.items()):
        if count > 1:
            findings.append(
                UnsupportedFinding(
                    code="ZIP_DUPLICATE_ENTRY",
                    locator=name,
                    detail=f"entry name appears {count} times",
                )
            )

    declared_total = 0
    for info in infos:
        declared_total += info.file_size
        findings.extend(_check_entry_name(info))
        if _is_symlink(info):
            findings.append(
                UnsupportedFinding(
                    code="ZIP_SYMLINK_ENTRY", locator=info.filename, detail="entry is a symlink"
                )
            )
        lowered = info.filename.lower()
        if any(marker in lowered for marker in _MACRO_MARKERS):
            findings.append(
                UnsupportedFinding(
                    code="ZIP_MACRO_PART", locator=info.filename, detail="macro-bearing part"
                )
            )

    if declared_total > MAX_EXPANDED_BYTES:
        findings.append(
            UnsupportedFinding(
                code="ZIP_EXPANSION_OVER_LIMIT",
                locator="package",
                detail=(
                    f"declared decompressed total {declared_total} bytes; "
                    f"limit is {MAX_EXPANDED_BYTES}"
                ),
            )
        )
    return findings


def _check_entry_name(info: zipfile.ZipInfo) -> list[UnsupportedFinding]:
    name = info.filename
    if name.startswith("/"):
        return [
            UnsupportedFinding(
                code="ZIP_UNSAFE_ENTRY_NAME", locator=name, detail="absolute entry path"
            )
        ]
    problems: list[str] = []
    if "\\" in name:
        problems.append("backslash in entry name")
    if "\x00" in name:
        problems.append("NUL byte in entry name")
    for component in name.split("/"):
        if ".." in component:
            problems.append(f"'..' in component {component!r}")
    if not problems:
        return []
    return [
        UnsupportedFinding(
            code="ZIP_UNSAFE_ENTRY_NAME", locator=name, detail="; ".join(problems)
        )
    ]


def _is_symlink(info: zipfile.ZipInfo) -> bool:
    """Unix mode bits are stored in the high 16 bits of ``external_attr``."""
    if info.create_system != 3:
        return False
    return stat.S_ISLNK(info.external_attr >> 16)


def _check_package(zf: zipfile.ZipFile) -> list[UnsupportedFinding]:
    findings: list[UnsupportedFinding] = []
    names = set(zf.namelist())

    if "[Content_Types].xml" not in names:
        findings.append(
            UnsupportedFinding(
                code="ZIP_MISSING_CONTENT_TYPES",
                locator="[Content_Types].xml",
                detail="package is not a normal DOCX structure",
            )
        )
    elif not _content_types_covers_document(zf):
        findings.append(
            UnsupportedFinding(
                code="ZIP_CONTENT_TYPES_INVALID",
                locator="[Content_Types].xml",
                detail="content types do not declare word/document.xml",
            )
        )

    if "word/document.xml" not in names:
        findings.append(
            UnsupportedFinding(
                code="ZIP_MISSING_DOCUMENT",
                locator="word/document.xml",
                detail="package is not a normal DOCX structure",
            )
        )

    findings.extend(_scan_relationships(zf, names))
    findings.extend(_scan_prologs(zf))
    return findings


def _content_types_covers_document(zf: zipfile.ZipFile) -> bool:
    try:
        data = _read(zf, "[Content_Types].xml")
    except ValueError:
        return False
    return b"word/document.xml" in data


def _scan_relationships(zf: zipfile.ZipFile, names: set[str]) -> list[UnsupportedFinding]:
    findings: list[UnsupportedFinding] = []
    for name in sorted(names):
        if not (name.startswith("word/_rels/") and name.endswith(".rels")):
            continue
        try:
            data = _read(zf, name)
        except ValueError as exc:
            findings.append(
                UnsupportedFinding(code="ZIP_REL_UNREADABLE", locator=name, detail=str(exc))
            )
            continue
        if b'TargetMode="External"' in data or b"TargetMode='External'" in data:
            findings.append(
                UnsupportedFinding(
                    code="ZIP_EXTERNAL_RELATIONSHIP",
                    locator=name,
                    detail="relationship targets an external resource",
                )
            )
    return findings


def _scan_prologs(zf: zipfile.ZipFile) -> list[UnsupportedFinding]:
    findings: list[UnsupportedFinding] = []
    for name in iter_xml_parts(zf):
        try:
            data = _read(zf, name)
        except ValueError as exc:
            findings.append(
                UnsupportedFinding(code="ZIP_PART_UNREADABLE", locator=name, detail=str(exc))
            )
            continue
        if has_doctype(data):
            findings.append(
                UnsupportedFinding(
                    code="XML_DOCTYPE", locator=name, detail="DOCTYPE declaration in XML part"
                )
            )
    return findings


def _read(zf: zipfile.ZipFile, name: str) -> bytes:
    return read_capped(zf, name, DEFAULT_READ_CAP)
