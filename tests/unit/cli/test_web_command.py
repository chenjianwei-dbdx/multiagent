"""``omas web`` 命令：回环限制与装配（uvicorn.run 以替身替换，不起真服务）。"""

from __future__ import annotations

import uvicorn
from typer.testing import CliRunner

from omas.cli.main import app
from tests.unit.cli.conftest import Workbench


def test_web_rejects_non_loopback_host(runner: CliRunner, bench: Workbench) -> None:
    result = bench.cli(runner, "web", "--host", "0.0.0.0")
    assert result.exit_code != 0
    assert "回环" in result.output


def test_web_assembles_on_default_loopback(
    runner: CliRunner, bench: Workbench, monkeypatch
) -> None:
    calls: list[tuple[object, dict[str, object]]] = []

    def fake_run(app_obj, **kwargs):  # type: ignore[no-untyped-def]
        calls.append((app_obj, kwargs))
        # 证明组合根确实装配完成（组合根在工厂期急切构建）
        assert app_obj.state.container is not None
        assert app_obj.state.container.ledger.connection is not None
        app_obj.state.container.ledger.close()

    monkeypatch.setattr(uvicorn, "run", fake_run)
    result = bench.cli(runner, "web", "--port", "8123")
    assert result.exit_code == 0, result.output
    assert "http://127.0.0.1:8123" in result.output
    assert f"home={bench.home}" in result.output

    assert len(calls) == 1
    _app_obj, kwargs = calls[0]
    assert kwargs == {"host": "127.0.0.1", "port": 8123, "log_level": "info"}


def test_web_command_registered_in_help(runner: CliRunner, bench: Workbench) -> None:
    result = bench.cli(runner, "web", "--help")
    assert result.exit_code == 0
    assert "本地回环 Web 控制台" in result.output


def test_app_importable_without_side_effects() -> None:
    from omas.web.app import create_web_app

    assert app  # CLI 已挂 web 命令
    assert callable(create_web_app)
