"""Assembler expert behaviour, fully offline (FunctionModel / TestModel).

Environment follows tests/unit/tools/test_materials.py: a real Ledger +
ArtifactStore in tmp_path (``env`` fixture from tests/unit/conftest.py), two
Chinese materials, and a three-slot contract. The model double is a scripted
FunctionModel (one script entry per model request, messages captured for
assertions); span handles in scripted outputs are issued for real through the
toolkit first — the Assembler must never accept invented ones.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError
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
from pydantic_ai.models.test import TestModel

from omas.agents.assembler import (
    ASSEMBLER_SYSTEM_PROMPT,
    Assembler,
    AssemblerInvalidError,
    AssemblerProposal,
    SlotProposal,
    assembler_tools,
)
from omas.artifacts.writers import InboxWriter
from omas.domain.artifact import Artifact, ArtifactKind
from omas.domain.errors import ModelGatewayError
from omas.domain.ids import ArtifactId, TaskId, TemplateVersionId
from omas.domain.task import Task
from omas.domain.template import (
    PlanSectionTemplate,
    SlotSpec,
    TemplateContract,
    TemplatePackageHashes,
)
from omas.security.model_gateway import ModelEndpointConfig, ModelGateway
from omas.tools.materials import MaterialToolkit, RunContext
from tests.unit.conftest import EnvBox, make_task

H64 = "a" * 64
TVER = TemplateVersionId("tver_" + "1" * 32)

SALES = (
    "销售周报\n"
    "本周销售额为一百二十三万元，同比增长百分之八。\n"
    "华东地区销售额贡献最大。\n"
    "下周计划：跟进重点客户。"
)
MINUTES = "会议纪要\n今日会议讨论了采购预算。\n华东地区物流成本上升。"

#: body-text marker that appears in no contract semantic requirement, so its
#: absence from the user message proves "metadata only, no body" (T22-adjacent).
BODY_CANARY = "同比增长百分之八"


def _contract() -> TemplateContract:
    return TemplateContract(
        template_id="weekly-report",
        version=1,
        extractor_version="test-1",
        hashes=TemplatePackageHashes(
            docx_sha256=H64,
            contract_sha256=H64,
            styles_sha256=H64,
            static_map_sha256=H64,
        ),
        sections=(
            PlanSectionTemplate(
                section_id="body",
                title="正文",
                slot_ids=("sales_summary", "next_plan", "optional_note"),
            ),
        ),
        slots=(
            SlotSpec(
                slot_id="sales_summary",
                placeholder="{{ sales_summary }}",
                kind="text_block",
                required=True,
                semantic_requirement="本周销售情况",
                style_key="body",
            ),
            SlotSpec(
                slot_id="next_plan",
                placeholder="{{ next_plan }}",
                kind="text_block",
                required=True,
                semantic_requirement="下周计划",
                style_key="body",
            ),
            SlotSpec(
                slot_id="optional_note",
                placeholder="{{ optional_note }}",
                kind="text_block",
                required=False,
                semantic_requirement="备注",
                style_key="body",
            ),
        ),
    )


def _ingest_canonical(
    env: EnvBox, task_id: str, text: str, name: str
) -> tuple[Artifact, str]:
    _raw, cfile, canonical = InboxWriter(env.store).ingest_text(
        task_id, name, text.encode("utf-8")
    )
    artifact = env.ledger.artifacts.register(
        Artifact(
            artifact_id=ArtifactId(cfile.name.removesuffix(".txt")),
            task_id=TaskId(task_id),
            kind=ArtifactKind.CANONICAL_TEXT,
            relative_path=cfile.relative_path,
            sha256=canonical.sha256,
            size=canonical.size_bytes,
            source_refs=(),
            created_at=datetime.now(UTC),
        )
    )
    return artifact, canonical.text


@dataclass
class Setup:
    env: EnvBox
    task: Task
    contract: TemplateContract
    toolkit: MaterialToolkit
    sales: Artifact
    sales_text: str
    minutes: Artifact
    minutes_text: str


@pytest.fixture()
def setup(env: EnvBox) -> Setup:
    task = make_task(env)
    task = env.ledger.tasks.bind_template(task.task_id, TVER)
    sales, sales_text = _ingest_canonical(env, task.task_id, SALES, "sales.md")
    minutes, minutes_text = _ingest_canonical(env, task.task_id, MINUTES, "minutes.md")
    toolkit = MaterialToolkit(
        env.ledger, env.store, RunContext(TaskId(task.task_id), epoch=task.epoch)
    )
    return Setup(
        env=env,
        task=task,
        contract=_contract(),
        toolkit=toolkit,
        sales=sales,
        sales_text=sales_text,
        minutes=minutes,
        minutes_text=minutes_text,
    )


class ScriptedAssembler:
    """FunctionModel-backed agent factory replaying scripted model behaviour.

    A script entry is either ``dict`` (args for the output tool → a final
    proposal) or a ready :class:`ModelResponse` (e.g. a tool call the model
    tries before answering). The last entry repeats; every request's messages
    and tool registry are recorded in :attr:`calls`.
    """

    def __init__(
        self, toolkit: MaterialToolkit, script: list[dict[str, Any] | ModelResponse]
    ) -> None:
        self._toolkit = toolkit
        self._script = script
        self.calls: list[tuple[list[ModelMessage], AgentInfo]] = []

    def factory(self) -> Agent[object, AssemblerProposal]:
        return Agent(
            FunctionModel(self._respond),
            output_type=AssemblerProposal,
            retries=2,
            system_prompt=ASSEMBLER_SYSTEM_PROMPT,
            tools=assembler_tools(self._toolkit),
        )

    def _respond(self, messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        self.calls.append((list(messages), info))
        index = min(len(self.calls) - 1, len(self._script) - 1)
        entry = self._script[index]
        if isinstance(entry, ModelResponse):
            return entry
        return ModelResponse(
            parts=[ToolCallPart(tool_name=info.output_tools[0].name, args=entry)]
        )


def _output_args(slots: list[dict[str, Any]]) -> dict[str, Any]:
    return {"slots": slots}


def _bound(slot_id: str, handles: list[str]) -> dict[str, Any]:
    return {"slot_id": slot_id, "binding_status": "bound", "span_handles": handles}


def _missing(slot_id: str, reason: str = "no_material") -> dict[str, Any]:
    return {"slot_id": slot_id, "binding_status": "missing", "reason_code": reason}


def _part_text(
    call: tuple[list[ModelMessage], AgentInfo],
    part_type: type[UserPromptPart] | type[SystemPromptPart],
) -> str:
    messages, _info = call
    for message in messages:
        if isinstance(message, ModelRequest):
            for part in message.parts:
                if isinstance(part, part_type) and isinstance(part.content, str):
                    return part.content
    raise AssertionError(f"no {part_type.__name__} with text content in captured call")


def user_prompt_of(call: tuple[list[ModelMessage], AgentInfo]) -> str:
    return _part_text(call, UserPromptPart)


def system_prompt_of(call: tuple[list[ModelMessage], AgentInfo]) -> str:
    return _part_text(call, SystemPromptPart)


def _real_handles(setup: Setup) -> tuple[str, str]:
    """Two handles issued by the real toolkit over the sales material."""
    start = setup.sales_text.index("本周销售额")
    first = setup.toolkit.resolve_span(
        setup.sales.artifact_id, start, start + 20
    ).span_handle
    start2 = setup.sales_text.index("下周计划")
    second = setup.toolkit.resolve_span(
        setup.sales.artifact_id, start2, len(setup.sales_text)
    ).span_handle
    return first, second


def _assemble(setup: Setup, model: ScriptedAssembler, **kwargs: Any) -> AssemblerProposal:
    assembler: Assembler = Assembler(**kwargs)
    return assembler.assemble(
        task=setup.task,
        plan=None,
        contract=setup.contract,
        toolkit=setup.toolkit,
        agent_factory=model.factory,
    )


# ------------------------------------------------------------------ happy path


def test_bound_slots_with_real_handles(setup: Setup) -> None:
    first, second = _real_handles(setup)
    model = ScriptedAssembler(
        setup.toolkit,
        [
            _output_args(
                [
                    _bound("sales_summary", [first]),
                    _bound("next_plan", [second]),
                    _missing("optional_note"),
                ]
            )
        ],
    )
    proposal = _assemble(setup, model)
    by_slot = {slot.slot_id: slot for slot in proposal.slots}
    assert by_slot["sales_summary"].binding_status == "bound"
    assert by_slot["sales_summary"].span_handles == (first,)
    assert by_slot["next_plan"].span_handles == (second,)
    assert by_slot["optional_note"].binding_status == "missing"
    assert by_slot["optional_note"].reason_code == "no_material"
    assert len(model.calls) == 1  # valid on the first try: no repair needed


def test_system_prompt_and_all_four_tools_reach_model(setup: Setup) -> None:
    first, _second = _real_handles(setup)
    model = ScriptedAssembler(
        setup.toolkit,
        [_output_args([_bound("sales_summary", [first]), _missing("next_plan")])],
    )
    _assemble(setup, model)
    messages, info = model.calls[0]
    assert system_prompt_of(model.calls[0]) == ASSEMBLER_SYSTEM_PROMPT
    assert sorted(tool.name for tool in info.function_tools) == [
        "list_materials",
        "read_material",
        "resolve_span",
        "search_materials",
    ]
    assert info.output_tools, "AssemblerProposal agent must expose an output tool"
    assert len(messages) >= 1


def test_user_message_carries_metadata_not_body(setup: Setup) -> None:
    first, _second = _real_handles(setup)
    model = ScriptedAssembler(
        setup.toolkit,
        [_output_args([_bound("sales_summary", [first]), _missing("next_plan")])],
    )
    _assemble(setup, model)
    prompt = user_prompt_of(model.calls[0])
    # slot requirements + material metadata are data in the user message …
    assert "slot_id=sales_summary" in prompt
    assert "required=True" in prompt
    assert "semantic_requirement=本周销售情况" in prompt
    assert setup.sales.artifact_id in prompt
    assert setup.minutes.artifact_id in prompt
    assert "共 2 份材料" in prompt
    # … but never body text
    assert BODY_CANARY not in prompt
    assert "会议纪要" not in prompt


# ------------------------------------------------------- DTO rigour (T02 first)


def test_slot_proposal_dto_rejects_extra_text_field() -> None:
    with pytest.raises(ValidationError):
        SlotProposal(
            slot_id="sales_summary",
            binding_status="missing",
            reason_code="no_material",
            text="模型自己写的正文",  # type: ignore[call-arg]
        )


def test_extra_field_hits_schema_retry_path(setup: Setup) -> None:
    """A smuggled `text` field is rejected by extra='forbid' inside
    pydantic-ai's bounded retry; the corrected output then succeeds."""
    first, _second = _real_handles(setup)
    bad = _output_args(
        [
            {**_bound("sales_summary", [first]), "text": "模型自己写的正文"},
            _missing("next_plan"),
        ]
    )
    good = _output_args([_bound("sales_summary", [first]), _missing("next_plan")])
    model = ScriptedAssembler(setup.toolkit, [bad, good])
    proposal = _assemble(setup, model)
    assert {slot.slot_id for slot in proposal.slots} == {"sales_summary", "next_plan"}
    assert len(model.calls) == 2  # validation retry consumed the second entry


