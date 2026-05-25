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
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Optional

from agentao.transport import AgentEvent, EventType, SdkTransport

from ._transport_artifact_attribution import (  # noqa: F401
    ArtifactAttributor,
    # 子域 helper / 常量在搬走后仍经此模块再导出 —— ``tests/test_share_artifacts``
    # 直接 ``from chahua.transport_bridge import _normalize_share_rel`` 校验 share/
    # 段级 normalize 规则；保旧路径 0 改动。
    _normalize_share_rel,
    _should_diff_for_attribution,
    _SHELL_TOOL_NAME,
    _MCP_TOOL_PREFIX,
)
from .debug_recorder import NOOP_RECORDER, TurnRecorder, classify_tool_source
from .events import (
    NOOP_SINK,
    STATUS_OK,
    ChahuaEnvelope,
    ChahuaEventType,
    EnvelopeSink,
    emit_to_sink,
)
from .message_artifacts import MessageArtifactRegistry
from .tasks_store import TasksStore

_log = logging.getLogger(__name__)


# P8.3：托管会话内拦截管理者 propose 的 hook 签名。收一条 ``TASK_PROPOSAL`` envelope
# + 当前 bind 的 sink，返回 ``True`` 表示「已处理，别再下发前端」（拦截）、``False``
# 表示「照常 emit」。Orchestrator 注入 :meth:`Orchestrator._intercept_task_proposal`。
TaskProposalHook = Callable[["ChahuaEnvelope", EnvelopeSink], bool]


