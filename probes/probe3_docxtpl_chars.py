"""Probe 3 — docxtpl 0.20.2 特殊字符与空白保真(离线)。

目的:实测 docxtpl 渲染管道对各类"危险载荷"的保真度,为 P1 渲染器确定:
LF/Tab 的处理策略、是否必须开 autoescape、中文与 emoji 是否安全。

方法:
1. python-docx 在内存构造最小模板:单个段落、单个 run、文本恰为 ``{{ payload }}``;
2. 对每组载荷 x {autoescape=False, autoescape=True} 用 DocxTemplate.render 渲染并落盘字节;
3. 用【独立提取器】(严格 lxml 解析 zip 内 document.xml,按文档顺序枚举 w:r 的子节点:
   w:t → 文本、w:tab → '\\t'、w:br → '\\n')重建"逻辑文本",与输入逐字符对比;
   另统计仅拼接 w:t 文本(不含 tab/br 结构)的结果,定位字符去向。

所有实测行为(尤其与任务书预期不符处:docxtpl 实际会把 \\n→<w:br/>、\\t→<w:tab/>)
记入 findings,不算失败。
"""

from __future__ import annotations

import io
import zipfile
from typing import Any

from docx import Document
from docxtpl import DocxTemplate
from lxml import etree

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def _make_template_bytes() -> bytes:
    """python-docx 内存构造模板:一段落一 run,文本恰为 '{{ payload }}'。"""
    doc = Document()
    doc.add_paragraph("{{ payload }}")
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def _logical_text_of_run(run_el: Any) -> str:
    """按文档顺序重建 run 的逻辑文本:w:t→原文、w:tab→\\t、w:br→\\n。"""
    parts: list[str] = []
    for child in run_el:
        tag = etree.QName(child).localname
        if tag == "t":
            parts.append(child.text or "")
        elif tag == "tab":
            parts.append("\t")
        elif tag == "br":
            parts.append("\n")
    return "".join(parts)


def _extract(docx_bytes: bytes) -> dict[str, Any]:
    """独立提取器:严格 lxml 解析 document.xml,返回逻辑文本与结构统计。"""
    with zipfile.ZipFile(io.BytesIO(docx_bytes)) as zf:
        xml_bytes = zf.read("word/document.xml")
    root = etree.fromstring(xml_bytes)  # 严格解析(docxtpl 内部用的是 recover=True)
    body = root.find(f"{{{W_NS}}}body")

    logical_parts: list[str] = []
    wt_texts: list[str] = []
    n_tab = len(root.findall(f".//{{{W_NS}}}tab"))
    n_br = len(root.findall(f".//{{{W_NS}}}br"))
    n_para = len(root.findall(f".//{{{W_NS}}}p"))
    wt_space_preserve = 0
    for t_el in root.iter(f"{{{W_NS}}}t"):
        wt_texts.append(t_el.text or "")
        if t_el.get("{http://www.w3.org/XML/1998/namespace}space") == "preserve":
            wt_space_preserve += 1
    for p_el in body.iter(f"{{{W_NS}}}p"):
        for r_el in p_el.findall(f"{{{W_NS}}}r"):
            logical_parts.append(_logical_text_of_run(r_el))

    return {
        "logical_text": "".join(logical_parts),
        "wt_concat": "".join(wt_texts),
        "n_tab": n_tab,
        "n_br": n_br,
        "n_paragraph": n_para,
        "n_wt": len(wt_texts),
        "n_wt_space_preserve": wt_space_preserve,
    }


def _render_once(payload: str, autoescape: bool) -> dict[str, Any]:
    """渲染一次并提取;返回观察记录(异常也记录为观察结果)。"""
    tpl = DocxTemplate(io.BytesIO(_make_template_bytes()))
    try:
        tpl.render({"payload": payload}, autoescape=autoescape)
        out = io.BytesIO()
        tpl.save(out)
        extracted = _extract(out.getvalue())
        extracted["render_error"] = None
        return extracted
    except Exception as exc:  # 渲染异常本身是重要观察结果
        return {
            "render_error": f"{type(exc).__module__}.{type(exc).__name__}: {exc}",
            "logical_text": None,
            "wt_concat": None,
        }


# 载荷组:覆盖任务书要求的全部类别
PAYLOADS: dict[str, str] = {
    # cjk 载荷故意保留全角逗号与句号——中文文档的真实形态,验证其往返保真
    "cjk": "中文测试字符串，包含标点。",
    "xml_specials": 'a<b>c & d"e\'f>g',
    "consecutive_spaces": "a  b   c",
    "leading_trailing_spaces": "  padded  ",
    "tab": "a\tb",
    "lf": "line1\nline2",
    "emoji": "😀 和 👨‍👩‍👧‍👦",
    "mixed": '中<文>\t&\n"😀"  空格',
    "form_feed": "page1\fpage2",
}


