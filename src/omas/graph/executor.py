"""GraphTaskExecutor: TaskExecutor over the LangGraph production graph.

thread_id == task_id; checkpoints live in runtime/checkpoints.sqlite3 (a
separate database from the ledger). Resume is driven with Command(resume=...)
after service.respond committed the decision; the interrupt is never
bypassed even when the ledger already shows the decision (v1.1 §8).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from omas.domain.task import Task
from omas.graph.build import build_graph
from omas.graph.deps import GraphDeps, RenderKwargs
from omas.services.commands import ExecutionOutcome, ExecutionStatus


class GraphTaskExecutor:
    def __init__(
        self,
        deps: GraphDeps,
        render_kwargs_provider: Callable[[str], RenderKwargs | None],
    ) -> None:
        self._deps = deps
        self._render_kwargs_provider = render_kwargs_provider

    def _checkpoint_path(self) -> Path:
        return self._deps.home / "runtime" / "checkpoints.sqlite3"

    def _open_saver(self) -> SqliteSaver:
        self._checkpoint_path().parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self._checkpoint_path()), check_same_thread=False)
        return SqliteSaver(conn)

    def execute(self, task: Task) -> ExecutionOutcome:
        graph = build_graph(self._deps, self._render_kwargs_provider)
        config: Any = {"configurable": {"thread_id": task.task_id}}
        payload: dict[str, Any] = {"task_id": task.task_id, "epoch": task.epoch}
        saver = self._open_saver()
        try:
            graph.checkpointer = saver
            snapshot = graph.get_state(config)
            invoke_input = payload
            if snapshot is not None and snapshot.next:
                pending = getattr(snapshot, "tasks", ())
                interrupting = any(getattr(t, "interrupts", None) for t in pending)
                if interrupting:
                    decision = self._latest_decision(task.task_id)
                    invoke_input = Command(resume=decision)  # type: ignore[assignment]
            result = graph.invoke(invoke_input, config)
        finally:
            saver.conn.close()
        if result.get("__interrupt__") or result.get("awaiting_reason"):
            missing = result.get("missing_slot_ids") or []
            return ExecutionOutcome(
                status=ExecutionStatus.AWAITING_USER, missing_slot_ids=tuple(missing)
            )
        if result.get("delivery_id"):
            return ExecutionOutcome(
                status=ExecutionStatus.COMPLETED, delivery_id=result["delivery_id"]
            )
        return ExecutionOutcome(
            status=ExecutionStatus.FAILED,
            error_code=result.get("last_error_code") or "UNKNOWN",
        )

    def _latest_decision(self, task_id: str) -> str:
        row = self._deps.ledger.connection.execute(
            "SELECT decision_id FROM user_decisions WHERE task_id = ?"
            " ORDER BY created_at DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        return row["decision_id"] if row is not None else ""