class ChahuaTransport(SdkTransport):
    """SDK 事件 → 茶话室 envelope 的转译器。"""

    def __init__(
        self,
        *,
        room_id: str,
        guest_name: str,
        message_artifacts: Optional[MessageArtifactRegistry] = None,
        share_dir: Optional[Path] = None,
        tasks_store: Optional[TasksStore] = None,
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
        # P8.3：托管会话内拦截管理者 propose 的 hook。``None`` = 缺省（非托管房间 /
        # 测试），``emit_chahua`` 行为与今天完全一致。session 装配时由 Orchestrator
        # 注入；与 transport 终身绑定（不随 bind / clear 变）。
        self._task_proposal_hook: Optional[TaskProposalHook] = None
        # P10 artifact 归属子域 —— args-known 写盘工具直拆 + shell/MCP TOOL_START/COMPLETE
        # diff 都搬到 :class:`ArtifactAttributor`。本 transport 通过 ``self._attribution``
        # 访问，``_handle`` TOOL_START / COMPLETE 分支调 ``self._attribution.<method>``。
        # ``message_artifacts`` / ``share_dir`` / ``tasks_store`` 全部委托给 attributor 持。
        self._attribution = ArtifactAttributor(
            transport=self,
            message_artifacts=message_artifacts,
            share_dir=share_dir,
            tasks_store=tasks_store,
        )

    def set_task_proposal_hook(self, hook: Optional[TaskProposalHook]) -> None:
        """注入 / 清除 ``TASK_PROPOSAL`` 拦截 hook（P8.3，docs §5.1）。

        session 装配期一次性注入；与 transport 同生命周期，不随每次 ``bind`` 变。
        """
        self._task_proposal_hook = hook

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
            # P10.4 review §6 修：``_tool_call_snapshots`` / ``_tool_call_pre_pending``
            # **不**在 bind 退出时清（晚到的 TOOL_COMPLETE 仍要 entry 里的 mid 完成
            # record_pending / rollback，不丢归属）；attributor 持有，自然延续。
            # baseline 清：``_diff_baseline`` 是同 bind 内多个 shell/MCP 共享的滚动
            # 快照，下次 bind 是新的对话上下文，应重新扫盘。
            self._attribution.clear_baseline()

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

    def inflight_snapshot(self) -> Optional[dict]:
        """当前 bind 中的 in-flight 消息快照 —— 未 bind（无活动 ``speak()``）→ ``None``。

        P9 切回一个 turn 在后台续跑的房间时，``emit_room_snapshot`` 据此补发那条
        进行中消息的 ``message_start`` + ``message_delta(partial_text)``，让切回前
        已流出的内容立即在聊天区 / 调试面板成形，不必干等 ``message_end``。
        ``partial_text`` 是已 emit 过 delta 的 chunk 拼接 —— 与后续真实 delta 严格
        续接（单事件循环线程，快照在 turn 恢复前同步发完）。
        """
        if self._message_id is None:
            return None
        return {
            "turn_id": self._turn_id,
            "message_id": self._message_id,
            "guest_name": self._guest_name,
            "task_id": self._task_id,
            "partial_text": self.partial_text,
        }

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
        env = ChahuaEnvelope(
            room_id=self._room_id,
            turn_id=self._turn_id,
            guest_name=self._guest_name,
            message_id=self._message_id,
            type=type,
            status=status,
            seq=seq,
            data=merged,
        )
        # P8.3：托管会话内管理者的 handoff_delegate / handoff_panel 提议被 hook 直接
        # 入队、拦下不下发前端（不渲采纳卡，docs §5.1）。hook 返回 True = 已拦截。
        # hook 缺省 None / 非 TASK_PROPOSAL 时这段零成本。hook 抛错不阻断 emit ——
        # 退化成照常下发（与 emit_to_sink「sink 不能挂掉生产者」同口径）。
        if type is ChahuaEventType.TASK_PROPOSAL and self._task_proposal_hook is not None:
            try:
                if self._task_proposal_hook(env, self._sink):
                    return
            except Exception:
                _log.exception("task_proposal_hook raised; falling back to emit")
        emit_to_sink(self._sink, env)

    # ── P10 artifact 归属子域：read-write @property 把 attributor 的内部状态透出来 ──
    # ``test_share_artifacts`` 直接 access ``transport._tool_call_snapshots`` /
    # ``transport._diff_baseline`` 验证 baseline 缓存与 snapshot 生命周期；属性转发
    # 保测试 0 改动。setter 同步透下来 —— 历史代码（含 pre-refactor 的 ``bind()`` finally
    # 的 ``self._diff_baseline = None``）习惯直接赋值，无 setter 会 AttributeError，
    # 与"字段从直属移到 helper 但仍透出"的不变契约不合。``_tool_call_pre_pending``
    # 顺手也透出（虽然测试当前没读）让三个字段在 transport 上看起来仍是一组。

    @property
    def _tool_call_snapshots(self) -> dict:
        return self._attribution._tool_call_snapshots

    @_tool_call_snapshots.setter
    def _tool_call_snapshots(self, value: dict) -> None:
        self._attribution._tool_call_snapshots = value

    @property
    def _tool_call_pre_pending(self) -> dict:
        return self._attribution._tool_call_pre_pending

    @_tool_call_pre_pending.setter
    def _tool_call_pre_pending(self, value: dict) -> None:
        self._attribution._tool_call_pre_pending = value

    @property
    def _diff_baseline(self):  # Optional[dict[str, FileStamp]]
        return self._attribution._diff_baseline

    @_diff_baseline.setter
    def _diff_baseline(self, value) -> None:
        self._attribution._diff_baseline = value

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
                self._attribution.maybe_record_artifact_path(
                    tool_name, args, call_id=call_id,
                )
                # P10.4：shell / MCP 类工具 args 推不出落盘路径，TOOL_START 这一刻
                # 冻结一份 share/ + active task artifacts/ 指纹快照与归属
                # (message_id / task_id)，等 TOOL_COMPLETE 再 diff 回填 pending。
                # 滚动 baseline 非空时 attributor 内部复用、跳过扫盘；为空时即扫一次。
                self._attribution.maybe_start_diff_snapshot(
                    tool_name=tool_name, call_id=call_id,
                )
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
            # P10.4：shell / MCP 工具走 diff 路径回填 pending —— 与 TOOL_START 那段
            # 配对。snapshot 命不中（白名单外 / call_id 缺）→ no-op。
            # **status 失败时分流**（P10.4 review §8）：rollback ``_maybe_record_artifact_path``
            # 在 TOOL_START 预落的 pending —— 全新 rel 的失败写盘 P10.3 stamp 守卫拦不住，
            # 若不主动 rollback、consume_pending 会把失败 message_id 永久落 jsonl。
            # 成功（status="ok"）的工具走正常 diff 路径，pre-pending 集合扔掉即可。
            if isinstance(call_id, str) and call_id:
                status = data.get("status")
                if status != "ok":
                    self._attribution.rollback_pre_pending(call_id)
                else:
                    # 成功路径：扔掉 pre-pending 追踪（pending 由正常 consume 路径走）。
                    self._attribution._tool_call_pre_pending.pop(call_id, None)
                self._attribution.consume_tool_diff(call_id)
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
