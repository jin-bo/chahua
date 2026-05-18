"""P5.6：``Orchestrator._maybe_render_scoring_task_block`` helper 测试。

scoring 专用极简块（不含 speak compact 的 ``./task/`` 写入指引）。helper 级覆盖：

① open task → ``(body, status_display)``，body 含 title / goal first line，
   且**不含** ``"./task/"`` 字样（与 speak compact 区分的核心回归点）
② parametrize ``[None, missing, closed]`` → ``None``

closed 路径直接构造 ``Task(status="done")`` 喂 stub store，不走 ``TasksStore.close_task``
端到端 —— 本测关心的是"helper 遇到 closed 返回 None"，不是 TasksStore 行为。
"""

from __future__ import annotations

from typing import Optional

import pytest

from chahua.cursor import GuestCursor
from chahua.orchestrator import Orchestrator, OrchestratorConfig
from chahua.room import Room
from chahua.task import Task
from chahua.user_md import USER_SPEAKER_ID, UserConfig

from conftest import NoopScorer, NoopSummarizer


class _StubStore:
    """只暴露 ``get_task`` —— helper 内部唯一会调的 store 方法。"""

    def __init__(self, *tasks: Task) -> None:
        self._tasks = {t.id: t for t in tasks}

    def get_task(self, task_id: str) -> Optional[Task]:
        return self._tasks.get(task_id)

    # Orchestrator.__init__ 会读 list_tasks/list_artifacts 初始化 _seen_artifacts，
    # 装出空列表即可。
    def list_tasks(self):
        return list(self._tasks.values())

    def list_artifacts(self, task_id: str):
        return []

    @property
    def active_task_id(self):
        return None


def _task(
    task_id: str = "t1",
    title: str = "写 README",
    goal: str = "把茶话室文档补齐",
    status: str = "open",
) -> Task:
    return Task(
        id=task_id, title=title, goal=goal, status=status, owner=None,
        created_at_ms=0, updated_at_ms=0, closed_at_ms=None,
    )


def _build_orch(*tasks: Task) -> Orchestrator:
    room = Room(name="t")
    room.add_participant(USER_SPEAKER_ID)
    return Orchestrator(
        room=room,
        user_config=UserConfig(display_name="老金", full_md=None, source=None),
        scorer=NoopScorer(),
        summarizer=NoopSummarizer(),
        cursor=GuestCursor(),
        config=OrchestratorConfig(summary_block_size=999),
        tasks_store=_StubStore(*tasks),  # type: ignore[arg-type]
    )


# ─── ① open task happy path ──────────────────────────────────────────────────


def test_open_task_returns_title_goal_status_without_task_dir_hint():
    """open task → (body, status_display)；body 含 title / goal first line，且不含
    ``./task/`` —— 这是与 speak compact (含 ``./task/ 是本任务工作目录`` 指令) 区分的
    核心回归点。"""
    orch = _build_orch(_task())
    rendered = orch._maybe_render_scoring_task_block("t1")
    assert rendered is not None
    body, status_display = rendered
    assert "标题：写 README" in body
    assert "目标：把茶话室文档补齐" in body
    assert status_display == "未开始"
    # 核心回归：scoring 极简块**不含** ./task/ 写入指引（避免发言阶段执行语义污染打分）
    assert "./task/" not in body
    assert "可读写" not in body
    assert "自动入任务" not in body


def test_open_task_compact_goal_takes_first_line_only():
    """goal 多行 → 只取首行（沿 speak compact 同口径，控制 token 预算）。"""
    orch = _build_orch(_task(goal="第一行目标\n第二行细节\n第三行更多"))
    rendered = orch._maybe_render_scoring_task_block("t1")
    assert rendered is not None
    body, _ = rendered
    assert "目标：第一行目标" in body
    assert "第二行细节" not in body


def test_open_task_empty_goal_skips_goal_line():
    orch = _build_orch(_task(goal=""))
    rendered = orch._maybe_render_scoring_task_block("t1")
    assert rendered is not None
    body, _ = rendered
    assert "标题：写 README" in body
    assert "目标：" not in body


# ─── ② parametrize [None, missing, closed] → None ────────────────────────────


@pytest.mark.parametrize(
    "scenario",
    ["none_task_id", "missing_task_id", "closed_done", "closed_abandoned"],
)
def test_returns_none_for_unavailable_or_closed_task(scenario: str):
    """task_id=None / store 无此 task / 任务终结态（done / abandoned）→ None。

    closed 路径直接构造 ``Task(status="done"|"abandoned")``，不走 TasksStore.close_task
    端到端 —— helper 关心的是 status 字段，不是关闭流程。
    """
    if scenario == "none_task_id":
        orch = _build_orch(_task())
        assert orch._maybe_render_scoring_task_block(None) is None
    elif scenario == "missing_task_id":
        orch = _build_orch(_task("t1"))
        assert orch._maybe_render_scoring_task_block("不存在") is None
    elif scenario == "closed_done":
        orch = _build_orch(_task(status="done"))
        assert orch._maybe_render_scoring_task_block("t1") is None
    elif scenario == "closed_abandoned":
        orch = _build_orch(_task(status="abandoned"))
        assert orch._maybe_render_scoring_task_block("t1") is None


def test_returns_none_when_no_store():
    """没有 tasks_store（CLI 路径）→ None。"""
    room = Room(name="t")
    room.add_participant(USER_SPEAKER_ID)
    orch = Orchestrator(
        room=room,
        user_config=UserConfig(display_name="老金", full_md=None, source=None),
        scorer=NoopScorer(),
        summarizer=NoopSummarizer(),
        cursor=GuestCursor(),
        config=OrchestratorConfig(summary_block_size=999),
        tasks_store=None,
    )
    assert orch._maybe_render_scoring_task_block("t1") is None
