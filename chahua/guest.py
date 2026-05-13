"""TeaGuest —— 一个茶客（设计文档 §3.1 / §3.5）。

P0 只做 ``embed`` backend：包一个 :class:`agentao.Agentao` 实例 + 人格 + 流式输出回调。
``acp`` backend 是 P4 的事（§3.9）。

流式实现：构造一个 :class:`agentao.transport.SdkTransport`，``on_event`` 派发到 TeaGuest
内部的当前 chunk 回调（每次 :meth:`speak` 调用前临时设上、调用后清掉）。这样
:class:`agentao.Agentao` 不需要知道前端 envelope 的存在 —— 它只管 emit ``LLM_TEXT``，
我们在外面合成 message 边界（§3.5.2 的雏形，P2 才完整实现到 WebSocket）。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable, Optional

from agentao import Agentao
from agentao.llm import LLMClient
from agentao.permissions import PermissionEngine
from agentao.transport import AgentEvent, EventType, SdkTransport

from .permissions import apply_permission_mode

ChunkCallback = Callable[[str], None]

_log = logging.getLogger(__name__)


class TeaGuest:
    """一个茶客。

    构造时：

    - ``working_directory`` 自动 ``mkdir(parents=True, exist_ok=True)``。
    - 人格 (``persona_md``) 通过 ``project_instructions=`` 注入到 agentao 的系统提示。
    - 权限模式走 :func:`apply_permission_mode` 双 API 同步（§3.4.1）。
    """

    def __init__(
        self,
        *,
        name: str,
        persona_md: str,
        llm_client: LLMClient,
        working_directory: Path,
        permission: str = "read-only",
    ) -> None:
        self.name = name
        self.working_directory = Path(working_directory)
        self.working_directory.mkdir(parents=True, exist_ok=True)

        # SdkTransport 的 on_event 在构造时就绑死了，没法每次 speak() 替换 transport；
        # 用可变属性透传当前 chunk 回调。
        self._current_on_chunk: Optional[ChunkCallback] = None

        transport = SdkTransport(on_event=self._on_event)

        # 显式喂空白 PermissionEngine —— 不传则 Agentao 默认 None，apply_permission_mode
        # 没东西可调。rules=[] 表示"不读盘配置，靠预设模式"。
        permission_engine = PermissionEngine(
            project_root=self.working_directory,
            rules=[],
            loaded_sources=[],
        )

        self.agent = Agentao(
            working_directory=self.working_directory,
            llm_client=llm_client,
            project_instructions=persona_md,
            transport=transport,
            permission_engine=permission_engine,
        )

        # 必须在 Agentao 构造完成后调 —— tool_runner 是构造时装配的。
        apply_permission_mode(self.agent, permission)

    @property
    def permission(self) -> str:
        """当前权限模式（每次从 agent.permission_engine 拉，避免与运行时切换脱节）。"""
        return self.agent.permission_engine.active_mode.value

    # ── 流式事件路由 ───────────────────────────────────────────────────────

    def _on_event(self, event: AgentEvent) -> None:
        """SdkTransport 回调入口。P0 只关心 LLM_TEXT；其他事件先丢弃。

        P2 这里会扩成"合成 message_start / message_end / 推到 WebSocket"。
        """
        if event.type is not EventType.LLM_TEXT:
            return
        cb = self._current_on_chunk
        if cb is None:
            return
        chunk = event.data.get("chunk", "")
        if not chunk:
            return
        try:
            cb(chunk)
        except Exception:
            # 守住 transport 不能抛异常（agentao 契约）。但要留痕 —— 沉默吞掉异常
            # 会让"流式没字"这种用户可见症状失去诊断线索。
            _log.exception("chunk callback raised for %s", self.name)

    async def speak(
        self,
        context_message: str,
        on_chunk: Optional[ChunkCallback] = None,
    ) -> str:
        """让茶客说一句话，返回完整文本。

        ``on_chunk`` 是流式回调，每个 ``LLM_TEXT`` chunk 调一次。``None`` = 不流式。
        """
        self._current_on_chunk = on_chunk
        try:
            return await self.agent.arun(context_message)
        finally:
            # 必须清，否则下次 speak() 还在用上次的回调写到错误的地方。
            self._current_on_chunk = None

    def close(self) -> None:
        """释放 agent 资源。多次调用安全。"""
        self.agent.close()
