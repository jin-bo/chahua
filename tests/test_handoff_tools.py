"""``chahua/handoff_tools.py`` 三个 handoff propose 工具 + register 工厂（P7.4.2）。

验收：
① 三工具 ``name`` / ``description`` / ``parameters`` 字段齐 + ``is_read_only=True``
② ``propose_delegate`` emit TASK_PROPOSAL，kind / payload 形状正确；reason 可选
③ ``propose_review`` reviewee 命中 → 解析 message_id + snippet；未命中 → 返 Error 不 emit
④ ``propose_panel`` emit，targets / summarizer 形状正确；不校验个数（留采纳兜底）
⑤ ``register_handoff_tools`` 后 ``agent.tools`` 含三个 propose_* 工具
"""

from __future__ import annotations

from typing import Any, Optional

from chahua.events import (
    TASK_PROPOSAL_KIND_HANDOFF_DELEGATE,
    TASK_PROPOSAL_KIND_HANDOFF_PANEL,
    TASK_PROPOSAL_KIND_HANDOFF_REVIEW,
    ChahuaEventType,
)
from chahua.handoff_tools import (
    ProposeDelegateTool,
    ProposePanelTool,
    ProposeReviewTool,
    register_handoff_tools,
)
from chahua.room import Room


class _FakeTransport:
    """ChahuaTransport 的最小替身 —— 只暴露 handoff_tools 用到的 guest_name + emit_chahua。"""

    def __init__(self, *, guest_name: str = "C") -> None:
        self._guest_name = guest_name
        self.emitted: list[tuple[ChahuaEventType, dict]] = []

    @property
    def guest_name(self) -> str:
        return self._guest_name

    def emit_chahua(self, type: ChahuaEventType, data: Optional[dict] = None, **_: Any) -> None:
        self.emitted.append((type, dict(data or {})))


class _FakeAgentTools:
    def __init__(self) -> None:
        self.tools: dict[str, Any] = {}

    def register(self, tool) -> None:
        self.tools[tool.name] = tool


class _FakeAgent:
    def __init__(self) -> None:
        self.tools = _FakeAgentTools()


def _room_with(*messages: tuple[str, str]) -> Room:
    """messages = (speaker_id, text) 序列。speaker_id 自动登记为参与者。"""
    room = Room(name="t")
    for sid, _ in messages:
        if sid not in room.participants:
            room.add_participant(sid)
    for sid, text in messages:
        room.append(sid, text)
    return room


# ─── ① 元数据 ───────────────────────────────────────────────────────────────


def test_three_tools_metadata_complete() -> None:
    transport = _FakeTransport()
    room = Room(name="t")
    tools = [
        ProposeDelegateTool(transport=transport),
        ProposeReviewTool(transport=transport, room=room),
        ProposePanelTool(transport=transport),
    ]
    assert {t.name for t in tools} == {
        "propose_delegate",
        "propose_review",
        "propose_panel",
    }
    for t in tools:
        assert t.description
        assert t.parameters["type"] == "object"
        assert t.is_read_only is True


# ─── ② propose_delegate ─────────────────────────────────────────────────────


def test_propose_delegate_emits_proposal() -> None:
    transport = _FakeTransport(guest_name="C")
    tool = ProposeDelegateTool(transport=transport)
    ack = tool.execute(target="范总", reason="他更熟水文")
    assert "after the user accepts" in ack
    assert len(transport.emitted) == 1
    ev_type, data = transport.emitted[0]
    assert ev_type is ChahuaEventType.TASK_PROPOSAL
    assert data["proposer"] == "C"
    assert data["kind"] == TASK_PROPOSAL_KIND_HANDOFF_DELEGATE
    assert data["payload"] == {"target": "范总", "reason": "他更熟水文"}


def test_propose_delegate_reason_optional() -> None:
    transport = _FakeTransport()
    ProposeDelegateTool(transport=transport).execute(target="范总")
    _, data = transport.emitted[0]
    assert data["payload"] == {"target": "范总"}


# ─── ③ propose_review ───────────────────────────────────────────────────────


def test_propose_review_resolves_latest_message() -> None:
    room = _room_with(("范总", "旧话"), ("user", "嗯"), ("范总", "新话——这条该被审"))
    transport = _FakeTransport(guest_name="C")
    tool = ProposeReviewTool(transport=transport, room=room)
    ack = tool.execute(reviewer="汪小姐", reviewee="范总")
    assert "after the user accepts" in ack
    _, data = transport.emitted[0]
    assert data["kind"] == TASK_PROPOSAL_KIND_HANDOFF_REVIEW
    latest = room.latest_message_by_speaker_id("范总")
    assert data["payload"]["target"] == "汪小姐"
    assert data["payload"]["message_id"] == latest.message_id
    assert data["payload"]["reviewee"] == "范总"
    assert data["payload"]["snippet"] == "新话——这条该被审"


def test_propose_review_reviewee_never_spoke_returns_error() -> None:
    room = _room_with(("user", "你好"))
    transport = _FakeTransport()
    tool = ProposeReviewTool(transport=transport, room=room)
    ack = tool.execute(reviewer="汪小姐", reviewee="范总")
    assert ack.startswith("Error:")
    assert transport.emitted == []


def test_propose_review_snippet_truncated() -> None:
    long = "字" * 200
    room = _room_with(("范总", long))
    transport = _FakeTransport()
    ProposeReviewTool(transport=transport, room=room).execute(
        reviewer="汪小姐", reviewee="范总"
    )
    _, data = transport.emitted[0]
    assert len(data["payload"]["snippet"]) == 80


# ─── ④ propose_panel ────────────────────────────────────────────────────────


def test_propose_panel_emits_proposal() -> None:
    transport = _FakeTransport(guest_name="C")
    tool = ProposePanelTool(transport=transport)
    tool.execute(targets=["范总", "汪小姐"], summarizer="爷叔")
    _, data = transport.emitted[0]
    assert data["kind"] == TASK_PROPOSAL_KIND_HANDOFF_PANEL
    assert data["payload"] == {"targets": ["范总", "汪小姐"], "summarizer": "爷叔"}


def test_propose_panel_summarizer_optional_and_no_count_check() -> None:
    transport = _FakeTransport()
    # 单人 targets —— 工具不拦，校验留采纳时 handoff_panel inbound 兜底（docs §5.4）。
    ProposePanelTool(transport=transport).execute(targets=["范总"])
    _, data = transport.emitted[0]
    assert data["payload"] == {"targets": ["范总"]}


# ─── ⑤ register 工厂 ────────────────────────────────────────────────────────


def test_register_handoff_tools() -> None:
    agent = _FakeAgent()
    register_handoff_tools(agent, transport=_FakeTransport(), room=Room(name="t"))
    assert set(agent.tools.tools) == {
        "propose_delegate",
        "propose_review",
        "propose_panel",
    }
