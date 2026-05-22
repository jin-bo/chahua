"""P9：``ChahuaTransport.inflight_snapshot`` —— 切回房间补发进行中消息用。

切回一个 turn 在后台续跑的房间时，``emit_room_snapshot`` 据 in-flight 快照补发
``message_start`` + ``message_delta(partial_text)``，让切回前已流出的内容立即在
聊天区 / 调试面板成形。覆盖：

- 未 ``bind``（无活动 ``speak()``）→ ``None``。
- ``bind`` 中 → 返 turn_id / message_id / guest_name / task_id / partial_text。
- ``LLM_TEXT`` chunk 进 ``_handle`` → ``partial_text`` 累积。
- ``bind`` 退出后 → 回 ``None``（``_message_id`` 复位）。
"""

from __future__ import annotations

from agentao.transport import AgentEvent, EventType

from chahua.transport_bridge import ChahuaTransport


def _transport() -> ChahuaTransport:
    return ChahuaTransport(room_id="r1", guest_name="宝总")


def test_inflight_snapshot_none_when_unbound() -> None:
    assert _transport().inflight_snapshot() is None


def test_inflight_snapshot_while_bound() -> None:
    t = _transport()
    with t.bind(
        sink=lambda _e: None,
        turn_id="turn_a",
        message_id="msg_a",
        task_id="task_a",
    ):
        snap = t.inflight_snapshot()
    assert snap == {
        "turn_id": "turn_a",
        "message_id": "msg_a",
        "guest_name": "宝总",
        "task_id": "task_a",
        "partial_text": "",
    }


def test_inflight_snapshot_accumulates_partial_text() -> None:
    t = _transport()
    with t.bind(
        sink=lambda _e: None, turn_id="turn_a", message_id="msg_a", task_id=None,
    ):
        for chunk in ("你好", "，", "世界"):
            t._handle(AgentEvent(type=EventType.LLM_TEXT, data={"chunk": chunk}))
        snap = t.inflight_snapshot()
    assert snap is not None
    assert snap["partial_text"] == "你好，世界"
    assert snap["task_id"] is None


def test_inflight_snapshot_none_after_bind_exit() -> None:
    """bind 退出后 _message_id 复位 —— 不再被误判成 in-flight。"""
    t = _transport()
    with t.bind(
        sink=lambda _e: None, turn_id="turn_a", message_id="msg_a", task_id=None,
    ):
        t._handle(AgentEvent(type=EventType.LLM_TEXT, data={"chunk": "x"}))
    assert t.inflight_snapshot() is None
