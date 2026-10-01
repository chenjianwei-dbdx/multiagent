"""Text canonicalisation and hashing.

Canonical rules (Master §7.1, v1.1 §4.1):

- input must be valid UTF-8 (strict; invalid encodings are rejected, not replaced)
- Unicode → NFC
- CRLF / CR → LF
- nothing else changes — no trimming, no whitespace collapsing

All spans are counted in Unicode code points over the canonical text; hashes
are SHA-256 over the full UTF-8 encoding (whole text) resp. the slice's UTF-8
encoding (span).

XML-illegal / docxtpl-dangerous control characters are detected but never
silently filtered: callers must reject and surface the positions (v1.1 §3.2).
"""

from __future__ import annotations

import hashlib
import unicodedata
from dataclasses import dataclass

from omas.domain.errors import DecodeError

#: Characters that may not appear in renderable text. XML 1.0 forbids most
#: C0 controls except TAB/LF/CR; docxtpl additionally treats ``\a`` and ``\f``
#: as rich-text control characters, so they are refused outright instead of
#: being given template semantics.
_RENDERABLE_FORBIDDEN = frozenset(chr(c) for c in range(0x20)) - {"\t", "\n", "\r"}


@dataclass(frozen=True, slots=True)
class CanonicalText:
    """Canonical (NFC + LF) text with its program-computed digest."""

    text: str
    sha256: str
    code_points: int
    size_bytes: int

    def __len__(self) -> int:
        return self.code_points


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    """SHA-256 over the full UTF-8 encoding of *text*."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def canonicalize(raw: str | bytes) -> CanonicalText:
    """Canonicalise raw text material. NFC + LF only; nothing else changes."""
    if isinstance(raw, bytes):
        try:
            text = raw.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise DecodeError(f"input is not valid UTF-8: byte {exc.start}") from exc
    else:
        text = raw
    normalized = unicodedata.normalize("NFC", text)
    # CR LF -> LF first so no lone CR survives; plain LF passes through.
    normalized = normalized.replace("\r\n", "\n").replace("\r", "\n")
    encoded = normalized.encode("utf-8")
    return CanonicalText(
        text=normalized,
        sha256=hashlib.sha256(encoded).hexdigest(),
        code_points=len(normalized),
        size_bytes=len(encoded),
    )


def find_illegal_control_chars(text: str) -> tuple[int, ...]:
    """Positions (code point indexes) of characters forbidden in renderable text."""
    return tuple(i for i, ch in enumerate(text) if ch in _RENDERABLE_FORBIDDEN)


def validate_renderable_text(text: str) -> None:
    """Reject text containing forbidden control characters.

    Raises ValueError listing the first offending positions; the caller must
    surface this to the user instead of filtering (v1.1 §3.2).
    """
    positions = find_illegal_control_chars(text)
    if positions:
        sample = ", ".join(f"index {i} (U+{ord(text[i]):04X})" for i in positions[:5])
        raise ValueError(f"text contains forbidden control characters: {sample}")
