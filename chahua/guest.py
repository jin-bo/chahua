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
from agentao.mcp import load_mcp_config
from agentao.paths import user_root
from agentao.permissions import PermissionEngine

from .events import (
    STATUS_CANCELLED,
    STATUS_ERROR,
    STATUS_OK,
    ChahuaEventType,
    EnvelopeSink,
    new_message_id,
)
from .mcp_thread import ThreadedMcpClientManager
from .permissions import apply_permission_mode
from .persona_assets import PersonaAssets
from .room import Message, Room
from .transport_bridge import ChahuaTransport

_log = logging.getLogger(__name__)


def _merged_mcp_configs(
    working_directory: Path,
    persona_servers: Optional[dict],
    room_level_servers: Optional[dict] = None,
) -> dict:
    """Overlay 三档 MCP 来源 → 单一 dict 喂 :class:`agentao.Agentao`：

    1. agentao 文件加载（``<workdir>/.agentao/mcp.json`` + ``~/.agentao/mcp.json``）。
    2. persona sidecar mcp（trust 门控已在 :func:`chahua.session._build_guests` 处生效 ——
       未受信任时 ``persona_servers`` 是 ``None``）。
    3. 房间级 ``[[guest.extra_mcp_servers]]`` —— 用户手写在自己 room.toml 里，等价用户
       意图自动信任，无 trust 清单。同名时**覆盖** persona / 文件加载层。

    设计 §2.4：trust 门控不对称（persona 来路可能任意可执行；房间级是用户当下意图）；
    覆盖顺序由"哪一层更接近用户当下意图"决定。
    """
    try:
        configs = load_mcp_config(
            project_root=working_directory,
            user_root=user_root(),
        )
    except Exception:
        _log.exception("Failed to load MCP config for %s", working_directory)
        configs = {}
    merged = dict(configs)
    if persona_servers:
        for name, cfg in persona_servers.items():
            if name in merged:
                _log.info("persona MCP server %r overrides file-loaded config", name)
            merged[name] = {**cfg, "trust": cfg.get("trust", True)}
    if room_level_servers:
        for name, cfg in room_level_servers.items():
            if name in merged:
                _log.info(
                    "room-level MCP server %r overrides persona / file-loaded config", name
                )
            # 房间级自动信任 —— 用户在自己 room.toml 里手写 == 用户意图。
            merged[name] = {**cfg, "trust": True}
    return merged


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
        assets: Optional[PersonaAssets] = None,
        room_level_mcp: Optional[dict[str, dict]] = None,
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

        if assets is not None:
            assets.materialize_skills(self.working_directory)

        mcp_manager = None
        mcp_configs = _merged_mcp_configs(
            self.working_directory,
            assets.mcp_servers if assets is not None else None,
            room_level_servers=room_level_mcp,
        )
        if mcp_configs:
            # chahua constructs TeaGuest inside the websocket event loop.  The
            # default agentao sync MCP manager nests run_until_complete there,
            # so use a background-loop manager and inject it as already-owned.
            # Keep agentao's original file-loaded + persona-extra merge order.
            mcp_manager = ThreadedMcpClientManager(mcp_configs)
            try:
                mcp_manager.connect_all()
            except Exception:
                _log.exception("%s: MCP connection failed", self.name)
                mcp_manager.disconnect_all()
                mcp_manager = None

        self.agent = Agentao(
            working_directory=self.working_directory,
            llm_client=llm_client,
            project_instructions=persona_md,
            transport=self._transport,
            permission_engine=permission_engine,
            mcp_manager=mcp_manager,
        )

        # tool_runner 是 Agentao.__init__ 里装的，read-only 拦截必须在那之后才能套上。
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