# ------------------------------------------------------- semantic repair path


def test_bound_without_handles_is_repaired(setup: Setup) -> None:
    first, _second = _real_handles(setup)
    bad = _output_args(
        [
            {"slot_id": "sales_summary", "binding_status": "bound", "span_handles": []},
            _missing("next_plan"),
        ]
    )
    good = _output_args(
        [_bound("sales_summary", [first]), _missing("next_plan")]
    )
    model = ScriptedAssembler(setup.toolkit, [bad, good])
    proposal = _assemble(setup, model, max_repair_attempts=2)
    assert proposal.slots[0].span_handles == (first,)
    assert len(model.calls) == 2  # one bad attempt, one repaired attempt
    assert "bound slot has no span_handles" in user_prompt_of(model.calls[1])
    assert "bound slot has no span_handles" not in user_prompt_of(model.calls[0])


def test_missing_without_reason_code_is_repaired(setup: Setup) -> None:
    bad = _output_args(
        [
            {"slot_id": "sales_summary", "binding_status": "missing"},
            _missing("next_plan"),
        ]
    )
    good = _output_args([_missing("sales_summary"), _missing("next_plan")])
    model = ScriptedAssembler(setup.toolkit, [bad, good])
    _assemble(setup, model)
    assert len(model.calls) == 2
    assert "missing slot lacks reason_code" in user_prompt_of(model.calls[1])


