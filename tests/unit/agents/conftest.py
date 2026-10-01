"""Offline Planner test support: real fixture contract + scripted FunctionModel.

Everything here is offline (pydantic-ai FunctionModel/TestModel); the default
tests must never touch the network (AGENTS.md 测试纪律).
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest
from pydantic_ai import Agent
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    SystemPromptPart,
    ToolCallPart,
    UserPromptPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel

from omas.agents.planner import PLANNER_SYSTEM_PROMPT, PlannerPlan
from omas.domain.template import TemplateContract
from omas.templates import EXTRACTOR_VERSION, build_contract

_FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"
if str(_FIXTURES) not in sys.path:
    sys.path.insert(0, str(_FIXTURES))


@pytest.fixture(scope="session")
def contract() -> TemplateContract:
    """Extract the real contract from the weekly-report fixture template."""
    from weekly_report import build_weekly_report_template

    fixture = build_weekly_report_template()
    return build_contract(
        docx_bytes=fixture.docx_bytes,
        sidecar=fixture.sidecar,
        styles_spec=fixture.styles_spec,
        static_map=fixture.static_map,
        template_id="weekly-report",
        version=1,
        extractor_version=EXTRACTOR_VERSION,
    )


def valid_output_args(contract: TemplateContract) -> dict[str, Any]:
    """A contract-echoing PlannerPlan as output-tool args."""
    sections: list[dict[str, Any]] = []
    for section in contract.sections:
        slots = []
        for slot_id in section.slot_ids:
            spec = contract.slot(slot_id)
            assert spec is not None
            slots.append(
                {
                    "slot_id": spec.slot_id,
                    "kind": spec.kind,
                    "required": spec.required,
                    "style_key": spec.style_key,
                    "semantic_requirement": spec.semantic_requirement,
                    "preferred_source_kind": ["user"],
                    "depends_on": [],
                    "notes": None,
                }
            )
        sections.append({"section_id": section.section_id, "slots": slots})
    return {"sections": sections}


def output_with_extra_slot(contract: TemplateContract) -> dict[str, Any]:
    """A plan that invents a `bonus` slot inside the first section."""
    args = valid_output_args(contract)
    rogue = dict(args["sections"][0]["slots"][0])
    rogue["slot_id"] = "bonus"
    args["sections"][0]["slots"].append(rogue)
    return args


class ScriptedModel:
    """FunctionModel-backed agent factory replaying scripted output-tool args.

    One script entry per model request; the last entry repeats when the model
    is called more often than scripted. Every message list the model receives
    is recorded in :attr:`calls` for assertions.
    """

    def __init__(self, outputs: list[dict[str, Any]]) -> None:
        self.outputs = outputs
        self.calls: list[list[ModelMessage]] = []

    def factory(self) -> Agent[object, PlannerPlan]:
        return Agent(
            FunctionModel(self._respond),
            output_type=PlannerPlan,
            retries=2,
            system_prompt=PLANNER_SYSTEM_PROMPT,
        )

    def _respond(self, messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        self.calls.append(list(messages))
        assert info.output_tools, "PlannerPlan agent must expose an output tool"
        index = min(len(self.calls) - 1, len(self.outputs) - 1)
        return ModelResponse(
            parts=[ToolCallPart(tool_name=info.output_tools[0].name, args=self.outputs[index])]
        )


def _first_part_text(
    call: list[ModelMessage], part_type: type[UserPromptPart] | type[SystemPromptPart]
) -> str:
    for message in call:
        if isinstance(message, ModelRequest):
            for part in message.parts:
                if isinstance(part, part_type):
                    content = part.content
                    if isinstance(content, str):
                        return content
    raise AssertionError(f"no {part_type.__name__} with text content in captured messages")


def user_prompt_of(call: list[ModelMessage]) -> str:
    """The user-turn text of one recorded model request."""
    return _first_part_text(call, UserPromptPart)


def system_prompt_of(call: list[ModelMessage]) -> str:
    """The system-turn text of one recorded model request."""
    return _first_part_text(call, SystemPromptPart)
