"""LangGraph workflow state (Master §9): IDs and flow fields only.

Business facts live in the ledger; bytes live in the pool. Anything that
looks like content here is a bug (I5).
"""

from __future__ import annotations

from typing import TypedDict


class WorkflowState(TypedDict, total=False):
    task_id: str
    epoch: int
    pipeline_profile: str

    template_version: str | None
    plan_artifact_id: str | None
    binding_artifact_id: str | None
    render_ir_artifact_id: str | None
    docx_artifact_id: str | None
    delivery_id: str | None

    awaiting_event_id: str | None
    awaiting_reason: str | None
    missing_slot_ids: list[str]
    last_error_code: str | None
