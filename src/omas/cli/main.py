"""omas 入口（P5）：Typer 应用组装。

数据目录解析顺序：``--home`` > 环境变量 ``OMAS_HOME`` > ``~/.omas``。
所有子命令只经由 :mod:`omas.app.bootstrap` 的组合根访问业务。
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from omas.cli.context import CliContext, default_home
from omas.cli.task import task_app
from omas.cli.template import template_app
from omas.cli.web import register_web

app = typer.Typer(
    name="omas",
    help="OMAS：办公文档装配与质检系统（离线 MVP，CLI-first）。",
    no_args_is_help=True,
    add_completion=False,
    pretty_exceptions_enable=False,
    pretty_exceptions_show_locals=False,
)

app.add_typer(template_app, name="template")
app.add_typer(task_app, name="task")
register_web(app)


@app.command("config")
def config_check(
    ctx: typer.Context,
) -> None:
    """显示生效的模型配置（不打印密钥）。"""
    context: CliContext = ctx.obj
    with context.errors():
        container = context.container()
        settings = container.model_settings
        if settings is None:
            typer.echo("model: <offline>（未配置 models.toml；local_only 确定性路径）")
            return
        import os

        key_state = (
            "set"
            if settings.api_key_env and os.environ.get(settings.api_key_env)
            else "UNSET"
        )
        typer.echo(f"provider: {settings.provider}")
        typer.echo(f"model_id: {settings.model_id}")
        typer.echo(f"base_url: {settings.base_url or '<official default>'}")
        typer.echo(f"api_key_env: {settings.api_key_env} ({key_state})")
        typer.echo("policy: 需任务显式 --data-policy llm_allowed 才会发起远端调用")



@app.callback()
def main_callback(
    ctx: typer.Context,
    home: Annotated[
        Path | None,
        typer.Option(
            "--home",
            envvar="OMAS_HOME",
            help=f"OMAS 数据目录（默认 {default_home()}，或环境变量 OMAS_HOME）。",
        ),
    ] = None,
    debug: Annotated[
        bool, typer.Option("--debug", help="出错时抛出原始异常（打印堆栈）。")
    ] = False,
    models: Annotated[
        Path | None,
        typer.Option(
            "--models",
            envvar="OMAS_MODELS_TOML",
            help="模型配置文件（默认 $OMAS_HOME/models.toml，可不存在=离线模式）。",
        ),
    ] = None,
) -> None:
    resolved = home.expanduser() if home is not None else default_home()
    ctx.obj = CliContext(
        home=resolved, debug=debug, models_path=models.expanduser() if models else None
    )


if __name__ == "__main__":  # pragma: no cover - manual invocation
    app()
