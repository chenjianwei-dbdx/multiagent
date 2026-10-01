"""Chat console JSON API (ADR 0002 amendment).

Boundaries (unchanged): HTTP touches only TaskService / AppContainer / read
repositories; the chat box feeds ``SubmitTask.intent`` only — never document
body; task execution runs in a background thread with its own AppContainer
(SQLite WAL + busy_timeout absorb cross-connection contention); delivered
bytes leave the pool only through the download endpoint after hash
verification against the committed delivery record.
"""

from __future__ import annotations

import hashlib
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from starlette.datastructures import FormData, UploadFile
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Route

from omas.app.bootstrap import AppContainer, build_app
from omas.config.settings import ModelsConfig, default_models_path, load_models_config
from omas.domain.errors import OmasError
from omas.domain.ids import AwaitingEventId, DecisionId, TaskId
from omas.services import MaterialInput, RespondTask
from omas.web.rendering import http_status_for


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _json(payload: Any, status: int = 200) -> JSONResponse:
    return JSONResponse(payload, status_code=status)


def _error_response(exc: OmasError) -> JSONResponse:
    return JSONResponse(
        {"error": {"code": exc.code, "message": str(exc)}},
        status_code=http_status_for(exc.code),
    )


class TaskRunner:
    """Background task execution: the chat polls events meanwhile."""

    def __init__(self, home: Path, models_path: Path | None) -> None:
        self._home = home
        self._models_path = models_path
        self._models: ModelsConfig | None = None
        self._threads: dict[str, threading.Thread] = {}
        self._guard = threading.Lock()

    def start(self, task_id: str, *, respond: RespondTask | None = None) -> bool:
        with self._guard:
            existing = self._threads.get(task_id)
            if existing is not None and existing.is_alive():
                return False
            thread = threading.Thread(
                target=self._execute, args=(task_id, respond), daemon=True
            )
            self._threads[task_id] = thread
            thread.start()
            return True

    def _execute(self, task_id: str, respond: RespondTask | None) -> None:
        if self._models is None:
            self._models = load_models_config(
                self._models_path
                if self._models_path is not None
                else default_models_path(self._home)
            )
        container = build_app(self._home, self._models)
        try:
            if respond is not None:
                container.service.respond(respond)
            container.service.run(TaskId(task_id), container.executor())
        except OmasError:
            pass  # failure state reaches the UI via the view/event feed
        finally:
            container.ledger.close()


# ---------------------------------------------------------------------- helpers


def _container(request: Request) -> AppContainer:
    container: AppContainer = request.app.state.container
    return container


def _runner(request: Request) -> TaskRunner:
    runner: TaskRunner = request.app.state.runner
    return runner


async def _materials(form: FormData, key: str = "materials") -> list[MaterialInput]:
    raw_uploads: list[Any] = form.getlist(key)
    items: list[MaterialInput] = []
    for upload in raw_uploads:
        if not isinstance(upload, UploadFile) or not upload.filename:
            continue
        content = await upload.read()
        safe = Path(upload.filename).name or "material.txt"
        items.append(MaterialInput(filename=safe, content=content))
    return items


def _task_payload(container: AppContainer, task_id: str) -> dict[str, Any]:
    view = container.service.status(TaskId(task_id))
    return {
        "task_id": task_id,
        "status": view.task.status.value,
        "epoch": view.task.epoch,
        "data_policy": view.task.data_policy.value,
        "material_count": view.material_count,
        "awaiting": (
            {
                "awaiting_event_id": view.awaiting.awaiting_event_id,
                "epoch": view.awaiting.epoch,
                "missing_slot_ids": list(view.awaiting.missing_slot_ids),
                "omittable_slot_ids": _omittable_slots(container, view),
            }
            if view.awaiting is not None
            else None
        ),
        "delivery": (
            {
                "delivery_id": view.delivery.delivery_id,
                "final_sha256": view.delivery.final_sha256,
            }
            if view.delivery is not None
            else None
        ),
    }


def _omittable_slots(container: AppContainer, view: Any) -> list[str]:
    """Slots the template explicitly allows users to omit (D5)."""
    task = view.task
    if task.template_version_id is None:
        return []
    try:
        from omas.templates.registry import TemplateRegistry

        contract = TemplateRegistry(container.store, container.ledger).get_contract(
            task.template_version_id
        )
    except OmasError:
        return []
    return [s.slot_id for s in contract.slots if s.allow_user_omit]


