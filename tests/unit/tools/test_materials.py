"""MaterialToolkit: task scope, budgets, hash authority and audit trail.

Everything runs against a real Ledger + ArtifactStore in tmp_path (the
``env`` fixture from tests/unit/conftest.py). Chinese materials are core
business data here — offsets are asserted in Unicode code points, not bytes.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from omas.artifacts.writers import InboxWriter
from omas.core.canonical import sha256_text
from omas.domain.artifact import Artifact, ArtifactKind
from omas.domain.errors import (
    ArtifactHashMismatchError,
    ArtifactNotFoundError,
    BudgetExceededError,
    OmasError,
    SpanEmptyError,
    SpanOutOfRangeError,
)
from omas.domain.ids import (
    ArtifactId,
    SpanHandle,
    TaskId,
    is_valid_id,
    new_artifact_id,
    new_node_run_id,
    new_task_id,
)
from omas.domain.task import DataPolicy, Task
from omas.tools.materials import (
    Budget,
    MaterialToolkit,
    ReadResult,
    RunContext,
)

SALES = (
    "销售周报\n"
    "本周销售额为一百二十三万元，同比增长百分之八。\n"
    "华东地区销售额贡献最大。\n"
    "下周计划：跟进重点客户。"
)
MINUTES = "会议纪要\n今日会议讨论了采购预算。\n华东地区物流成本上升。"
EMOJI = "收入💰统计：金额为一百二十三。\n👨‍👩‍👧‍👦 家庭消费上升。"
OTHER_TASK_MATERIAL = "另一个任务的机密材料：华东大区专用，禁止跨任务读取。"


@dataclass
class Setup:
    env: object
    task1: Task
    task2: Task
    sales: Artifact
    sales_text: str
    minutes: Artifact
    minutes_text: str
    other: Artifact


def _make_task(env: object, request_id: str) -> Task:
    now = datetime.now(UTC)
    task = Task(
        task_id=new_task_id(),
        request_id=request_id,
        data_policy=DataPolicy.LOCAL_ONLY,
        created_at=now,
        updated_at=now,
    )
    return env.ledger.tasks.create(task, "a" * 64)


def _register(env: object, artifact: Artifact) -> Artifact:
    return env.ledger.artifacts.register(artifact)


def _ingest_canonical(env: object, task_id: str, text: str, name: str) -> tuple[Artifact, str]:
    _raw, cfile, canonical = InboxWriter(env.store).ingest_text(
        task_id, name, text.encode("utf-8")
    )
    artifact = Artifact(
        artifact_id=ArtifactId(cfile.name.removesuffix(".txt")),
        task_id=TaskId(task_id),
        kind=ArtifactKind.CANONICAL_TEXT,
        relative_path=cfile.relative_path,
        sha256=canonical.sha256,
        size=canonical.size_bytes,
        source_refs=(),
        created_at=datetime.now(UTC),
    )
    return _register(env, artifact), canonical.text


def _ingest_raw(env: object, task_id: str, text: str, name: str) -> Artifact:
    raw_file, _cfile, _canonical = InboxWriter(env.store).ingest_text(
        task_id, name, text.encode("utf-8")
    )
    return _register(
        env,
        Artifact(
            artifact_id=new_artifact_id(),
            task_id=TaskId(task_id),
            kind=ArtifactKind.RAW_TEXT,
            relative_path=raw_file.relative_path,
            sha256=raw_file.sha256,
            size=raw_file.size,
            source_refs=(),
            created_at=datetime.now(UTC),
        ),
    )


def _toolkit(
    env: object, task_id: str, budget: Budget | None = None, run_id: str | None = None
) -> MaterialToolkit:
    return MaterialToolkit(
        env.ledger, env.store, RunContext(TaskId(task_id), run_id=run_id), budget
    )


@pytest.fixture()
def setup(env: object) -> Setup:
    task1 = _make_task(env, "req-1")
    sales, sales_text = _ingest_canonical(env, task1.task_id, SALES, "sales.md")
    minutes, minutes_text = _ingest_canonical(env, task1.task_id, MINUTES, "minutes.md")
    task2 = _make_task(env, "req-2")
    other, _other_text = _ingest_canonical(
        env, task2.task_id, OTHER_TASK_MATERIAL, "other.md"
    )
    return Setup(
        env=env,
        task1=task1,
        task2=task2,
        sales=sales,
        sales_text=sales_text,
        minutes=minutes,
        minutes_text=minutes_text,
        other=other,
    )


# ------------------------------------------------------------------- list


def test_list_returns_metadata_without_body(setup: Setup) -> None:
    tk = _toolkit(setup.env, setup.task1.task_id)
    result = tk.list_materials()
    assert result.total_matched == 2
    assert not result.truncated
    by_id = {item.artifact_id: item for item in result.items}
    assert set(by_id) == {setup.sales.artifact_id, setup.minutes.artifact_id}
    item = by_id[setup.sales.artifact_id]
    assert item.display_name == f"{setup.sales.artifact_id}.txt"
    assert item.kind == "canonical_text"
    assert item.size_bytes == setup.sales.size
    assert item.line_count == 4
    assert item.paragraph_count == 4
    # metadata only — no body text anywhere in the DTO
    assert "销售额" not in result.model_dump_json()


def test_list_kind_filter(setup: Setup) -> None:
    raw = _ingest_raw(setup.env, setup.task1.task_id, SALES, "sales-raw.md")
    tk = _toolkit(setup.env, setup.task1.task_id)
    default = tk.list_materials()  # canonical_text only by default
    assert raw.artifact_id not in {item.artifact_id for item in default.items}
    assert default.total_matched == 2
    raws = tk.list_materials(kind_filter="raw_text")
    assert [item.artifact_id for item in raws.items] == [raw.artifact_id]
    assert raws.items[0].kind == "raw_text"
    with pytest.raises(OmasError):
        tk.list_materials(kind_filter="not_a_kind")


def test_list_truncates_to_budget(setup: Setup) -> None:
    tk = _toolkit(setup.env, setup.task1.task_id, Budget(max_list_items=1))
    result = tk.list_materials()
    assert len(result.items) == 1
    assert result.total_matched == 2
    assert result.truncated


# ----------------------------------------------------------------- search


def test_search_hits_scoring_and_order(setup: Setup) -> None:
    tk = _toolkit(setup.env, setup.task1.task_id)
    single = tk.search_materials("销售")
    assert single.scanned_artifacts == 2  # task scope only, task2 excluded
    assert len(single.items) == 1
    hit = single.items[0]
    assert hit.artifact_id == setup.sales.artifact_id
    assert hit.match_count == SALES.count("销售")
    assert hit.score == hit.match_count + 1  # occurrences + distinct-term bonus
    # multi-term: sales hits both terms, minutes only 华东
    multi = tk.search_materials("华东 销售")
    assert multi.items[0].artifact_id == setup.sales.artifact_id
    assert multi.items[0].score == 3 + 1 + 2  # 销售 x3 + 华东 x1 + 2 distinct
    assert multi.items[1].artifact_id == setup.minutes.artifact_id
    assert multi.items[1].score == 1 + 1


def test_search_snippet_truncation(setup: Setup) -> None:
    long_text = "前置填充内容。" * 30 + "目标关键词在这里。" + "后置填充内容。" * 30
    artifact, _text = _ingest_canonical(setup.env, setup.task1.task_id, long_text, "long.md")
    tk = _toolkit(setup.env, setup.task1.task_id, Budget(snippet_chars=16))
    result = tk.search_materials("目标关键词")
    snippet = result.items[0].snippet
    assert result.items[0].artifact_id == artifact.artifact_id
    assert len(snippet) <= 16
    assert snippet.startswith("…") and snippet.endswith("…")
    assert "目标" in snippet
    # short material: whole text as snippet, no ellipsis
    tk_default = _toolkit(setup.env, setup.task1.task_id)
    short = tk_default.search_materials("销售")
    assert short.items[0].snippet == setup.sales_text


def test_search_top_k_capped_by_budget(setup: Setup) -> None:
    tk = _toolkit(setup.env, setup.task1.task_id, Budget(search_top_k=1))
    result = tk.search_materials("华东", top_k=5)  # both materials contain 华东
    assert len(result.items) == 1
    tk_default = _toolkit(setup.env, setup.task1.task_id)
    assert len(tk_default.search_materials("华东", top_k=1).items) == 1
    assert len(tk_default.search_materials("华东", top_k=99).items) == 2
    with pytest.raises(OmasError):
        tk_default.search_materials("销售", top_k=0)


def test_search_query_never_enters_sqlite(setup: Setup) -> None:
    env = setup.env
    canary = "zqcanary7f"
    _ingest_canonical(
        env, setup.task1.task_id, f"内部标记 {canary} 请勿外发。", "canary.md"
    )
    tk = _toolkit(env, setup.task1.task_id)
    result = tk.search_materials(canary)
    assert len(result.items) == 1
    assert canary in result.items[0].snippet  # the hit is real …
    env.ledger.checkpoint()  # flush WAL so the byte scan sees everything
    db_files = [env.home / "ledger.sqlite3", *env.home.glob("ledger.sqlite3-*")]
    blob = b"".join(path.read_bytes() for path in db_files)
    assert canary.encode("utf-8") not in blob  # … but never persisted


# ------------------------------------------------------------------- read


def test_read_exact_code_point_slice(setup: Setup) -> None:
    tk = _toolkit(setup.env, setup.task1.task_id)
    text = setup.sales_text
    start = text.index("本周")
    result = tk.read_material(setup.sales.artifact_id, start=start, length=5)
    assert result.text == text[start : start + 5]
    assert (result.start, result.end) == (start, start + 5)
    assert result.total_code_points == len(text)
    assert not result.truncated
    # emoji: code points, not UTF-16 units or bytes
    emoji_art, emoji_text = _ingest_canonical(setup.env, setup.task1.task_id, EMOJI, "e.md")
    whole = tk.read_material(emoji_art.artifact_id)
    assert whole.text == emoji_text
    assert whole.total_code_points == len(EMOJI)  # 💰 = 1 cp, ZWJ family = 7 cp


def test_read_length_caps(setup: Setup) -> None:
    tk = _toolkit(setup.env, setup.task1.task_id, Budget(read_chars=10))
    capped = tk.read_material(setup.sales.artifact_id, start=0, length=100)
    assert len(capped.text) == 10
    assert capped.end == 10
    assert capped.truncated
    # default cap 8000: requesting more than exists is not "truncated"
    tk_default = _toolkit(setup.env, setup.task1.task_id)
    whole = tk_default.read_material(setup.sales.artifact_id, start=0, length=10_000)
    assert whole.text == setup.sales_text
    assert not whole.truncated
    with pytest.raises(OmasError):
        tk_default.read_material(setup.sales.artifact_id, start=10_000)
    with pytest.raises(OmasError):
        tk_default.read_material(setup.sales.artifact_id, start=-1)


def test_read_rejects_cross_task_artifact(setup: Setup) -> None:
    tk = _toolkit(setup.env, setup.task1.task_id)
    with pytest.raises(ArtifactNotFoundError):
        tk.read_material(setup.other.artifact_id, start=0, length=5)
    # and the reverse scope: task2's toolkit sees only its own material
    tk2 = _toolkit(setup.env, setup.task2.task_id)
    only = tk2.list_materials()
    assert {item.artifact_id for item in only.items} == {setup.other.artifact_id}


def test_read_detects_tampered_file(setup: Setup) -> None:
    env = setup.env
    tk = _toolkit(env, setup.task1.task_id)
    path = env.store.resolve_path(setup.sales.relative_path)
    path.write_bytes("篡改后的正文".encode("utf-8"))
    with pytest.raises(ArtifactHashMismatchError):
        tk.read_material(setup.sales.artifact_id, start=0, length=5)


# ---------------------------------------------------------------- resolve


def test_resolve_span_correctness(setup: Setup) -> None:
    env = setup.env
    run_id = new_node_run_id()
    tk = _toolkit(env, setup.task1.task_id, run_id=run_id)
    text = setup.sales_text
    start = text.index("本周")
    end = start + 12
    result = tk.resolve_span(setup.sales.artifact_id, start, end)
    assert is_valid_id(result.span_handle)
    assert result.exact_text == text[start:end]
    ref = result.source_span_ref
    assert ref.artifact_id == setup.sales.artifact_id
    assert ref.canonical_sha256 == setup.sales.sha256
    assert ref.span_sha256 == sha256_text(text[start:end])  # program-computed
    assert (ref.start, ref.end) == (start, end)
    assert result.illegal_positions == ()
    row = env.ledger.resolved_spans.get(SpanHandle(result.span_handle))
    assert row is not None
    assert row["task_id"] == setup.task1.task_id
    assert row["run_id"] == run_id
    assert row["artifact_id"] == setup.sales.artifact_id
    assert row["span_sha256"] == ref.span_sha256
    assert row["start"] == start and row["end"] == end


def test_resolve_span_reuses_handle_for_same_span(setup: Setup) -> None:
    env = setup.env
    tk = _toolkit(env, setup.task1.task_id)
    start = setup.sales_text.index("华东")
    first = tk.resolve_span(setup.sales.artifact_id, start, start + 4)
    second = tk.resolve_span(setup.sales.artifact_id, start, start + 4)
    assert second.span_handle == first.span_handle
    count_row = env.ledger.connection.execute(
        "SELECT COUNT(*) AS n FROM resolved_spans WHERE task_id = ?",
        (setup.task1.task_id,),
    ).fetchone()
    assert count_row["n"] == 1


def test_resolve_span_range_errors(setup: Setup) -> None:
    tk = _toolkit(setup.env, setup.task1.task_id)
    aid = setup.sales.artifact_id
    total = len(setup.sales_text)
    with pytest.raises(SpanEmptyError) as empty:
        tk.resolve_span(aid, 5, 5)
    assert empty.value.code == "SPAN_EMPTY"
    with pytest.raises(SpanEmptyError):
        tk.resolve_span(aid, 6, 5)
    with pytest.raises(SpanOutOfRangeError) as oor:
        tk.resolve_span(aid, 0, total + 1)
    assert oor.value.code == "SPAN_OUT_OF_RANGE"
    with pytest.raises(SpanOutOfRangeError):
        tk.resolve_span(aid, -1, 5)


def test_resolve_span_has_no_hash_or_scope_parameters(setup: Setup) -> None:
    """A caller cannot supply span_sha256/task_id: the fields do not exist.

    Documented contract (v1.1 §4.2): hash and scope authority is the tool's,
    so forged values fail at the signature itself with TypeError instead of
    being silently ignored or, worse, believed.
    """
    tk = _toolkit(setup.env, setup.task1.task_id)
    with pytest.raises(TypeError):
        tk.resolve_span(setup.sales.artifact_id, 0, 5, span_sha256="f" * 64)  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        tk.resolve_span(
            setup.sales.artifact_id, 0, 5, task_id=setup.task2.task_id  # type: ignore[call-arg]
        )
    with pytest.raises(TypeError):
        tk.resolve_span(setup.sales.artifact_id, 0, 5, span_handle="span_forged")  # type: ignore[call-arg]


def test_resolve_span_rejects_cross_task(setup: Setup) -> None:
    tk = _toolkit(setup.env, setup.task1.task_id)
    with pytest.raises(ArtifactNotFoundError):
        tk.resolve_span(setup.other.artifact_id, 0, 3)


def test_resolve_span_reports_illegal_control_chars_without_filtering(setup: Setup) -> None:
    dirty = "正常文本\x0c含换页符\n第二行。"
    artifact, _text = _ingest_canonical(setup.env, setup.task1.task_id, dirty, "dirty.md")
    tk = _toolkit(setup.env, setup.task1.task_id)
    start = dirty.index("正常")
    end = dirty.index("第二行") + len("第二行")
    result = tk.resolve_span(artifact.artifact_id, start, end)
    assert "\x0c" in result.exact_text  # returned verbatim, never filtered
    assert result.illegal_positions == (result.exact_text.index("\x0c"),)


# ---------------------------------------------------------------- budgets


def test_budget_total_read_chars_accumulates(setup: Setup) -> None:
    # sales_text is 54 code points; 40 returned now, the remaining 14 would
    # push the running total past the 50-char per-turn budget
    tk = _toolkit(setup.env, setup.task1.task_id, Budget(total_read_chars=50))
    first = tk.read_material(setup.sales.artifact_id, start=0, length=40)
    assert len(first.text) == 40
    with pytest.raises(BudgetExceededError) as exc:
        tk.read_material(setup.sales.artifact_id, start=40, length=40)
    assert exc.value.code == "BUDGET_EXCEEDED"


def test_budget_tool_calls_per_turn(setup: Setup) -> None:
    tk = _toolkit(setup.env, setup.task1.task_id, Budget(max_tool_calls_per_turn=2))
    tk.list_materials()
    tk.list_materials()
    with pytest.raises(BudgetExceededError):
        tk.list_materials()


def test_budget_query_and_repair_counters() -> None:
    budget = Budget(schema_repairs=2)
    assert not budget.exceeded("schema_repairs")
    assert not budget.exceeded("tool_calls")
    budget.record_schema_repair()
    budget.record_schema_repair()
    assert budget.exceeded("schema_repairs")
    with pytest.raises(BudgetExceededError):
        budget.record_schema_repair()
    with pytest.raises(OmasError):
        budget.exceeded("not_a_dimension")
    with pytest.raises(OmasError):
        Budget(max_list_items=0)
    # budgets compare by configuration, not by live counters
    other = Budget(read_chars=10)
    other.record_read_chars(10)
    assert other == Budget(read_chars=10)
    assert other.remaining_read_chars == 39990


# ------------------------------------------------------------------- audit


def test_tool_call_events_are_metadata_only(setup: Setup) -> None:
    env = setup.env
    tk = _toolkit(env, setup.task1.task_id)
    tk.list_materials()
    tk.search_materials("销售")
    tk.read_material(setup.sales.artifact_id, start=0, length=10)
    start = setup.sales_text.index("华东")
    tk.resolve_span(setup.sales.artifact_id, start, start + 2)
    events = env.ledger.events.list_after(setup.task1.task_id, 0)
    assert len(events) == 4
    assert all(event.event_code == "tool_call" for event in events)
    tools = {event.refs.get("tool") for event in events}
    assert tools == {"list_materials", "search_materials", "read_material", "resolve_span"}
    blob = json.dumps(
        [event.refs for event in events] + [event.counts for event in events],
        ensure_ascii=False,
    )
    assert "销售" not in blob and "华东" not in blob  # no query/body in events


# -------------------------------------------------------------- DTO rigor


def test_dtos_are_frozen_and_closed() -> None:
    dto = ReadResult(
        artifact_id="art_x",
        start=0,
        end=0,
        total_code_points=0,
        text="",
        truncated=False,
    )
    with pytest.raises(ValidationError):
        dto.text = "rewritten"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        ReadResult(
            artifact_id="art_x",
            start=0,
            end=0,
            total_code_points=0,
            text="",
            truncated=False,
            summary="模型自述摘要",  # extra fields are rejected
        )


def test_run_context_defaults() -> None:
    ctx = RunContext(TaskId("task_" + "0" * 32))
    assert ctx.run_id is None
    assert ctx.epoch == 1
