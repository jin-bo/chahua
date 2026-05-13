"""ChahuaTransport —— agentao SDK 事件到茶话室前端 envelope 的桥（设计文档 §3.5）。

每个 :class:`chahua.guest.TeaGuest` 持一个 :class:`ChahuaTransport`。终身绑定
``(room_id, guest_name)``；每次 ``speak()`` 前调 :meth:`set_envelope` 设
``(turn_id, message_id)``，调用后 :meth:`clear_envelope` 重置 —— 防止下条消息的
流式 chunk 错挂到上条 message_id 上。

**职责切分**：

- ``message_start`` / ``message_end`` 由 :class:`TeaGuest.speak` 外层合成，不在这里。
  这里只负责"agentao 事件 → 茶话室事件"的纯转译（无副作用、不闭包）。
- ``turn_start`` / ``turn_end`` 由 orchestrator 合成。
- ``ERROR`` 转 ``guest_thinking`` 风格的提示事件，但**消息状态以 message_end.status
  为准**（§3.5.2 明确不依赖 ERROR 事件本身决定流的边界）。

**partial_text 累积**：每条 ``LLM_TEXT`` chunk 在这里追加到 :attr:`partial_text`，供
:class:`TeaGuest.speak` 在异常/取消路径上读取（落 ``message_end.data.partial_text``，
不入 transcript，§3.5.2）。:meth:`set_envelope` 调用时清零。
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Any, Iterator, Mapping, Optional

from agentao.transport import AgentEvent, EventType, SdkTransport

from .events import (
    NOOP_SINK,
    STATUS_OK,
    ChahuaEnvelope,
    ChahuaEventType,
    EnvelopeSink,
    emit_to_sink,
)

_log = logging.getLogger(__name__)


class ChahuaTransport(SdkTransport):
    """SDK 事件 → 茶话室 envelope 的转译器。"""

    def __init__(
        self,
        *,
        room_id: str,
        guest_name: str,
    ) -> None:
        # SdkTransport.on_event 在构造时绑死；我们把 _handle 注册上去。
        super().__init__(on_event=self._handle)
        self._sink: EnvelopeSink = NOOP_SINK
        self._room_id = room_id
        self._guest_name = guest_name
        self._turn_id: Optional[str] = None
        self._message_id: Optional[str] = None
        self._partial: list[str] = []

    # ── per-speak 生命周期 ─────────────────────────────────────────────────

    @contextmanager
    def bind(
        self,
        *,
        sink: EnvelopeSink,
        turn_id: str,
        message_id: str,
    ) -> Iterator["ChahuaTransport"]:
        """绑定一次 speak() 的 (sink, turn_id, message_id)；退出时复位。

        ``with self._transport.bind(...): self._transport.emit_chahua(...)`` ——
        把"开 envelope / 任何路径都得复位"用 Python 语法收紧，避免 set/clear 配对
        在 speak() 的多 except 分支里手抖漏掉。partial_text 缓冲在进入 with 时清。
        """
        self._sink = sink
        self._turn_id = turn_id
        self._message_id = message_id
        self._partial.clear()
        try:
            yield self
        finally:
            # 复位顺序无关；都置成"无活动 envelope"状态。partial_text 不清，让
            # 调用方在 finally 里还能读（下次 bind 时清零）。
            self._sink = NOOP_SINK
            self._turn_id = None
            self._message_id = None

    @property
    def partial_text(self) -> str:
        """已收到的 chunk 拼接结果。speak() 在 error/cancel 路径上用。"""
        return "".join(self._partial)

    # ── 给上层 emit 用（message_start / message_end 走这里） ───────────────

    def emit_chahua(
        self,
        type: ChahuaEventType,
        data: Optional[Mapping[str, Any]] = None,
        *,
        status: str = STATUS_OK,
        seq: Optional[int] = None,
    ) -> None:
        """合成 envelope 走 sink。turn_id / message_id 取当前 :meth:`bind` 的值；
        没绑就丢 + WARN（防 agentao 在 speak() 外异步 emit 误流入）。
        """
        if self._turn_id is None:
            _log.warning(
                "ChahuaTransport.emit_chahua(%s) called without active envelope; dropped",
                type.value,
            )
            return
        emit_to_sink(
            self._sink,
            ChahuaEnvelope(
                room_id=self._room_id,
                turn_id=self._turn_id,
                guest_name=self._guest_name,
                message_id=self._message_id,
                type=type,
                status=status,
                seq=seq,
                data=data or {},
            ),
        )

    # ── agentao 事件回调 ───────────────────────────────────────────────────

    def _handle(self, event: AgentEvent) -> None:
        """SDK on_event 入口。按类型转译到对应 envelope；其余事件丢弃。"""
        et = event.type
        data = event.data

        if et is EventType.LLM_TEXT:
            chunk = data.get("chunk", "")
            if not chunk:
                return
            self._partial.append(chunk)
            self.emit_chahua(ChahuaEventType.MESSAGE_DELTA, {"chunk": chunk})
            return

        if et is EventType.THINKING:
            text = data.get("text", "")
            if not text:
                return
            self.emit_chahua(ChahuaEventType.GUEST_THINKING, {"text": text})
            return

        if et is EventType.TOOL_START:
            # 字段直接转 —— agentao 用的 key 与设计文档 §3.5.1 一致（见 events.py 注释）。
            self.emit_chahua(
                ChahuaEventType.TOOL_START,
                {
                    "tool": data.get("tool"),
                    "args": data.get("args"),
                    "call_id": data.get("call_id"),
                },
            )
            return

        if et is EventType.TOOL_COMPLETE:
            self.emit_chahua(
                ChahuaEventType.TOOL_COMPLETE,
                {
                    "tool": data.get("tool"),
                    "call_id": data.get("call_id"),
                    "status": data.get("status"),
                    "duration_ms": data.get("duration_ms"),
                    "error": data.get("error"),
                },
            )
            return

        if et is EventType.ERROR:
            # 转为 guest_thinking 风格的提示事件，前端可选显示；**不**作为消息状态的根据
            # （§3.5.2：message_end.status 才是边界 truth）。
            msg = data.get("message") or data.get("detail") or "(无 detail)"
            self.emit_chahua(
                ChahuaEventType.GUEST_THINKING,
                {"text": f"[运行时错误：{msg}]"},
            )
            return

        # 其余事件（TURN_BEGIN/END、AGENT_START/END、MEMORY_*、SKILL_*、PERMISSION_*、
        # LLM_CALL_*、TOOL_OUTPUT/RESULT、ASK_USER_*、BACKGROUND_*、PLUGIN_HOOK_FIRED、
        # MODEL_CHANGED、CONTEXT_COMPRESSED、SESSION_SUMMARY_WRITTEN、TURN_START、
        # TOOL_CONFIRMATION、READONLY_MODE_CHANGED 等）—— 茶话室前端不关心，丢弃。
