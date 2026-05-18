"""P5.5 任务事件合成消息文案 happy path（docs/P5.5-任务事件全房广播.md §4.4）。

每函数 1 个 happy path；``close_task_text`` 覆盖 ``done`` / ``abandoned`` 两分支。
不做 emoji 参数化矩阵 / 标点细节验证 —— 这层是单点维护，加测试反让"调 emoji" 变贵。
"""

from __future__ import annotations

from chahua.task import Decision, Task
from chahua.task_event_text import (
    add_decision_text,
    attach_artifact_text,
    close_task_text,
    detect_artifacts_text,
    open_task_text,
)


def _make_task(title: str = "报告", goal: str = "周五前出初稿") -> Task:
    return Task.new(title=title, goal=goal)


def test_open_task_text_with_goal():
    text = open_task_text(_make_task())
    assert text == "📋 用户开启任务【报告】 · 目标：周五前出初稿"


def test_open_task_text_empty_goal():
    text = open_task_text(_make_task(goal=""))
    assert text == "📋 用户开启任务【报告】"


def test_close_task_text_done():
    text = close_task_text(_make_task(title="审稿"), status="done")
    assert text == "✅ 任务【审稿】已完成"


def test_close_task_text_abandoned():
    text = close_task_text(_make_task(title="审稿"), status="abandoned")
    assert text == "✗ 任务【审稿】已放弃"


def test_add_decision_text():
    decision = Decision.new(
        task_id="task_x",
        supporting_message_ids=[],
        summary="走方案 B",
    )
    assert add_decision_text(decision) == "📌 决策：走方案 B"


def test_attach_artifact_text():
    assert attach_artifact_text("草稿.md") == "📎 用户挂载产物：草稿.md"


def test_detect_artifacts_text_multiple():
    text = detect_artifacts_text(["a.md", "b.png"])
    assert text == "📎 茶客产出：a.md, b.png"
