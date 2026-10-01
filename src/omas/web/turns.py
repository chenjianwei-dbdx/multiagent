"""对话轮次分流（ADR 0002 修订 B）：意图识别 → 文档任务 / 综合问答 / 追问。

运行于后台线程；local_only 会话不分流（无出网），一律走文档路径。
llm_allowed 会话经 TriageAgent 分类；question 走 QAAgent（回答只进会话
消息，永不进入文档正文与溯源链）；needs_info 落一条追问消息。
"""

from __future__ import annotations

import contextlib
import traceback
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from omas.app.bootstrap import AppContainer, build_app
from omas.config.settings import ModelsConfig
from omas.domain.ids import TaskId
from omas.domain.task import DataPolicy
from omas.services import MaterialInput, SubmitTask


def _new_ids(prefix: str, home: Path) -> Any:
    import uuid

    return f"{prefix}_{uuid.uuid4().hex}"


class TurnExecutor:
    """One in-flight turn per conversation; the UI polls the message list."""

    def __init__(self, home: Path, models: ModelsConfig, model_builder: Any = None) -> None:
        self._home = home
        self._models = models
        self._model_builder = model_builder  # test seam: settings/gateway/policy -> Model
        self._busy: set[str] = set()
        self._guard = __import__("threading").Lock()

    def is_busy(self, conversation_id: str) -> bool:
        with self._guard:
            return conversation_id in self._busy

    def start(
        self,
        conversation_id: str,
        *,
        intent: str,
        materials: list[tuple[str, bytes]],
    ) -> bool:
        import threading

        with self._guard:
            if conversation_id in self._busy:
                return False
            self._busy.add(conversation_id)
            thread = threading.Thread(
                target=self._execute,
                args=(conversation_id, intent, materials),
                daemon=True,
            )
            thread.start()
            return True

    # ------------------------------------------------------------------ turn

    def _execute(
        self, conversation_id: str, intent: str, materials: list[tuple[str, bytes]]
    ) -> None:
        container = build_app(self._home, self._models)
        try:
            row = container.ledger.conversations.get(conversation_id)
            if row is None:
                return
            # 用户提问统一在此落账：QA/追问路径也要留下提问记录，
            # 否则刷新会话历史时会丢失这一轮的问题
            self._append(container, conversation_id, "text", intent, role="user")
            policy = DataPolicy(row["data_policy"])
            settings = container.model_settings
            if policy is DataPolicy.LLM_ALLOWED and settings is not None:
                self._triage_then_dispatch(container, row, intent, materials)
            else:
                self._run_document(container, row, intent, materials)
        except Exception:
            # 诊断：吞掉异常却不留堆栈会让生产排障不可能；先打印再降级提示
            traceback.print_exc()
            with contextlib.suppress(Exception):
                container.ledger.conversation_messages.append(
                    message_id=_new_ids("msg", self._home),
                    conversation_id=conversation_id,
                    role="assistant",
                    kind="note",
                    content="本轮处理出现内部错误，请重试或改用文档任务。",
                    task_id=None,
                    created_at=datetime.now(UTC),
                )
        finally:
            with self._guard:
                self._busy.discard(conversation_id)
            container.ledger.close()

    def _triage_then_dispatch(
        self,
        container: AppContainer,
        row: Any,
        intent: str,
        materials: list[tuple[str, bytes]],
    ) -> None:
        from pydantic_ai import Agent

        from omas.agents.triage import TRIAGE_SYSTEM_PROMPT, TriageAgent, TriageResult
        from omas.domain.task import DataPolicy as _DP
        from omas.security.model_gateway import ModelGateway

        gateway = ModelGateway(policy=_DP.LLM_ALLOWED)
        assert container.model_settings is not None
        model = self._build_model(container, gateway)
        template_summary = self._template_summary(container, row["template_version_id"])

        def triage_factory() -> Any:
            return Agent(
                model=model,
                output_type=TriageResult,
                retries=1,
                system_prompt=TRIAGE_SYSTEM_PROMPT,
            )

        triage = TriageAgent()
        verdict = triage.classify(
            message=intent,
            has_attachments=bool(materials),
            template_summary=template_summary,
            agent_factory=triage_factory,
        )
        self._record_call(container, "triage", row["conversation_id"])
        if verdict.category == "question":
            self._run_qa(container, row, intent)
            return
        if verdict.category == "needs_info":
            self._append(
                container,
                row["conversation_id"],
                "clarify",
                verdict.clarifying_question or "请补充：你需要生成什么文档？",
            )
            return
        self._run_document(container, row, intent, materials)

    def _run_qa(self, container: AppContainer, row: Any, question: str) -> None:
        from pydantic_ai import Agent

        from omas.agents.qa import QA_SYSTEM_PROMPT, QAAgent, QAAnswer
        from omas.domain.task import DataPolicy
        from omas.security.model_gateway import ModelGateway

        gateway = ModelGateway(policy=DataPolicy.LLM_ALLOWED)
        assert container.model_settings is not None
        model = self._build_model(container, gateway)
        search = self._search_client(container, gateway)
        qa = QAAgent()

        def qa_factory(**_kwargs: Any) -> Any:
            tools: list[Any] = []
            if search is not None and search.enabled:
                tools = qa._tools(search, gateway)
            return Agent(
                model=model,
                output_type=QAAnswer,
                retries=2,
                system_prompt=QA_SYSTEM_PROMPT,
                tools=tools,
            )

        answer = qa.answer_question(
            question=question, search=search, gateway=gateway, agent_factory=qa_factory
        )
        self._record_call(container, "qa", row["conversation_id"])
        for query in qa.search_log:
            self._append(container, row["conversation_id"], "note", f"联网搜索：{query}")
        body = answer.answer
        if answer.sources:
            body += "\n\n来源：\n" + "\n".join(answer.sources)
        self._append(container, row["conversation_id"], "answer", body)

    def _build_model(self, container: AppContainer, gateway: Any) -> Any:
        assert container.model_settings is not None
        if self._model_builder is not None:
            return self._model_builder(container.model_settings, gateway)
        from omas.domain.task import DataPolicy
        from omas.security.provider_factory import build_model

        return build_model(
            container.model_settings, gateway=gateway, policy=DataPolicy.LLM_ALLOWED
        )

    def _search_client(self, container: AppContainer, gateway: Any) -> Any:
        search_settings = getattr(self._models, "search", None)
        if search_settings is None:
            return None
        try:
            from omas.websearch.client import SearchClient

            return SearchClient(search_settings, gateway)
        except Exception:
            return None

    def _run_document(
        self,
        container: AppContainer,
        row: Any,
        intent: str,
        materials: list[tuple[str, bytes]],
    ) -> None:
        from omas.domain.task import DataPolicy

        now = datetime.now(UTC)
        request_id = _new_ids("webreq", self._home)
        items = tuple(
            MaterialInput(filename=name, content=content) for name, content in materials
        ) or (MaterialInput(filename="empty.txt", content=b""),)
        receipt = container.service.submit(
            SubmitTask(
                request_id=request_id,
                template_version_id=row["template_version_id"],
                intent=intent,
                materials=items,
                data_policy=DataPolicy(row["data_policy"]),
            )
        )
        ledger = container.ledger
        cid = row["conversation_id"]
        ledger.conversation_messages.append(
            message_id=_new_ids("msg", self._home),
            conversation_id=cid,
            role="assistant",
            kind="task_started",
            content="",
            task_id=receipt.task_id,
            created_at=now,
        )
        ledger.conversations.touch(cid, now)
        container.service.run(TaskId(receipt.task_id), container.executor())

    def _template_summary(self, container: AppContainer, version_id: str) -> str:
        try:
            from omas.domain.ids import TemplateVersionId
            from omas.templates.registry import TemplateRegistry

            contract = TemplateRegistry(container.store, container.ledger).get_contract(
                TemplateVersionId(version_id)
            )
            slots = ", ".join(f"{s.slot_id}({s.semantic_requirement})" for s in contract.slots)
            return f"{contract.template_id} v{contract.version}，槽位：{slots}"
        except Exception:
            return "（模板信息不可用）"

    def _record_call(self, container: AppContainer, node: str, conversation_id: str) -> None:
        import uuid
        from datetime import UTC, datetime

        settings = container.model_settings
        if settings is None:
            return
        container.ledger.llm_calls.insert(
            call_id=f"llm_{uuid.uuid4().hex}",
            provider=settings.provider,
            model_id=settings.model_id,
            task_id=None,
            node_name=node,
            created_at=datetime.now(UTC),
        )
        _ = conversation_id

    def _append(
        self,
        container: AppContainer,
        conversation_id: str,
        kind: str,
        content: str,
        *,
        role: str = "assistant",
        task_id: str | None = None,
    ) -> None:
        container.ledger.conversation_messages.append(
            message_id=_new_ids("msg", self._home),
            conversation_id=conversation_id,
            role=role,
            kind=kind,
            content=content,
            task_id=task_id,
            created_at=datetime.now(UTC),
        )
        container.ledger.conversations.touch(conversation_id, datetime.now(UTC))
