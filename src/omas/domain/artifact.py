"""Artifact records: the ledger's pointer to immutable bytes in the file pool."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .common import Sha256Hex, UtcDatetime
from .ids import ArtifactId, OperationId, TaskId


class ArtifactKind(StrEnum):
    INTENT = "intent"                    # user intent text
    RAW_TEXT = "raw_text"                # raw uploaded text, pre-canonicalisation
    CANONICAL_TEXT = "canonical_text"    # NFC + LF canonical text
    TEMPLATE_DOCX = "template_docx"
    TEMPLATE_SIDECAR = "template_sidecar"  # contract.json / styles.json / static-map.json
    TEMPLATE_MANIFEST = "template_manifest"
    PLAN_IR = "plan_ir"
    BINDING_IR = "binding_ir"
    RENDER_IR = "render_ir"
    DOCX_CANDIDATE = "docx_candidate"
    GATE_REPORT = "gate_report"
    DELIVERY_MANIFEST = "delivery_manifest"
    DELIVERY_DOCX = "delivery_docx"
    LLM_RAW_OUTPUT = "llm_raw_output"
    LLM_PARSED_OUTPUT = "llm_parsed_output"
    AWAITING_EVENT = "awaiting_event"
    USER_DECISION = "user_decision"
    NODE_LOG = "node_log"


class Artifact(BaseModel):
    """Registered file in the artifact pool.

    Immutability is enforced by the ArtifactStore (no overwrite, hash verify
    on read/commit); ``frozen=True`` only protects the DTO itself.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    artifact_id: ArtifactId
    #: None only for template-pool artifacts owned by no single task.
    task_id: TaskId | None = None
    kind: ArtifactKind
    #: POSIX relative path under OMAS_HOME; absolute paths are rejected.
    relative_path: str = Field(min_length=1, pattern=r"^[^/\\]+(.*/[^/\\]+)*$")

    @field_validator("relative_path")
    @classmethod
    def _reject_dot_components(cls, value: str) -> str:
        if any(part in {".", ".."} for part in value.split("/")):
            raise ValueError(f"relative_path must not contain '.' or '..' components: {value!r}")
        return value
    sha256: Sha256Hex
    size: int = Field(ge=0)
    created_by_operation_id: OperationId | None = None
    #: ids of artifacts this artifact was derived from (lineage closure is
    #: reconstructible by walking these refs).
    source_refs: tuple[ArtifactId, ...] = ()
    content_type: str | None = None
    created_at: UtcDatetime