def test_unknown_slot_is_repaired(setup: Setup) -> None:
    bad = _output_args([_bound("exec_summary", ["span_" + "0" * 32])])
    good = _output_args([_missing("sales_summary"), _missing("next_plan")])
    model = ScriptedAssembler(setup.toolkit, [bad, good])
    proposal = _assemble(setup, model)
    assert "exec_summary" not in {slot.slot_id for slot in proposal.slots}
    assert "unknown slot: exec_summary" in user_prompt_of(model.calls[1])


def test_persistent_violation_exhausts_repair_budget(setup: Setup) -> None:
    bad = _output_args(
        [{"slot_id": "sales_summary", "binding_status": "bound", "span_handles": []}]
    )
    model = ScriptedAssembler(setup.toolkit, [bad])
    with pytest.raises(AssemblerInvalidError) as excinfo:
        _assemble(setup, model, max_repair_attempts=2)
    # initial attempt + 2 repairs = 3 model requests, then a hard stop
    assert len(model.calls) == 3
    assert excinfo.value.code == "ASSEMBLER_INVALID"
    assert "bound slot has no span_handles: sales_summary" in str(excinfo.value)
    # violations carry rule names/slot ids only, never invented model text
    assert "span_handles" in str(excinfo.value)


def test_zero_repair_budget_fails_fast(setup: Setup) -> None:
    bad = _output_args(
        [{"slot_id": "sales_summary", "binding_status": "bound", "span_handles": []}]
    )
    model = ScriptedAssembler(setup.toolkit, [bad])
    with pytest.raises(AssemblerInvalidError):
        _assemble(setup, model, max_repair_attempts=0)
    assert len(model.calls) == 1


