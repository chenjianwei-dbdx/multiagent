"""Bounded ZIP IO helpers shared by the docx safety checker and extractor.

Nothing here trusts ZIP headers: every read is capped at a byte budget so a
lying ``file_size`` cannot turn a scan into an unbounded decompression
(v1.1 §9: "按流读取限制实际解压字节,不能只信 ZIP header").
"""

from __future__ import annotations

import io
import zipfile
from collections.abc import Iterator

#: Default per-part read cap (parts are XML metadata, never near this size).
DEFAULT_READ_CAP = 4 * 1024 * 1024


def open_zip(data: bytes) -> zipfile.ZipFile:
    """Open *data* as a ZIP archive.

    Raises :class:`ValueError` (never a raw zipfile error) for anything that
    is not a readable archive. The caller is responsible for closing.
    """
    try:
        return zipfile.ZipFile(io.BytesIO(data))
    except (zipfile.BadZipFile, zipfile.LargeZipFile, OSError) as exc:
        raise ValueError(f"not a readable ZIP archive: {exc}") from exc


def read_capped(zf: zipfile.ZipFile, name: str, cap: int = DEFAULT_READ_CAP) -> bytes:
    """Read at most *cap* decompressed bytes of *name*.

    Raises :class:`ValueError` when the entry is absent or its decompressed
    stream exceeds the cap (fail closed on lying headers).
    """
    try:
        with zf.open(name) as handle:
            data = handle.read(cap)
            extra = handle.read(1)
    except (KeyError, zipfile.BadZipFile, OSError, RuntimeError) as exc:
        raise ValueError(f"cannot read zip entry {name!r}: {exc}") from exc
    if extra:
        raise ValueError(f"zip entry {name!r} exceeds the {cap} byte read cap")
    return data


def text_part_names(zf: zipfile.ZipFile) -> list[str]:
    """``word/document.xml`` plus every ``word/header*.xml`` / ``word/footer*.xml``.

    Headers/footers are enumerated from the archive itself (probe 4): the
    sectPr relationship indirection is not needed to enumerate text carriers.
    """
    names = set(zf.namelist())
    parts: list[str] = []
    if "word/document.xml" in names:
        parts.append("word/document.xml")
    for prefix in ("header", "footer"):
        indexed = []
        for name in names:
            if name.startswith(f"word/{prefix}") and name.endswith(".xml"):
                digits = name[len(f"word/{prefix}") : -len(".xml")]
                if digits.isdigit():
                    indexed.append((int(digits), name))
        parts.extend(name for _idx, name in sorted(indexed))
    return parts


def iter_xml_parts(zf: zipfile.ZipFile) -> Iterator[str]:
    """Every entry that looks like XML (``.xml`` / ``.rels``), in name order."""
    for name in sorted(zf.namelist()):
        if name.endswith(".xml") or name.endswith(".rels"):
            yield name
