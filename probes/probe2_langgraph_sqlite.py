"""Probe 2 — LangGraph 1.2.12 + SqliteSaver 跨进程 interrupt/resume(离线)。

目的:实测 LangGraph 的 human-in-the-loop 中断在【真实跨进程】场景下的持久化与重放语义,
为 P1 的断点续跑(跨 CLI 调用共享 sqlite checkpoint)确定用法:

1. 同步 ``SqliteSaver``(``langgraph.checkpoint.sqlite``)+ StateGraph 两节点小图;
2. node_a 对共享计数器 +1 并追加事件到 JSONL 事件文件,随后 ``interrupt(...)``;
3. 父进程 ``invoke`` 到 interrupt 停止后退出图运行,记录 node_a 执行次数;
4. 父进程用 ``subprocess`` 以 ``sys.executable probes/probe2_langgraph_sqlite.py
   --child <thread_id> <resume_value>`` 启动【独立子进程】完成 ``Command(resume=...)``,
   共享同一 sqlite checkpoint 文件与同一 thread_id;
5. 统计 resume 后 node_a 是否被从头重新执行(官方文档行为:interrupt 所在节点重放)。

所有事件落盘(含 pid),跨进程证据可复核。"与文档预期不符但可运行"的事实记入 findings。
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from typing import Any

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from typing_extensions import TypedDict

DIR_ENV = "OMAS_PROBE2_DIR"
SQLITE_NAME = "checkpoints.sqlite"
EVENTS_NAME = "events.jsonl"
RESULT_NAME = "result.json"
RESUME_VALUE = "RESUMED_FROM_CHILD_PROCESS"


class ProbeState(TypedDict, total=False):
    """图状态:计数器 + resume 值 + node_b 汇总结果。"""

    counter: int
    resume_value: Any
    final: dict[str, Any]


def _append_event(run_dir: str, event: str, extra: dict[str, Any] | None = None) -> None:
    """向 JSONL 事件文件追加一条带 pid 的执行记录(父子进程共用的执行证据)。"""
    record = {"event": event, "pid": os.getpid(), **(extra or {})}
    with open(os.path.join(run_dir, EVENTS_NAME), "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def _read_events(run_dir: str) -> list[dict[str, Any]]:
    path = os.path.join(run_dir, EVENTS_NAME)
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def _build_graph(saver: SqliteSaver, run_dir: str):
    """两节点图:node_a 中断等待人工输入,node_b 汇总写结果文件。"""

    def node_a(state: ProbeState) -> dict[str, Any]:
        _append_event(run_dir, "node_a_enter", {"counter_before": state.get("counter", 0)})
        value = interrupt({"question": "need input"})
        _append_event(run_dir, "node_a_after_interrupt", {"resume_value": value})
        # 若节点被整段重放,此处 +1 会执行多次;事件文件可区分 enter 与 after 的次数。
        return {"counter": state.get("counter", 0) + 1, "resume_value": value}

    def node_b(state: ProbeState) -> dict[str, Any]:
        _append_event(run_dir, "node_b_enter", {"counter": state.get("counter")})
        result = {
            "resume_value": state["resume_value"],
            "counter": state["counter"],
            "child_pid": os.getpid(),
        }
        with open(os.path.join(run_dir, RESULT_NAME), "w", encoding="utf-8") as fh:
            json.dump(result, fh, ensure_ascii=False)
        return {"final": result}

    graph = StateGraph(ProbeState)
    graph.add_node("node_a", node_a)
    graph.add_node("node_b", node_b)
    graph.add_edge(START, "node_a")
    graph.add_edge("node_a", "node_b")
    graph.add_edge("node_b", END)
    return graph.compile(checkpointer=saver)


def _open_saver(run_dir: str) -> SqliteSaver:
    conn = sqlite3.Connection(os.path.join(run_dir, SQLITE_NAME), check_same_thread=False)
    saver = SqliteSaver(conn)
    saver.setup()
    return saver


def run_child(thread_id: str, resume_value: str) -> dict[str, Any]:
    """子进程模式:重新打开同一 sqlite checkpoint,Command(resume=...) 完成图。"""
    run_dir = os.environ[DIR_ENV]
    saver = _open_saver(run_dir)
    graph = _build_graph(saver, run_dir)
    config = {"configurable": {"thread_id": thread_id}}

    final_state = graph.invoke(Command(resume=resume_value), config)

    snapshot = graph.get_state(config)
    child_report = {
        "status": "ok",
        "child_pid": os.getpid(),
        "final_state": {k: v for k, v in final_state.items() if k != "__interrupt__"},
        "snapshot_next_after_resume": list(snapshot.next),
    }
    # 子进程内自检:resume 值必须贯通到最终状态。
    if final_state.get("resume_value") != resume_value:
        raise RuntimeError(
            f"子进程 resume 后 final_state.resume_value != 传入值: "
            f"{final_state.get('resume_value')!r} != {resume_value!r}"
        )
    if not os.path.exists(os.path.join(run_dir, RESULT_NAME)):
        raise RuntimeError("子进程完成后 node_b 未写出 result.json")
    return child_report


def run_parent() -> dict[str, Any]:
    """父进程模式:跑到 interrupt,再 subprocess 拉起子进程完成 resume,最后校验。"""
    findings: list[str] = []
    run_dir = tempfile.mkdtemp(prefix="omas_probe2_")
    thread_id = "probe2-thread"

    saver = _open_saver(run_dir)
    graph = _build_graph(saver, run_dir)
    config = {"configurable": {"thread_id": thread_id}}

    # ---- 第一段:invoke 到 interrupt ----
    first = graph.invoke({"counter": 0}, config)
    interrupts = first.get("__interrupt__")
    snapshot = graph.get_state(config)
    first_events = _read_events(run_dir)
    a_enters_before_resume = sum(1 for e in first_events if e["event"] == "node_a_enter")

    if not interrupts:
        raise RuntimeError(f"第一次 invoke 未产生 __interrupt__,返回: {first!r}")
    # interrupt 值应为 node_a 里给出的 dict
    interrupt_payload = getattr(interrupts[0], "value", None)
    payload_is_question = (
        isinstance(interrupt_payload, dict) and interrupt_payload.get("question") == "need input"
    )
    if not payload_is_question:
        raise RuntimeError(f"interrupt 载荷不符: {interrupt_payload!r}")

    # ---- 第二段:真实跨进程 resume ----
    child_env = {**os.environ, DIR_ENV: run_dir}
    proc = subprocess.run(
        [sys.executable, os.path.abspath(__file__), "--child", thread_id, RESUME_VALUE],
        env=child_env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"子进程 resume 失败 rc={proc.returncode}\n"
            f"stdout={proc.stdout[-2000:]}\nstderr={proc.stderr[-2000:]}"
        )
    child_report = json.loads(proc.stdout.strip().splitlines()[-1])

    # ---- 校验 ----
    result_path = os.path.join(run_dir, RESULT_NAME)
    if not os.path.exists(result_path):
        raise RuntimeError("resume 后结果文件不存在")
    with open(result_path, encoding="utf-8") as fh:
        result = json.load(fh)

    events = _read_events(run_dir)
    a_enters_total = sum(1 for e in events if e["event"] == "node_a_enter")
    a_after_total = sum(1 for e in events if e["event"] == "node_a_after_interrupt")
    b_enters = sum(1 for e in events if e["event"] == "node_b_enter")
    parent_pid = os.getpid()

    if result.get("resume_value") != RESUME_VALUE:
        raise RuntimeError(f"结果文件 resume_value 不符: {result!r}")
    if result.get("child_pid") == parent_pid:
        raise RuntimeError("node_b 似乎在父进程执行——未发生真实跨进程")
    if child_report.get("child_pid") == parent_pid:
        raise RuntimeError("子进程报告 pid 与父进程相同——未发生真实跨进程")

    # ---- findings ----
    findings.append(
        f"跨进程 resume 成功:父进程 pid={parent_pid} 停在 interrupt 后退出,子进程 pid="
        f"{child_report['child_pid']} 用同一 sqlite 文件 + 同一 thread_id 通过 "
        "graph.invoke(Command(resume=...), config) 完成图,node_b 结果文件由子进程写出。"
    )
    findings.append(
        f"interrupt 所在节点会被【从头重新执行】:node_a_enter 事件 resume 前出现 "
        f"{a_enters_before_resume} 次、resume 后总计 {a_enters_total} 次(子进程重放了整个节点函数),"
        f"而 node_a_after_interrupt 仅 {a_after_total} 次——interrupt() 调用点之后、"
        "之前的代码都会重跑,"
        "节点内 interrupt 之前对图状态的写入不会提交(counter 只 +1)。"
        "P1 设计要点:interrupt 前的节点代码必须是幂等的(或把副作用放在 interrupt 之后)。"
    )
    findings.append(
        f"第一次 invoke 返回值含 '__interrupt__' 键(Interrupt(id=..., value={interrupt_payload!r}),"
        f"同时 graph.get_state(config).next == {list(snapshot.next)}——"
        "即中断时该节点仍是 pending 状态(因为要整节点重放),不能把 next 为空当作完成判据。"
    )
    findings.append(
        "同步 SqliteSaver 可直接构造:SqliteSaver(sqlite3.Connection(path, "
        "check_same_thread=False)) + saver.setup();跨进程顺序访问(父先子后)无锁问题,"
        "不需要 AsyncSqliteSaver/asyncio。"
    )
    if a_enters_total != 2:
        findings.append(
            f"注意:node_a_enter 总计 {a_enters_total} 次(非预期的 2 次)——重放次数与预期不符,"
            "需进一步核查 checkpoint 版本。"
        )
    if b_enters != 1:
        findings.append(
            f"注意:node_b_enter 出现 {b_enters} 次(非 1 次),interrupt 下游节点也被重放。"
        )

    return {
        "status": "pass",
        "probe": "probe2_langgraph_sqlite",
        "findings": findings,
        "details": {
            "run_dir": run_dir,
            "thread_id": thread_id,
            "parent_pid": parent_pid,
            "child_pid": child_report["child_pid"],
            "child_returncode": proc.returncode,
            "interrupt_payload": interrupt_payload,
            "snapshot_next_while_interrupted": list(snapshot.next),
            "node_a_enter_before_resume": a_enters_before_resume,
            "node_a_enter_total": a_enters_total,
            "node_a_after_interrupt_total": a_after_total,
            "node_b_enter_total": b_enters,
            "result_file": result,
            "final_state_from_child": child_report["final_state"],
            "events": events,
        },
    }


def run() -> dict[str, Any]:
    """执行探针,返回 {status, findings, details}。"""
    return run_parent()


if __name__ == "__main__":
    if len(sys.argv) >= 4 and sys.argv[1] == "--child":
        # 子进程模式:输出单行 JSON 供父进程解析,失败退出码非 0。
        try:
            report: dict[str, Any] = run_child(sys.argv[2], sys.argv[3])
        except Exception as exc:
            import traceback

            report = {
                "status": "error",
                "error": f"{type(exc).__module__}.{type(exc).__name__}: {exc}",
                "traceback": traceback.format_exc(),
            }
        print(json.dumps(report, ensure_ascii=False, default=str))
        sys.exit(0 if report.get("status") == "ok" else 1)
    else:
        try:
            report = run()
        except Exception as exc:  # 探针本身跑不通 → fail + 退出码 1
            import traceback

            report = {
                "status": "fail",
                "probe": "probe2_langgraph_sqlite",
                "error": f"{type(exc).__module__}.{type(exc).__name__}: {exc}",
                "traceback": traceback.format_exc(),
            }
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
        sys.exit(0 if report["status"] == "pass" else 1)
