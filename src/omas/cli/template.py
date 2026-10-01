"""``omas template`` 子命令：模板提取、内置播种与注册（版本不可变，永不覆盖）。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any

import typer

from omas.cli.context import EXIT_UNSUPPORTED_TEMPLATE, cli_context
from omas.cli.render import echo_kv
from omas.templates.registry import TemplateRegistry
from omas.templates.seed import seed_builtin_templates

template_app = typer.Typer(
    help="模板提取与版本注册（重提取产生新版本，不覆盖）。",
    no_args_is_help=True,
)


def _load_json_object(path: Path, option: str) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise typer.BadParameter(f"{path} 不是合法 JSON: {exc}", param_hint=option) from exc
    if not isinstance(data, dict):
        raise typer.BadParameter(f"{path} 顶层必须是 JSON object", param_hint=option)
    return data


@template_app.command("extract")
def extract(
    ctx: typer.Context,
    docx: Annotated[
        Path,
        typer.Argument(help="模板 DOCX 文件（.docx）。",
                       exists=True, dir_okay=False, readable=True),
    ],
    contract: Annotated[
        Path,
        typer.Option("--contract", help="语义 sidecar（槽位/章节契约）JSON 文件。",
                     exists=True, dir_okay=False, readable=True),
    ],
    template_id: Annotated[str, typer.Option("--template-id", help="模板标识。")],
    styles: Annotated[
        Path | None,
        typer.Option("--styles", help="样式规格 JSON（缺省为空对象）。",
                     exists=True, dir_okay=False, readable=True),
    ] = None,
    static_map: Annotated[
        Path | None,
        typer.Option("--static-map", help="静态区域映射 JSON（缺省为空对象）。",
                     exists=True, dir_okay=False, readable=True),
    ] = None,
) -> None:
    """提取模板 → 注册为不可变新版本；打印 version_id 与版本号。

    含不支持结构（unsupported findings）时仍会注册（可审计），但以退出码 2
    列明全部 findings，模板不可激活。
    """
    cli = cli_context(ctx)
    with cli.errors():
        sidecar = _load_json_object(contract, "--contract")
        styles_spec = _load_json_object(styles, "--styles") if styles else {}
        static = _load_json_object(static_map, "--static-map") if static_map else {}
        docx_bytes = docx.read_bytes()
        container = cli.container()
        registry = TemplateRegistry(container.store, container.ledger)
        version_id, template_contract = registry.register(
            docx_bytes=docx_bytes,
            sidecar=sidecar,
            styles_spec=styles_spec,
            static_map=static,
            template_id=template_id,
        )
        echo_kv("template_id", template_id)
        echo_kv("version", template_contract.version)
        echo_kv("version_id", version_id)
        findings = template_contract.unsupported_findings
        echo_kv("activatable", template_contract.is_activatable())
        if findings:
            echo_kv("unsupported_findings", len(findings))
            for finding in findings:
                location = f" {finding.locator}" if finding.locator else ""
                detail = f": {finding.detail}" if finding.detail else ""
                typer.echo(f"- [{finding.code}]{location}{detail}")
            typer.echo(
                f"ERROR [TEMPLATE_UNSUPPORTED]: 模板已注册为版本 {version_id}"
                f"（version {template_contract.version}），但存在 {len(findings)}"
                " 项不支持结构，不可激活",
                err=True,
            )
            raise typer.Exit(code=EXIT_UNSUPPORTED_TEMPLATE)


@template_app.command("seed")
def seed(ctx: typer.Context) -> None:
    """注册随包发布的内置模板到当前 OMAS_HOME（幂等：内容未变不产生新版本）。"""
    cli = cli_context(ctx)
    with cli.errors():
        container = cli.container()
        results = seed_builtin_templates(store=container.store, ledger=container.ledger)
        unsupported = 0
        for result in results:
            action = "registered" if result.newly_registered else "up-to-date"
            echo_kv(result.template_id, f"v{result.version} {action}")
            echo_kv("version_id", result.version_id)
            echo_kv("activatable", result.activatable)
            if result.findings:
                unsupported += 1
                for finding in result.findings:
                    typer.echo(f"- {finding}")
        if unsupported:
            typer.echo(
                f"ERROR [TEMPLATE_UNSUPPORTED]: {unsupported} 项内置模板存在不支持结构，"
                "已注册留档但不可激活",
                err=True,
            )
            raise typer.Exit(code=EXIT_UNSUPPORTED_TEMPLATE)
