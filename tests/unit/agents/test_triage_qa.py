"""Triage 与 QA Agent 离线测试（FunctionModel 脚本化输出）。"""

from __future__ import annotations

from typing import Any

from pydantic_ai import Agent
from pydantic_ai.messages import ModelMessage, ModelResponse, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from omas.agents.qa import QAAgent, QAAnswer
from omas.agents.triage import TriageAgent, TriageResult
from omas.security.model_gateway import ModelGateway


def _scripted(fn: Any) -> FunctionModel:
    return FunctionModel(fn)


def _triage_model(category: str, clarify: str | None = None) -> FunctionModel:
    def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        args: dict[str, Any] = {"category": category, "reason": "测试"}
        if clarify:
            args["clarifying_question"] = clarify
        return ModelResponse(
            parts=[ToolCallPart(tool_name=info.output_tools[0].name, args=args)]
        )

    return _scripted(fn)


def _qa_model(answer: str, sources: list[str] | None = None) -> FunctionModel:
    def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return ModelResponse(
            parts=[
                ToolCallPart(
                    tool_name=info.output_tools[0].name,
                    args={"answer": answer, "sources": sources or []},
                )
            ]
        )

    return _scripted(fn)


def _factory_for(model: FunctionModel, output_type: type) -> Any:
    def factory() -> Agent[object, Any]:
        return Agent(model=model, output_type=output_type, retries=1)

    return factory


def test_triage_categories() -> None:
    for category in ("document_task", "question", "needs_info"):
        agent = TriageAgent()
        result = agent.classify(
            message="随便一句话",
            has_attachments=False,
            template_summary="weekly-report",
            agent_factory=_factory_for(_triage_model(category), TriageResult),
        )
        assert result.category == category
        assert agent.last_usage is not None


def test_triage_fills_default_clarification() -> None:
    agent = TriageAgent()
    result = agent.classify(
        message="帮我弄个东西",
        has_attachments=False,
        template_summary="t",
        agent_factory=_factory_for(_triage_model("needs_info"), TriageResult),
    )
    assert result.clarifying_question  # never empty for needs_info


class _FakeSearch:
    """记录查询的假搜索客户端（离线）。"""

    enabled = True

    def __init__(self) -> None:
        self.queries: list[str] = []

    def search(self, query: str, *, max_results: int = 5) -> Any:
        from omas.websearch.client import WebResult  # noqa: F401

        self.queries.append(query)

        class _Outcome:
            provider = "fake"
            results = ()

        return _Outcome()


def test_qa_answer_without_and_with_search() -> None:
    gateway = ModelGateway()
    agent = QAAgent()
    answer = agent.answer_question(
        question="今天天气如何？",
        search=None,
        gateway=gateway,
        agent_factory=lambda **kw: _factory_for(_qa_model("不知道，未联网"), QAAnswer)(),
    )
    assert answer.answer.startswith("不知道")
    assert answer.sources == ()
    assert agent.search_log == []

    fake = _FakeSearch()

    class _Recorder:
        def __call__(self, **kwargs: Any) -> Agent[object, Any]:
            qa = kwargs.get("search")
            assert qa is fake

            def factory() -> Agent[object, Any]:
                inner = Agent(
                    model=_qa_model("晴，20 度", ["天气网 | https://weather.example"]),
                    output_type=QAAnswer,
                    retries=1,
                    tools=QAAgent()._tools(fake, kwargs["gateway"]),
                )
                return inner

            return factory()

    # 直接用可调用工具的脚本模型走一次带搜索路径
    agent2 = QAAgent()

    def scripted(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        # 第一步先调工具，第二步给最终答案
        if not any(
            getattr(p, "tool_name", None) == "web_search"
            for m in messages
            if hasattr(m, "parts")
            for p in getattr(m, "parts", [])
        ):
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name="web_search", args={"query": "上海 天气"}, tool_call_id="c1"
                    )
                ]
            )
        return ModelResponse(
            parts=[
                ToolCallPart(
                    tool_name=info.output_tools[0].name,
                    args={"answer": "上海晴，20 度", "sources": ["天气网 | https://w.example"]},
                )
            ]
        )

    model = FunctionModel(scripted)
    answer2 = agent2.answer_question(
        question="上海天气？",
        search=None,
        gateway=gateway,
        agent_factory=lambda **kw: Agent(
            model=model, output_type=QAAnswer, retries=2, tools=agent2._tools(fake, gateway)
        ),
    )
    assert answer2.answer == "上海晴，20 度"
    assert fake.queries == ["上海 天气"]
    assert agent2.search_log == ["上海 天气"]
