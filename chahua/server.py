"""茶话室 WebSocket server（P2.3，设计文档 §2 / §3.5）。

本地 ws server，envelope JSON 下行 / ``user_message`` 上行。

**线协议**：

- 服务端 → 客户端：每帧一条 JSON，即 :meth:`ChahuaEnvelope.to_dict` 输出（含
  ``schema_version`` 供前端版本协商）。
- 客户端 → 服务端：每帧一条 JSON。当前仅识别一种 ``type``：

  .. code-block:: json

     {"type": "user_message", "text": "..."}

  其余 ``type`` WARN 后忽略（友好容忍 —— 前端在协议升级期发未来 type 不会被踢线）。
  非 JSON / 二进制帧 → ``close(CloseCode.UNSUPPORTED_DATA)``。

其余策略（单客户端、session 跨连接复用、SIGINT 优雅关停、端口/host 默认值）见
DESIGN.md §6 P2.3 行 + §8 落地决策。
"""

from __future__ import annotations

import asyncio
import json
import logging
import operator
import re
import sys
from pathlib import Path
from typing import Awaitable, Callable, Literal, Optional

from websockets import CloseCode
from websockets.asyncio.server import ServerConnection, serve
from websockets.exceptions import ConnectionClosed

from ._paths import Paths
from ._server_helpers import (
    check_keys_whitelist as _check_keys_whitelist,
    require_str as _require_str,
)
from .events import (
    ChahuaEnvelope,
    ChahuaEventType,
    EnvelopeSink,
    NOTICE_LEVEL_ERROR,
    NOTICE_LEVEL_INFO,
)
from .handoff import MANAGED_SESSION_REASON_USER_CANCEL
from .orchestrator import OrchestratorConfig
# Re-export 给测试 / 外部调用方（``from chahua.server import _llm_summary`` 路径不变）。
from .server_room_snapshot import (  # noqa: F401
    LlmSource,
    _llm_summary,
    _mcp_summary_list,
    _orchestrator_effective_dict,
    _read_room_toml,
)
from .server_room_snapshot import (
    emit_room_history as _do_emit_room_history,
    emit_room_info as _do_emit_room_info,
    emit_room_snapshot as _do_emit_room_snapshot,
)
from .server_inbound_admin import (
    AdminHandlers,
    INBOUND_ADD_GUEST,
    INBOUND_CREATE_ROOM,
    INBOUND_DELETE_ROOM,
    INBOUND_REMOVE_GUEST,
    INBOUND_SET_PERSONA_MCP_TRUST,
    INBOUND_UPDATE_GUEST_EXTRA_MCP,
    INBOUND_UPDATE_GUEST_ISOLATION,
    INBOUND_UPDATE_GUEST_LLM,
    INBOUND_UPDATE_GUEST_PERMISSION,
    INBOUND_UPDATE_ROOM_LLM,
    INBOUND_UPDATE_ROOM_ORCHESTRATOR,
)
from .server_inbound_io import (
    INBOUND_DOWNLOAD_FILE,
    INBOUND_EXPORT_ROOM,
    INBOUND_IMPORT_PERSONA_FOLDER,
    INBOUND_IMPORT_PERSONA_GITHUB,
    INBOUND_UPLOAD_FILE,
    IOHandlers,
)
from .server_inbound_settings import (
    INBOUND_UPDATE_ROOM_TOML,
    INBOUND_UPDATE_USER_AVATAR,
    INBOUND_UPDATE_USER_MD,
    SettingsHandlers,
)
from .server_inbound_task import (
    INBOUND_ADD_DECISION,
    INBOUND_ATTACH_ARTIFACT,
    INBOUND_CLEAR_TASK_ARTIFACTS,
    INBOUND_CLOSE_TASK,
    INBOUND_OPEN_TASK,
    INBOUND_SET_ACTIVE_TASK,
    INBOUND_UPDATE_TASK,
    TaskHandlers,
)
# INBOUND_HANDOFF_* / INFLIGHT_KIND_* 经此再导出 —— 既给本模块 _INBOUND_ROUTES /
# _inbound_user_message 用，也让 `from chahua.server import INBOUND_HANDOFF_*` 旧路径
# （测试 / 外部调用方）在 P7 inbound 拆模块后保持不变。
from .server_inbound_handoff import (  # noqa: F401
    HandoffHandlers,
    INBOUND_HANDOFF_CLEAR,
    INBOUND_HANDOFF_DELEGATE,
    INBOUND_HANDOFF_PANEL,
    INBOUND_HANDOFF_REVIEW,
    INBOUND_MANAGED_SESSION_START,
    INBOUND_MANAGED_SESSION_STOP,
    INFLIGHT_KIND_HANDOFF,
    INFLIGHT_KIND_USER,
)
from .session import RoomSession, build_room_session

_log = logging.getLogger(__name__)


DEFAULT_PORT = 7860
DEFAULT_HOST = "127.0.0.1"

# 入站帧上限。``websockets`` 默认 1MB —— 房间文件上限 200MB（_UPLOAD_MAX_BYTES）
# × 4/3 base64 ≈ 267MB，再加 JSON quoting + 字段开销。设 300MB 给上传留头：用户传
# 200MB 上限的文件不会被默默断线（websockets 会发 1009 + close 链接，sidecar 看上
# 去是中途挂掉，排查噩梦）。
_WS_MAX_INBOUND_BYTES = 300 * 1024 * 1024

