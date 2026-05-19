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

from .debug_recorder import NOOP_RECORDER, TurnRecorder, classify_tool_source
from .events import (
    NOOP_SINK,
    STATUS_OK,
    ChahuaEnvelope,
    ChahuaEventType,
    EnvelopeSink,
    emit_to_sink,
)
from .task_tools import TASK_WRITE_ARTIFACT_TOOL_NAME
from .tasks_store import _validate_artifact_name

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
        # 每次 :meth:`bind` 时从 :class:`TeaGuest.speak` 接管的 active task；用 envelope
        # ``data.task_id`` 透出去，让前端把流式 chunk 与任务面板挂钩。
        self._task_id: Optional[str] = None
        self._partial: list[str] = []
        # P6.1：bind() 时由 TeaGuest 传入；workflow tool_start/complete 事件下钩到这里。
        # 未 bind 时为 NOOP_RECORDER；与 sink 同生命周期。
        self._recorder: TurnRecorder = NOOP_RECORDER

    # ── per-speak 生命周期 ─────────────────────────────────────────────────

    @contextmanager
    def bind(
        self,
        *,
        sink: EnvelopeSink,
        turn_id: str,
        message_id: str,
        task_id: Optional[str] = None,
        recorder: TurnRecorder = NOOP_RECORDER,
    ) -> Iterator["ChahuaTransport"]:
        """绑定一次 speak() 的 (sink, turn_id, message_id [, task_id, recorder])；
        退出时复位。

        ``with self._transport.bind(...): self._transport.emit_chahua(...)`` ——
        把"开 envelope / 任何路径都得复位"用 Python 语法收紧，避免 set/clear 配对
        在 speak() 的多 except 分支里手抖漏掉。partial_text 缓冲在进入 with 时清。

        ``task_id``：本轮 message_* envelope 的 ``data.task_id``。``None`` = 房间级闲聊，
        data 里不写这个键（envelope schema 不变，老前端无感）。

        ``recorder``：P6.1 起 ``TeaGuest.speak`` 透传；TOOL_START / TOOL_COMPLETE 帧
        转译时同步 ``record_tool_start`` / ``record_tool_complete``。未 bind 期间为
        ``NOOP_RECORDER`` —— 防 agentao 在 speak 外异步 emit 误流入。
        """
        self._sink = sink
        self._turn_id = turn_id
        self._message_id = message_id
        self._task_id = task_id
        self._recorder = recorder
        self._partial.clear()
        try:
            yield self
        finally:
            # 复位顺序无关；都置成"无活动 envelope"状态。partial_text 不清，让
            # 调用方在 finally 里还能读（下次 bind 时清零）。
            self._sink = NOOP_SINK
            self._turn_id = None
            self._message_id = None
            self._task_id = None
            self._recorder = NOOP_RECORDER

    @property
    def partial_text(self) -> str:
        """已收到的 chunk 拼接结果。speak() 在 error/cancel 路径上用。"""
        return "".join(self._partial)

    @property
    def guest_name(self) -> str:
        """终身绑定的茶客名（构造时设、不可变）。"""
        return self._guest_name

    @property
    def current_task_id(self) -> Optional[str]:
        """当前 :meth:`bind` 上下文的 task_id 快照；未 bind 时 ``None``。

        P5.3.4 task_tools 在 ``tool.execute()`` 内读：这时正跑在 LLM 的
        ``agent.arun()`` 里、``TeaGuest.speak()`` 已 bind 上下文 → 这里读到的是
        进 speak 时被 snapshot 的归属，与 message_* envelope 同源。"""
        return self._task_id

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

        bind 时若传了 ``task_id``，自动塞到 ``data.task_id`` —— envelope schema_version
        不变，前端 reducer 按 ``data.task_id`` 把流式 chunk 挂到任务面板。``None`` 时不写键，
        老前端不感知。
        """
        if self._turn_id is None:
            _log.warning(
                "ChahuaTransport.emit_chahua(%s) called without active envelope; dropped",
                type.value,
            )
            return
        # 无任务时跳过 dict 拷贝 —— message_delta 每个 chunk 都过这里，常见场景是房间级闲聊。
        if self._task_id is None:
            merged: Mapping[str, Any] = data or {}
        else:
            merged = {**(data or {}), "task_id": self._task_id}
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
                data=merged,
            ),
        )

    def _maybe_record_artifact_path(
        self, tool_name: str, args: Any
    ) -> None:
        """从已知写盘工具的 args 派生 artifact 绝对路径，喂给 recorder（docs/P6 §6）。

        派生表显式枚举：MVP 仅 ``task_write_artifact``；后续加新写盘工具按 tool name
        扩本函数。**不挂 ArtifactDetector**（debug 是侧路能力，与任务系统解耦）。

        前置条件：调用方已确保 ``self._message_id is not None``（与 ``record_tool_start``
        合并守卫，避免重复 nullable 判定）。
        """
        if (
            tool_name != TASK_WRITE_ARTIFACT_TOOL_NAME
            or self._task_id is None
            or not isinstance(args, dict)
        ):
            return
        name = args.get("name")
        if not isinstance(name, str) or not name:
            return
        # 与写盘路径同口径：name 合法（无 ``/`` / ``\\`` / ``..`` / 前缀 ``.``）才记
        # —— 否则 ``TasksStore.write_artifact`` 会拒、根本不会落盘，调试日志记一条
        # "幽灵 artifact" 会误导用户去查根本不存在的文件。前端 ``deriveArtifactPath``
        # 同算法跟进。
        if _validate_artifact_name(name) is not None:
            return
        self._recorder.record_artifact_path(
            message_id=self._message_id,  # type: ignore[arg-type]
            path=f"tasks/{self._task_id}/artifacts/{name}",
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
            tool_name = data.get("tool") or ""
            call_id = data.get("call_id")
            args = data.get("args")
            self.emit_chahua(
                ChahuaEventType.TOOL_START,
                {"tool": tool_name, "args": args, "call_id": call_id},
            )
            # P6.1：MCP 来源走 tool 名启发式（不变量"_classify_tool_source 仅
            # best-effort"），不为识别 MCP 改 agentao event 形态。
            if self._message_id is not None:
                source, mcp_server = classify_tool_source(tool_name)
                self._recorder.record_tool_start(
                    message_id=self._message_id,
                    call_id=call_id,
                    tool=tool_name,
                    args=args,
                    source=source,
                    mcp_server=mcp_server,
                )
                # P6.1 artifact 派生表显式枚举（docs §不变量"仅从特定写盘工具派生"）：
                # ``task_write_artifact(name, content)`` → ``tasks/<task_id>/artifacts/<name>``。
                # ``task_id`` 缺 / ``name`` 缺时跳过（不入 task）。后续加新写盘工具按
                # tool name 扩派生表 —— 不挂 ArtifactDetector。
                self._maybe_record_artifact_path(tool_name, args)
            return

        if et is EventType.TOOL_COMPLETE:
            call_id = data.get("call_id")
            self.emit_chahua(
                ChahuaEventType.TOOL_COMPLETE,
                {
                    "tool": data.get("tool"),
                    "call_id": call_id,
                    "status": data.get("status"),
                    "duration_ms": data.get("duration_ms"),
                    "error": data.get("error"),
                },
            )
            if self._message_id is not None:
                self._recorder.record_tool_complete(
                    message_id=self._message_id,
                    call_id=call_id,
                    status=data.get("status"),
                    duration_ms=data.get("duration_ms"),
                    error=data.get("error"),
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
