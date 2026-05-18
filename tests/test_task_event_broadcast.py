"""P5.5 任务事件以"合成用户消息"全房广播回归（docs/P5.5-任务事件全房广播.md §8）。

四个 task inbound（open / close / add_decision / attach_artifact）末尾应：
1. 往 transcript append 一条 ``speaker_id="user"`` 的合成消息
2. 创建新 ``_inflight_turn_task``（走 ``_kick_synthesized_user_message``）

set_active_task / update_task 反面不变量见 ``test_set_active_no_broadcast.py``。
夹具与 ``_drain_inflight`` 在 ``conftest.py``（与 ``test_set_active_no_broadcast`` 共用）。
"""

from __future__ import annotations

from chahua.server import (
    INBOUND_ADD_DECISION,
    INBOUND_ATTACH_ARTIFACT,
    INBOUND_CLOSE_TASK,
    INBOUND_OPEN_TASK,
)
from chahua.user_md import USER_SPEAKER_ID

from conftest import drain_inflight


def _last_user_message_text(session) -> str | None:
    for m in reversed(session.room.messages_since(0)):
        if m.speaker_id == USER_SPEAKER_ID:
            return m.text
    return None


async def test_open_task_appends_synthesized_message(task_inbound_srv):
    session, srv = task_inbound_srv
    await srv._handle_inbound(
        {"type": INBOUND_OPEN_TASK, "title": "写 README", "goal": "MVP 上线前出"},
        lambda env: None,
    )
    # _run_turn finally 把 slot 清回 None，所以 drain 之前先抓引用。
    assert srv._inflight_turn_task is not None
    await drain_inflight(srv)
    text = _last_user_message_text(session)
    assert text == "📋 用户开启任务【写 README】 · 目标：MVP 上线前出"


async def test_close_task_appends_synthesized_message(task_inbound_srv):
    session, srv = task_inbound_srv
    task = session.tasks_store.open_task(title="审稿", goal="g")
    await srv._handle_inbound(
        {"type": INBOUND_CLOSE_TASK, "task_id": task.id, "status": "done"},
        lambda env: None,
    )
    assert srv._inflight_turn_task is not None
    await drain_inflight(srv)
    assert _last_user_message_text(session) == "✅ 任务【审稿】已完成"


async def test_add_decision_appends_synthesized_message(task_inbound_srv):
    session, srv = task_inbound_srv
    task = session.tasks_store.open_task(title="t", goal="g")
    await srv._handle_inbound(
        {
            "type": INBOUND_ADD_DECISION,
            "task_id": task.id,
            "summary": "走方案 B",
        },
        lambda env: None,
    )
    assert srv._inflight_turn_task is not None
    await drain_inflight(srv)
    assert _last_user_message_text(session) == "📌 决策：走方案 B"


async def test_attach_artifact_appends_synthesized_message(task_inbound_srv):
    session, srv = task_inbound_srv
    task = session.tasks_store.open_task(title="t", goal="g")
    share_root = session.room_config.room_dir / "share"
    share_root.mkdir(exist_ok=True)
    (share_root / "草稿.md").write_text("hi", encoding="utf-8")
    await srv._handle_inbound(
        {
            "type": INBOUND_ATTACH_ARTIFACT,
            "task_id": task.id,
            "share_rel": "草稿.md",
        },
        lambda env: None,
    )
    assert srv._inflight_turn_task is not None
    await drain_inflight(srv)
    assert _last_user_message_text(session) == "📎 用户挂载产物：草稿.md"
