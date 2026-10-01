"""Shared value-level validation types for domain DTOs."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from pydantic import AfterValidator, Field, StringConstraints

Sha256Hex = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
"""Lowercase hex SHA-256 digest (64 chars)."""


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("datetime must be timezone-aware (UTC)")
    return value.astimezone(UTC)


UtcDatetime = Annotated[datetime, AfterValidator(_ensure_utc)]
"""Timezone-aware datetime, normalised to UTC on validation."""

Epoch = Annotated[int, Field(ge=1)]
"""Material-set epoch; starts at 1 and only increases on re-supply."""
