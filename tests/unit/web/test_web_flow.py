"""Web 控制台主链路（ADR 0002）：提交 → 运行 → 缺槽应答 → 下载，全部离线。

与 ``tests/unit/cli/test_task_flow.py`` 平行：同一条业务链路，经 HTTP 而非 CLI。
"""

from __future__ import annotations

import hashlib
import io
import zipfile

from tests.unit.web.conftest import (
    MATERIAL_NO_PLAN,
    MATERIAL_PARTIAL,
    MATERIAL_RISKS,
    WebEnv,
)


def test_index_renders_form_and_template_dropdown(web_env: WebEnv) -> None:
    response = web_env.client.get("/")
    assert response.status_code == 200
    body = response.text
    assert "提交任务" in body
    assert web_env.version_id in body
    assert "weekly-report" in body
    # 离线：模型未配置提示 + local_only 默认
    assert "未配置 models.toml" in body
    assert "local_only" in body


def test_healthz(web_env: WebEnv) -> None:
    response = web_env.client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_static_assets_served(web_env: WebEnv) -> None:
    response = web_env.client.get("/static/style.css")
    assert response.status_code == 200
    assert "badge" in response.text


def test_submit_validation_errors(web_env: WebEnv) -> None:
    response = web_env.client.post(
        "/tasks", data={"intent": "", "template": "", "request_id": "req-bad"}
    )
    assert response.status_code == 400
    body = response.text
    assert "提交未通过校验" in body
    assert "意图不能为空" in body
    assert "至少上传一份材料" in body


def test_task_list_page(web_env: WebEnv) -> None:
    response = web_env.client.post(
        "/tasks",
        data={
            "intent": "生成周报",
            "template": web_env.version_id,
            "request_id": "req-list",
        },
        files=[
            (
                "materials",
                ("full.md", "# 项目周报材料\n\n## 一、本周销售情况\n\nok".encode("utf-8")),
            )
        ],
        follow_redirects=False,
    )
    assert response.status_code == 303
    task_id = web_env.task_id_of(response)

    listing = web_env.client.get("/tasks")
    assert listing.status_code == 200
    assert task_id in listing.text


def test_detail_of_unknown_task_is_404(web_env: WebEnv) -> None:
    response = web_env.client.get("/tasks/task_unknown")
    assert response.status_code == 404
    assert "ARTIFACT_NOT_FOUND" in response.text


def test_full_loop_completed_and_export(web_env: WebEnv) -> None:
    client = web_env.client
    submitted = web_env.submit_task(
        materials=[("full.md", web_env.full_material)], request_id="req-web-full"
    )
    assert submitted.status_code == 303
    task_id = web_env.task_id_of(submitted)

    ran = client.post(f"/tasks/{task_id}/run", follow_redirects=False)
    assert ran.status_code == 303
    assert "?" not in ran.headers["location"]  # 无 run_error 参数

    detail = client.get(f"/tasks/{task_id}")
    assert detail.status_code == 200
    body = detail.text
    assert "completed" in body
    assert "已交付" in body
    assert "dlv_" in body
    # 终态任务页带轮询终止标记
    assert 'data-terminal="1"' in body
    # 事件流含提交与交付事件
    assert "task_submitted" in body

    exported = client.post(f"/tasks/{task_id}/export")
    assert exported.status_code == 200
    assert exported.headers["content-type"].startswith(
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )
    payload = exported.content
    assert zipfile.is_zipfile(io.BytesIO(payload))
    sha = hashlib.sha256(payload).hexdigest()
    assert sha in body  # 页面展示了交付 sha256

    # 再次导出同一交付物：路径含 delivery_id，同字节重放，幂等
    again = client.post(f"/tasks/{task_id}/export")
    assert again.status_code == 200
    assert hashlib.sha256(again.content).hexdigest() == sha


