"""Starlette 应用工厂（ADR 0002）：本地回环 Web 控制台。

- 组合根在工厂期**急切构建**（启动即失败，而非首个请求才暴露）；
  应用关闭时关闭 ledger 连接；
- 自定义异常处理器把 OMAS 错误码映射为 HTTP 状态码并渲染错误页；
  未经映射的异常走 starlette 默认 500（uvicorn 记录 traceback）；
- 路由注册集中于此，处理器见 :mod:`omas.web.routes`。
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from omas.app.bootstrap import AppContainer, build_app
from omas.config.settings import ModelsConfig, default_models_path, load_models_config
from omas.domain.errors import OmasError
from omas.web import api, routes
from omas.web.rendering import STATIC_DIR, http_status_for, render_error

_STATIC_FILES = StaticFiles(directory=str(STATIC_DIR))
# uvicorn 自己的 logger 才挂着可见 handler；默认配置下 root logger 没有
# handler，用 __name__ 的模块 logger 会被静默丢弃。
logger = logging.getLogger("uvicorn.error")


async def _omas_error_handler(request: Request, exc: Exception) -> Response:
    if isinstance(exc, OmasError):
        return render_error(
            request,
            status_code=http_status_for(exc.code),
            code=exc.code,
            message=str(exc),
        )
    return render_error(
        request, status_code=500, code="INTERNAL_ERROR", message="内部错误"
    )


async def _http_exception_handler(request: Request, exc: Exception) -> Response:
    if isinstance(exc, HTTPException):
        return render_error(
            request,
            status_code=exc.status_code,
            code=f"HTTP_{exc.status_code}",
            message=exc.detail,
        )
    return render_error(
        request, status_code=500, code="INTERNAL_ERROR", message="内部错误"
    )


def _seed_builtin(container: AppContainer) -> None:
    """Seed the shipped built-in templates at factory time (ADR 0003).

    Idempotent: a library whose latest versions already carry the shipped
    digests is left untouched, so a restart writes nothing. Seeding failures
    are logged but never block startup — the console still serves the
    library as-is; use ``omas template seed`` to surface them loudly.
    """
    from omas.templates.seed import seed_builtin_templates

    try:
        results = seed_builtin_templates(store=container.store, ledger=container.ledger)
    except (OmasError, OSError, ValueError) as exc:
        logger.warning("内置模板播种失败（控制台继续启动）: %s", exc)
        return
    freshly = [r for r in results if r.newly_registered]
    if freshly:
        logger.info(
            "已注册内置模板: %s",
            ", ".join(f"{r.template_id} v{r.version}" for r in freshly),
        )


def create_web_app(home: Path, models_path: Path | None = None) -> Starlette:
    """构建控制台应用；``home`` 为 OMAS_HOME，``models_path`` 缺省取 ``home/models.toml``。"""
    config: ModelsConfig = load_models_config(
        models_path if models_path is not None else default_models_path(home)
    )
    container = build_app(home, config)

    @asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncIterator[None]:
        # 播种放在 lifespan 而非工厂期：uvicorn 要等 run() 之后才配置日志，
        # 工厂期的日志记录没有任何 handler 会接住；lifespan 在 accept 连接前
        # 运行，播种结果才可见（ADR 0003）。
        try:
            _seed_builtin(container)
            yield
        finally:
            container.ledger.close()

    app = Starlette(
        routes=[
            Route("/", routes.index, methods=["GET"]),
            Route("/console", routes.console_page, methods=["GET"]),
            *api.api_routes,
            Route("/tasks", routes.task_list, methods=["GET"]),
            Route("/tasks", routes.task_submit, methods=["POST"]),
            Route("/tasks/{task_id}", routes.task_detail, methods=["GET"]),
            Route("/tasks/{task_id}/run", routes.task_run, methods=["POST"]),
            Route("/tasks/{task_id}/respond", routes.task_respond, methods=["POST"]),
            Route("/tasks/{task_id}/cancel", routes.task_cancel, methods=["POST"]),
            Route("/tasks/{task_id}/recover", routes.task_recover, methods=["POST"]),
            Route("/tasks/{task_id}/export", routes.task_export, methods=["POST"]),
            Route("/tasks/{task_id}/events", routes.task_events, methods=["GET"]),
            Route("/healthz", routes.healthz, methods=["GET"]),
            Mount("/static", app=_STATIC_FILES),
        ],
        exception_handlers={
            OmasError: _omas_error_handler,
            HTTPException: _http_exception_handler,
        },
        lifespan=lifespan,
    )
    app.state.container = container
    from omas.web.api import TaskRunner
    from omas.web.turns import TurnExecutor

    app.state.runner = TaskRunner(home, models_path)
    app.state.turns = TurnExecutor(home, config)
    return app