def _msg_row(row: Any) -> dict[str, Any]:
    return {
        "message_id": row["message_id"],
        "conversation_id": row["conversation_id"],
        "role": row["role"],
        "kind": row["kind"],
        "content": row["content"],
        "task_id": row["task_id"],
        "created_at": row["created_at"],
    }


# ----------------------------------------------------------------- conversations


async def api_conversations(request: Request) -> Response:
    ledger = _container(request).ledger
    rows = ledger.conversations.list()
    return _json(
        {
            "conversations": [
                {
                    "conversation_id": r["conversation_id"],
                    "title": r["title"],
                    "template_version_id": r["template_version_id"],
                    "data_policy": r["data_policy"],
                    "updated_at": r["updated_at"],
                }
                for r in rows
            ]
        }
    )


async def api_conversation_create(request: Request) -> Response:
    try:
        payload: dict[str, Any] = await request.json()
    except ValueError:
        return _json({"error": {"code": "INPUT_INVALID", "message": "JSON body required"}}, 400)
    template_version_id = str(payload.get("template_version_id") or "")
    if not template_version_id:
        return _json(
            {"error": {"code": "INPUT_INVALID", "message": "template_version_id required"}}, 400,
        )
    data_policy = str(payload.get("data_policy") or "local_only")
    if data_policy not in ("local_only", "llm_allowed"):
        return _json({"error": {"code": "INPUT_INVALID", "message": "bad data_policy"}}, 400)
    container = _container(request)
    if container.ledger.template_versions.get(template_version_id) is None:
        from omas.domain.errors import ArtifactNotFoundError

        return _error_response(
            ArtifactNotFoundError(f"template version {template_version_id} not registered")
        )
    conversation_id = _new_id("conv")
    title = str(payload.get("title") or "新会话")[:80]
    now = datetime.now(UTC)
    container.ledger.conversations.create(
        conversation_id=conversation_id,
        title=title,
        template_version_id=template_version_id,
        data_policy=data_policy,
        created_at=now,
    )
    return _json({"conversation_id": conversation_id, "title": title}, 201)


async def api_conversation_detail(request: Request) -> Response:
    conversation_id = request.path_params["conversation_id"]
    ledger = _container(request).ledger
    row = ledger.conversations.get(conversation_id)
    if row is None:
        return _json({"error": {"code": "NOT_FOUND", "message": "no such conversation"}}, 404)
    messages = [_msg_row(m) for m in ledger.conversation_messages.list(conversation_id)]
    tasks = []
    for message in messages:
        if message["task_id"]:
            tasks.append(_task_payload(_container(request), message["task_id"]))
    return _json(
        {
            "conversation": {
                "conversation_id": row["conversation_id"],
                "title": row["title"],
                "template_version_id": row["template_version_id"],
                "data_policy": row["data_policy"],
            },
            "messages": messages,
            "tasks": tasks,
            "busy": request.app.state.turns.is_busy(conversation_id),
        }
    )


async def api_conversation_message(request: Request) -> Response:
    """One chat turn: intent (+materials) → task → background run."""
    conversation_id = request.path_params["conversation_id"]
    container = _container(request)
    ledger = container.ledger
    row = ledger.conversations.get(conversation_id)
    if row is None:
        return _json({"error": {"code": "NOT_FOUND", "message": "no such conversation"}}, 404)
    form = await request.form()
    intent = str(form.get("intent") or "").strip()
    if not intent:
        return _json({"error": {"code": "INPUT_INVALID", "message": "意图不能为空"}}, 400)
    materials = [(m.filename, m.content) for m in await _materials(form)]
    started = request.app.state.turns.start(conversation_id, intent=intent, materials=materials)
    if not started:
        return _json({"error": {"code": "TURN_BUSY", "message": "本会话上一轮仍在处理中"}}, 409)
    return _json({"status": "turn_started"}, 202)


# ------------------------------------------------------------------------ tasks


async def api_task_feed(request: Request) -> Response:
    """Polling feed: new events + live task view for the progress timeline."""
    task_id = request.path_params["task_id"]
    container = _container(request)
    try:
        after = int(request.query_params.get("after", "0"))
    except ValueError:
        after = 0
    try:
        payload = _task_payload(container, task_id)
    except OmasError as exc:
        return _error_response(exc)
    page = container.service.events(TaskId(task_id), after)
    events = [
        {
            "seq": item.seq,
            "event_code": item.event_code,
            "refs": item.refs,
            "counts": item.counts,
            "created_at": item.created_at.isoformat(),
        }
        for item in page.items
    ]
    return _json({"view": payload, "events": events, "last_seq": page.last_seq})