# 客户端 → 服务端 message type 字段值。
INBOUND_USER_MESSAGE = "user_message"
INBOUND_SWITCH_ROOM = "switch_room"
INBOUND_CLEAR_ROOM = "clear_room"
INBOUND_CANCEL = "cancel"
# P6.3.A：调试抽屉点击历史索引行按 turn_id 拉详情。inbound 严格白名单（同 task
# inbound 口径），turn_id regex 强校验拒穿越（路径片段，``prompts/<turn_id>/*``
# 后端直接拼字符串）。响应回 TURN_DETAIL envelope。
INBOUND_FETCH_TURN_DETAIL = "fetch_turn_detail"
# 显式 handoff inbound type 常量（INBOUND_HANDOFF_*）已随 handler 迁到
# :mod:`chahua.server_inbound_handoff`，本模块顶 import 处再导出。
# ``/tools`` / ``/skills`` slash 查询：只读 introspection，回茶客 agent 注册的
# tools + 可用 skills + 权限模式。响应回 GUEST_CAPS_INFO envelope。归核心层
# （与 fetch_turn_detail 同口径——introspection 不属任何 feature slot）。
INBOUND_LIST_GUEST_CAPS = "list_guest_caps"

# Payload 白名单——module-level 与 ``_OPEN_TASK_ALLOWED`` 等 task inbound 常量同
# 位置（便于 grep，无 ``self.`` lookup 开销）。handoff 的几个白名单随 handler 迁到
# :mod:`chahua.server_inbound_handoff`；``INFLIGHT_KIND_*`` 同理（本模块顶 import 再导出）。
_LIST_GUEST_CAPS_ALLOWED = frozenset({"type", "guest", "view"})

# ``turn_id`` 形态：与 :func:`chahua.events.new_turn_id` 一致 = ``turn_<10 字节 hex>``。
# 接受 ``turn_<≥ 1 hex>`` 让未来 ID 字节数变动不需要 inbound 端也跟着改；穿越（``../``）
# / 空段 / 非 hex 字符一概拒。改 :func:`new_turn_id` ID 形态时同步动这条 regex。
_TURN_ID_RE = re.compile(r"^turn_[0-9a-f]+$")
# P5.2 重构（docs/P5-任务房间.md §7.2）：admin / io / settings / task / handoff 五类
# inbound 由独立 handler 类承担，:class:`ChahuaServer` 在 ``__init__`` 里实例化为五个 slot。
# 模块级 :data:`_INBOUND_ROUTES` 是 wire 字符串 → 属性路径（"_inbound_cancel" /
# "admin._inbound_add_guest"）的纯字符串表；``__init__`` 用 :func:`operator.attrgetter`
# 把它解析成 per-instance bound-method 字典 ``self._inbound_handlers``，``_handle_inbound``
# 走一次 dict lookup 直接 ``await``，没有 per-frame getattr。



def _attach_files_to_text(text: str, files: object) -> str:
    """把 ``user_message.files`` 列表里的相对路径附到文本末尾。

    每个文件渲染成单独一行 ``<./xxx>`` —— 用户友好可读（保留在 transcript），同时
    给茶客一个明显的"这里挂了文件，去自己 cwd 下 ``./share/...`` 读"信号。

    防御：``files`` 不是 list / 元素不是 str → 忽略；空串元素跳过；不允许绝对路径
    或 ``..`` —— ``share/`` 之外的路径不该被一条用户消息暗中塞进上下文。
    """
    if not isinstance(files, list):
        return text
    refs: list[str] = []
    for f in files:
        if not isinstance(f, str):
            continue
        s = f.strip()
        # 同时拒 `/` 和 `\` —— 后者在 Windows 上也是路径分隔符，``s.split("/")`` 单
        # 拆 forward slash 会让 ``share\..\boom`` 漏过去。
        parts = s.replace("\\", "/").split("/")
        if not s or s.startswith("/") or s.startswith("\\") or ".." in parts:
            _log.warning("user_message: 跳过非法文件引用 %r", f)
            continue
        refs.append(f"<./{s}>")
    if not refs:
        return text
    appendix = "\n".join(refs)
    return f"{text}\n{appendix}" if text else appendix


# ── server ────────────────────────────────────────────────────────────────


