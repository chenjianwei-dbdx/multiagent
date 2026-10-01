"""Graph node implementations: thin, idempotent wrappers over services.

Interrupt resume re-executes whole nodes (probe 2), so every node re-checks
the ledger before acting; deterministic stages are content-addressed inside
DeterministicRenderPipeline, so replays reuse committed outputs (v1.1 §8.1).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from omas.domain.artifact import ArtifactKind
from omas.domain.errors import GateBlockedError, MissingRequiredSlotsError, OmasError
from omas.domain.ids import ArtifactId, TaskId, TemplateVersionId, new_awaiting_event_id
from omas.domain.task import Task, TaskStatus
from omas.domain.template import TemplateContract
from omas.graph.deps import GraphDeps, RenderKwargs
from omas.graph.state import WorkflowState
from omas.pipeline.deterministic import DeterministicRenderPipeline

NodeFn = Callable[[WorkflowState], WorkflowState]


def _styles_of(kwargs: RenderKwargs) -> dict[str, Any] | None:
    from omas.domain.template import StyleSpec

    if kwargs.styles_spec is None:
        return None
    return {
        key: spec if isinstance(spec, StyleSpec) else StyleSpec.model_validate(spec)
        for key, spec in kwargs.styles_spec.items()
    }
RenderKwargsProvider = Callable[[str], "RenderKwargs | None"]


def _task(deps: GraphDeps, state: WorkflowState) -> Task:
    task = deps.ledger.tasks.get(TaskId(state["task_id"]))
    if task is None:
        raise OmasError(f"task {state['task_id']} vanished")
    return task



def _contract_of(deps: GraphDeps, task: Task) -> TemplateContract:
    from omas.templates.registry import TemplateRegistry

    assert task.template_version_id is not None, "task not bound to a template"
    return TemplateRegistry(deps.store, deps.ledger).get_contract(task.template_version_id)

def _intent_text(deps: GraphDeps, task_id: str) -> str:
    if deps.intent_loader is not None:
        return deps.intent_loader(task_id)
    row = deps.ledger.connection.execute(
        "SELECT artifact_id FROM artifacts WHERE task_id = ? AND kind = 'intent'"
        " ORDER BY created_at LIMIT 1",
        (task_id,),
    ).fetchone()
    if row is None:
        return ""
    artifact = deps.ledger.artifacts.get(row["artifact_id"])
    if artifact is None:
        return ""
    return deps.store.read_verified(artifact.relative_path, artifact.sha256).decode("utf-8")


def _persist_plan(deps: GraphDeps, task_id: str, plan_json: str) -> str:
    from omas.core.canonical import sha256_bytes

    name = f"{sha256_bytes(plan_json.encode('utf-8'))}.json"
    relative = deps.store.node_out_relative(task_id, "run_plan", name)
    if deps.store.exists(relative):
        existing = deps.ledger.artifacts.get_by_path(relative)
        if existing is not None:
            return existing.artifact_id
    from omas.artifacts.writers import NodeArtifactWriter
    from omas.pipeline.recorders import LedgerRecorder

    recorder = LedgerRecorder(deps.ledger)
    writer = NodeArtifactWriter(deps.store, task_id, "run_plan", recorder=recorder)
    staged = writer.stage(name, plan_json.encode("utf-8"))
    artifact = writer.commit_out(
        staged, kind=ArtifactKind.PLAN_IR, final_name=name, content_type="application/json"
    )
    return artifact.artifact_id


# --------------------------------------------------------------------- nodes


def make_inventory_node(deps: GraphDeps) -> NodeFn:
    def inventory(state: WorkflowState) -> WorkflowState:
        task = _task(deps, state)
        row = deps.ledger.connection.execute(
            "SELECT COUNT(*) AS n FROM artifacts WHERE task_id = ? AND kind = 'canonical_text'",
            (task.task_id,),
        ).fetchone()
        state["epoch"] = task.epoch
        state["template_version"] = task.template_version_id
        deps.ledger.events.append(
            task.task_id, "inventory", refs={}, counts={"materials": int(row["n"])}
        )
        return state

    return inventory


def make_plan_node(deps: GraphDeps) -> NodeFn:
    def plan(state: WorkflowState) -> WorkflowState:
        if state.get("plan_artifact_id"):
            return state  # committed plan survives re-supply (v1.1 §8)
        task = _task(deps, state)
        if deps.planner is None:
            # no LLM in the loop: the binder receives the contract directly
            state["plan_artifact_id"] = None
            return state
        contract = _contract_of(deps, task)
        intent = _intent_text(deps, task.task_id)
        content_plan = deps.planner.plan(
            intent=intent,
            contract=contract,
            task_id=task.task_id,
            template_version_id=task.template_version_id,
        )
        state["plan_artifact_id"] = _persist_plan(
            deps, task.task_id, content_plan.model_dump_json()
        )
        plan_id = state["plan_artifact_id"]
        assert plan_id is not None
        deps.ledger.tasks.set_active_refs(
            TaskId(task.task_id), plan=ArtifactId(plan_id)
        )
        return state

    return plan


def make_research_node(deps: GraphDeps) -> NodeFn:
    """Web gathering (ADR D27): an LLM picks URLs, the program fetches+registers.

    Gated to ``llm_allowed`` tasks with a configured researcher; local_only and
    unconfigured deployments pass through unchanged (today's deterministic
    path with user-supplied materials). Interrupt resume re-executes the whole
    node, so it is idempotent: the researcher's sink registers each (task, url)
    once, and an epoch-scoped operation row stops a replay from re-billing the
    model. A research failure is logged and skipped — the task still proceeds
    to assembly; gap_check remains the safety net for missing slots.
    """

    def research(state: WorkflowState) -> WorkflowState:
        task = _task(deps, state)
        if deps.researcher is None:
            return state
        from omas.domain.task import DataPolicy

        if task.data_policy is not DataPolicy.LLM_ALLOWED:
            return state
        key = f"research:{task.task_id}:{task.epoch}"
        row = deps.ledger.connection.execute(
            "SELECT state FROM operations WHERE operation_key = ?", (key,)
        ).fetchone()
        if row is not None and row["state"] == "committed":
            return state  # 复放复用，不再调模型
        intent = _intent_text(deps, task.task_id)
        contract = _contract_of(deps, task)
        try:
            added = deps.researcher.research(task, contract, intent)
        except Exception as exc:
            # 诊断：记录异常类名与程序自身消息（无响应体）；采集失败非致命
            logging.warning(
                "research node failed (non-fatal) for task %s: %s",
                task.task_id,
                exc,
                exc_info=True,
            )
            deps.ledger.events.append(task.task_id, "research_failed", refs={}, counts={})
            return state
        deps.ledger.events.append(
            task.task_id, "researched", refs={}, counts={"sources": added}
        )
        # 标记本 epoch 采集完成（内容寻址操作行，复放跳过、不重复计费）
        from omas.core.digest import payload_digest
        from omas.domain.ids import new_operation_id
        from omas.domain.operations import Operation, OperationState

        deps.ledger.operations.ensure_intent(
            Operation(
                operation_id=new_operation_id(),
                operation_key=key,
                task_id=task.task_id,
                node_name="research",
                epoch=task.epoch,
                payload_digest=payload_digest(key, str(task.epoch), str(added)),
                state=OperationState.COMMITTED,
                committed_at=datetime.now(UTC),
            )
        )
        return state

    return research


def make_assemble_node(deps: GraphDeps) -> NodeFn:
    def assemble_bind(state: WorkflowState) -> WorkflowState:
        task = _task(deps, state)
        if deps.binder is None:
            state["last_error_code"] = "ASSEMBLER_NOT_CONFIGURED"
            return state
        contract = _contract_of(deps, task)
        from omas.domain.errors import BudgetExceededError

        try:
            binding, artifact = deps.binder.bind(task, contract)
        except BudgetExceededError as exc:
            # 预算耗尽是停止条件（v1.1 §6）：不猜、不糊弄，降级为缺槽追问，
            # 由用户补充材料后重放——而不是让整轮崩溃
            deps.ledger.events.append(task.task_id, "budget_exhausted", refs={}, counts={})
            state["last_error_code"] = exc.code
            return state
        # 绑定失败（非预算）由上层节点状态机处理
        state["binding_artifact_id"] = artifact.artifact_id
        state["epoch"] = task.epoch
        deps.ledger.tasks.set_active_refs(
            TaskId(task.task_id), binding=artifact.artifact_id
        )
        deps.ledger.events.append(
            task.task_id,
            "assembled",
            refs={"binding": artifact.artifact_id},
            counts={"slots": len(binding.bindings)},
        )
        return state

    return assemble_bind


def make_gap_check_node(deps: GraphDeps) -> NodeFn:
    def gap_check(state: WorkflowState) -> WorkflowState:
        """Idempotently record one AwaitingEvent for this epoch's gaps."""
        task = _task(deps, state)
        row = deps.ledger.connection.execute(
            "SELECT missing_slot_ids_json, awaiting_event_id FROM awaiting_events"
            " WHERE task_id = ? AND epoch = ? AND resolved_at IS NULL LIMIT 1",
            (task.task_id, task.epoch),
        ).fetchone()
        if row is not None:
            import json

            state["awaiting_event_id"] = row["awaiting_event_id"]
            state["missing_slot_ids"] = list(json.loads(row["missing_slot_ids_json"]))
            state["awaiting_reason"] = "missing_material"
            return state
        missing = _missing_required(deps, task)
        if not missing:
            state["awaiting_reason"] = None
            state["missing_slot_ids"] = []
            return state
        import json

        event_id = new_awaiting_event_id()
        deps.ledger.awaiting_events.create(
            __import__("omas").domain.decisions.AwaitingEvent(
                awaiting_event_id=event_id,
                task_id=TaskId(task.task_id),
                epoch=task.epoch,
                missing_slot_ids=tuple(missing),
                created_at=datetime.now(UTC),
            )
        )
        deps.ledger.tasks.update_status(TaskId(task.task_id), TaskStatus.AWAITING_USER)
        deps.ledger.events.append(
            task.task_id,
            "awaiting_user",
            refs={"awaiting_event": event_id},
            counts={"missing": len(missing)},
        )
        state["awaiting_event_id"] = event_id
        state["missing_slot_ids"] = list(missing)
        state["awaiting_reason"] = "missing_material"
        return state

    return gap_check


