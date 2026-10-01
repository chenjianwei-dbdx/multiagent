"""Deterministic core: canonicalisation, hashing, span resolution, digests."""

from .canonical import (
    CanonicalText,
    canonicalize,
    find_illegal_control_chars,
    sha256_bytes,
    sha256_text,
    validate_renderable_text,
)
from .digest import canonical_model_sha, material_set_digest, operation_key, payload_digest
from .spans import resolve_span, verify_span

__all__ = [
    "CanonicalText",
    "canonical_model_sha",
    "canonicalize",
    "find_illegal_control_chars",
    "material_set_digest",
    "operation_key",
    "payload_digest",
    "resolve_span",
    "sha256_bytes",
    "sha256_text",
    "validate_renderable_text",
    "verify_span",
]
