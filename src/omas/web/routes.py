"""HTTP 处理器（ADR 0002）。

- 所有业务调用只经 ``AppContainer``（service / 只读仓储枚举），与 CLI 同一扇门；
- 阻塞型业务（图执行、SQLite 写）经 ``run_in_threadpool`` 在线程池运行，
  不阻塞事件轮询；Ledger 每连接 RLock 已串行化跨线程访问；
- 幂等键（request_id / decision_id）在渲染期由程序生成并写入表单隐藏域，
  同一表单的重复提交是重放而非重复创建；
- 用户输入只进入 ``SubmitTask.intent`` / 材料，永不成为正文来源（I2）。
"""

from __future__ import annotations

import os
import uuid
from urllib.parse import quote

from starlette.concurrency import run_in_threadpool
from starlette.datastructures import FormData, UploadFile
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, RedirectResponse, Response

from omas.app.bootstrap import AppContainer
from omas.domain.ids import (
    AwaitingEventId,
    DecisionId,
    TaskId,
    TemplateVersionId,
    new_decision_id,
)
from omas.domain.task import DataPolicy, Task
from omas.services import (
    CancelTask,
    ExecutionOutcome,
    ExportTask,
    MaterialInput,
    RecoverTask,
    RespondTask,
    SubmitTask,
    TaskExecutor,
)
from omas.web.rendering import render_error, templates

DOCX_MEDIA_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)
MAX_MATERIALS = 50  # SubmitTask.materials max_length（D15）


# --------------------------------------------------------------------- helpers


def _container(request: Request) -> AppContainer:
    container: AppContainer = request.app.state.container
    return container


def _form_str(form: FormData, key: str) -> str:
    value = form.get(key)
    if value is None or isinstance(value, UploadFile):
        return ""
    return value.strip()


def _uploads(form: FormData, key: str) -> list[UploadFile]:
    items: list[UploadFile] = []
    for value in form.getlist(key):
        if isinstance(value, UploadFile) and (value.filename or "").strip():
            items.append(value)
    return items


def _sanitized_filename(filename: str | None) -> str:
    name = os.path.basename((filename or "").replace("\\", "/")).strip()
    return name[:255] or "material"


async def _to_materials(uploads: list[UploadFile]) -> list[MaterialInput]:
    materials: list[MaterialInput] = []
    for upload in uploads:
        content = await upload.read()
        materials.append(
            MaterialInput(
                filename=_sanitized_filename(upload.filename), content=content
            )
        )
    return materials


def _model_summary(container: AppContainer) -> dict[str, object]:
    """镜像 ``omas config`` 的展示契约；密钥只报 set/UNSET，绝不打印值。"""
    settings = container.model_settings
    if settings is None:
        return {"configured": False}
    key_state = (
        "set" if settings.api_key_env and os.environ.get(settings.api_key_env) else "UNSET"
    )
    return {
        "configured": True,
        "provider": settings.provider,
        "model_id": settings.model_id,
        "base_url": settings.base_url or "<official default>",
        "api_key_env": settings.api_key_env,
        "api_key_state": key_state,
    }


def _index_context(
    container: AppContainer, *, errors: list[str] | None = None
) -> dict[str, object]:
    template_rows = container.ledger.template_versions.list_recent(50)
    recent = container.ledger.tasks.list_recent(10)
    return {
        "active_nav": "index",
        "templates": template_rows,
        "recent_tasks": recent,
        "model": _model_summary(container),
        "request_id": f"web-{uuid.uuid4().hex}",
        "errors": errors or [],
    }


class _RecordingExecutor:
    """记录最近一次执行结果的 error_code（与 ``cli/task.py`` 同形）。"""

    def __init__(self, inner: TaskExecutor) -> None:
        self._inner = inner
        self.last_outcome: ExecutionOutcome | None = None

    def execute(self, task: Task) -> ExecutionOutcome:
        outcome = self._inner.execute(task)
        self.last_outcome = outcome
        return outcome


# ---------------------------------------------------------------------- routes


async def index(request: Request) -> Response:
    container = _container(request)
    context = await run_in_threadpool(_index_context, container)
    return templates.TemplateResponse(request, "index.html", context)


async def task_list(request: Request) -> Response:
    container = _container(request)
    tasks = await run_in_threadpool(container.ledger.tasks.list_recent, 100)
    return templates.TemplateResponse(
        request, "tasks.html", {"active_nav": "tasks", "tasks": tasks}
    )


async def task_detail(request: Request) -> Response:
    container = _container(request)
    task_id = TaskId(request.path_params["task_id"])
    view = await run_in_threadpool(container.service.status, task_id)
    page = await run_in_threadpool(container.service.events, task_id, 0)
    omissible = await run_in_threadpool(container.service.omissible_missing_slots, view)
    shown = list(reversed(page.items[-50:]))
    context: dict[str, object] = {
        "active_nav": "tasks",
        "view": view,
        "omissible_slots": omissible,
        "events": shown,
        "last_seq": page.last_seq,
        "run_error_code": request.query_params.get("run_error"),
        "decision_id": new_decision_id(),
    }
    return templates.TemplateResponse(request, "task.html", context)