def _missing_required(deps: GraphDeps, task: Task) -> list[str]:
    """Required slots with neither a bound span nor a recorded user omission."""
    contract = _contract_of(deps, task)
    overrides = deps.ledger.slot_overrides.active_for_task(TaskId(task.task_id), task.epoch)
    binding_row = deps.ledger.connection.execute(
        "SELECT binding_id FROM bindings WHERE task_id = ? AND epoch = ?"
        " ORDER BY binding_version DESC LIMIT 1",
        (task.task_id, task.epoch),
    ).fetchone()
    bound_slots: set[str] = set()
    if binding_row is not None:
        rows = deps.ledger.connection.execute(
            "SELECT slot_id, binding_status FROM bindings WHERE task_id = ? AND epoch = ?",
            (task.task_id, task.epoch),
        ).fetchall()
        bound_slots = {r["slot_id"] for r in rows if r["binding_status"] == "bound"}
    missing: list[str] = []
    for spec in contract.slots:
        if not spec.required:
            continue
        if spec.slot_id in bound_slots:
            continue
        if spec.slot_id in overrides:
            continue
        missing.append(spec.slot_id)
    return missing


def make_render_finalize_node(
    deps: GraphDeps, render_kwargs_provider: RenderKwargsProvider
) -> NodeFn:
    def render_and_finalize(state: WorkflowState) -> WorkflowState:
        """Deterministic tail: IR → Gate A → render → Gate B → format → finalize.

        The pipeline is content-addressed and replay-safe, so this node is
        invoked once per epoch and simply reuses committed outputs on replay.
        """
        task = _task(deps, state)
        if state.get("delivery_id"):
            return state
        if deps.binder is None:
            state["last_error_code"] = "ASSEMBLER_NOT_CONFIGURED"
            return state
        contract = _contract_of(deps, task)
        kwargs = render_kwargs_provider(task.task_id)
        if kwargs is None:
            state["last_error_code"] = "TEMPLATE_UNAVAILABLE"
            return state
        binding, binding_artifact = deps.binder.bind(task, contract)
        plan_artifact = ArtifactId(state.get("plan_artifact_id") or binding_artifact.artifact_id)
        pipeline = DeterministicRenderPipeline(deps.home, deps.store, deps.ledger)
        try:
            delivery = pipeline.run(
                task=task,
                binding=binding,
                contract=kwargs.contract,
                template_version_id=TemplateVersionId(kwargs.template_version_id),
                template_docx=kwargs.template_docx,
                template_relative_path=kwargs.template_relative_path,
                static_map=kwargs.static_map,
                styles_spec=_styles_of(kwargs),
                plan_artifact_id=plan_artifact,
                binding_artifact_id=binding_artifact.artifact_id,
            )
        except MissingRequiredSlotsError as exc:
            state["last_error_code"] = exc.code
            return state
        except GateBlockedError as exc:
            state["last_error_code"] = exc.code
            deps.ledger.tasks.update_status(TaskId(task.task_id), TaskStatus.FAILED)
            return state
        state["binding_artifact_id"] = binding_artifact.artifact_id
        state["docx_artifact_id"] = delivery.candidate_artifact_id
        state["delivery_id"] = delivery.delivery_id
        return state

    return render_and_finalize


def make_wait_for_input_node(deps: GraphDeps) -> NodeFn:
    def wait_for_input(state: WorkflowState) -> WorkflowState:
        """The single fixed interrupt point (v1.1 §8).

        The AwaitingEvent was already committed idempotently by gap_check;
        this node only parks the graph. Resume is driven by the executor with
        Command(resume=<decision_id>) after service.respond committed it.
        """
        from langgraph.types import interrupt

        payload = {
            "reason": state.get("awaiting_reason") or "missing_material",
            "missing_slot_ids": state.get("missing_slot_ids", []),
            "awaiting_event_id": state.get("awaiting_event_id"),
        }
        interrupt(payload)
        # after resume: re-supply goes back through inventory (v1.1 §8)
        return state

    return wait_for_input
