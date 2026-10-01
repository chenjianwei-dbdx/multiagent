"""SourceSpanRef: the only permitted pointer for dynamic body text.

Offsets are half-open ``[start, end)`` counted in **Unicode code points** of
the canonical (NFC + LF) text — never bytes and never UTF-16 units. Hashes
are computed and verified by program code only.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .common import Sha256Hex
from .ids import ArtifactId


class SourceSpanRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    artifact_id: ArtifactId
    canonical_sha256: Sha256Hex
    start: int = Field(ge=0)
    end: int = Field(gt=0)
    span_sha256: Sha256Hex

    @model_validator(mode="after")
    def _check_half_open(self) -> SourceSpanRef:
        if self.start >= self.end:
            raise ValueError(
                f"span must be non-empty with start < end, got [{self.start}, {self.end})"
            )
        return self

    def length(self) -> int:
        return self.end - self.start
