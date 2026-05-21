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
        return "已提议，等用户在 UI 采纳后才会生效。"


class ProposeDelegateTool(_ProposeHandoffBase):
    _kind = TASK_PROPOSAL_KIND_HANDOFF_DELEGATE

    @property
    def name(self) -> str:
        return "propose_delegate"

    @property
    def description(self) -> str:
        return (
            "提议把下一句发言交给某位茶客。提议 ≠ 立刻生效 —— UI 渲染成采纳 / 忽略"
            "卡片，用户点采纳后才指派。target 传茶客名（房间成员名册见 onboarding）。"
            "仅在确有必要让特定茶客接话时才提议，别每轮都提。"
        )

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": "被指派发言的茶客名。"},
                "reason": {
                    "type": "string",
                    "description": "提议理由（可选，会显示在采纳卡片上）。",
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
            "提议请某位茶客审阅另一位茶客最近一条发言。提议 ≠ 立刻生效 —— UI 渲染成"
            "采纳 / 忽略卡片，用户点采纳后才请审。reviewer / reviewee 都传茶客名。"
            "审的固定是 reviewee 最近一条发言。仅在确有必要复核时才提议。"
        )

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "reviewer": {"type": "string", "description": "审阅者茶客名。"},
                "reviewee": {
                    "type": "string",
                    "description": "被审者茶客名（审其最近一条发言）。",
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
            return f"Error: 「{reviewee}」最近没有发言可审，无法提议请审。"
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
            "提议拉一场圆桌：多位茶客就当前话题各自表态。提议 ≠ 立刻生效 —— UI 渲染成"
            "采纳 / 忽略卡片，用户点采纳后才发起。targets 传 ≥2 个茶客名；summarizer "
            "（可选）传一位负责汇总的茶客名。仅在话题确实需要多方观点时才提议。"
        )

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "targets": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "参与圆桌的茶客名（≥2 位）。",
                },
                "summarizer": {
                    "type": "string",
                    "description": "汇总者茶客名（可选）。",
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