def test_awaiting_respond_provide_material(web_env: WebEnv) -> None:
    client = web_env.client
    task_id = web_env.task_id_of(
        web_env.submit_task(
            materials=[("partial.md", MATERIAL_PARTIAL)], request_id="req-web-partial"
        )
    )
    client.post(f"/tasks/{task_id}/run")
    detail = client.get(f"/tasks/{task_id}")
    body = detail.text
    assert "awaiting_user" in body
    assert "等待用户输入" in body
    assert "risks" in body  # 缺槽列表含 risks
    # risks 允许省略（模板契约），另两个槽位不允许
    assert "省略槽位 risks" in body
    assert "省略槽位 sales_summary" not in body
    assert "省略槽位 next_plan" not in body

    # 从页面取隐藏域（与浏览器一致），补交 risks 材料
    hidden = {
        name: web_env.hidden(body, name)
        for name in ("decision_id", "expected_epoch", "awaiting_event_id")
    }
    responded = client.post(
        f"/tasks/{task_id}/respond",
        data={"action": "provide_material", **hidden},
        files=[("materials", ("risks.md", MATERIAL_RISKS.encode("utf-8")))],
        follow_redirects=False,
    )
    assert responded.status_code == 303

    ran = client.post(f"/tasks/{task_id}/run", follow_redirects=False)
    assert ran.status_code == 303
    detail = client.get(f"/tasks/{task_id}")
    assert "completed" in detail.text
    assert "已交付" in detail.text


def test_awaiting_omit_slot(web_env: WebEnv) -> None:
    client = web_env.client
    task_id = web_env.task_id_of(
        web_env.submit_task(
            materials=[("partial.md", MATERIAL_PARTIAL)], request_id="req-web-omit"
        )
    )
    client.post(f"/tasks/{task_id}/run")
    body = client.get(f"/tasks/{task_id}").text
    hidden = {
        name: web_env.hidden(body, name)
        for name in ("decision_id", "expected_epoch", "awaiting_event_id")
    }
    responded = client.post(
        f"/tasks/{task_id}/respond",
        data={"action": "omit_slot", "slot_id": "risks", **hidden},
        follow_redirects=False,
    )
    assert responded.status_code == 303
    client.post(f"/tasks/{task_id}/run")
    detail = client.get(f"/tasks/{task_id}")
    assert "completed" in detail.text
    assert "已交付" in detail.text


def test_omit_slot_rejects_non_omissible_slot(web_env: WebEnv) -> None:
    """缺少的是不可省略的槽位（next_plan）：页面不给省略按钮，提交省略被拒。"""
    client = web_env.client
    task_id = web_env.task_id_of(
        web_env.submit_task(
            materials=[("no_plan.md", MATERIAL_NO_PLAN)], request_id="req-web-omitbad"
        )
    )
    client.post(f"/tasks/{task_id}/run")
    body = client.get(f"/tasks/{task_id}").text
    assert "next_plan" in body  # 缺槽
    assert "省略槽位 next_plan" not in body  # 契约不允许省略

    hidden = {
        name: web_env.hidden(body, name)
        for name in ("decision_id", "expected_epoch", "awaiting_event_id")
    }
    response = client.post(
        f"/tasks/{task_id}/respond",
        data={"action": "omit_slot", "slot_id": "next_plan", **hidden},
        files=[],
    )
    assert response.status_code == 409
    assert "TASK_STATE_INVALID" in response.text


def test_events_json_pagination(web_env: WebEnv) -> None:
    client = web_env.client
    task_id = web_env.task_id_of(
        web_env.submit_task(
            materials=[("full.md", web_env.full_material)], request_id="req-web-events"
        )
    )
    page = client.get(f"/tasks/{task_id}/events?after=0")
    assert page.status_code == 200
    payload = page.json()
    codes = [item["event_code"] for item in payload["items"]]
    assert "task_submitted" in codes
    last_seq = payload["last_seq"]
    assert last_seq > 0

    empty = client.get(f"/tasks/{task_id}/events?after={last_seq}")
    assert empty.json()["items"] == []

    bad = client.get(f"/tasks/{task_id}/events?after=notanint")
    assert bad.status_code == 200
    assert bad.json()["items"]  # 回退为全量


def test_export_before_delivery_is_409(web_env: WebEnv) -> None:
    task_id = web_env.task_id_of(
        web_env.submit_task(
            materials=[("full.md", web_env.full_material)], request_id="req-web-early"
        )
    )
    response = web_env.client.post(f"/tasks/{task_id}/export")
    assert response.status_code == 409
    assert "TASK_STATE_INVALID" in response.text


def test_cancel_then_run_is_409(web_env: WebEnv) -> None:
    client = web_env.client
    task_id = web_env.task_id_of(
        web_env.submit_task(
            materials=[("full.md", web_env.full_material)], request_id="req-web-cancel"
        )
    )
    cancelled = client.post(f"/tasks/{task_id}/cancel", follow_redirects=False)
    assert cancelled.status_code == 303
    detail = client.get(f"/tasks/{task_id}")
    assert "cancelled" in detail.text

    response = client.post(f"/tasks/{task_id}/run")
    assert response.status_code == 409
    assert "TASK_STATE_INVALID" in response.text


