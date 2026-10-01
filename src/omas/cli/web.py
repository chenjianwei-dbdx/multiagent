"""``omas web``：本地回环 Web 控制台（ADR 0002）。

只允许绑定回环地址——这不是多用户服务：不做鉴权、会话与多租户。
生命周期由 Starlette lifespan 负责（关闭 ledger）；本命令只负责装配与启动。
"""

from __future__ import annotations

from typing import Annotated

import typer
import uvicorn

from omas.cli.context import CliContext, cli_context
from omas.config.settings import default_models_path
from omas.web.app import create_web_app

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def register_web(app: typer.Typer) -> None:
    """把 ``web`` 命令挂到 Typer 应用上（避免与 cli.main 产生循环导入）。"""

    @app.command("web")
    def web(
        ctx: typer.Context,
        host: Annotated[
            str,
            typer.Option("--host", help="绑定地址，仅允许回环地址。"),
        ] = "127.0.0.1",
        port: Annotated[
            int, typer.Option("--port", help="监听端口。", min=1, max=65535)
        ] = 8000,
    ) -> None:
        """启动本地回环 Web 控制台（提交任务 / 运行 / 缺槽应答 / 下载交付物）。"""
        cli: CliContext = cli_context(ctx)
        if host not in LOOPBACK_HOSTS:
            raise typer.BadParameter(
                f"仅允许回环地址：{sorted(LOOPBACK_HOSTS)}", param_hint="--host"
            )
        models_path = cli.models_path or default_models_path(cli.home)
        web_app = create_web_app(cli.home, models_path)
        typer.echo(f"OMAS 控制台启动：http://{host}:{port}（home={cli.home}；Ctrl-C 退出）")
        uvicorn.run(web_app, host=host, port=port, log_level="info")
