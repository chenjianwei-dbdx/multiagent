"""Span resolution and verification against canonical text.

Resolution and verification are pure functions over :class:`CanonicalText`;
loading the file behind it and re-checking its hash is the artifact store's
job. ``verify_span`` is what gates and the renderer must call at submit and
render time (v1.1 §4.1: SourceSpanRef is re-validated at submit and render).
"""

from __future__ import annotations

from omas.core.canonical import CanonicalText, sha256_text
from omas.domain.errors import (
    ArtifactHashMismatchError,
    SpanEmptyError,
    SpanHashMismatchError,
    SpanOutOfRangeError,
)
from omas.domain.ids import ArtifactId
from omas.domain.spans import SourceSpanRef


def resolve_span(
    artifact_id: ArtifactId,
    canonical: CanonicalText,
    start: int,
    end: int,
) -> SourceSpanRef:
    """Compute a SourceSpanRef for ``canonical[start:end]``.

    Offsets are Unicode code points (half-open). Hashes are computed here —
    callers (including LLM tools) can never assert their own.
    """
    if start >= end:
        raise SpanEmptyError(f"span must satisfy start < end, got [{start}, {end})")
    if start < 0 or end > canonical.code_points:
        raise SpanOutOfRangeError(
            f"span [{start}, {end}) out of range for {canonical.code_points} code points"
        )
    return SourceSpanRef(
        artifact_id=artifact_id,
        canonical_sha256=canonical.sha256,
        start=start,
        end=end,
        span_sha256=sha256_text(canonical.text[start:end]),
    )


def verify_span(canonical: CanonicalText, ref: SourceSpanRef) -> None:
    """Re-verify a span ref against freshly loaded canonical text.

    Raises on any mismatch; silence means the ref is currently valid.
    """
    if ref.canonical_sha256 != canonical.sha256:
        raise ArtifactHashMismatchError(
            f"canonical hash mismatch for {ref.artifact_id}: "
            f"ref={ref.canonical_sha256} actual={canonical.sha256}"
        )
    if ref.start < 0 or ref.end > canonical.code_points:
        raise SpanOutOfRangeError(
            f"span [{ref.start}, {ref.end}) out of range for {canonical.code_points} code points"
        )
    if ref.start >= ref.end:
        raise SpanEmptyError(f"span must satisfy start < end, got [{ref.start}, {ref.end})")
    actual = sha256_text(canonical.text[ref.start : ref.end])
    if actual != ref.span_sha256:
        raise SpanHashMismatchError(
            f"span hash mismatch for {ref.artifact_id}[{ref.start}, {ref.end}): "
            f"ref={ref.span_sha256} actual={actual}"
        )
