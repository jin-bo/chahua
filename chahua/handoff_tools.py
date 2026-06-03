"""P7.4：茶客侧 handoff propose 工具集（docs/P7.4-茶客 propose handoff.md §2 / §3）。

注册三个 in-process Python 工具给 agentao，让茶客在发言里**提议**一次 handoff：

- ``propose_delegate(target, reason?)`` —— 提议「下一句交给 target 发」。
- ``propose_review(reviewer, reviewee)`` —— 提议「请 reviewer 审 reviewee 最近一条发言」。
- ``propose_panel(targets, summarizer?)`` —— 提议「拉一场 targets 的圆桌」。

三个工具均 ``is_read_only = True``，但**这是为权限层放行而非声明"无副作用"**——与
``task_tools.py`` 的 ``task_propose_*`` 同口径：工具不写库不落盘，但**会 emit
TASK_PROPOSAL envelope**。用户在 UI 点「采纳」后由前端 ``proposal_card.js`` 拼回
P7.1~7.3 既有的 ``handoff_*`` inbound，server handler 零改动（docs §4）。

**工具不属于任务域**：handoff 是房间级调度通道，工具落本模块（不带 ``task_`` 前缀、
不进 ``task_tools.py``，docs §5.1）。``register_handoff_tools`` 要 ``room``——
``propose_review`` 用它把 ``reviewee`` 茶客名解析成最近一条发言的 message_id（§3.4）。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar, Optional

from agentao.tools import Tool

from .events import (
    TASK_PROPOSAL_KIND_HANDOFF_DELEGATE,
    TASK_PROPOSAL_KIND_HANDOFF_PANEL,
    TASK_PROPOSAL_KIND_HANDOFF_REVIEW,
    ChahuaEventType,
)

if TYPE_CHECKING:
    from .room import Room
    from .transport_bridge import ChahuaTransport

# 被审消息在卡片预览里的截断长度（payload.snippet 仅供卡片展示，采纳时丢弃）。
_REVIEW_SNIPPET_LEN = 80


class _ProposeHandoffBase(Tool):
    """三个 handoff propose 工具的共同基类 —— 共享 envelope emit 路径。

    子类必须设 ``_kind`` 字面值（``TASK_PROPOSAL_KIND_HANDOFF_*``）—— 前端"采纳"
    按钮按这个 kind 拼对应的 ``handoff_*`` inbound。结构同
    :class:`chahua.task_tools._TaskProposeBase`，不同的只是 ``_kind`` 与 payload 形状。
    """

    _kind: ClassVar[str]

    def __init__(self, *, transport: ChahuaTransport) -> None:
        super().__init__()
        self._transport = transport

    @property
    def is_read_only(self) -> bool:
        return True

    def _emit_proposal(self, payload: dict[str, Any]) -> str:
        """统一组 envelope.data + emit + 返 LLM ack。

        与 ``task_propose_*`` 同 envelope（``TASK_PROPOSAL``）—— ``task_id`` 由
        ``ChahuaTransport.emit_chahua`` 从 bind 上下文自动合成进 ``data.task_id``，
        前端对 handoff kind 不读它（仅 decision 用）。
        """
        self._transport.emit_chahua(
            ChahuaEventType.TASK_PROPOSAL,
            {
                "proposer": self._transport.guest_name,
                "kind": self._kind,
                "payload": payload,
            },
        )
        return "Proposed; takes effect only after the user accepts in the UI."


class ProposeDelegateTool(_ProposeHandoffBase):
    _kind = TASK_PROPOSAL_KIND_HANDOFF_DELEGATE

    @property
    def name(self) -> str:
        return "propose_delegate"

    @property
    def description(self) -> str:
        return (
            "Propose handing the next turn to a specific guest. Proposing does NOT "
            "take effect immediately — the UI renders an accept / dismiss card; only "
            "after the user accepts is it delegated. Pass target as a guest name "
            "(roster is in onboarding). Only propose when a specific guest really "
            "needs to speak next; don't propose every turn."
        )

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": "Name of the guest to hand the turn to."},
                "reason": {
                    "type": "string",
                    "description": "Reason for the proposal (optional, shown on the accept card).",
                },
            },
            "required": ["target"],
            "additionalProperties": False,
        }

    def execute(self, *, target: str, reason: Optional[str] = None, **_: Any) -> str:
        payload: dict[str, Any] = {"target": target}
        if reason:
            payload["reason"] = reason
        return self._emit_proposal(payload)


class ProposeReviewTool(_ProposeHandoffBase):
    _kind = TASK_PROPOSAL_KIND_HANDOFF_REVIEW

    def __init__(self, *, transport: ChahuaTransport, room: Room) -> None:
        super().__init__(transport=transport)
        self._room = room

    @property
    def name(self) -> str:
        return "propose_review"

    @property
    def description(self) -> str:
        return (
            "Propose asking one guest to review another guest's most recent message. "
            "Proposing does NOT take effect immediately — the UI renders an accept / "
            "dismiss card; only after the user accepts is the review requested. Pass "
            "reviewer / reviewee as guest names. The reviewed message is always "
            "reviewee's most recent one. Only propose when a re-check is really needed."
        )

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "reviewer": {"type": "string", "description": "Reviewer guest name."},
                "reviewee": {
                    "type": "string",
                    "description": "Reviewee guest name (their most recent message is reviewed).",
                },
            },
            "required": ["reviewer", "reviewee"],
            "additionalProperties": False,
        }

    def execute(self, *, reviewer: str, reviewee: str, **_: Any) -> str:
        # message_id 在 propose 时解析并冻结 —— 即使用户采纳前 reviewee 又发了言，
        # 审的仍是茶客本意指的那条（docs §5.3）。
        msg = self._room.latest_message_by_speaker_id(reviewee)
        if msg is None:
            return (
                f"Error: \"{reviewee}\" has no recent message to review; "
                "cannot propose a review."
            )
        return self._emit_proposal(
            {
                "target": reviewer,
                "message_id": msg.message_id,
                "reviewee": reviewee,
                "snippet": msg.text[:_REVIEW_SNIPPET_LEN],
            }
        )


class ProposePanelTool(_ProposeHandoffBase):
    _kind = TASK_PROPOSAL_KIND_HANDOFF_PANEL

    @property
    def name(self) -> str:
        return "propose_panel"

    @property
    def description(self) -> str:
        return (
            "Propose a round-table: several guests each state their view on the "
            "current topic. Proposing does NOT take effect immediately — the UI "
            "renders an accept / dismiss card; only after the user accepts is it "
            "started. Pass targets as ≥2 guest names; summarizer (optional) is one "
            "guest name to write the synthesis. Only propose when the topic really "
            "needs multiple perspectives."
        )

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "targets": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Round-table participant guest names (≥2).",
                },
                "summarizer": {
                    "type": "string",
                    "description": "Summarizer guest name (optional).",
                },
            },
            "required": ["targets"],
            "additionalProperties": False,
        }

    def execute(
        self,
        *,
        targets: list[str],
        summarizer: Optional[str] = None,
        **_: Any,
    ) -> str:
        # 不校验 targets 个数 / 在场 / cap —— 校验留给采纳时既有的 handoff_panel
        # inbound 五道校验兜底（docs §5.4）。
        payload: dict[str, Any] = {"targets": list(targets)}
        if summarizer:
            payload["summarizer"] = summarizer
        return self._emit_proposal(payload)


def register_handoff_tools(
    agent: Any,
    *,
    transport: ChahuaTransport,
    room: Room,
) -> None:
    """把三个 handoff propose 工具注册到 agentao agent。工厂函数 —— TeaGuest 一行调用。

    与 :func:`chahua.task_tools.register_task_tools` 并列、不合并：后者要 ``tasks_store``，
    本工厂要 ``room``（``propose_review`` 解析 reviewee → message_id 用，docs §9）。
    """
    agent.tools.register(ProposeDelegateTool(transport=transport))
    agent.tools.register(ProposeReviewTool(transport=transport, room=room))
    agent.tools.register(ProposePanelTool(transport=transport))
