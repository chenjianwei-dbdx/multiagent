"""Research node/sink/agent: idempotency, policy gating, tool resilience.

All offline: the httpx module reference inside ``omas.websearch.client`` is
stubbed, and the LLM is a scripted FunctionModel that calls ``add_source``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic_ai.messages import ModelMessage, ModelResponse, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from omas.agents.research import ResearchAgent, ResearchReport
from omas.domain.ids import TaskId, new_task_id
from omas.domain.task import DataPolicy, Task
from omas.security.model_gateway import ModelGateway
from omas.services.research import RESEARCH_MAX_SOURCES, ResearchService

# ------------------------------------------------------------------ harness


class _Context:
    """Minimal home/store/ledger wiring shared by these tests."""

    def __init__(self, tmp_path: Path) -> None:
        self.home = tmp_path
        from omas.artifacts.store import ArtifactStore
        from omas.storage.db import Ledger, connect

        self.conn = connect(tmp_path / "ledger.sqlite3")
        self.ledger = Ledger(self.conn)
        self.store = ArtifactStore(tmp_path)
        self.task = self._make_task()

    def _make_task(self) -> Task:
        now_task_id = new_task_id()
        task = Task(
            task_id=TaskId(now_task_id),
            request_id=f"req_{now_task_id}",
            data_policy=DataPolicy.LLM_ALLOWED,
            created_at=__import__("datetime").datetime.now(
                __import__("datetime").UTC
            ),
            updated_at=__import__("datetime").datetime.now(
                __import__("datetime").UTC
            ),
        )
        self.ledger.tasks.create(task, "digest-test")
        return task

    def close(self) -> None:
        self.ledger.close()


class _FakeFetch:
    """Substitutes omas.websearch.client.fetch_page for add_source."""

    def __init__(self, pages: dict[str, str]) -> None:
        self.pages = pages
        self.calls: list[str] = []

    def fetch_page(self, url: str, **_kwargs: object) -> str:
        self.calls.append(url)
        if url not in self.pages:
            raise RuntimeError("unknown url in test fixture")
        return self.pages[url]


def _install_fake_fetch(monkeypatch: pytest.MonkeyPatch, pages: dict[str, str]) -> _FakeFetch:
    fake = _FakeFetch(pages)
    monkeypatch.setattr(
        "omas.agents.research.fetch_page_import_shim", fake.fetch_page, raising=False
    )
    # The research agent imports fetch_page inside its tool; patch the module
    # attribute the tool looks it up from.
    import omas.websearch.client as ws

    monkeypatch.setattr(ws, "fetch_page", fake.fetch_page)
    return fake


def _scripted_research_model(calls: list[dict[str, Any]]) -> FunctionModel:
    """A model that calls add_source(url=...) once per scripted call, then returns the report."""

    def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        already = sum(
            1 for m in messages for p in getattr(m, "parts", [])
            if getattr(p, "tool_name", None) == "add_source"
        )
        if already < len(calls):
            call = calls[already]
            return ModelResponse(
                parts=[
                    ToolCallPart(tool_name="add_source", args=call, tool_call_id=f"c{already}")
                ]
            )
        return ModelResponse(
            parts=[
                ToolCallPart(
                    tool_name=info.output_tools[0].name,
                    args={"slot_notes": ["ok"], "notes": "done"},
                )
            ]
        )

    return FunctionModel(fn)


# ------------------------------------------------------------------ sink


def test_sink_registers_material_with_provenance(tmp_path: Path) -> None:
    ctx = _Context(tmp_path)
    try:
        sink = ResearchService(ctx.store, ctx.ledger, str(ctx.task.task_id))
        material = sink.register_fetched(
            url="https://example.com/a", query="test query", text="第一段正文内容"
        )
        assert material.registered
        row = ctx.ledger.research_sources.for_task(str(ctx.task.task_id))
        assert len(row) == 1
        assert row[0]["source_url"] == "https://example.com/a"
        assert row[0]["query"] == "test query"
        # the artifact is a canonical-text material, visible to the assembler
        artifact = ctx.ledger.artifacts.get(material.artifact.artifact_id)
        assert artifact is not None
        assert artifact.kind.value == "canonical_text"
        assert (
            ctx.store.read_verified(artifact.relative_path, artifact.sha256)
            .decode("utf-8")
            .startswith("第一段")
        )
    finally:
        ctx.close()


def test_sink_is_idempotent_per_task_and_url(tmp_path: Path) -> None:
    ctx = _Context(tmp_path)
    try:
        sink = ResearchService(ctx.store, ctx.ledger, str(ctx.task.task_id))
        first = sink.register_fetched(url="https://example.com/a", query="q", text="正文")
        second = sink.register_fetched(url="https://example.com/a", query="q", text="正文")
        assert first.artifact.artifact_id == second.artifact.artifact_id
        assert second.registered is False
        assert sink.source_count() == 1
    finally:
        ctx.close()


def test_sink_budget_caps_sources(tmp_path: Path) -> None:
    ctx = _Context(tmp_path)
    try:
        sink = ResearchService(ctx.store, ctx.ledger, str(ctx.task.task_id))
        for index in range(RESEARCH_MAX_SOURCES):
            sink.register_fetched(
                url=f"https://example.com/{index}", query="q", text=f"正文{index}"
            )
        with pytest.raises(Exception, match="budget"):
            sink.register_fetched(url="https://example.com/overflow", query="q", text="x")
    finally:
        ctx.close()


# ------------------------------------------------------------------ node


def test_research_node_skips_local_only(tmp_path: Path) -> None:
    """local_only tasks never construct the model — zero egress."""
    from omas.graph.deps import GraphDeps
    from omas.graph.nodes import make_research_node

    ctx = _Context(tmp_path)
    # flip policy to local_only by creating a second task
    local = Task(
        task_id=TaskId(new_task_id()),
        request_id=f"req_{new_task_id()}",
        data_policy=DataPolicy.LOCAL_ONLY,
        created_at=ctx.task.created_at,
        updated_at=ctx.task.updated_at,
    )
    ctx.ledger.tasks.create(local, "digest-local")

    class _Boom:
        def research(self, task: Task, contract: Any, intent: str) -> int:
            raise AssertionError("local_only must not reach the researcher")

    deps = GraphDeps(
        home=ctx.home, store=ctx.store, ledger=ctx.ledger, researcher=_Boom()
    )
    node = make_research_node(deps)
    try:
        node({"task_id": str(local.task_id), "epoch": 1})
        sources = ctx.ledger.research_sources.for_task(str(local.task_id))
        assert sources == []
        ops = ctx.ledger.connection.execute(
            "SELECT * FROM operations WHERE operation_key LIKE 'research:%'",
        ).fetchall()
        assert ops == []
    finally:
        ctx.close()


def _bind_weekly_template(ctx: _Context) -> str:
    """Register the real weekly-report fixture and bind the task to it."""
    import sys

    fixtures = Path(__file__).resolve().parents[2] / "fixtures"
    if str(fixtures) not in sys.path:
        sys.path.insert(0, str(fixtures))
    from weekly_report import build_weekly_report_template

    from omas.templates.registry import TemplateRegistry

    weekly = build_weekly_report_template()
    registry = TemplateRegistry(ctx.store, ctx.ledger)
    version_id, _contract = registry.register(
        docx_bytes=weekly.docx_bytes,
        sidecar=weekly.sidecar,
        styles_spec=weekly.styles_spec,
        static_map=weekly.static_map,
        template_id="weekly-report",
    )
    ctx.ledger.tasks.bind_template(ctx.task.task_id, version_id)
    return str(version_id)


def test_research_node_registers_and_is_replay_safe(tmp_path: Path) -> None:
    from omas.graph.deps import GraphDeps
    from omas.graph.nodes import make_research_node

    ctx = _Context(tmp_path)
    pages = {"https://example.com/research": "调研发现正文片段"}
    _install_fake_fetch(pytest.MonkeyPatch(), pages)
    _bind_weekly_template(ctx)

    added: list[int] = []

    class _FakeResearcher:
        def research(self, task: Task, contract: Any, intent: str) -> int:
            sink = ResearchService(ctx.store, ctx.ledger, str(task.task_id))
            sink.register_fetched(
                url="https://example.com/research", query=intent, text="调研发现正文片段"
            )
            added.append(1)
            return 1

    deps = GraphDeps(
        home=ctx.home, store=ctx.store, ledger=ctx.ledger, researcher=_FakeResearcher()
    )
    node = make_research_node(deps)
    state = {"task_id": str(ctx.task.task_id), "epoch": 1}
    try:
        node(state)
        node(state)  # 复放：committed 操作行使第二次不再调 researcher
        assert len(added) == 1
        events = ctx.ledger.connection.execute(
            "SELECT event_code FROM events WHERE task_id = ?", (str(ctx.task.task_id),)
        ).fetchall()
        assert [e["event_code"] for e in events] == ["researched"]
        assert len(ctx.ledger.research_sources.for_task(str(ctx.task.task_id))) == 1
    finally:
        ctx.close()


# ------------------------------------------------------------------ agent


def test_research_agent_adds_sources_via_tool(tmp_path: Path) -> None:
    ctx = _Context(tmp_path)
    pages = {"https://example.com/good": "权威来源的正文内容，应被登记为材料。"}
    fake = _install_fake_fetch(pytest.MonkeyPatch(), pages)
    sink = ResearchService(ctx.store, ctx.ledger, str(ctx.task.task_id))
    agent = ResearchAgent()

    model = _scripted_research_model(
        [{"url": "https://example.com/good", "query": "主题"}]
    )

    from pydantic_ai import Agent

    gateway = ModelGateway(policy=DataPolicy.LLM_ALLOWED)

    def factory(**_kwargs: object) -> Any:
        return Agent(
            model=model,
            output_type=ResearchReport,
            retries=2,
            system_prompt="test",
            tools=agent.tools(None, gateway, sink),
        )

    report = agent.research(
        intent="调研某主题",
        contract=None,  # type: ignore[arg-type]
        task=ctx.task,
        search=None,
        gateway=gateway,
        sink=sink,
        agent_factory=factory,
    )
    assert fake.calls == ["https://example.com/good"]
    assert sink.source_count() == 1
    assert agent.added == ["https://example.com/good"]
    assert report.slot_notes == ("ok",)
    assert agent.last_usage is not None


def test_research_agent_survives_fetch_failure(tmp_path: Path) -> None:
    """抓取失败的来源只是少一条材料，不能让整轮中断。"""
    ctx = _Context(tmp_path)
    # fetch_page for this URL raises inside the tool -> string result, not an exception
    import omas.websearch.client as ws
    from omas.websearch.client import WebSearchError

    def broken_fetch(url: str, **_kwargs: object) -> str:
        raise WebSearchError("page fetch failed (ConnectTimeout)")

    monkey = pytest.MonkeyPatch()
    monkey.setattr(ws, "fetch_page", broken_fetch)
    sink = ResearchService(ctx.store, ctx.ledger, str(ctx.task.task_id))
    agent = ResearchAgent()

    model = _scripted_research_model(
        [{"url": "https://example.com/dead", "query": "主题"}]
    )
    from pydantic_ai import Agent

    gateway = ModelGateway(policy=DataPolicy.LLM_ALLOWED)

    def factory(**_kwargs: object) -> Any:
        return Agent(
            model=model,
            output_type=ResearchReport,
            retries=2,
            system_prompt="test",
            tools=agent.tools(None, gateway, sink),
        )

    report = agent.research(
        intent="调研某主题",
        contract=None,  # type: ignore[arg-type]
        task=ctx.task,
        search=None,
        gateway=gateway,
        sink=sink,
        agent_factory=factory,
    )
    monkey.undo()
    try:
        assert sink.source_count() == 0  # 失败来源未登记
        assert report.notes == "done"  # 但轮次正常完成
    finally:
        ctx.close()


def test_digest_does_not_consume_web_source_budget(tmp_path: Path) -> None:
    ctx = _Context(tmp_path)
    try:
        sink = ResearchService(ctx.store, ctx.ledger, str(ctx.task.task_id))
        for index in range(RESEARCH_MAX_SOURCES):
            sink.register_fetched(
                url=f"https://example.com/{index}", query="q", text=f"正文{index}"
            )
        # 网页来源已满，digest 仍可登记（它是本步骤的衍生物，不是抓取）
        digest = sink.register_text(
            label="research-digest", query="意图", text="按槽位整理的笔记"
        )
        assert digest.registered
        assert digest.url == "program://research-digest"
        # digest 幂等：同 label 再登记返回既有 artifact
        again = sink.register_text(
            label="research-digest", query="意图", text="按槽位整理的笔记"
        )
        assert again.registered is False
        assert again.artifact.artifact_id == digest.artifact.artifact_id
        # 来源表仍是 12 个网页（digest 单列一行，不计入抓取预算即可）
        assert sink.source_count() == RESEARCH_MAX_SOURCES + 1
    finally:
        ctx.close()
