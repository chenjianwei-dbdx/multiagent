"""P0 离线兼容性探针的 pytest 包装。

四个探针全部离线:pydantic-ai TestModel/FunctionModel(无 API key、无网络)、本地 sqlite
checkpoint、内存构造的 DOCX。probe2 会以 sys.executable 拉起真实子进程,但仍在本地完成。
"""

from __future__ import annotations

import importlib.util
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

PROBES_DIR = Path(__file__).resolve().parent.parent / "probes"

PROBE_MODULES: dict[str, str] = {
    "probe1": "probe1_pydantic_ai.py",
    "probe2": "probe2_langgraph_sqlite.py",
    "probe3": "probe3_docxtpl_chars.py",
    "probe4": "probe4_ooxml_anchors_styles.py",
}


def _load_probe(module_key: str) -> Any:
    """按文件路径加载探针模块(probes/ 不是包,避免改动打包配置)。"""
    path = PROBES_DIR / PROBE_MODULES[module_key]
    spec = importlib.util.spec_from_file_location(f"probes.{module_key}", path)
    if spec is None or spec.loader is None:  # pragma: no cover - 理论不可达
        raise ImportError(f"无法加载探针模块: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _assert_probe_pass(run: Callable[[], dict[str, Any]]) -> None:
    report = run()
    assert report["status"] == "pass", f"探针失败: {report}"
    findings = report.get("findings") or []
    assert isinstance(findings, list) and findings, "探针必须产出至少一条 findings"
    assert isinstance(report.get("details"), dict), "探针必须返回 details dict"


@pytest.mark.probe
def test_probe1_pydantic_ai_testmodel_structured_output() -> None:
    """TestModel 离线运行、工具调用、DTO 解析、重试预算与 usage 形态。"""
    _assert_probe_pass(_load_probe("probe1").run)


@pytest.mark.probe
def test_probe2_langgraph_sqlite_cross_process_resume() -> None:
    """SqliteSaver 跨进程 interrupt/resume 与节点重放语义。"""
    _assert_probe_pass(_load_probe("probe2").run)


@pytest.mark.probe
def test_probe3_docxtpl_character_fidelity() -> None:
    """docxtpl 对 LF/Tab/中文/emoji/XML 特殊字符的保真与 autoescape 差异。"""
    _assert_probe_pass(_load_probe("probe3").run)


@pytest.mark.probe
def test_probe4_ooxml_anchors_and_styles_reading() -> None:
    """lxml/python-docx 对锚点、文本结构、有效样式、numbering、页眉页脚的读取。"""
    _assert_probe_pass(_load_probe("probe4").run)