class ChahuaServer:
    """单房间 ws server。

    在同一 session 上跨多次客户端连接复用 —— 客户端断开后房间状态保留，下次连上
    就是续聊（与 :mod:`chahua.cli` 的 ``/quit`` → 重启 → 续聊一个意思）。

    P3.2.x 加 :meth:`_switch_room` 支持运行时换房：tear down 当前 session（
    所有茶客 agentao close），按新 ``room_id`` 装配新 session 替换 ``_session``，
    复用 ws 连接 + ``_emit_room_info`` / ``_emit_room_history`` —— 客户端拿到
    新 room_info + 全量历史回放，DOM ``replaceChildren`` 自动清掉前一个房间残留。

    P5.2 起 inbound handler 按 feature 切到独立类（:class:`AdminHandlers` /
    :class:`IOHandlers` / :class:`SettingsHandlers` / :class:`TaskHandlers` /
    :class:`HandoffHandlers`），各持
    ``self.server`` 反向引用做依赖注入；dispatch 走 ``__init__`` 时一次性把
    :data:`_INBOUND_ROUTES` 解析成 ``self._inbound_handlers`` bound-method 字典。
    """

    def __init__(
        self,
        session: RoomSession,
        *,
        host: str,
        port: int,
        paths: Paths,
    ) -> None:
        self._session = session
        self._host = host
        self._port = port
        self._paths = paths
        # 当前在线的客户端句柄。``None`` 表示空闲；非 ``None`` 时第二个连接被拒。
        self._active: Optional[ServerConnection] = None
        # 当前在跑的 turn task —— P3.3 cancel 入口对这个 task 做 ``task.cancel()``。
        # 单 client + 单 in-flight 策略下（``user_message`` 在 task 未结束前 drop），
        # 同时只会有一个。
        self._inflight_turn_task: Optional[asyncio.Task[None]] = None
        # P7.1.6 in-flight 类型标签——与 ``_inflight_turn_task`` 同生命周期，wrapper
        # finally 同槽清两个。**仅** ``_inbound_handoff_delegate`` 用来判"in-flight
        # 是否已是 handoff drain"，决定 cancel + 启 wrapper 还是只 append 队尾
        # （docs §4.4 反向评审 v3-#1：drain 中再 delegate 走 append 不抢占，否则
        # 队列语义崩——连点 N 次只剩最后一个执行）。
        self._inflight_kind: Optional[Literal["user", "handoff"]] = None
        # P5.2 inbound handler 五个 slot + 一次性把 wire 路由解析成 bound-method 字典。
        _install_handler_slots(self)
        self._inbound_handlers = _bind_inbound_handlers(self)

    def close(self) -> None:
        """关停**当前**活动 session。

        换房会替换 ``self._session`` 引用（旧 session 在 ``_switch_room`` 里已 close），
        所以进程退出时该关的是 server 持有的当前 session，不是 ``_serve`` 局部变量里
        最初装配的那个（那个换房后变 stale）。
        """
        self._session.close()

    async def serve_forever(self, stop: asyncio.Event) -> None:
        """起 ws server，跑到 ``stop`` 被 set。关闭由 :func:`serve` 的 ``__aexit__``
        兜底（停 accept + 等已连接客户端处理完）。
        """
        async with serve(
            self._handle, self._host, self._port, max_size=_WS_MAX_INBOUND_BYTES
        ):
            # 这行 "监听 ws://" 措辞被 app/main/sidecar.js 的 SIDECAR_READY_RE
            # 字符串匹配 —— 改文案时同步那边的正则。
            print(
                f"茶话室 server 监听 ws://{self._host}:{self._port}",
                file=sys.stderr,
            )
            spec = self._session.room_default_spec
            print(
                f"房间：{self._session.room_config.name}  "
                f"({self._session.room.latest_seq} 条历史)  "
                f"房间默认模型：{spec.provider}/{spec.model}",
                file=sys.stderr,
            )
            await stop.wait()

    # ── 单连接处理 ────────────────────────────────────────────────────

    async def _handle(self, ws: ServerConnection) -> None:
        if self._active is not None:
            await ws.close(
                CloseCode.POLICY_VIOLATION,
                "another client connected; P2.3 server accepts one client at a time",
            )
            _log.info("rejected second client from %s", ws.remote_address)
            return

        self._active = ws
        _log.info("client connected from %s", ws.remote_address)
        try:
            await self._serve_one(ws)
        except ConnectionClosed:
            # 正常断线（含 1000 / 1001）—— 不当错误。
            pass
        except Exception:
            _log.exception("connection from %s crashed", ws.remote_address)
        finally:
            self._active = None
            _log.info("client disconnected")

    async def _serve_one(self, ws: ServerConnection) -> None:
        """单个客户端的会话。

        envelope 流通过一个 :class:`asyncio.Queue` 桥接：sink 是 sync callback，
        投到 queue；后台 writer task 异步 send 到 ws。orchestrator 调用方（async
        for 循环里的 ``submit_user_message``）串行，保证 envelope 在 queue 里也按
        因果顺序到达前端。
        """
        # TODO(P3): 引入 broadcast 时改 maxsize=N + 慢客户端 close(1011)；现在单
        # 客户端 loopback 不会拥塞，无界队列简单。
        outbound: asyncio.Queue[dict] = asyncio.Queue()
        sink: EnvelopeSink = lambda env: outbound.put_nowait(env.to_dict())

        writer = asyncio.create_task(self._writer(ws, outbound), name="ws-writer")
        try:
            self._emit_room_snapshot(sink)
            async for raw in ws:
                if not isinstance(raw, str):
                    # 二进制帧不在协议里（envelope 是 JSON 文本）。
                    await ws.close(CloseCode.UNSUPPORTED_DATA, "expected text frame")
                    return
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError as e:
                    await ws.close(CloseCode.UNSUPPORTED_DATA, f"invalid JSON: {e}")
                    return
                await self._handle_inbound(data, sink)
        finally:
            # P8.3：MTS 活着 ⟺ drain task 在跑；断线必经下面 _cancel_and_drain_inflight
            # 把 drain cancel 掉，MTS 既不会自然推进也无人能停。先结束 MTS（清队列 + 置
            # None）再 cancel drain —— 否则重连后房间快照会让前端「托管中」按钮复活、
            # 却对着一个已死的调度（Codex review P2）。
            self._maybe_end_managed_session(
                sink, reason=MANAGED_SESSION_REASON_USER_CANCEL,
            )
            # 残留 turn task 必须先收掉再 cancel writer —— turn task 在被 cancel 后还要
            # 走 ``except CancelledError`` 补 turn_end(cancelled)，那条 envelope 会
            # ``put_nowait`` 进 outbound queue。先 drain producer 再砍 writer，避免
            # producer 写已"cancelling"的 writer 拿到的"task exception was never
            # retrieved"告警（websockets close 本身不依赖这条帧送达）。
            await self._cancel_and_drain_inflight()
            writer.cancel()
            try:
                await writer
            except asyncio.CancelledError:
                pass

    async def _writer(
        self, ws: ServerConnection, q: asyncio.Queue[dict]
    ) -> None:
        """从队列里取 envelope dict 序列化发 ws。一直跑到 task 被取消。"""
        try:
            while True:
                item = await q.get()
                # ensure_ascii=False —— 中文 envelope.data 直接走 UTF-8，避免
                # \uXXXX 翻倍体积 + 前端解码歧义。
                await ws.send(json.dumps(item, ensure_ascii=False))
        except ConnectionClosed:
            # 客户端先断 —— 静默退出；reader 端也会感知到。
            return

    def _emit_room_info(self, sink: EnvelopeSink) -> None:
        """转发到 :func:`server_room_snapshot.emit_room_info`。"""
        _do_emit_room_info(self, sink)

    def _emit_room_history(self, sink: EnvelopeSink) -> None:
        """转发到 :func:`server_room_snapshot.emit_room_history`。"""
        _do_emit_room_history(self, sink)

    def _switch_room(self, room_id: str, sink: EnvelopeSink) -> None:
        """换房：tear down 当前 session + 装配新房 + 重发 room_info / room_history。

        失败（room_id 不存在、room.toml 坏、LLM 凭据缺）→ WARN + 保留当前 session +
        重发当前 room_info 让前端把"切换到 X…"状态复位回"已连接"。proper 错误反馈
        wire（错误码 + 用户可见原因）留给 P3.3 跟 cancel 事件一并设计。

        ws 连接复用：``_serve_one`` 的 ``async for raw in ws`` 串行消费 inbound，
        所以 switch_room 永远在上一条 user_message 的 submit 之后才到达 ——
        天然无 race。（顺带 UX 注：长 turn 期间用户 click 切房会感觉"卡"，
        要 P3.3 cancel 完才能即时切。）
        """
        # 同房间忽略 —— 频繁 click 同一项不该 close + 重建。
        if room_id == self._session.room_config.room_dir.name:
            _log.info("switch_room: %r already current, noop", room_id)
            return
        new_room_dir = self._paths.user_data_root / "rooms" / room_id
        if not new_room_dir.is_dir():
            _log.warning("switch_room: room_id=%r 目录不存在：%s", room_id, new_room_dir)
            self._emit_room_info(sink)
            return
        if not self._replace_session(new_room_dir, sink, label=f"switch_room→{room_id!r}"):
            return
        _log.info("switch_room: → %r", room_id)
        self._emit_room_snapshot(sink)

    def _replace_session(
        self, new_room_dir: Path, sink: EnvelopeSink, *, label: str
    ) -> bool:
        """共用：装配 `new_room_dir` 的新 session 替换 `self._session`，旧 session close。

        失败 → WARN + emit 当前 room_info（让前端 UI 状态复位）+ 返回 ``False``，
        调用方自行决定后续动作。

        三处调用：换房（`_switch_room`）、加/删茶客（`_add_guest`/`_remove_guest`，
        房间路径不变 但要重建 agentao instances）、新建房 + 切换（`_create_room`）。
        """
        try:
            new_session = build_room_session(new_room_dir, paths=self._paths)
        except Exception:
            _log.exception("%s: build_room_session 失败", label)
            self._emit_room_info(sink)
            return False
        old = self._session
        # P8.3：旧 session 若有 MTS 在跑，重建会直接丢掉旧 orchestrator 的
        # _managed_session、新 orchestrator 没有 MTS —— 同房重建（加/删茶客 / 改权限
        # / 改 LLM / trust）room_id 不变，前端切房判定不触发，会一直显示「托管中」却
        # 停不掉。显式 emit managed_session_ended 让前端状态条复位（Codex review P2）。
        if old.orchestrator.managed_session is not None:
            old.orchestrator.end_managed_session(
                sink, reason=MANAGED_SESSION_REASON_USER_CANCEL,
            )
        self._session = new_session
        try:
            old.close()
        except Exception:
            _log.exception("%s: 旧 session close 出错（已切换，忽略）", label)
        return True

    def _emit_notice(self, sink: EnvelopeSink, *, level: str, text: str) -> None:
        """发一条 ``notice`` envelope —— 给 mutator 返回用户可见的成功 / 失败原因。

        不挂房间 turn，前端用 toast / alert 显示完即丢。失败时与 ``_emit_room_info``
        组合：notice 说明原因 + room_info 让按钮 / picker 状态复位。
        """
        sink(
            ChahuaEnvelope(
                room_id=self._session.room.name,
                turn_id=None,
                guest_name=None,
                message_id=None,
                type=ChahuaEventType.NOTICE,
                data={"level": level, "text": text},
            )
        )

    def _emit_room_snapshot(self, sink: EnvelopeSink) -> None:
        """转发到 :func:`server_room_snapshot.emit_room_snapshot` —— 一帧 room_info +
        room_history + task_info，三处调用点：首次连接、换房成功、清空房间。
        """
        _do_emit_room_snapshot(self, sink)


    def _clear_room(self, sink: EnvelopeSink) -> None:
        """清空当前房间公共状态 + 重发 room snapshot 让前端复位。

        不另开新 envelope 类型 —— 与换房同口径减少 wire 表面积；服务端串行 inbound 循环
        保证与 ``submit_user_message`` 互斥；编排器内部 ``_summary_task`` 由 ``reset_room``
        cancel。
        """
        self._session.orchestrator.reset_room()
        # 同步擦 debug 取证落盘 —— 否则 room_history.turns_index / fetch_turn_detail
        # 仍能把"已清"房间的老 turn 与 prompt 喂回前端。
        self._session.recorder.clear()
        _log.info("clear_room: %r 已清空", self._session.room.name)
        self._emit_room_snapshot(sink)

    # ── cancel / in-flight task 生命周期 ────────────────────────────────

    def _inflight_alive(self) -> bool:
        return self._inflight_turn_task is not None and not self._inflight_turn_task.done()

    def _set_inflight(
        self,
        task: Optional[asyncio.Task[None]],
        kind: Optional[Literal["user", "handoff"]],
    ) -> None:
        """单点设/清 ``(_inflight_turn_task, _inflight_kind)`` 这对耦合状态。

        两字段必须 same-time live or same-time None——否则 ``_inbound_handoff_delegate``
        里"in-flight 是 user-turn 才 cancel"判定会因 kind 滞后 / task 滞后
        产生与设计意图相反的行为。assertion 把漂移在写入点炸出来。
        """
        assert (task is None) == (kind is None), (task, kind)
        self._inflight_turn_task = task
        self._inflight_kind = kind

    def _cancel_inflight(self) -> None:
        """通知当前在跑的 turn task 退场，不 await —— cancel 入口要尽快返回让 inbound
        循环继续消费帧。task 完成由 ``_run_turn.finally`` 清 ``_inflight_turn_task``。
        """
        task = self._inflight_turn_task
        if task is not None and not task.done():
            task.cancel()

    async def _cancel_and_drain_inflight(self) -> None:
        """cancel 当前 turn task **并等它收尾**。switch_room / clear_room / 连接断开走这
        条路径 —— 它们要在 task 完全退出后再继续操作 session，否则 orchestrator 还在写
        transcript / cursor，新 session 装配会撞上。
        """
        task = self._inflight_turn_task
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            # ``_run_turn`` 自己 swallow 了 Cancelled / Exception，正常情况这里 await
            # 不会再抛；保留 CancelledError 兜底是为 cancel→reraise 极小竞态窗口。
            pass

    async def _run_turn(
        self, text: str, sink: EnvelopeSink, *, task_id: Optional[str],
    ) -> None:
        """承载一条 user_message 的整个 AI 链。挂在 ``_inflight_turn_task`` 上让 cancel
        入口能 ``task.cancel()`` 它。

        - CancelledError：``orchestrator._run_ai_chain`` 在补完 ``turn_end(cancelled)``
          后 reraise；这里 swallow 让 task 正常完成。
        - 其它异常：兜底 log + swallow，避免 task 异常逃逸触发 asyncio "Task exception
          was never retrieved" warning。

        ``task_id`` 由 :meth:`_inbound_user_message` 在接帧同步上下文里快照后传入 —— 不能
        在本协程里读 ``tasks_store.active_task_id`` 兜底，那样会与 inbound 队列里排在后面
        的 ``open_task`` 帧形成 race。
        """
        try:
            await self._session.orchestrator.submit_user_message(
                text, sink=sink, task_id=task_id,
            )
        except asyncio.CancelledError:
            _log.info("turn cancelled by user")
        except Exception:
            _log.exception("submit_user_message crashed")
        finally:
            self._set_inflight(None, None)

    async def _run_handoff_turn(
        self, sink: EnvelopeSink, *, task_id: Optional[str],
    ) -> None:
        """承载一次 handoff drain。结构照搬 :meth:`_run_turn`：cancel-safe + finally
        同槽清两个，让 cancel / busy 判定与 user-turn 同口径（docs §3.4 反向评审 v3-#3）。
        """
        try:
            await self._session.orchestrator.run_pending_handoff(
                sink, task_id=task_id,
            )
        except asyncio.CancelledError:
            _log.info("handoff drain cancelled by user")
        except Exception:
            _log.exception("handoff drain crashed")
        finally:
            self._set_inflight(None, None)

    async def _handle_inbound(self, data: dict, sink: EnvelopeSink) -> None:
        """分派一条客户端消息到对应 handler。

        各 handler 自己做 payload 校验（多走 :func:`_require_str` / :func:`_require_bool`
        简化"missing/非字符串"分支），校验失败 → WARN + 早返；动会话的（add/remove
        guest、switch_room 等）调用前过 :meth:`_cancel_and_drain_inflight`，不动会话
        的（upload / 头像 / persona import）直接执行让在飞 turn 自然收尾。

        Dispatch 表 ``self._inbound_handlers`` 在 ``__init__`` 里由
        :func:`_bind_inbound_handlers` 一次性把 :data:`_INBOUND_ROUTES` 解析成 bound
        method 字典；这里直接 dict lookup → ``await``。
        """
        msg_type = data.get("type")
        handler = self._inbound_handlers.get(msg_type)
        if handler is not None:
            await handler(data, sink)
            return
        # 友好容忍：未知 type 不断连，仅 WARN。前端在协议升级期发新 type 时
        # 服务端旧版本也不至于把它踢下线。
        _log.warning("ignoring inbound message of unknown type=%r", msg_type)

    # ── 各 inbound 帧的 handler；wire 路由表 :data:`_INBOUND_ROUTES` 在文件底部。────

    async def _inbound_cancel(self, data: dict, sink: EnvelopeSink) -> None:
        # turn_id 由前端塞，服务端只记日志：单 in-flight 模型下当前 task 必定就是
        # 前端能看到 turn_id 的那个；race 窗口（前一 turn 刚 end / 下一 turn 刚
        # start 之间）下错杀也只是少说半句话，无 transcript 污染。
        turn_id = data.get("turn_id")
        if not self._inflight_alive():
            _log.info("cancel ignored: no in-flight turn (turn_id=%r)", turn_id)
            # P8.3：无 in-flight 但 MTS 还活着（如 stop 后队列已清、本轮自然跑完后
            # 用户再点 cancel）—— 仍结束 MTS 让状态条收起。
            self._maybe_end_managed_session(
                sink, reason=MANAGED_SESSION_REASON_USER_CANCEL,
            )
            return
        _log.info("cancel: turn_id=%r", turn_id)
        self._cancel_inflight()
        # P8.3：取消当前 turn 中途介入托管会话 → 一并结束 MTS（user_cancel）；
        # end_managed_session 清 _handoff_queue —— 已自动入队没跑的 worker 不再跑
        # （docs §3.3）。被 cancel 的 turn 自己走 cancel fixup，不在这里处理。
        self._maybe_end_managed_session(
            sink, reason=MANAGED_SESSION_REASON_USER_CANCEL,
        )

    def _maybe_end_managed_session(self, sink: EnvelopeSink, *, reason: str) -> None:
        """若当前有托管会话在跑则结束它（P8.3）。``cancel`` 中途介入用。

        ``end_managed_session`` 自带「无 MTS 时空操作」守卫；本方法多一层是为给
        ``test_server_inbound`` 的路由 spy 一个干净的覆盖点（spy 刻意不挂 ``_session``）。
        """
        orch = self._session.orchestrator
        if orch.managed_session is not None:
            orch.end_managed_session(sink, reason=reason)

    async def _inbound_switch_room(self, data: dict, sink: EnvelopeSink) -> None:
        room_id = _require_str(data, "room_id", where=INBOUND_SWITCH_ROOM)
        if room_id is None:
            return
        await self._cancel_and_drain_inflight()
        self._switch_room(room_id, sink)

    async def _inbound_clear_room(self, data: dict, sink: EnvelopeSink) -> None:
        await self._cancel_and_drain_inflight()
        self._clear_room(sink)

    async def _inbound_fetch_turn_detail(
        self, data: dict, sink: EnvelopeSink,
    ) -> None:
        """按 ``turn_id`` 查 ``debug/turns.jsonl`` + 读关联 prompt 文件后回 TURN_DETAIL。

        P6.3.A 行为约束（docs/P6.3 §4.2 + §10 不变量）：

        - 字段白名单严格 → 未知键 NOTICE error + 丢帧（同 task inbound 口径）。
        - ``turn_id`` regex 校验 ``^turn_[0-9a-f]+$``，拒穿越 / 空段（这是路径片段，
          后端 ``prompts/<turn_id>/*`` 直接拼字符串）；非法 → NOTICE error。
        - ``debug.enabled=False`` / 未扫到 / rotation 清掉 → ``data={"found": False}``
          **不** emit NOTICE（前端协议过期是预期场景，抖 user 体验恼人）。
        - happy path → ``data={"found": True, "turn": <row>, "prompts": <dict>}``，
          ``prompts`` 字段始终存在（最少 ``{}``，便于前端代码统一访问）。
        - 不挂房间 turn / 不进 in-flight 流程（取证读盘，不动 transcript / cursor）。
        """
        if not self._reject_unknown_keys(
            data, frozenset({"type", "turn_id"}),
            where=INBOUND_FETCH_TURN_DETAIL, sink=sink,
        ):
            return
        turn_id = _require_str(data, "turn_id", where=INBOUND_FETCH_TURN_DETAIL)
        if turn_id is None:
            return
        if not _TURN_ID_RE.fullmatch(turn_id):
            self._emit_notice(
                sink, level=NOTICE_LEVEL_ERROR,
                text=f"{INBOUND_FETCH_TURN_DETAIL}: 非法 turn_id={turn_id!r}",
            )
            return
        room_id = self._session.room.name

        def _emit(data_payload: dict) -> None:
            sink(ChahuaEnvelope(
                room_id=room_id, turn_id=turn_id, guest_name=None,
                message_id=None, type=ChahuaEventType.TURN_DETAIL,
                data=data_payload,
            ))

        recorder = self._session.recorder
        if not recorder.enabled:
            _emit({"found": False})
            return
        turn, prompts = recorder.load_turn(turn_id)
        if turn is None:
            _emit({"found": False})
            return
        _emit({"found": True, "turn": turn, "prompts": prompts})

    async def _inbound_list_guest_caps(
        self, data: dict, sink: EnvelopeSink,
    ) -> None:
        """``{"type":"list_guest_caps","guest":"<name>","view":"tools"|"skills"}``
        → 回 GUEST_CAPS_INFO。

        ``/tools`` / ``/skills`` slash 查询的只读 introspection：回茶客 agent 注册的
        tools + 可用 skills + 权限模式。不挂房间 turn / 不进 transcript / 不动
        in-flight。guest 走 :meth:`Orchestrator.get_guest`（反映运行时增删，不读
        ``RoomSession.guests`` boot 快照）；不在场 → NOTICE error。

        ``view`` 是纯展示回声 —— 前端按它裁剪显示 tools / skills 段。server 不解释、
        只规范化后原样回传：把"查的是哪段"绑在每个响应里，多查询并发时前端不靠一个
        可变全局态对号入座（否则两条 in-flight 查询会串台）。
        """
        if not self._reject_unknown_keys(
            data, _LIST_GUEST_CAPS_ALLOWED,
            where=INBOUND_LIST_GUEST_CAPS, sink=sink,
        ):
            return
        guest_name = _require_str(data, "guest", where=INBOUND_LIST_GUEST_CAPS)
        if guest_name is None:
            return
        guest = self._session.orchestrator.get_guest(guest_name)
        if guest is None:
            self._emit_notice(
                sink, level=NOTICE_LEVEL_ERROR,
                text=f"{INBOUND_LIST_GUEST_CAPS}: guest={guest_name!r} 不在场",
            )
            return
        caps = guest.describe_capabilities()
        caps["view"] = "skills" if data.get("view") == "skills" else "tools"
        sink(ChahuaEnvelope(
            room_id=self._session.room.name, turn_id=None, guest_name=None,
            message_id=None, type=ChahuaEventType.GUEST_CAPS_INFO,
            data=caps,
        ))

    # ── P5.1.7 任务 inbound（docs/P5-任务房间.md §4.3 P5.1 段）─────────────
    #
    # 共通契约：
    #   - **入站严格**：payload 顶层只接白名单字段（详见每条 ``_ALLOWED`` 常量）；多余键
    #     直接 NOTICE error + 丢帧。等价 docs §8.1 "入站严格 / 落盘宽容"。
    #   - **不挡 inflight turn**：与 upload / 头像 / persona import 同口径 —— 让在跑的
    #     turn 自然完成；任务 mutator 改的是 :class:`TasksStore` 镜像，与 transcript
    #     append 不冲突。
    #   - **成功路径**：emit hint 事件（``task_open`` / ``task_update`` / 等）+ 重发整份
    #     ``task_info``（权威快照）。前端任务状态以最近 ``task_info`` 为准（§4.2 事件分工）。

    def _reject_unknown_keys(
        self,
        data: dict,
        allowed: frozenset[str],
        *,
        where: str,
        sink: EnvelopeSink,
    ) -> bool:
        """Return False + NOTICE error 当 payload 含 ``allowed`` 之外的顶层键。
        """
        err = _check_keys_whitelist(data, allowed, where=where)
        if err is None:
            return True
        self._emit_notice(sink, level=NOTICE_LEVEL_ERROR, text=err)
        return False

    def _notice_persist_failure(
        self, sink: EnvelopeSink, where: str, err: OSError,
    ) -> None:
        """tasks/ 落盘出错（disk full / 权限）时给前端发可读 NOTICE，而不是让异常逃逸
        ``_handle_inbound`` 让 ws 断线。partial-write 留下的内存 vs state.json 不一致，
        重启时靠 :meth:`TasksStore._load` 的双向修复兜底（docs §10 § "tasks/state.json
        与 <id>/task.json 不一致"）。
        """
        _log.exception("%s: 落盘失败", where)
        self._emit_notice(
            sink, level=NOTICE_LEVEL_ERROR,
            text=f"{where}: 落盘失败（{err}）；请检查磁盘空间 / 目录权限",
        )

    async def _inbound_user_message(self, data: dict, sink: EnvelopeSink) -> None:
        text = data.get("text")
        if not isinstance(text, str):
            _log.warning(
                "ignoring %s: text 必须是 str，收到 %r",
                INBOUND_USER_MESSAGE, type(text),
            )
            return
        files = data.get("files")
        text = _attach_files_to_text(text, files)
        if not text:
            # 用户既没打字也没附文件 —— 没东西可投。
            _log.warning("ignoring %s: 空 text + 无 files", INBOUND_USER_MESSAGE)
            return
        if self._inflight_alive():
            # 单 in-flight 严格策略：当前 turn 没结束前 drop 后续 user_message。前端
            # composer 在 turn_start / turn_end 之间禁用，正常情况打不到这条；防御性保护
            # 老前端 / wscat 直发场景。
            _log.warning("user_message dropped: previous turn still in flight")
            return
        snapshot_task_id = self.task._snapshot_active_task_id()
        self._set_inflight(
            asyncio.create_task(
                self._run_turn(text, sink, task_id=snapshot_task_id),
                name="chahua-turn",
            ),
            INFLIGHT_KIND_USER,
        )


