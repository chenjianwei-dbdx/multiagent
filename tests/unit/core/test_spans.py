"""Span resolution / verification tests (T03, T17 foundations)."""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from omas.core.canonical import canonicalize
from omas.core.spans import resolve_span, verify_span
from omas.domain.errors import (
    ArtifactHashMismatchError,
    SpanEmptyError,
    SpanHashMismatchError,
    SpanOutOfRangeError,
)
from omas.domain.ids import ArtifactId

ART = ArtifactId("art_" + "0" * 32)


@given(st.text(min_size=2, max_size=100), st.data())
def test_roundtrip(text: str, data: st.DataObject) -> None:
    canonical = canonicalize(text)
    start = data.draw(st.integers(min_value=0, max_value=canonical.code_points - 1))
    end = data.draw(st.integers(min_value=start + 1, max_value=canonical.code_points))
    ref = resolve_span(ART, canonical, start, end)
    assert ref.start == start
    assert ref.end == end
    assert ref.canonical_sha256 == canonical.sha256
    verify_span(canonical, ref)  # must not raise
    # span hash is over the slice's UTF-8 encoding
    import hashlib

    expected = hashlib.sha256(canonical.text[start:end].encode("utf-8")).hexdigest()
    assert ref.span_sha256 == expected


def test_empty_span_rejected() -> None:
    canonical = canonicalize("hello")
    with pytest.raises(SpanEmptyError):
        resolve_span(ART, canonical, 2, 2)
    with pytest.raises(SpanEmptyError):
        resolve_span(ART, canonical, 3, 2)


def test_out_of_range_rejected() -> None:
    canonical = canonicalize("hello")
    with pytest.raises(SpanOutOfRangeError):
        resolve_span(ART, canonical, 0, 6)
    with pytest.raises(SpanOutOfRangeError):
        resolve_span(ART, canonical, -1, 3)


def test_span_hash_mismatch_detected() -> None:
    """T03 core: a forged/self-reported span hash must be caught by the verifier."""
    canonical = canonicalize("本周销售情况非常好")
    forged = resolve_span(ART, canonical, 0, canonical.code_points).model_copy(
        update={"span_sha256": "b" * 64}
    )
    with pytest.raises(SpanHashMismatchError):
        verify_span(canonical, forged)


def test_edited_material_changes_canonical_hash() -> None:
    """Content edited after resolution flips the canonical hash -> blocked."""
    original = canonicalize("本周销售情况非常好")
    ref = resolve_span(ART, original, 0, original.code_points)
    tampered = canonicalize("本周销售情况非常糟糕")
    with pytest.raises(ArtifactHashMismatchError):
        verify_span(tampered, ref)


def test_canonical_file_hash_mismatch_detected() -> None:
    original = canonicalize("same length text one")
    other = canonicalize("same length text two")
    ref = resolve_span(ART, original, 0, 5)
    with pytest.raises(ArtifactHashMismatchError):
        verify_span(other, ref)


def test_span_offsets_are_code_points_not_bytes() -> None:
    canonical = canonicalize("a😀b")
    assert canonical.code_points == 3
    ref = resolve_span(ART, canonical, 1, 2)  # exactly the emoji
    verify_span(canonical, ref)
    # the span sha equals the emoji's UTF-8 encoding digest, not a byte slice
    import hashlib

    assert ref.span_sha256 == hashlib.sha256("😀".encode()).hexdigest()


def test_crlf_normalisation_affects_offsets() -> None:
    """Offsets must be computed over canonical (LF) text, not raw CRLF text."""
    raw = "line one\r\nline two"
    canonical = canonicalize(raw)
    assert canonical.code_points == len("line one\nline two")
    # span across the former CRLF boundary
    ref = resolve_span(ART, canonical, 7, 10)
    assert canonical.text[7:10] == "e\nl"
    verify_span(canonical, ref)