# ------------------------------------------------------------ gateway gating


def test_guard_allows_test_endpoint_under_local_only(setup: Setup) -> None:
    first, _second = _real_handles(setup)
    model = ScriptedAssembler(
        setup.toolkit, [_output_args([_bound("sales_summary", [first])])]
    )
    assembler = Assembler(
        gateway=ModelGateway(),  # default local_only
        endpoint=ModelEndpointConfig(provider="test", model_id="offline-double"),
    )
    proposal = assembler.assemble(
        task=setup.task,
        plan=None,
        contract=setup.contract,
        toolkit=setup.toolkit,
        agent_factory=model.factory,
    )
    assert proposal.slots[0].span_handles == (first,)
    assert len(model.calls) == 1


def test_guard_rejects_cloud_endpoint_before_any_model_call(setup: Setup) -> None:
    first, _second = _real_handles(setup)
    model = ScriptedAssembler(
        setup.toolkit, [_output_args([_bound("sales_summary", [first])])]
    )
    assembler = Assembler(
        gateway=ModelGateway(),  # local_only
        endpoint=ModelEndpointConfig(provider="openai", model_id="gpt-test"),
    )
    with pytest.raises(ModelGatewayError) as excinfo:
        assembler.assemble(
            task=setup.task,
            plan=None,
            contract=setup.contract,
            toolkit=setup.toolkit,
            agent_factory=model.factory,
        )
    assert excinfo.value.code == "MODEL_GATEWAY_REJECTED"
    assert model.calls == []  # the model was never invoked


# ------------------------------------------------- tool-error feedback wiring


def test_structured_tool_error_reaches_model_as_retry(setup: Setup) -> None:
    """The model first calls resolve_span with a bogus artifact id; the
    wrapper turns the structured ArtifactNotFoundError into a bounded
    ModelRetry, and the model's next answer binds a real handle."""
    first, _second = _real_handles(setup)
    bogus_call = ModelResponse(
        parts=[
            ToolCallPart(
                tool_name="resolve_span",
                args={"artifact_id": "art_" + "0" * 32, "start": 0, "end": 5},
                tool_call_id="tc_bogus",
            )
        ]
    )
    good = _output_args([_bound("sales_summary", [first]), _missing("next_plan")])
    model = ScriptedAssembler(setup.toolkit, [bogus_call, good])
    proposal = _assemble(setup, model)
    assert proposal.slots[0].span_handles == (first,)
    # two model requests: the failed tool call, then the final answer
    assert len(model.calls) == 2


# ------------------------------------------------------- offline default path


def test_default_factory_is_offline_and_bounded(setup: Setup) -> None:
    """Without an injected factory the offline TestModel default is used; its
    generated slot data cannot satisfy the contract, so the bounded repair
    loop must end in AssemblerInvalidError — with no network access."""
    assembler = Assembler(max_repair_attempts=1)
    with pytest.raises(AssemblerInvalidError):
        assembler.assemble(
            task=setup.task,
            plan=None,
            contract=setup.contract,
            toolkit=setup.toolkit,
        )


def test_default_agent_registers_exactly_four_tools(setup: Setup) -> None:
    """The offline default agent carries the four toolkit tools even though
    TestModel(call_tools=[]) never invokes them (asserted via the request
    parameters TestModel itself captures)."""
    test_model = TestModel(call_tools=[])

    def factory() -> Agent[object, AssemblerProposal]:
        return Agent(
            test_model,
            output_type=AssemblerProposal,
            retries=1,
            system_prompt=ASSEMBLER_SYSTEM_PROMPT,
            tools=assembler_tools(setup.toolkit),
        )

    with pytest.raises(AssemblerInvalidError):
        # expected: schema-generated slot data is outside the contract
        Assembler(max_repair_attempts=0).assemble(
            task=setup.task,
            plan=None,
            contract=setup.contract,
            toolkit=setup.toolkit,
            agent_factory=factory,
        )
    params = test_model.last_model_request_parameters
    assert params is not None
    assert sorted(tool.name for tool in params.function_tools) == [
        "list_materials",
        "read_material",
        "resolve_span",
        "search_materials",
    ]
