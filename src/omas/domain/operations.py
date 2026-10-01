"""Operations: the idempotency ledger for every business write.

operation_key = task_id + epoch + node_name + input_digest +
implementation_version (v1.1 §8.1). A committed operation is never re-executed
for side effects; replays verify hashes and return the recorded outputs.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from .common import Sha256Hex, UtcDatetime
from .ids import OperationId, TaskId

IMPLEMENTATION_VERSION = "p0.1"


class OperationState(StrEnum):
    INTENT = "intent"
    COMMITTED = "committed"
    ABANDONED = "abandoned"


class Operation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    operation_id: OperationId
    #: unique idempotency key; collisions with a different payload digest are
    #: IDEMPOTENCY_CONFLICT
    operation_key: str = Field(min_length=1)
    state: OperationState = OperationState.INTENT
    task_id: TaskId | None = None
    node_name: str | None = None
    epoch: int | None = None
    payload_digest: Sha256Hex
    created_at: UtcDatetime = Field(default_factory=lambda: datetime.now(UTC))
    committed_at: UtcDatetime | None = None
    #: artifact outputs recorded at commit time (refs only)
    output_artifact_ids: tuple[str, ...] = ()
