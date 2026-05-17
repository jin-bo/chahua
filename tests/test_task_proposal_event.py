"""P5.3.3：TASK_PROPOSAL 事件类型 round-trip + schema_version 不动。"""

from __future__ import annotations

from chahua.events import (
    SCHEMA_VERSION,
    ChahuaEnvelope,
    ChahuaEventType,
)


def test_task_proposal_event_type_value():
    assert ChahuaEventType.TASK_PROPOSAL.value == "task_proposal"
    # 从字符串重建 —— wire 上回来时按 type 字符串解析
    assert ChahuaEventType("task_proposal") is ChahuaEventType.TASK_PROPOSAL


def test_task_proposal_envelope_serialization_round_trip():
    env = ChahuaEnvelope(
        room_id="t",
        turn_id="turn_x",
        guest_name="A",
        message_id="msg_x",
        type=ChahuaEventType.TASK_PROPOSAL,
        data={
            "task_id": "task_abc",
            "proposer": "A",
            "kind": "decision",
            "payload": {"summary": "用 Electron 做", "supporting_message_ids": ["msg_1"]},
        },
    )
    wire = env.to_dict()
    assert wire["type"] == "task_proposal"
    assert wire["data"]["proposer"] == "A"
    assert wire["data"]["kind"] == "decision"
    assert wire["data"]["payload"]["summary"] == "用 Electron 做"


def test_schema_version_unchanged():
    """P5.3.3 不 bump schema_version —— 老前端按"未知 type WARN 跳过"兜底。"""
    assert SCHEMA_VERSION == 1


def test_task_proposal_open_kind_payload_shape():
    """kind=open 的 payload 是 {title, goal}（与 decision 形状不同，由前端按 kind 分发）。"""
    env = ChahuaEnvelope(
        room_id="t",
        turn_id="turn_x",
        guest_name="A",
        message_id="msg_x",
        type=ChahuaEventType.TASK_PROPOSAL,
        data={
            "task_id": None,
            "proposer": "A",
            "kind": "open",
            "payload": {"title": "新任务", "goal": "做点什么"},
        },
    )
    wire = env.to_dict()
    assert wire["data"]["kind"] == "open"
    assert wire["data"]["payload"]["title"] == "新任务"
    assert wire["data"]["payload"]["goal"] == "做点什么"