async def api_task_respond(request: Request) -> Response:
    """Answer a gap: supply materials or omit one slot; re-runs in background."""
    task_id = request.path_params["task_id"]
    container = _container(request)
    form = await request.form()
    omit_slot = str(form.get("omit_slot") or "").strip() or None
    materials = await _materials(form)
    try:
        view = container.service.status(TaskId(task_id))
    except OmasError as exc:
        return _error_response(exc)
    if view.awaiting is None:
        return _json(
            {"error": {"code": "TASK_STATE_INVALID", "message": "任务不在等待输入状态"}}, 409,
        )
    awaiting = view.awaiting
    action = "omit_slot" if omit_slot else "provide_material"
    digest_source = (
        f"{awaiting.awaiting_event_id}:{awaiting.epoch}:{action}:{omit_slot or ''}:"
        + ":".join(
            f"{m.filename}:{hashlib.sha256(m.content).hexdigest()}" for m in materials
        )
    )
    decision_id = DecisionId(f"dec_{hashlib.sha256(digest_source.encode()).hexdigest()[:32]}")
    try:
        receipt = container.service.respond(
            RespondTask(
                task_id=TaskId(task_id),
                decision_id=decision_id,
                expected_epoch=awaiting.epoch,
                awaiting_event_id=AwaitingEventId(awaiting.awaiting_event_id),
                action=action,
                slot_id=omit_slot,
                materials=tuple(materials),
            )
        )
    except OmasError as exc:
        return _error_response(exc)
    ledger = container.ledger
    conv_row = ledger.connection.execute(
        "SELECT conversation_id FROM conversation_messages WHERE task_id = ?"
        " AND role = 'user' LIMIT 1",
        (task_id,),
    ).fetchone()
    if conv_row is not None:
        now = datetime.now(UTC)
        ledger.conversation_messages.append(
            message_id=_new_id("msg"),
            conversation_id=conv_row["conversation_id"],
            role="user",
            kind="text",
            content=("（豁免槽位 " + omit_slot + "）") if omit_slot else "（补充了材料）",
            task_id=task_id,
            created_at=now,
        )
        ledger.conversations.touch(conv_row["conversation_id"], now)
    _runner(request).start(task_id)
    return _json({"epoch_after": receipt.epoch_after, "replayed": receipt.replayed}, 202)


async def api_task_download(request: Request) -> Response:
    """Stream the committed delivery (hash-verified) to the local user."""
    task_id = request.path_params["task_id"]
    container = _container(request)
    try:
        view = container.service.status(TaskId(task_id))
    except OmasError as exc:
        return _error_response(exc)
    if view.delivery is None:
        return _json({"error": {"code": "TASK_STATE_INVALID", "message": "尚无已交付成品"}}, 409)
    path = container.store.delivery_relative(task_id, f"{task_id}.docx")
    container.store.verify(path, view.delivery.final_sha256)
    return FileResponse(
        container.store.resolve_path(path),
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        filename=f"{task_id}.docx",
    )


# --------------------------------------------------------------------- templates


async def api_templates(request: Request) -> Response:
    container = _container(request)
    ledger = container.ledger
    rows = ledger.connection.execute(
        "SELECT tv.template_id, tv.version, tv.template_version_id, tv.created_at"
        " FROM template_versions tv"
        " JOIN (SELECT template_id, MAX(version) AS v FROM template_versions"
        "       GROUP BY template_id) latest"
        "   ON tv.template_id = latest.template_id AND tv.version = latest.v"
        " ORDER BY tv.created_at DESC"
    ).fetchall()
    metas = {m["template_id"]: m for m in ledger.template_meta.list()}
    from omas.templates.registry import TemplateRegistry

    registry = TemplateRegistry(container.store, ledger)
    templates = []
    for row in rows:
        meta = metas.get(row["template_id"])
        slots: list[dict[str, Any]] = []
        activatable = True
        try:
            contract = registry.get_contract(row["template_version_id"])
            activatable = contract.is_activatable()
            slots = [
                {
                    "slot_id": s.slot_id,
                    "semantic_requirement": s.semantic_requirement,
                    "allow_user_omit": s.allow_user_omit,
                    "required": s.required,
                }
                for s in contract.slots
            ]
        except OmasError:
            pass
        templates.append(
            {
                "template_id": row["template_id"],
                "version": row["version"],
                "template_version_id": row["template_version_id"],
                "display_name": meta["display_name"] if meta else row["template_id"],
                "description": meta["description"] if meta else "",
                "activatable": activatable,
                "slots": slots,
                "created_at": row["created_at"],
            }
        )
    return _json({"templates": templates})


