"""Deterministic digests over artifact sets.

``material_set_digest`` binds a BindingIR to the exact material set it was
assembled against: re-supply (new epoch) changes the set, the digest changes,
and a stale binding can no longer pass Gate A (v1.1 §4.1 / §7).
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable

from pydantic import BaseModel

from .canonical import sha256_text


def material_set_digest(items: Iterable[tuple[str, str]]) -> str:
    """sha256 over the sorted, newline-joined ``artifact_id:sha256`` pairs.

    Order-independent (sorted) so the digest is stable across inventory
    iterations; duplicates collapse because callers pass a set of live refs.
    """
    lines = sorted(f"{artifact_id}:{artifact_sha}" for artifact_id, artifact_sha in items)
    return sha256_text("\n".join(lines))


def operation_key(
    *, task_id: str, epoch: int, node_name: str, input_digest: str, implementation_version: str
) -> str:
    """Canonical idempotency key (v1.1 §8.1)."""
    return f"{task_id}:{epoch}:{node_name}:{input_digest}:{implementation_version}"


def payload_digest(*parts: str) -> str:
    """Digest over request payloads for idempotency-conflict detection."""
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()


def canonical_model_sha(model: BaseModel) -> str:
    """Stable sha256 over a pydantic model's canonical JSON.

    Gate reports bind to ``render_ir_sha`` / subject hashes computed this way;
    the same DTO always yields the same digest (field order is schema-fixed).
    """
    return sha256_text(model.model_dump_json())
