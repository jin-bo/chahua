"""TeaGuest —— 一个茶客（设计文档 §3.1 / §3.5）。

P2.2 起 :meth:`speak` 不再吃 ``on_chunk`` 回调，改吃 envelope ``sink``。茶客自己持
:class:`chahua.transport_bridge.ChahuaTransport`：

- :meth:`speak` 用 :meth:`ChahuaTransport.bind` 包住整段 ``arun`` 调用，``with``
  退出时清 envelope —— set/clear 配对再也不会在某个 except 分支里漏掉。
- 内层 try 走 ``message_end`` 的三态分支（ok / cancelled / error）。message_id 在
  :meth:`speak` 开头分配一次，前端 envelope 和 transcript 落盘 record 共用同一 ID
  —— 前端能把流式 chunk 与持久化 record 串起来。
- :class:`agentao.cancellation.CancellationToken` 通过参数透传给
  ``agent.arun(cancellation_token=…)``；CancelledError 与 KeyboardInterrupt 都按
  cancelled 路径走（Python 3.11+ asyncio 把 SIGINT 翻成 CancelledError；同步 REPL
  input 拿到的是 KeyboardInterrupt——两条都得保 message_start/end 配对）。
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Optional

from agentao import Agentao
from agentao.cancellation import CancellationToken
from agentao.llm import LLMClient
from agentao.permissions import PermissionEngine

from .events import (
    STATUS_CANCELLED,
    STATUS_ERROR,
    STATUS_OK,
    ChahuaEventType,
    EnvelopeSink,
    new_message_id,
)
from .permissions import apply_permission_mode
from .room import Message, Room
from .transport_bridge import ChahuaTransport

_log = logging.getLogger(__name__)


class TeaGuest:
    """一个茶客。持 :class:`Room` 引用 —— 写入 transcript / 合成 message_end 都从这里。"""

    def __init__(
        self,
        *,
        name: str,
        persona_md: str,
        llm_client: LLMClient,
        working_directory: Path,
        room: Room,
        permission: str = "read-only",
    ) -> None:
        self.name = name
        self.room = room
        self.working_directory = Path(working_directory)
        self.working_directory.mkdir(parents=True, exist_ok=True)

        # transport 终身绑定 (room_id, guest_name)；per-speak 的 (sink, turn_id,
        # message_id) 通过 bind() 临时设。room_id 暂用 room.name —— P4 加 [room].id
        # 后由 cli 显式传，这里换个 helper 即可。
        self._transport = ChahuaTransport(room_id=room.name, guest_name=name)

        permission_engine = PermissionEngine(
            project_root=self.working_directory,
            rules=[],
            loaded_sources=[],
        )

        self.agent = Agentao(
            working_directory=self.working_directory,
            llm_client=llm_client,
            project_instructions=persona_md,
            transport=self._transport,
            permission_engine=permission_engine,
        )

        # 必须在 Agentao 构造完成后调 —— tool_runner 是构造时装配的。
        apply_permission_mode(self.agent, permission)

    @property
    def permission(self) -> str:
        """当前权限模式（每次从 agent.permission_engine 拉，避免与运行时切换脱节）。"""
        return self.agent.permission_engine.active_mode.value

    # ── 主入口 ────────────────────────────────────────────────────────────

    async def speak(
        self,
        context_message: str,
        *,
        turn_id: str,
        sink: EnvelopeSink,
        cancellation_token: Optional[CancellationToken] = None,
    ) -> Optional[Message]:
        """让茶客说一句话。

        返回追加到 transcript 的 :class:`Message`（成功）或 ``None``（异常 / 取消）。
        ``message_start`` / ``message_end`` 由本函数合成 —— 保证一对一：

        - 成功：``message_end(status=ok, data={text}, seq=msg.seq)`` —— transcript
          里的 ``Message.message_id`` 与 envelope ``message_id`` 一致。
        - 取消：``message_end(status=cancelled, data={partial_text})``；不写 transcript；
          重抛让 orchestrator 决定是否中止后续。
        - 失败：``message_end(status=error, data={partial_text, error})``；不写 transcript；
          异常被吞，函数返回 ``None`` —— orchestrator 让链跑下去（与 P1 一致）。
        """
        message_id = new_message_id()
        with self._transport.bind(
            sink=sink, turn_id=turn_id, message_id=message_id
        ):
            self._transport.emit_chahua(ChahuaEventType.MESSAGE_START, {})
            try:
                text = await self.agent.arun(
                    context_message,
                    cancellation_token=cancellation_token,
                )
            except (asyncio.CancelledError, KeyboardInterrupt):
                self._emit_failure(STATUS_CANCELLED, "cancelled")
                raise
            except Exception as e:
                _log.exception("%s.speak() failed", self.name)
                self._emit_failure(STATUS_ERROR, str(e))
                return None

            msg = self.room.append(self.name, text, message_id=message_id)
            # message_end.data.text 是兜底字段（前端可以靠它做一次性渲染，不用拼 chunk）。
            self._transport.emit_chahua(
                ChahuaEventType.MESSAGE_END,
                {"text": text},
                status=STATUS_OK,
                seq=msg.seq,
            )
            return msg

    def _emit_failure(self, status: str, error: str) -> None:
        """两条失败分支（cancelled / error）共用的 message_end emit。"""
        self._transport.emit_chahua(
            ChahuaEventType.MESSAGE_END,
            {"partial_text": self._transport.partial_text, "error": error},
            status=status,
        )

    def close(self) -> None:
        """释放 agent 资源。多次调用安全。"""
        self.agent.close()
