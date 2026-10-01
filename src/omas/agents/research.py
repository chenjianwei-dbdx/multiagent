"""Research agent: web gathering before assembly (ADR D27).

The agent's job is *judgement only*: read the intent and the template's slot
semantics, decide which queries to run, which pages look authoritative, and
call ``add_source(url)`` for the ones worth using. The bytes are fetched and
registered by :class:`omas.services.research.ResearchService` — the model
never supplies body content, so the Renderer still has no free-text entry
and every body character remains traceable to a span of a hash-verified,
origin-recorded material.

- runs only for ``llm_allowed`` tasks (the node gates on policy; local_only
  gets the deterministic path with user-supplied materials);
- every tool failure degrades to a plain string result (retry or proceed
  with fewer sources) — a web hiccup must never fail the document task;
- output is a small DTO (notes + how many sources it added), not prose that
  could leak into the body.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field
from pydantic_ai import Agent
from pydantic_ai.models.test import TestModel
from pydantic_ai.usage import RunUsage

from omas.domain.errors import ModelGatewayError
from omas.domain.task import DataPolicy, Task
from omas.domain.template import TemplateContract
from omas.security.model_gateway import ModelGateway
from omas.services.research import RESEARCH_MAX_SOURCES, ResearchService
from omas.websearch.client import WebSearchError

if TYPE_CHECKING:
    from omas.websearch.client import SearchClient

RESEARCH_SYSTEM_PROMPT = (
    "你是 OMAS 调研采集器。用户要按模板生成文档，但任务材料不足，需要你从公开网络"
    "采集素材。\n\n"
    "工作方式：\n"
    "1. 先看任务意图与模板各槽位的语义说明，确定每个槽位需要哪类信息；\n"
    "2. 用 web_search 检索（每个槽位 1-3 个不同关键词，中英文都试）；\n"
    "3. 挑选权威、与槽位直接相关的页面，调用 add_source(url) —— 页面正文会由"
    "程序抓取并登记为任务材料，你不需要也无法直接写入正文；\n"
    "4. 重复直到每个必填槽位都有素材，或达到来源上限；\n"
    "5. 最后输出结构化结果：digest 字段必须是按模板槽位整理的研究笔记，"
    "每段内容只能来自你已登记的页面（整理与摘录，不得添加采集范围外的"
    "信息），并在笔记中用【来源：URL】标注关键内容出处；slot_notes 列出"
    "每个槽位是否覆盖。\n\n"
    "约束：只选 http/https 公开页面；不要采集验证码/登录墙页面（工具会回报失败，"
    "换源即可）；不要重复添加同一 URL；来源重质不重量。"
)


class ResearchReport(BaseModel):
    """Structured outcome of the research step (metadata only, never body)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    slot_notes: tuple[str, ...] = Field(default=(), max_length=20)
    notes: str = Field(default="", max_length=500)
    #: 按模板槽位整理的研究笔记：只能来自已登记页面的内容（整理与摘录，
    #: 不得超出采集范围），由程序登记为 program:// 材料（ADR D27）。
    digest: str = Field(default="", max_length=24000)


ResearchAgentFactory = Callable[..., Agent[object, ResearchReport]]


class ResearchAgent:
    """LLM-driven gathering with a program-owned write side."""

    def __init__(self) -> None:
        self.last_usage: RunUsage | None = None
        self.added: list[str] = []  # URLs actually registered this run

    def research(
        self,
        *,
        intent: str,
        contract: TemplateContract,
        task: Task,
        search: SearchClient | None,
        gateway: ModelGateway,
        sink: ResearchService,
        agent_factory: ResearchAgentFactory | None = None,
    ) -> ResearchReport:

        factory: ResearchAgentFactory = agent_factory or self._default_factory
        agent = factory(search=search, gateway=gateway, sink=sink)
        result = agent.run_sync(
            _research_brief(intent=intent, contract=contract, task=task)
        )
        self.last_usage = result.usage
        output = result.output
        if not isinstance(output, ResearchReport):
            raise TypeError(f"research agent produced {type(output).__name__}")
        return output

    # ---------------------------------------------------------------- seam

    def _default_factory(
        self, *, search: Any, gateway: Any, sink: Any
    ) -> Agent[object, ResearchReport]:
        return Agent(
            model=TestModel(),
            output_type=ResearchReport,
            retries=1,
            system_prompt=RESEARCH_SYSTEM_PROMPT,
            tools=[],
        )

    def tools(
        self, search: SearchClient | None, gateway: ModelGateway, sink: ResearchService
    ) -> list[Callable[..., object]]:
        """The model-facing tools: discover, register, read."""

        def web_search(query: str, max_results: int = 6) -> str:
            """联网搜索：返回标题/URL/摘要列表。query 为搜索词。"""
            if search is None:
                return "搜索未配置（本次按已有材料进行）"
            try:
                outcome = search.search(query, max_results=max_results)
            except (WebSearchError, ModelGatewayError) as exc:
                return f"搜索失败（{exc.code}）：请换关键词或换来源"
            lines = [
                f"{i + 1}. {r.title}\n   {r.url}\n   {r.snippet[:180]}"
                for i, r in enumerate(outcome.results)
            ]
            return "\n".join(lines) if lines else "（无结果，请换关键词）"

        def add_source(url: str, query: str = "") -> str:
            """登记一个网页为任务材料：正文由程序抓取，你只指定 URL。"""
            from omas.websearch.client import fetch_page

            try:
                text = fetch_page(url, gateway=gateway)
            except (WebSearchError, ModelGatewayError) as exc:
                return f"读取页面失败（{exc.code}）：该站点不可达或被拒，请改用其他来源"
            try:
                material = sink.register_fetched(
                    url=url, query=query or "research", text=text
                )
            except Exception as exc:
                return f"登记失败（{type(exc).__name__}）：{exc}"
            self.added.append(url)
            status = "已登记" if material.registered else "已存在（复用）"
            head = text[:1500].replace("\n", " ")
            return (
                f"{status}：{url}\n正文前 1500 字：{head}\n"
                f"当前来源数 {sink.source_count()}/{RESEARCH_MAX_SOURCES}"
            )

        def source_count() -> str:
            """查看当前已登记来源数与上限。"""
            return f"{sink.source_count()}/{RESEARCH_MAX_SOURCES}"

        return [web_search, add_source, source_count]


def _research_brief(
    *, intent: str, contract: TemplateContract | None, task: Task
) -> str:
    slot_list = list(contract.slots) if contract is not None else []
    slots = "\n".join(
        f"- {spec.slot_id}（{'必填' if spec.required else '可选'}）："
        f"{spec.semantic_requirement}"
        for spec in slot_list
    )
    policy_note = (
        "（本任务为 llm_allowed，允许联网采集）"
        if task.data_policy is DataPolicy.LLM_ALLOWED
        else "（本任务为 local_only，不应执行联网）"
    )
    return (
        f"用户意图：{intent}\n"
        f"任务 ID：{task.task_id} {policy_note}\n"
        f"模板槽位（共 {len(slot_list)} 个）：\n{slots}\n\n"
        "请为必填槽位采集素材，直到来源数达上限或全部覆盖。"
    )