async def task_submit(request: Request) -> Response:
    container = _container(request)
    form = await request.form()
    intent = _form_str(form, "intent")
    template_version_id = _form_str(form, "template")
    request_id = _form_str(form, "request_id")
    policy = (
        DataPolicy.LLM_ALLOWED
        if _form_str(form, "data_policy") == "llm_allowed"
        else DataPolicy.LOCAL_ONLY
    )
    uploads = _uploads(form, "materials")
    errors: list[str] = []
    if not intent:
        errors.append("意图不能为空")
    if not template_version_id:
        errors.append("必须选择一个模板（先在 CLI 运行 `omas template extract` 注册）")
    if not uploads:
        errors.append("至少上传一份材料 Markdown 文件")
    elif len(uploads) > MAX_MATERIALS:
        errors.append(f"材料数量上限为 {MAX_MATERIALS} 份")
    if not request_id:
        errors.append("表单缺少 request_id（请刷新页面重试）")
    if errors:
        context = await run_in_threadpool(
            _index_context, container, errors=errors
        )
        context["form_intent"] = intent
        return templates.TemplateResponse(request, "index.html", context, status_code=400)
    materials = await _to_materials(uploads)
    receipt = await run_in_threadpool(
        container.service.submit,
        SubmitTask(
            request_id=request_id,
            template_version_id=TemplateVersionId(template_version_id),
            intent=intent,
            materials=tuple(materials),
            data_policy=policy,
        ),
    )
    return RedirectResponse(url=f"/tasks/{receipt.task_id}", status_code=303)


async def task_run(request: Request) -> Response:
    container = _container(request)
    task_id = TaskId(request.path_params["task_id"])
    executor = _RecordingExecutor(container.executor())
    await run_in_threadpool(container.service.run, task_id, executor)
    error_code = executor.last_outcome.error_code if executor.last_outcome else None
    target = f"/tasks/{task_id}"
    if error_code:
        target = f"{target}?run_error={quote(error_code, safe='')}"
    return RedirectResponse(url=target, status_code=303)


async def task_respond(request: Request) -> Response:
    container = _container(request)
    task_id = TaskId(request.path_params["task_id"])
    form = await request.form()
    action = _form_str(form, "action")
    decision_id = _form_str(form, "decision_id")
    awaiting_event_id = _form_str(form, "awaiting_event_id")
    slot_id = _form_str(form, "slot_id") or None
    expected_epoch_raw = _form_str(form, "expected_epoch")
    if action not in ("provide_material", "omit_slot"):
        return render_error(
            request, status_code=400, code="INPUT_INVALID", message="非法的应答动作"
        )
    if not decision_id:
        return render_error(
            request, status_code=400, code="INPUT_INVALID", message="表单缺少 decision_id"
        )
    if not awaiting_event_id:
        return render_error(
            request,
            status_code=400,
            code="INPUT_INVALID",
            message="表单缺少 awaiting_event_id",
        )
    if action == "omit_slot" and not slot_id:
        return render_error(
            request, status_code=400, code="INPUT_INVALID", message="省略槽位必须指定 slot_id"
        )
    try:
        expected_epoch = int(expected_epoch_raw)
        if expected_epoch < 1:
            raise ValueError("expected_epoch must be >= 1")
    except ValueError:
        return render_error(
            request,
            status_code=400,
            code="INPUT_INVALID",
            message="表单缺少合法的 expected_epoch",
        )
    materials: list[MaterialInput] = []
    if action == "provide_material":
        materials = await _to_materials(_uploads(form, "materials"))
        if not materials:
            return render_error(
                request,
                status_code=400,
                code="INPUT_INVALID",
                message="补料应答至少上传一份材料",
            )
    await run_in_threadpool(
        container.service.respond,
        RespondTask(
            task_id=task_id,
            decision_id=DecisionId(decision_id),
            expected_epoch=expected_epoch,
            awaiting_event_id=AwaitingEventId(awaiting_event_id),
            action=action,
            slot_id=slot_id,
            materials=tuple(materials),
        ),
    )
    return RedirectResponse(url=f"/tasks/{task_id}", status_code=303)


async def task_cancel(request: Request) -> Response:
    container = _container(request)
    task_id = TaskId(request.path_params["task_id"])
    await run_in_threadpool(
        container.service.cancel,
        CancelTask(task_id=task_id, request_id=f"web-{uuid.uuid4().hex}"),
    )
    return RedirectResponse(url=f"/tasks/{task_id}", status_code=303)


async def task_recover(request: Request) -> Response:
    container = _container(request)
    task_id = TaskId(request.path_params["task_id"])
    await run_in_threadpool(
        container.service.recover,
        RecoverTask(task_id=task_id, request_id=f"web-{uuid.uuid4().hex}"),
    )
    return RedirectResponse(url=f"/tasks/{task_id}", status_code=303)


async def task_export(request: Request) -> Response:
    container = _container(request)
    task_id = TaskId(request.path_params["task_id"])
    view = await run_in_threadpool(container.service.status, task_id)
    delivery = view.delivery
    if delivery is None:
        return render_error(
            request,
            status_code=409,
            code="TASK_STATE_INVALID",
            message="任务尚无已提交交付物，无法导出",
        )
    target = container.home / "web-exports" / f"{delivery.delivery_id}.docx"
    await run_in_threadpool(
        container.service.export,
        ExportTask(
            task_id=task_id,
            request_id=f"web-{uuid.uuid4().hex}",
            output_path=str(target),
        ),
    )
    return FileResponse(target, media_type=DOCX_MEDIA_TYPE, filename=f"{task_id}.docx")


async def task_events(request: Request) -> Response:
    container = _container(request)
    task_id = TaskId(request.path_params["task_id"])
    after_raw = request.query_params.get("after", "0")
    try:
        after = int(after_raw)
        if after < 0:
            raise ValueError("after must be >= 0")
    except ValueError:
        after = 0
    page = await run_in_threadpool(container.service.events, task_id, after)
    return JSONResponse(page.model_dump(mode="json"))


async def healthz(request: Request) -> Response:
    return JSONResponse({"status": "ok"})


async def console_page(request: Request) -> Response:
    """Chat console SPA (ADR 0002 amendment; served from /static)."""
    return RedirectResponse(url="/static/console.html", status_code=307)