_InboundHandler = Callable[[dict, EnvelopeSink], Awaitable[None]]

# wire 字符串 → 属性路径（解析 ``self`` 上的 attrgetter 串）。加新 wire 帧只动
# INBOUND_* 常量 + 对应 ``_inbound_<name>`` 方法 + 这张表一行。``__init__`` 期间用
# :func:`_bind_inbound_handlers` 一次把它转成 bound-method 字典装上 ``self._inbound_handlers``。
_INBOUND_ROUTES: dict[str, str] = {
    # 核心 4 个：cancel / switch_room / clear_room / user_message 留在 ChahuaServer。
    # P6.3.A 加 fetch_turn_detail 也归核心层（debug 取证不属任何 feature slot —— 不
    # 与 admin / task / io / settings 同维度）。
    INBOUND_CANCEL: "_inbound_cancel",
    INBOUND_SWITCH_ROOM: "_inbound_switch_room",
    INBOUND_CLEAR_ROOM: "_inbound_clear_room",
    INBOUND_USER_MESSAGE: "_inbound_user_message",
    INBOUND_FETCH_TURN_DETAIL: "_inbound_fetch_turn_detail",
    INBOUND_LIST_GUEST_CAPS: "_inbound_list_guest_caps",
    # handoff slot：delegate / review / panel / clear（P7 调度层 inbound）。
    INBOUND_HANDOFF_DELEGATE: "handoff._inbound_handoff_delegate",
    INBOUND_HANDOFF_REVIEW: "handoff._inbound_handoff_review",
    INBOUND_HANDOFF_PANEL: "handoff._inbound_handoff_panel",
    INBOUND_HANDOFF_CLEAR: "handoff._inbound_handoff_clear",
    # P8.3 托管会话——归 handoff slot（MTS 跑在 handoff drain loop 上）。
    INBOUND_MANAGED_SESSION_START: "handoff._inbound_managed_session_start",
    INBOUND_MANAGED_SESSION_STOP: "handoff._inbound_managed_session_stop",
    # admin slot：guest / room / persona / permission。
    INBOUND_ADD_GUEST: "admin._inbound_add_guest",
    INBOUND_REMOVE_GUEST: "admin._inbound_remove_guest",
    INBOUND_SET_PERSONA_MCP_TRUST: "admin._inbound_set_persona_mcp_trust",
    INBOUND_UPDATE_GUEST_PERMISSION: "admin._inbound_update_guest_permission",
    INBOUND_CREATE_ROOM: "admin._inbound_create_room",
    INBOUND_DELETE_ROOM: "admin._inbound_delete_room",
    INBOUND_UPDATE_ROOM_ORCHESTRATOR: "admin._inbound_update_room_orchestrator",
    INBOUND_UPDATE_ROOM_LLM: "admin._inbound_update_room_llm",
    INBOUND_UPDATE_GUEST_LLM: "admin._inbound_update_guest_llm",
    INBOUND_UPDATE_GUEST_ISOLATION: "admin._inbound_update_guest_isolation",
    INBOUND_UPDATE_GUEST_EXTRA_MCP: "admin._inbound_update_guest_extra_mcp",
    # settings slot：USER.md / 房间 toml / 用户头像。
    INBOUND_UPDATE_USER_MD: "settings._inbound_update_user_md",
    INBOUND_UPDATE_ROOM_TOML: "settings._inbound_update_room_toml",
    INBOUND_UPDATE_USER_AVATAR: "settings._inbound_update_user_avatar",
    # io slot：persona import / 文件上传 / 房间导出。
    INBOUND_IMPORT_PERSONA_FOLDER: "io._inbound_import_persona_folder",
    INBOUND_IMPORT_PERSONA_GITHUB: "io._inbound_import_persona_github",
    INBOUND_UPLOAD_FILE: "io._inbound_upload_file",
    INBOUND_EXPORT_ROOM: "io._inbound_export_room",
    INBOUND_DOWNLOAD_FILE: "io._inbound_download_file",
    # task slot：任务房间六个 inbound（P5.2.5 起多 set_active_task / close_task）。
    INBOUND_OPEN_TASK: "task._inbound_open_task",
    INBOUND_UPDATE_TASK: "task._inbound_update_task",
    INBOUND_ATTACH_ARTIFACT: "task._inbound_attach_artifact",
    INBOUND_ADD_DECISION: "task._inbound_add_decision",
    INBOUND_SET_ACTIVE_TASK: "task._inbound_set_active_task",
    INBOUND_CLOSE_TASK: "task._inbound_close_task",
    INBOUND_CLEAR_TASK_ARTIFACTS: "task._inbound_clear_task_artifacts",
}