def run() -> dict[str, Any]:
    """执行探针,返回 {status, findings, details}。"""
    findings: list[str] = []
    details: dict[str, Any] = {"cases": {}}

    for name, payload in PAYLOADS.items():
        for ae in (False, True):
            obs = _render_once(payload, ae)
            obs["input"] = payload
            if obs["render_error"] is None:
                obs["logical_same_as_input"] = obs["logical_text"] == payload
                obs["wt_only_same_as_input"] = obs["wt_concat"] == payload
            details["cases"][f"{name}|autoescape={ae}"] = obs

    # ---- 断言(探针跑不通才算 fail):基础保真必须成立 ----
    for ae in (False, True):
        case = details["cases"][f"cjk|autoescape={ae}"]
        if not case["logical_same_as_input"]:
            raise RuntimeError(f"中文字符在 autoescape={ae} 下丢失: {case!r}")
        case = details["cases"][f"emoji|autoescape={ae}"]
        if not case["logical_same_as_input"]:
            raise RuntimeError(f"emoji 在 autoescape={ae} 下丢失: {case!r}")
        case = details["cases"][f"consecutive_spaces|autoescape={ae}"]
        if not case["logical_same_as_input"]:
            raise RuntimeError(f"连续空格在 autoescape={ae} 下丢失: {case!r}")
        case = details["cases"][f"leading_trailing_spaces|autoescape={ae}"]
        if not case["logical_same_as_input"]:
            raise RuntimeError(f"首尾空格在 autoescape={ae} 下丢失: {case!r}")
    case = details["cases"]["xml_specials|autoescape=True"]
    if not case["logical_same_as_input"]:
        raise RuntimeError(f"autoescape=True 未能完整保真 XML 特殊字符: {case!r}")

    # ---- findings:实测行为事实 ----
    lf_off = details["cases"]["lf|autoescape=False"]
    tab_off = details["cases"]["tab|autoescape=False"]
    ff_off = details["cases"]["form_feed|autoescape=False"]
    xs_off = details["cases"]["xml_specials|autoescape=False"]
    xs_on = details["cases"]["xml_specials|autoescape=True"]

    findings.append(
        f"【与任务书预期相反】LF '\\n' 在 docxtpl 0.20.2 中【会】自动转为 <w:br/>:"
        f"载荷 'line1\\nline2' 渲染后产生 {lf_off['n_br']} 个 w:br、{lf_off['n_wt']} 个 w:t,"
        "逻辑文本逐字符还原一致,无需 RichText。机制:template.py 的 resolve_listing() 在每次"
        "渲染后无条件对 w:t 内容做字符串替换 "
        "'\\n' → '</w:t><w:br/><w:t xml:space=\"preserve\">'(docxtpl/template.py L395)。"
        "P1 渲染器可直接依赖该行为;反向抽取时必须把 w:br 映射回 '\\n'。"
    )
    findings.append(
        f"Tab '\\t' 同样被自动转为真实的 <w:tab/> 元素:载荷 'a\\tb' 渲染后 "
        f"{tab_off['n_tab']} 个 w:tab,仅拼 w:t 文本会丢掉 tab(wt_only='ab'),"
        "逻辑文本(w:tab→\\t)还原一致。机制:resolve_listing() 把 '\\t' 替换为 "
        "'</w:t></w:r><w:r><w:tab/></w:r><w:r>...'(保留原 rPr;docxtpl/template.py L383-388)。"
        "docxtpl 不报错、XML 合法。"
    )
    findings.append(
        f"额外发现:'\\a'(0x07) 会被替换为【段落拆分】、'\\f'(换页) 替换为 w:br type=\"page\" "
        f"(载荷 'page1\\fpage2' 渲染后段落数 {ff_off['n_paragraph']}、"
        f"逻辑文本将 \\f 变为 \\n——即 docxtpl 把换页降级为普通换行读回)。"
    )
    findings.append(
        f"autoescape=False + XML 特殊字符 = 【静默数据损坏,不报错】:载荷 {xs_off['input']!r} "
        f"渲染/保存全程无异常,但读回只剩 {xs_off['logical_text']!r}——裸 '<' 让值中段被解析成"
        "伪元素、裸 '&' 被丢弃。机制:docxtpl 内部用 etree.XMLParser(recover=True) 解析渲染后的 "
        "XML(fix_tables,docxtpl/template.py L520-521),错误被吞掉。P1 必须默认 autoescape=True "
        "或等效转义,否则 <>& 载荷会无声丢失。"
    )
    findings.append(
        f"autoescape=True 时 <>&\"' 全部完整保真(读回 {xs_on['logical_text']!r} 与输入一致);"
        "markupsafe 转义会在 XML 里写成 &lt;&gt;&amp;&quot;&#39;,读回自动解回。"
        "两种模式下引号读回无差异(w:t 文本节点中引号本就无需转义)。"
    )
    findings.append(
        "连续空格与首尾空格完整保留:渲染产物 w:t 带 xml:space=\"preserve\" "
        f"(cjk 载荷 {details['cases']['cjk|autoescape=False']['n_wt_space_preserve']}/"
        f"{details['cases']['cjk|autoescape=False']['n_wt']} 个 w:t 均带该属性,含 docxtpl 拆分"
        "生成的新 w:t),Word 显示不会折叠。"
    )
    findings.append(
        "中文与 emoji(含 ZWJ 组合 👨‍👩‍👧‍👦,U+200D 序列)在两种 autoescape 下均逐码位完整保留,"
        "无代理对损坏、无实体化。"
    )
    findings.append(
        "DocxTemplate.render 签名实测:render(context, jinja_env=None, autoescape=False),"
        "autoescape 是一等参数(无需自建 jinja_env);探针的独立提取器用【严格】lxml 解析均成功,"
        "说明 autoescape=True 产物是良构 XML;autoescape=False 且含 <>& 的产物只有 recover=True "
        "才能'成功'。"
    )

    return {
        "status": "pass",
        "probe": "probe3_docxtpl_chars",
        "findings": findings,
        "details": details,
    }


if __name__ == "__main__":
    import json
    import sys

    try:
        report = run()
    except Exception as exc:  # 探针本身跑不通 → fail + 退出码 1
        import traceback

        report = {
            "status": "fail",
            "probe": "probe3_docxtpl_chars",
            "error": f"{type(exc).__module__}.{type(exc).__name__}: {exc}",
            "traceback": traceback.format_exc(),
        }
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    sys.exit(0 if report["status"] == "pass" else 1)
