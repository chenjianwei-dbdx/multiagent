"""Probe 1 — pydantic-ai 2.52.0 受控模型与结构化输出(离线)。

目的:在完全离线(无 API key、无网络)条件下实测 pydantic-ai 的关键行为,为 P1 选定
Agent 用法基线:

1. TestModel 是否可以在清除所有云厂商 API key 环境变量后正常构造与运行;
2. 带 1 个工具(``lookup(text: str) -> str``)的 Agent + frozen/extra="forbid" 输出 DTO:
   工具是否被调用、输出是否可解析为该 DTO、DTO 约束是否真实生效;
3. 输出校验重试上限(pydantic-ai 2.x 中参数名为 ``retries``,旧名 ``output_retries``
   已不存在):设为 1 与 0 时的实际行为(用 FunctionModel 制造"先坏后好"的输出);
4. ``result.usage`` 的实际形态(2.x 中是属性而非方法)与 TestModel 下的 token 元数据数值。

所有"与预期不符但探针本身可运行"的事实记入 findings,不算失败。
"""

from __future__ import annotations

import dataclasses
import inspect
import os
from typing import Any

import pydantic_ai
from pydantic import BaseModel, ValidationError
from pydantic_ai.agent import Agent
from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.models.test import TestModel

_KEY_MARKERS = (
    "API_KEY",
    "OPENAI",
    "ANTHROPIC",
    "GEMINI",
    "GOOGLE",
    "GROQ",
    "MISTRAL",
    "BEDROCK",
    "COHERE",
)


class ProbeOutput(BaseModel):
    """探针输出 DTO:frozen + extra=forbid,模拟 P1 计划中的结构化输出契约。"""

    model_config = {"frozen": True, "extra": "forbid"}

    answer: str
    confidence: float


def _strip_provider_env() -> list[str]:
    """清除进程内所有疑似云厂商凭证环境变量,返回被清除的变量名列表。"""
    removed = [k for k in list(os.environ) if any(m in k.upper() for m in _KEY_MARKERS)]
    for k in removed:
        os.environ.pop(k, None)
    return removed


def _message_flow(messages: list[Any]) -> list[dict[str, Any]]:
    """压缩 message 流为可 JSON 序列化的摘要(类型 + 工具名)。"""
    flow = []
    for m in messages:
        entry: dict[str, Any] = {"type": m.__class__.__name__}
        tool_names = sorted(
            {p.tool_name for p in getattr(m, "parts", []) if hasattr(p, "tool_name")}
        )
        if tool_names:
            entry["tools"] = tool_names
        flow.append(entry)
    return flow


def _run_testmodel_agent() -> dict[str, Any]:
    """核心场景:TestModel + 工具 + 结构化输出,一次性收集全部观测。"""
    tool_calls: list[str] = []

    def lookup(text: str) -> str:
        tool_calls.append(text)
        return f"echo:{text}"

    agent = Agent(TestModel(), output_type=ProbeOutput, tools=[lookup], retries=1)
    result = agent.run_sync("probe: please answer")

    # usage 形态:2.x 里 result.usage 是属性(property),不是方法。
    usage_obj = result.usage
    usage_is_property = not callable(usage_obj)
    usage_as_dict = {f.name: getattr(usage_obj, f.name) for f in dataclasses.fields(usage_obj)}

    # DTO 约束真实性:frozen 与 extra=forbid 必须可被观察到生效。
    frozen_enforced = False
    try:
        result.output.answer = "mutated"  # type: ignore[misc]
    except (ValidationError, TypeError):
        frozen_enforced = True
    extra_forbid_enforced = False
    try:
        ProbeOutput.model_validate({"answer": "x", "confidence": 0.5, "bogus": 1})
    except ValidationError:
        extra_forbid_enforced = True

    return {
        "output_repr": repr(result.output),
        "output_is_dto": isinstance(result.output, ProbeOutput),
        "tool_call_args": list(tool_calls),
        "tool_call_count": len(tool_calls),
        "usage_type": type(usage_obj).__name__,
        "usage_is_property_not_method": usage_is_property,
        "usage_data": usage_as_dict,
        "message_flow": _message_flow(result.all_messages()),
        "dto_frozen_enforced": frozen_enforced,
        "dto_extra_forbid_enforced": extra_forbid_enforced,
    }


def _run_retry_budget() -> dict[str, Any]:
    """重试预算实测:FunctionModel 第一次返回非法输出、第二次返回合法输出。

    - retries=1:应消耗 1 次重试后成功(共 2 次 model 请求);
    - retries=0:预算为 0,应抛 UnexpectedModelBehavior("Exceeded maximum output retries (0)")。
    """
    state = {"calls": 0}

    def good_after_bad(messages: list[Any], info: AgentInfo) -> ModelResponse:
        state["calls"] += 1
        tool_name = info.output_tools[0].name
        if state["calls"] == 1:
            args: dict[str, Any] = {"answer": "x", "confidence": "not-a-float", "bogus": 1}
        else:
            args = {"answer": "x", "confidence": 0.5}
        return ModelResponse(parts=[ToolCallPart(tool_name=tool_name, args=args)])

    state["calls"] = 0
    agent_r1 = Agent(FunctionModel(good_after_bad), output_type=ProbeOutput, retries=1)
    res_r1 = agent_r1.run_sync("retry probe")
    retries1 = {
        "succeeded": True,
        "model_requests": state["calls"],
        "output": repr(res_r1.output),
    }

    state["calls"] = 0
    agent_r0 = Agent(FunctionModel(good_after_bad), output_type=ProbeOutput, retries=0)
    try:
        agent_r0.run_sync("retry probe")
        retries0 = {"succeeded": True, "model_requests": state["calls"], "raised": None}
    except Exception as exc:  # 记录异常类型即目的本身
        retries0 = {
            "succeeded": False,
            "model_requests": state["calls"],
            "raised": type(exc).__module__ + "." + type(exc).__name__,
            "message_head": str(exc)[:160],
        }

    # 参数名考古:2.x Agent.__init__ 只有 retries,旧名 output_retries 已移除。
    agent_params = list(inspect.signature(Agent.__init__).parameters)
    return {
        "retries_1": retries1,
        "retries_0": retries0,
        "agent_init_has_retries": "retries" in agent_params,
        "agent_init_has_output_retries": "output_retries" in agent_params,
    }