def _install_handler_slots(srv: ChahuaServer) -> None:
    """装五个 handler slot。``ChahuaServer.__init__`` 与 ``object.__new__`` 跳 __init__
    的测试夹具共用 —— 唯一真理源，将来加 slot 这里一处加完即可。
    """
    srv.admin = AdminHandlers(srv)
    srv.io = IOHandlers(srv)
    srv.settings = SettingsHandlers(srv)
    srv.task = TaskHandlers(srv)
    srv.handoff = HandoffHandlers(srv)


def _bind_inbound_handlers(srv: ChahuaServer) -> dict[str, _InboundHandler]:
    """按 :data:`_INBOUND_ROUTES` 把属性路径解析成 bound method 字典。

    调用方需先装好 slot（``srv.admin`` / ``srv.io`` 等）；spy 测试夹具自己装完 spy slot
    后调本函数取分派表。每条路径通过 :func:`operator.attrgetter` 解析 —— 错路径在
    server 启动期就 AttributeError 而不是首次该 inbound 进来时才炸。
    """
    return {
        wire: operator.attrgetter(path)(srv)
        for wire, path in _INBOUND_ROUTES.items()
    }


# ── 入口 ──────────────────────────────────────────────────────────────────
#
# 进程生命周期层（argv / serve / stdin EOF / parent-pid watch / Windows tree-kill）
# 全部在 :mod:`chahua.server_entry`。``server.py`` **不在顶层 import server_entry**
# 否则循环：``server_entry`` 顶头 ``from .server import ChahuaServer`` 在 ``python -m
# chahua.server`` 时会双装载 server.py（一次作 ``__main__``、一次作 ``chahua.server``），
# 第二次到底部 reexport 时 ``server_entry`` 还没定义到那些名字 → ``ImportError``。
#
# 入口分两条都走 ``chahua.server_entry``：
#   1. CLI 脚本：pyproject ``chahua-server = "chahua.server_entry:main"``
#   2. sidecar 在 Electron 内：``python -m chahua.server`` 进 ``if __name__`` 走延迟
#      import（也可改 ``python -m chahua.server_entry``，已为兼容老路径保留）

if __name__ == "__main__":
    from .server_entry import main as _main  # noqa: E402

    _main()
