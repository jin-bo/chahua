"""P5.8 任务事件不进 transcript / 不触发 AI 回归（docs/P5.8-任务事件系统气泡.md §8.1）。

P5.5 时代 open / close / add_decision / attach_artifact / clear_task_artifacts 五个
task inbound 末尾合成一条 ``speaker_id="user"`` 消息进 transcript 并起一轮 ``_run_turn``。
P5.8 把这条砍掉 —— task event 是 UI 系统气泡通知、不是对话消息，所以：

1. transcript 不多 ``USER_SPEAKER_ID`` 行；
2. 不创建 ``_inflight_turn_task``（不触发 scoring）。

外加协议侧两点：``TASK_CLOSE`` envelope 的 ``data`` 带 ``title``（前端系统气泡文案需要）；
``clear_task_artifacts`` emit ``TASK_ARTIFACTS_CLEARED`` hint。

set_active_task / update_task 的同类不变量见 ``test_set_active_no_broadcast.py``。
夹具在 ``conftest.py``。
"""

from __future__ import annotations

from chahua.events import ChahuaEventType
from chahua.server_inbound_task import (
    INBOUND_ADD_DECISION,
    INBOUND_ATTACH_ARTIFACT,
    INBOUND_CLEAR_TASK_ARTIFACTS,
    INBOUND_CLOSE_TASK,
    INBOUND_OPEN_TASK,
)
from chahua.user_md import USER_SPEAKER_ID


def _user_message_count(session) -> int:
    return sum(
        1 for m in session.room.messages_since(0) if m.speaker_id == USER_SPEAKER_ID
    )


async def test_open_task_no_transcript_no_turn(task_inbound_srv):
    session, srv = task_inbound_srv
    base = _user_message_count(session)
    await srv._handle_inbound(
        {"type": INBOUND_OPEN_TASK, "title": "写 README", "goal": "MVP 上线前出"},
        lambda env: None,
    )
    assert _user_message_count(session) == base
    assert srv._inflight_turn_task is None


async def test_close_task_no_transcript_envelope_has_title(task_inbound_srv):
    session, srv = task_inbound_srv
    task = session.tasks_store.open_task(title="审稿", goal="g")
    base = _user_message_count(session)
    captured = []
    await srv._handle_inbound(
        {"type": INBOUND_CLOSE_TASK, "task_id": task.id, "status": "done"},
        captured.append,
    )
    assert _user_message_count(session) == base
    assert srv._inflight_turn_task is None
    # TASK_CLOSE 的 data 带 title —— 前端系统气泡文案要"任务【审稿】已完成"。
    closes = [e for e in captured if e.type == ChahuaEventType.TASK_CLOSE]
    assert len(closes) == 1
    assert closes[0].data["title"] == "审稿"


async def test_add_decision_no_transcript_no_turn(task_inbound_srv):
    session, srv = task_inbound_srv
    task = session.tasks_store.open_task(title="t", goal="g")
    base = _user_message_count(session)
    await srv._handle_inbound(
        {"type": INBOUND_ADD_DECISION, "task_id": task.id, "summary": "走方案 B"},
        lambda env: None,
    )
    assert _user_message_count(session) == base
    assert srv._inflight_turn_task is None


async def test_attach_artifact_no_transcript_no_turn(task_inbound_srv):
    session, srv = task_inbound_srv
    task = session.tasks_store.open_task(title="t", goal="g")
    share_root = session.room_config.room_dir / "share"
    share_root.mkdir(exist_ok=True)
    (share_root / "草稿.md").write_text("hi", encoding="utf-8")
    base = _user_message_count(session)
    await srv._handle_inbound(
        {"type": INBOUND_ATTACH_ARTIFACT, "task_id": task.id, "share_rel": "草稿.md"},
        lambda env: None,
    )
    assert _user_message_count(session) == base
    assert srv._inflight_turn_task is None


async def test_clear_task_artifacts_no_transcript_emits_hint(task_inbound_srv):
    session, srv = task_inbound_srv
    task = session.tasks_store.open_task(title="评审 PR", goal="g")
    adir = session.tasks_store.artifacts_dir(task.id)
    adir.mkdir(parents=True, exist_ok=True)
    (adir / "v1.md").write_bytes(b"x")
    base = _user_message_count(session)
    captured = []
    await srv._handle_inbound(
        {"type": INBOUND_CLEAR_TASK_ARTIFACTS, "task_id": task.id},
        captured.append,
    )
    assert _user_message_count(session) == base
    assert srv._inflight_turn_task is None
    cleared = [e for e in captured if e.type == ChahuaEventType.TASK_ARTIFACTS_CLEARED]
    assert len(cleared) == 1
    assert cleared[0].data["title"] == "评审 PR"
    assert cleared[0].data["count"] == 1
    assert cleared[0].data["names"] == ["v1.md"]