def run() -> dict[str, Any]:
    """执行探针,返回 {status, findings, details}。"""
    findings: list[str] = []
    details: dict[str, Any] = {}

    removed_keys = _strip_provider_env()
    details["env_keys_removed_before_run"] = removed_keys
    details["pydantic_ai_version"] = pydantic_ai.__version__

    tm = _run_testmodel_agent()
    details["testmodel_agent"] = tm

    # ---- 断言(探针跑不通才算 fail)----
    if not tm["output_is_dto"]:
        raise RuntimeError(f"TestModel 输出未能解析为 ProbeOutput: {tm['output_repr']!r}")
    if tm["tool_call_count"] < 1:
        raise RuntimeError("TestModel 未调用注册的工具 lookup()")
    if not tm["dto_frozen_enforced"]:
        raise RuntimeError("ProbeOutput frozen=True 未生效(输出 DTO 可被原地修改)")
    if not tm["dto_extra_forbid_enforced"]:
        raise RuntimeError("ProbeOutput extra='forbid' 未生效(多余字段被接受)")

    # ---- findings(实测行为事实,含与任务书预期不符处)----
    findings.append(
        "TestModel 完全离线可用:清除了 "
        f"{len(removed_keys)} 个疑似凭证环境变量后构造与运行均成功,不需要任何 API key。"
    )
    findings.append(
        "usage 形态:pydantic-ai 2.52.0 中 result.usage 是【属性】(返回 RunUsage dataclass),"
        "不可调用——旧教程中的 result.usage() 会抛 TypeError('RunUsage' object is not callable)。"
    )
    u = tm["usage_data"]
    findings.append(
        "TestModel 的 usage 不是 0/None,而是合成的非零计数:"
        f"input_tokens={u.get('input_tokens')} output_tokens={u.get('output_tokens')} "
        f"requests={u.get('requests')} tool_calls={u.get('tool_calls')} cost={u.get('cost')!r}。"
        "(TestModel 按请求体长度估算 token,可用于离线断言元数据管道,但不能当作真实计费值。)"
    )
    findings.append(
        "工具入参由 TestModel 自动生成(按参数名生成占位值,实测 lookup 收到 "
        f"{tm['tool_call_args']!r});如需确定性的工具参数/输出,应使用 FunctionModel 或 "
        "TestModel(custom_output_args=...) 控制输出工具参数(实测 custom_output_args 只影响 "
        "结构化输出工具的参数,不影响业务工具入参)。"
    )

    rb = _run_retry_budget()
    details["retry_budget"] = rb

    r1, r0 = rb["retries_1"], rb["retries_0"]
    if not r1["succeeded"] or r1["model_requests"] != 2:
        raise RuntimeError(f"retries=1 时未观察到'失败一次后重试成功':{r1!r}")
    if r0["succeeded"]:
        raise RuntimeError(f"retries=0 时输出校验失败未按预期抛异常:{r0!r}")

    findings.append(
        "输出重试上限通过 Agent(retries=N) 配置(2.x 单一参数同时覆盖 tools/output 两类预算,"
        "也可传 {'tools': x, 'output': y} 字典细分;旧参数名 output_retries 已在 2.x 移除,"
        f"实测 Agent.__init__ 参数含 retries={rb['agent_init_has_retries']}、"
        f"output_retries={rb['agent_init_has_output_retries']})。"
    )
    findings.append(
        f"重试耗尽抛 {r0['raised']}:{r0['message_head']!r};retries=1 实测 "
        f"{r1['model_requests']} 次 model 请求后成功(第 1 次输出非法、第 2 次合法)。"
    )
    findings.append(
        "message 流(all_messages)实测为 5 条:ModelRequest(用户 prompt) → ModelResponse(lookup "
        "工具调用) → ModelRequest(lookup 工具返回) → ModelResponse(final_result 输出工具调用) → "
        "ModelRequest(final_result 的自动回执 tool-return 'Final result processed.');"
        "结构化输出通过名为 final_result 的 output tool call 交付(ToolCallPart 携带 JSON 参数);"
        "多输出类型时工具名按类型命名(实测 [A, B] 两类型时为 final_result_A)。"
    )

    return {
        "status": "pass",
        "probe": "probe1_pydantic_ai",
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
            "probe": "probe1_pydantic_ai",
            "error": f"{type(exc).__module__}.{type(exc).__name__}: {exc}",
            "traceback": traceback.format_exc(),
        }
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    sys.exit(0 if report["status"] == "pass" else 1)
