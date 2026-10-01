"""本地回环 Web 控制台（ADR 0002）：Starlette + 服务器渲染 HTML。

约束见 ``docs/adr/0002-web-console.md``：仅 loopback、默认 local_only、
输入框只喂判断不进正文、HTTP 层只经由 ``AppContainer`` / ``TaskService``。
"""

from omas.web.app import create_web_app

__all__ = ["create_web_app"]
