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

#: 事件码 → 中文展示标签。与 console.js 的 describeEvent 保持同步（两个前端
#: 渲染同一批事件码）；尤其 research_failed 必须明示"非任务失败"，
#: 否则容易被误读为任务执行失败（图内采集失败只记录并跳过）。
EVENT_LABELS: dict[str, str] = {
    "task_submitted": "任务已提交",
    "inventory": "盘点材料",
    "tool_call": "工具调用",
    "plan": "生成内容计划",
    "researched": "联网采集完成",
    "research_failed": "联网采集失败（已跳过，非任务失败）",
    "assembled": "装配槽位绑定",
    "binding_committed": "绑定已提交",
    "awaiting_user": "等待补充材料",
    "decision_provide_material": "已补充材料，任务继续",
    "decision_omit_slot": "已豁免槽位，任务继续",
    "exported": "交付物已导出",
    "execution_failed": "执行失败",
    "task_cancelled": "任务已取消",
    "task_recovered": "任务已恢复",
}

#: 需要追加善后说明的事件（非致命语义、对用户的影响）
EVENT_NOTES: dict[str, str] = {
    "research_failed": "采集失败不阻断装配：任务继续执行，缺槽时由门禁提示补充材料",
}


def _event_label_filter(code: object) -> str:
    return EVENT_LABELS.get(str(code), str(code))


def _event_note_filter(code: object) -> str:
    return EVENT_NOTES.get(str(code), "")


templates.env.filters["event_label"] = _event_label_filter
templates.env.filters["event_note"] = _event_note_filter
