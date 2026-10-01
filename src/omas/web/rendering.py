"""页面渲染与 HTTP 错误映射（ADR 0002）。

错误页只展示 OMAS 错误码与程序自身消息：正文、文件字节、密钥永远不进入
任何 HTTP 响应（硬边界；domain/errors.py 的消息契约同样禁止字节内容）。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from starlette.requests import Request
from starlette.responses import HTMLResponse
from starlette.templating import Jinja2Templates

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
STATIC_DIR = Path(__file__).resolve().parent / "static"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

#: OMAS 错误码 → HTTP 状态码；未列出的码一律 500（程序内部错误对待）。
HTTP_STATUS_BY_OMAS_CODE: dict[str, int] = {
    "ARTIFACT_NOT_FOUND": 404,
    "IDEMPOTENCY_CONFLICT": 409,
    "CONCURRENCY_CONFLICT": 409,
    "TASK_STATE_INVALID": 409,
    "LEDGER_CONFLICT": 409,
    "CAPABILITY_DENIED": 403,
    "MODEL_GATEWAY_REJECTED": 403,
}


def http_status_for(code: str) -> int:
    return HTTP_STATUS_BY_OMAS_CODE.get(code, 500)


def render_error(
    request: Request, *, status_code: int, code: str, message: str
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "error.html",
        {"code": code, "message": message, "status_code": status_code},
        status_code=status_code,
    )


def _datetime_filter(value: object) -> str:
    if not isinstance(value, datetime):
        return "-"
    return value.strftime("%Y-%m-%d %H:%M:%S")


templates.env.filters["datetime"] = _datetime_filter
