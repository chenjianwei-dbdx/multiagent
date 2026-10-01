"""Human-in-the-loop decisions (v1.1 §8).

One awaiting event accepts exactly one valid decision. Decisions carry
``expected_epoch`` so a stale decision can never overwrite a newer round of
waiting. Re-supply keeps old files, bumps epoch, and never changes task_id.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .common import Epoch, Sha256Hex, UtcDatetime
from .ids import ArtifactId, AwaitingEventId, DecisionId, TaskId


class AwaitingEventKind(StrEnum):
    MISSING_MATERIAL = "missing_material"


class AwaitingEvent(BaseModel):
    """A single pause point; refs only, never body text (v1.1 §8)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    awaiting_event_id: AwaitingEventId
    task_id: TaskId
    epoch: Epoch
    kind: AwaitingEventKind = AwaitingEventKind.MISSING_MATERIAL
    missing_slot_ids: tuple[str, ...] = Field(min_length=1)
    created_at: UtcDatetime
    resolved_at: UtcDatetime | None = None

    def is_resolved(self) -> bool:
        return self.resolved_at is not None


class DecisionAction(StrEnum):
    PROVIDE_MATERIAL = "provide_material"
    OMIT_SLOT = "omit_slot"


class Decision(BaseModel):
    """A user decision accepted once; duplicates return the first receipt."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    decision_id: DecisionId
    task_id: TaskId
    expected_epoch: Epoch
    awaiting_event_id: AwaitingEventId
    action: DecisionAction
    #: artifact refs for supplied materials / omission rationale
    attachment_artifact_ids: tuple[ArtifactId, ...] = ()
    #: digest over the canonical payload; same decision_id + different digest
    #: is an IDEMPOTENCY_CONFLICT
    payload_digest: Sha256Hex
    created_at: UtcDatetime


class SlotOverride(BaseModel):
    """User omission of an allowed slot. The binding stays ``missing``.

    Rendered as an empty paragraph with no filler text; disclosed in the
    delivery manifest (v1.1 §8).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: TaskId
    slot_id: str = Field(min_length=1)
    decision_id: DecisionId
    epoch: Epoch
    action: Literal["omit"] = "omit"
    reason_artifact_id: ArtifactId | None = None
    created_at: UtcDatetime = Field(default_factory=lambda: datetime.now(UTC))
