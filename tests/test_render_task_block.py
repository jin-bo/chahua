"""P5.3.1：``_render_task_block`` 纯字符串 renderer 测试。

不背状态分支（closed / 不存在 task 在 P5.3.2 调用方处理），所以这里不测 closed 路径
—— 那是 P5.3.2 wiring 层的责任。这里只测拼字符串：
① 全字段 task full 块齐
② full vs compact 输出格式
③ owner=None 跳"负责人"行
④ decisions / artifacts / summary tail 各自截断
⑤ goal 多行 compact 只取首行

XML 化重构后：``_render_task_block`` 返回 ``(body, status_display)`` 元组，
status 由调用方拼到 ``<current_task status="...">`` XML 属性里，body 不再含状态行。
"""

from __future__ import annotations

from chahua.orchestrator import _render_task_block
from chahua.summarizer import SummarySpan
from chahua.task import Decision, Task


def _task(title: str = "写 README", goal: str = "把 README 写完", *, owner=None, status="open"):
    return Task(
        id="task_abc",
        title=title,
        goal=goal,
        status=status,
        owner=owner,
        created_at_ms=0,
        updated_at_ms=0,
        closed_at_ms=None,
    )


def _decision(summary: str, did: str = "dec_x") -> Decision:
    return Decision(
        decision_id=did,
        task_id="task_abc",
        supporting_message_ids=(),
        summary=summary,
        marked_by="user",
        ts_ms=0,
    )


def _summary_span(text: str, start: int = 1, end: int = 20) -> SummarySpan:
    return SummarySpan(start_seq=start, end_seq=end, text=text)


def _artifact(name: str, size: int = 2300, mtime_ms: int = 1715850000000) -> dict:
    return {
        "name": name,
        "size": size,
        "mtime_ms": mtime_ms,
        "rel": f"tasks/task_abc/artifacts/{name}",
    }


# ─── ① 全字段 full 块齐 ───────────────────────────────────────────────────────


def test_full_block_all_fields_present():
    task = _task(owner="汪小姐", status="in_progress")
    body, status_display = _render_task_block(
        task,
        decisions=[_decision("用 Electron")],
        artifacts=[_artifact("README.md")],
        summary_tail=[_summary_span("- 老金说想做个茶话室")],
        compact=False,
    )
    assert "标题：写 README" in body
    assert "目标：" in body and "把 README 写完" in body
    assert status_display == "进行中"
    # status 不再出现在 body 里——由调用方拼到 <current_task status="..."> 属性
    assert "状态：" not in body
    assert "负责人：汪小姐" in body
    assert "近期决策" in body and "用 Electron" in body
    assert "当前产物" in body and "README.md" in body
    # P5.4：artifact label 强调 ./task/ 可读写 + 新产物自动入任务
    assert "./task/" in body
    assert "可读写" in body
    assert "自动入任务" in body
    assert "任务近期进展" in body and "老金说想做个茶话室" in body


# ─── ② full vs compact 输出格式 ──────────────────────────────────────────────


def test_compact_block_short_header():
    task = _task()  # 默认 goal 非空 → 完整 3 行
    body, status_display = _render_task_block(task, [], [], [], compact=True)
    lines = body.split("\n")
    assert len(lines) == 3
    assert lines[0] == "标题：写 README"
    assert lines[1] == "目标：把 README 写完"
    # P5.4：compact 文案告诉茶客 ./task/ 可读写 + 新产物自动入任务
    assert "./task/" in lines[2]
    assert "可读写" in lines[2]
    assert "自动入任务" in lines[2]
    # compact 不含 full 块的小标题
    assert "近期决策" not in body
    assert "当前产物" not in body
    assert "任务近期进展" not in body
    assert "状态：" not in body
    # status_display 仍然返回，给调用方拼 XML 属性用
    assert status_display == "未开始"


def test_full_block_no_artifacts_no_decisions_no_summary():
    # 空 decisions / artifacts / summary_tail → 对应小节整段省略，不出现空"近期决策："等
    # 但 full 模式无 artifact 时会渲一行 ./task/ 工作目录可读写 + 当前为空 + 怎么写入的提示
    task = _task(owner=None)
    body, _ = _render_task_block(task, [], [], [], compact=False)
    assert "近期决策" not in body
    assert "任务近期进展" not in body
    assert "标题：写 README" in body
    assert "目标：" in body
    # P5.4 不变量：full 模式始终告诉茶客 ./task/ 可读写 + 怎么贡献产物
    assert "./task/" in body
    assert "可读写" in body
    assert "当前为空" in body
    assert "自动入任务" in body


# ─── ③ owner=None 跳"负责人"行 ────────────────────────────────────────────────


def test_full_block_owner_none_skips_owner_line():
    task = _task(owner=None, status="open")
    body, status_display = _render_task_block(task, [], [], [], compact=False)
    assert status_display == "未开始"
    assert "状态：" not in body
    assert "负责人" not in body


def test_full_block_owner_user_renders_raw():
    # 渲染层不做 display name 映射；"user" 字面值原样喂给 LLM（wiring 层若要换 display
    # name 应在调用方传 task 前替换 owner，不在本函数）。
    task = _task(owner="user")
    body, _ = _render_task_block(task, [], [], [], compact=False)
    assert "负责人：user" in body


# ─── ④ decisions / artifacts / summary tail 各自截断 ─────────────────────────


def test_decisions_truncated_to_5_keep_last():
    decisions = [_decision(f"决策{i}", did=f"dec_{i}") for i in range(8)]
    body, _ = _render_task_block(_task(), decisions, [], [], compact=False)
    # 取末 5 条
    assert "决策3" in body  # 末 5 个：3,4,5,6,7
    assert "决策7" in body
    assert "决策2" not in body  # 第 3 条往前被砍
    assert "近期决策（最近 5 条）" in body


def test_artifacts_truncated_to_10_keep_first():
    artifacts = [_artifact(f"file{i:02d}.md") for i in range(15)]
    body, _ = _render_task_block(_task(), [], artifacts, [], compact=False)
    assert "file00.md" in body  # 前 10 个
    assert "file09.md" in body
    assert "file10.md" not in body
    assert "file14.md" not in body


def test_summary_tail_truncated_to_3_keep_last():
    spans = [_summary_span(f"- 摘要{i}", start=i, end=i + 1) for i in range(6)]
    body, _ = _render_task_block(_task(), [], [], spans, compact=False)
    assert "摘要3" in body  # 末 3 个：3,4,5
    assert "摘要5" in body
    assert "摘要2" not in body


# ─── ⑤ goal 多行 compact 只取首行 ────────────────────────────────────────────


def test_compact_goal_multiline_first_line_only():
    task = _task(goal="第一行目标\n第二行细节\n第三行更多细节")
    body, _ = _render_task_block(task, [], [], [], compact=True)
    assert "目标：第一行目标" in body
    assert "第二行细节" not in body
    assert "第三行更多细节" not in body


def test_compact_empty_goal_skips_goal_line():
    task = _task(goal="")
    body, _ = _render_task_block(task, [], [], [], compact=True)
    assert "标题：写 README" in body
    assert "目标：" not in body
    assert "./task/" in body
