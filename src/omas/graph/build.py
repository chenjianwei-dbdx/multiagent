"""Graph assembly: the production topology (v1.1 §7 / P4 prompt).

    ingest(submit) → bind_template(submit) → inventory → plan → research
      → assemble_bind → gap_check ─┬─ missing → wait_for_input (interrupt) ─→ inventory
                                   └─ ok → render_docx → provenance_gate → format_gate → finalize
    (research: llm_allowed only — the LLM picks URLs, the program fetches
    and registers the bytes as materials, ADR D27)

The deterministic tail (render/gates/finalize) shares the content-addressed
DeterministicRenderPipeline; the node names exist as checkpoint boundaries and
each short-circuits once its output id is in state (ADR D18).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast

from langgraph.graph import END, START, StateGraph

from omas.graph.deps import GraphDeps, RenderKwargs
from omas.graph.nodes import (
    make_assemble_node,
    make_gap_check_node,
    make_inventory_node,
    make_plan_node,
    make_render_finalize_node,
    make_research_node,
    make_wait_for_input_node,
)
from omas.graph.state import WorkflowState


def build_graph(
    deps: GraphDeps, render_kwargs_provider: Callable[[str], RenderKwargs | None]
) -> Any:
    inventory = make_inventory_node(deps)
    plan = make_plan_node(deps)
    research = make_research_node(deps)
    assemble = make_assemble_node(deps)
    gap = make_gap_check_node(deps)
    wait = make_wait_for_input_node(deps)
    tail = make_render_finalize_node(deps, render_kwargs_provider)

    builder: StateGraph[WorkflowState] = StateGraph(WorkflowState)

    def gate_after_gap(state: WorkflowState) -> str:
        if state.get("awaiting_reason"):
            return "wait_for_input"
        return "render_docx"

    builder.add_node("inventory", cast(Any, inventory))  # langgraph overload shim
    builder.add_node("plan", cast(Any, plan))  # langgraph overload shim
    builder.add_node("research", cast(Any, research))  # langgraph overload shim
    builder.add_node("assemble_bind", cast(Any, assemble))  # langgraph overload shim
    builder.add_node("gap_check", cast(Any, gap))  # langgraph overload shim
    builder.add_node("wait_for_input", cast(Any, wait))  # langgraph overload shim
    builder.add_node("render_docx", cast(Any, tail))  # langgraph overload shim
    builder.add_node("provenance_gate", cast(Any, _pass_through("provenance_gate")))
    builder.add_node("format_gate", cast(Any, _pass_through("format_gate")))
    builder.add_node("finalize", cast(Any, _finalize_guard(deps)))

    builder.add_edge(START, "inventory")
    builder.add_edge("inventory", "plan")
    builder.add_edge("plan", "research")
    builder.add_edge("research", "assemble_bind")
    builder.add_edge("assemble_bind", "gap_check")
    builder.add_conditional_edges(
        "gap_check",
        gate_after_gap,
        {"wait_for_input": "wait_for_input", "render_docx": "render_docx"},
    )
    builder.add_edge("wait_for_input", "inventory")
    builder.add_edge("render_docx", "provenance_gate")
    builder.add_edge("provenance_gate", "format_gate")
    builder.add_edge("format_gate", "finalize")
    builder.add_edge("finalize", END)
    return builder.compile()


def _pass_through(name: str) -> object:
    """Checkpoint boundary for the deterministic tail's inner stages."""

    def node(state: WorkflowState) -> WorkflowState:
        return state

    node.__name__ = name
    return node


def _finalize_guard(deps: GraphDeps) -> object:
    def finalize(state: WorkflowState) -> WorkflowState:
        if not state.get("delivery_id"):
            state["last_error_code"] = state.get("last_error_code") or "NO_DELIVERY"
        return state

    return finalize