async def api_template_upload(request: Request) -> Response:
    from omas.templates.scaffold import discover_placeholders, scaffold_and_register

    form = await request.form()
    upload = form.get("docx")
    if not isinstance(upload, UploadFile):
        return _json({"error": {"code": "INPUT_INVALID", "message": "缺少 docx 文件"}}, 400)
    docx_bytes = await upload.read()
    template_id = str(form.get("template_id") or "").strip()
    display_name = str(form.get("display_name") or "").strip()
    description = str(form.get("description") or "").strip()
    import json as jsonlib

    try:
        semantics_raw = str(form.get("slot_semantics") or "{}")
        semantics: dict[str, str] = {
            str(k): str(v) for k, v in jsonlib.loads(semantics_raw).items()
        }
    except ValueError:
        return _json(
            {"error": {"code": "INPUT_INVALID", "message": "slot_semantics 必须是 JSON 对象"}}, 400,
        )
    allowed = template_id.replace("-", "").replace("_", "")
    if not template_id or not allowed.isalnum():
        return _json(
            {"error": {"code": "INPUT_INVALID", "message": "template_id 仅限字母数字与 -_"}}, 400,
        )
    slots = discover_placeholders(docx_bytes)
    if not slots:
        return _json(
            {
                "error": {
                    "code": "INPUT_INVALID",
                    "message": "未发现 {{ 槽位 }} 占位符；请先在文档中标记占位符",
                }
            },
            400,
        )
    container = _container(request)
    try:
        result = scaffold_and_register(
            store=container.store,
            ledger=container.ledger,
            docx_bytes=docx_bytes,
            template_id=template_id,
            display_name=display_name or template_id,
            description=description,
            slot_semantics=semantics,
        )
    except (OmasError, ValueError) as exc:
        message = str(exc)
        code = getattr(exc, "code", "INPUT_INVALID")
        return _json({"error": {"code": code, "message": message}}, 400)
    return _json(
        {
            "template_id": result.template_id,
            "version": result.version,
            "template_version_id": result.version_id,
            "activatable": result.activatable,
            "findings": list(result.findings),
            "slots": list(result.slots),
        },
        201,
    )


async def api_template_precheck(request: Request) -> Response:
    """Upload pre-flight: which placeholders does this document declare?"""
    from omas.templates.scaffold import discover_placeholders

    form = await request.form()
    upload = form.get("docx")
    if not isinstance(upload, UploadFile):
        return _json({"error": {"code": "INPUT_INVALID", "message": "缺少 docx 文件"}}, 400)
    docx_bytes = await upload.read()
    return _json({"slots": discover_placeholders(docx_bytes)})


async def api_template_meta(request: Request) -> Response:
    template_id = request.path_params["template_id"]
    try:
        payload: dict[str, Any] = await request.json()
    except ValueError:
        return _json({"error": {"code": "INPUT_INVALID", "message": "JSON body required"}}, 400)
    display_name = str(payload.get("display_name") or "").strip()
    description = str(payload.get("description") or "").strip()
    if not display_name:
        return _json({"error": {"code": "INPUT_INVALID", "message": "display_name required"}}, 400)
    ledger = _container(request).ledger
    ledger.template_meta.upsert(
        template_id=template_id,
        display_name=display_name[:80],
        description=description[:500],
        at=datetime.now(UTC),
    )
    return _json(
        {"template_id": template_id, "display_name": display_name, "description": description}
    )


api_routes = [
    Route("/api/conversations", api_conversations, methods=["GET"]),
    Route("/api/conversations", api_conversation_create, methods=["POST"]),
    Route(
        "/api/conversations/{conversation_id}", api_conversation_detail, methods=["GET"]
    ),
    Route(
        "/api/conversations/{conversation_id}/messages",
        api_conversation_message,
        methods=["POST"],
    ),
    Route("/api/tasks/{task_id}/feed", api_task_feed, methods=["GET"]),
    Route("/api/tasks/{task_id}/respond", api_task_respond, methods=["POST"]),
    Route("/api/tasks/{task_id}/download", api_task_download, methods=["GET"]),
    Route("/api/templates", api_templates, methods=["GET"]),
    Route("/api/templates/precheck", api_template_precheck, methods=["POST"]),
    Route("/api/templates", api_template_upload, methods=["POST"]),
    Route("/api/templates/{template_id}/meta", api_template_meta, methods=["POST"]),
]
