"""不变量：``set_active_task`` 与 ``update_task`` 不进 transcript / 不显系统气泡。

set_active 是纯 UI 焦点切换，transcript 不该多一行、不发任何 hint；茶客对 active 切换
的感知靠下一次 prompt 注入的 ``<current_task>`` XML 块（onboarding / incremental 两路径
都覆盖）天然完成。``update_task`` 仍不发系统气泡 —— 要 diff old/new 才能挑出实质变更
（docs/P5.8-任务事件系统气泡.md §3）。

P5.8 起其它五个 task inbound 也不再合成 user 消息，反面回归见
``test_task_event_no_broadcast.py``。夹具与 ``drain_inflight`` 在 ``conftest.py``。
"""

from __future__ import annotations

from chahua.server import INBOUND_SET_ACTIVE_TASK, INBOUND_UPDATE_TASK
from chahua.user_md import USER_SPEAKER_ID

from conftest import drain_inflight


def _user_message_count(session) -> int:
    return sum(
        1 for m in session.room.messages_since(0) if m.speaker_id == USER_SPEAKER_ID
    )


async def test_set_active_task_does_not_append(task_inbound_srv):
    """开两个任务后切回第一个任务的 active，transcript 用户消息数不该 +1。"""
    session, srv = task_inbound_srv
    t1 = session.tasks_store.open_task(title="t1", goal="g1")
    session.tasks_store.open_task(title="t2", goal="g2")  # 自动 set_active 到 t2
    base = _user_message_count(session)
    await srv._handle_inbound(
        {"type": INBOUND_SET_ACTIVE_TASK, "task_id": t1.id},
        lambda env: None,
    )
    await drain_inflight(srv)
    assert _user_message_count(session) == base
    # 切到 null（清回房间级）也不该追加。
    await srv._handle_inbound(
        {"type": INBOUND_SET_ACTIVE_TASK, "task_id": None},
        lambda env: None,
    )
    await drain_inflight(srv)
    assert _user_message_count(session) == base


async def test_update_task_does_not_append(task_inbound_srv):
    """update_task 不进 transcript —— 改 owner / goal / title 不该往 transcript 追加。"""
    session, srv = task_inbound_srv
    t = session.tasks_store.open_task(title="t", goal="g")
    base = _user_message_count(session)
    await srv._handle_inbound(
        {
            "type": INBOUND_UPDATE_TASK,
            "task_id": t.id,
            "patch": {"goal": "new goal"},
        },
        lambda env: None,
    )
    await drain_inflight(srv)
    assert _user_message_count(session) == base