def test_respond_replay_is_idempotent(web_env: WebEnv) -> None:
    """同一表单（同 decision_id）重复提交是重放，不产生重复决策。"""
    client = web_env.client
    task_id = web_env.task_id_of(
        web_env.submit_task(
            materials=[("partial.md", MATERIAL_PARTIAL)], request_id="req-web-replay"
        )
    )
    client.post(f"/tasks/{task_id}/run")
    body = client.get(f"/tasks/{task_id}").text
    hidden = {
        name: web_env.hidden(body, name)
        for name in ("decision_id", "expected_epoch", "awaiting_event_id")
    }

    def post_respond():
        return client.post(
            f"/tasks/{task_id}/respond",
            data={"action": "omit_slot", "slot_id": "risks", **hidden},
            files=[],
            follow_redirects=False,
        )

    first = post_respond()
    assert first.status_code == 303
    second = post_respond()
    assert second.status_code == 303  # 幂等重放，同 key 同 payload

    client.post(f"/tasks/{task_id}/run")
    assert "completed" in client.get(f"/tasks/{task_id}").text


def test_llm_allowed_without_model_config_is_rejected(web_env: WebEnv) -> None:
    """llm_allowed 任务在无 models.toml 时拒绝远端调用（D25 / T12）。"""
    client = web_env.client
    task_id = web_env.task_id_of(
        web_env.submit_task(
            materials=[("full.md", web_env.full_material)],
            request_id="req-web-llm",
            llm_allowed=True,
        )
    )
    detail = client.get(f"/tasks/{task_id}")
    assert "llm_allowed" in detail.text

    response = client.post(f"/tasks/{task_id}/run", follow_redirects=False)
    # 网关/模型缺失在图执行中抛 OmasError → 错误页（不重试、不回退）
    assert response.status_code == 500
    assert "OMAS_ERROR" in response.text or "MODEL_" in response.text


def test_submit_replay_is_idempotent(web_env: WebEnv) -> None:
    first = web_env.submit_task(
        materials=[("full.md", web_env.full_material)], request_id="req-web-same"
    )
    second = web_env.submit_task(
        materials=[("full.md", web_env.full_material)], request_id="req-web-same"
    )
    assert first.status_code == second.status_code == 303
    assert web_env.task_id_of(first) == web_env.task_id_of(second)


def test_error_page_shape(web_env: WebEnv) -> None:
    response = web_env.client.get("/no-such-page")
    assert response.status_code == 404
    assert "HTTP_404" in response.text
    assert "返回提交任务" in response.text


_BUILTIN_IDS = ("work-report", "monthly-report", "research-report")


def test_builtin_templates_seeded_at_startup(web_env: WebEnv) -> None:
    """ADR 0003：控制台启动即播种内置模板，库不为空且全部可激活。"""
    templates = web_env.client.get("/api/templates").json()["templates"]
    by_id = {t["template_id"]: t for t in templates}
    assert set(_BUILTIN_IDS) <= set(by_id)
    for template_id in _BUILTIN_IDS:
        seeded = by_id[template_id]
        assert seeded["activatable"] is True, seeded
        assert seeded["slots"], f"{template_id} 槽位未暴露"
    assert by_id["weekly-report"]["template_id"] == "weekly-report"


def test_builtin_seeding_does_not_overwrite_user_templates(web_env: WebEnv) -> None:
    """用户重命名内置模板的展示名后，再次构建应用不回滚 meta（I10 边界外、meta 可变）。"""
    rename = web_env.client.post(
        "/api/templates/work-report/meta",
        json={"display_name": "季度工作报告", "description": "自定义简介"},
    )
    assert rename.status_code == 200
    # 同一 OMAS_HOME 再次创建应用：播种检测到内容一致，不产生新版本
    from omas.web.app import create_web_app

    home = web_env.home
    with_builtins_again = create_web_app(home)
    from starlette.testclient import TestClient

    with TestClient(with_builtins_again) as client:
        templates = client.get("/api/templates").json()["templates"]
    by_id = {t["template_id"]: t for t in templates}
    assert by_id["work-report"]["display_name"] == "季度工作报告"
    versions = [
        t["version"] for t in templates if t["template_id"] == "work-report"
    ]
    assert versions == [1]
