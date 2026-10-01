"""Web 控制台离线测试夹具（ADR 0002）：真实 Starlette app + TestClient。

模板经 TemplateRegistry 直接注册（与 ``omas template extract`` 同路径），
全流程 local_only 确定性，零联网、零真实模型。
"""

from __future__ import annotations

import re
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path

import pytest
from starlette.testclient import TestClient

_FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"
if str(_FIXTURES) not in sys.path:
    sys.path.insert(0, str(_FIXTURES))

from weekly_report import build_weekly_report_template  # noqa: E402

from omas.app.bootstrap import AppContainer  # noqa: E402
from omas.templates.registry import TemplateRegistry  # noqa: E402
from omas.web.app import create_web_app  # noqa: E402

MATERIAL_PARTIAL = (
    "# 项目周报材料\n\n## 一、本周销售情况\n\n销售额 500 万，符合预期。\n\n"
    "## 三、下周计划\n\n推进验收。\n"
)
MATERIAL_RISKS = "## 二、风险与依赖\n\n接口联调存在一天延期风险，已登记。"
MATERIAL_NO_PLAN = (
    "# 项目周报材料\n\n## 一、本周销售情况\n\n销售额 500 万，符合预期。\n\n"
    "## 二、风险与依赖\n\n接口联调存在一天延期风险，已登记。\n"
)


@dataclass
class WebEnv:
    home: Path
    client: TestClient
    version_id: str
    container: AppContainer
    full_material: str

    def submit_task(
        self,
        *,
        intent: str = "生成周报",
        materials: list[tuple[str, str]],
        request_id: str | None = None,
        llm_allowed: bool = False,
        follow_redirects: bool = False,
    ):
        files = [
            ("materials", (name, text.encode("utf-8"), "text/markdown"))
            for name, text in materials
        ]
        data: dict[str, str] = {
            "intent": intent,
            "template": self.version_id,
            "request_id": request_id or f"req-{uuid.uuid4().hex}",
        }
        if llm_allowed:
            data["data_policy"] = "llm_allowed"
        return self.client.post(
            "/tasks", data=data, files=files, follow_redirects=follow_redirects
        )

    def task_id_of(self, response) -> str:
        return response.headers["location"].removesuffix("/").rsplit("/", 1)[-1]

    def hidden(self, html: str, name: str) -> str:
        """从任务页的应答表单里取隐藏域（模拟浏览器的表单状态）。"""
        match = re.search(rf'name="{re.escape(name)}"\s+value="([^"]*)"', html)
        if match is None:
            raise AssertionError(f"hidden field {name!r} not found in page")
        return match.group(1)


@pytest.fixture(scope="session")
def weekly():
    return build_weekly_report_template()


@pytest.fixture()
def web_env(tmp_path: Path, weekly) -> WebEnv:
    home = tmp_path / "home"
    home.mkdir()
    app = create_web_app(home)
    client = TestClient(app)
    with client:
        container: AppContainer = app.state.container
        registry = TemplateRegistry(container.store, container.ledger)
        version_id, _contract = registry.register(
            docx_bytes=weekly.docx_bytes,
            sidecar=weekly.sidecar,
            styles_spec=weekly.styles_spec,
            static_map=weekly.static_map,
            template_id="weekly-report",
        )
        yield WebEnv(
            home=home,
            client=client,
            version_id=version_id,
            container=container,
            full_material=weekly.material_markdown,
        )
