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

import argparse
import asyncio
import ctypes
import json
import logging
import operator
import os
import signal
import sys
from pathlib import Path
from typing import Awaitable, Callable, Optional

from websockets import CloseCode
from websockets.asyncio.server import ServerConnection, serve
from websockets.exceptions import ConnectionClosed

from ._paths import Paths, resolve_under
from ._server_helpers import (
    check_keys_whitelist as _check_keys_whitelist,
    require_str as _require_str,
)
from .config import RoomConfigError
from .events import (
    ChahuaEnvelope,
    ChahuaEventType,
    EnvelopeSink,
    NOTICE_LEVEL_ERROR,
    NOTICE_LEVEL_INFO,
)
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
    INBOUND_CLOSE_TASK,
    INBOUND_OPEN_TASK,
    INBOUND_SET_ACTIVE_TASK,
    INBOUND_UPDATE_TASK,
    TaskHandlers,
)
from .session import (
    DEFAULT_ROOM_REL,
    RoomSession,
    build_room_session,
    discover_rooms,
    load_env_files,
)

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
# P5.2 重构（docs/P5-任务房间.md §7.2）：admin / io / settings / task 四类 inbound
# 由独立 handler 类承担，:class:`ChahuaServer` 在 ``__init__`` 里实例化为四个 slot。
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

    P5.2 起 inbound handler 按 feature 切到四个独立类（:class:`AdminHandlers` /
    :class:`IOHandlers` / :class:`SettingsHandlers` / :class:`TaskHandlers`），各持
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
        # P5.2 inbound handler 四个 slot + 一次性把 wire 路由解析成 bound-method 字典。
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
        _log.info("clear_room: %r 已清空", self._session.room.name)
        self._emit_room_snapshot(sink)

    # ── cancel / in-flight task 生命周期 ────────────────────────────────

    def _inflight_alive(self) -> bool:
        return self._inflight_turn_task is not None and not self._inflight_turn_task.done()

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

    async def _kick_synthesized_user_message(
        self,
        text: str,
        sink: EnvelopeSink,
        *,
        task_id: Optional[str],
    ) -> None:
        """系统侧合成的"用户消息"（P5.5，docs/P5.5-任务事件全房广播.md §4.1）。

        语义：UI 上按钮操作 = 用户行为，紧跟一句话进 transcript / orchestrator
        scoring 循环，让茶客知道刚发生了什么。``text`` 由调用方（task handler / 茶客
        自动归集）构造（emoji 前缀 + 任务摘要），不来自 ws 帧。

        与 :meth:`_inbound_user_message` 的差异：
          - 串行口径相同：先 ``_cancel_and_drain_inflight`` 再创建新 turn，与
            add_guest / set_active_task 同口径 —— 任何改变房间状态的用户操作都
            重启 in-flight turn。
          - 不 emit user envelope：与真用户消息口径一致（``submit_user_message``
            不 emit，靠前端 local echo 显示气泡）；合成路径无 echo，本会话内
            不进聊天气泡，切房 / 刷新后从 ``room_history`` 重建时才出现。
        """
        await self._cancel_and_drain_inflight()
        self._inflight_turn_task = asyncio.create_task(
            self._run_turn(text, sink, task_id=task_id),
            name="chahua-synth-turn",
        )

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
            self._inflight_turn_task = None

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
            return
        _log.info("cancel: turn_id=%r", turn_id)
        self._cancel_inflight()

    async def _inbound_switch_room(self, data: dict, sink: EnvelopeSink) -> None:
        room_id = _require_str(data, "room_id", where=INBOUND_SWITCH_ROOM)
        if room_id is None:
            return
        await self._cancel_and_drain_inflight()
        self._switch_room(room_id, sink)

    async def _inbound_clear_room(self, data: dict, sink: EnvelopeSink) -> None:
        await self._cancel_and_drain_inflight()
        self._clear_room(sink)

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
        self._inflight_turn_task = asyncio.create_task(
            self._run_turn(text, sink, task_id=snapshot_task_id),
            name="chahua-turn",
        )


_InboundHandler = Callable[[dict, EnvelopeSink], Awaitable[None]]

# wire 字符串 → 属性路径（解析 ``self`` 上的 attrgetter 串）。加新 wire 帧只动
# INBOUND_* 常量 + 对应 ``_inbound_<name>`` 方法 + 这张表一行。``__init__`` 期间用
# :func:`_bind_inbound_handlers` 一次把它转成 bound-method 字典装上 ``self._inbound_handlers``。
_INBOUND_ROUTES: dict[str, str] = {
    # 核心 4 个：cancel / switch_room / clear_room / user_message 留在 ChahuaServer。
    INBOUND_CANCEL: "_inbound_cancel",
    INBOUND_SWITCH_ROOM: "_inbound_switch_room",
    INBOUND_CLEAR_ROOM: "_inbound_clear_room",
    INBOUND_USER_MESSAGE: "_inbound_user_message",
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
}


def _install_handler_slots(srv: ChahuaServer) -> None:
    """装四个 handler slot。``ChahuaServer.__init__`` 与 ``object.__new__`` 跳 __init__
    的测试夹具共用 —— 唯一真理源，将来加 slot 这里一处加完即可。
    """
    srv.admin = AdminHandlers(srv)
    srv.io = IOHandlers(srv)
    srv.settings = SettingsHandlers(srv)
    srv.task = TaskHandlers(srv)


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


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="chahua-server",
        description="多 Agent 群聊「茶话室」WebSocket server（P2.3）",
    )
    parser.add_argument(
        "--room",
        type=Path,
        default=DEFAULT_ROOM_REL,
        help=(
            f"房间目录，含 room.toml（默认 {DEFAULT_ROOM_REL}）。"
            f"相对路径相对 user_data_root（CHAHUA_USER_DATA 或 dev 仓库根），"
            f"绝对路径原样。"
        ),
    )
    parser.add_argument(
        "--host",
        default=DEFAULT_HOST,
        help=f"绑定地址（默认 {DEFAULT_HOST}，仅本机回环）",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help=(
            f"监听端口（默认从 CHAHUA_WS_PORT 读，未设则 {DEFAULT_PORT}）"
        ),
    )
    return parser.parse_args(argv)


async def _serve(args: argparse.Namespace) -> int:
    paths = Paths.from_env()
    load_env_files(paths)

    room_dir = resolve_under(paths.user_data_root, args.room)
    try:
        session = build_room_session(room_dir, paths=paths)
    except RoomConfigError as e:
        print(f"房间配置错误：\n{e}", file=sys.stderr)
        return 2

    # 端口优先级：CLI > env > 默认。
    port = args.port
    if port is None:
        env_port = os.environ.get("CHAHUA_WS_PORT")
        port = int(env_port) if env_port else DEFAULT_PORT

    # 启动日志打出两个 root —— 打包后区分 dev / packaged 路径走的是哪条，排查方便。
    if paths.app_root != paths.user_data_root:
        print(f"app_root      : {paths.app_root}", file=sys.stderr)
        print(f"user_data_root: {paths.user_data_root}", file=sys.stderr)

    server = ChahuaServer(session, host=args.host, port=port, paths=paths)
    stop = asyncio.Event()

    # 三条触发 stop 的路径，覆盖所有"父进程要我停"语义：
    #
    # 1. SIGINT / SIGTERM signal handler —— CLI 用户 Ctrl-C / `kill <pid>` 进来，
    #    asyncio loop 内 add_signal_handler 设 stop。Windows 不支持 add_signal_handler
    #    （只能通过 ProactorEventLoop + 老 signal 模块的两段式 trick），P3.3.2.d 走
    #    第 3 条 stdin EOF 路径替代 —— 跨平台一致、还不踩 Windows 信号坑。
    #
    # 2. KeyboardInterrupt —— 上层 main() catch，stop 来不及 set 但 asyncio.run
    #    会 cancel 所有 task。
    #
    # 3. stdin EOF watcher —— Electron 关 sidecar 时 child.stdin.end() 关写端，
    #    Python 这边 sys.stdin 读到 EOF → set stop。stdin 是 tty 时（CLI 交互模式）
    #    不装这个 watcher，避免乱抢用户敲的字符。
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass

    # 平台分流：POSIX 走 stdin EOF（child.stdin.end() → 收 EOF → set stop）；Windows
    # ProactorEventLoop 上 connect_read_pipe(sys.stdin) 拿 WinError 6 静默挂掉，stdin
    # 路径形同虚设，改走 OpenProcess + WaitForSingleObject 监 Electron owner PID。
    stdin_watcher_task: Optional[asyncio.Task] = None
    if os.name != "nt" and not sys.stdin.isatty():
        stdin_watcher_task = asyncio.create_task(_watch_stdin_eof(stop))
    parent_watcher_task: Optional[asyncio.Task] = None
    if os.name == "nt":
        owner_pid = _owner_pid_from_env()
        if owner_pid > 0:
            parent_watcher_task = asyncio.create_task(
                _watch_parent_process(stop, owner_pid)
            )

    try:
        await server.serve_forever(stop)
    finally:
        # 关 server 持有的当前 session（换房后 self._session 已不是局部 `session`）。
        server.close()
        if stdin_watcher_task and not stdin_watcher_task.done():
            stdin_watcher_task.cancel()
        if parent_watcher_task and not parent_watcher_task.done():
            parent_watcher_task.cancel()
    return 0


async def _watch_stdin_eof(stop: asyncio.Event) -> None:
    """监 sys.stdin EOF，作为跨平台 sidecar 优雅关停信号。

    Electron main 进程关 sidecar 前调 ``child.stdin.end()`` 关 stdin pipe 的写端
    （sidecar.js:stop）；Python 这边读到 EOF 即 set stop，server.serve_forever 返回
    → 整套 graceful 关。Windows 的 ``child.kill("SIGINT")`` 实际是
    TerminateProcess（不 graceful），全靠这条路径替代。

    ``connect_read_pipe`` 在 Unix / Windows ProactorEventLoop 都支持；少数边角设置下
    可能失败（如 dev tty 模式调到这里，但我们已经在调用方 ``isatty()`` 过滤；保留
    try/except 兜底 OSError / NotImplementedError）。
    """
    log = logging.getLogger(__name__)
    loop = asyncio.get_running_loop()
    try:
        reader = asyncio.StreamReader(loop=loop)
        protocol = asyncio.StreamReaderProtocol(reader, loop=loop)
        await loop.connect_read_pipe(lambda: protocol, sys.stdin)
    except (OSError, NotImplementedError) as e:
        log.debug("stdin watcher disabled: %s", e)
        return

    # 父进程往 stdin 写东西不该触发关停（保留未来"命令通道"扩展空间，比如 main
    # 想塞个 ``{"type":"switch_room"}`` 进来）—— 只 EOF（空 bytes）才 set stop。
    while not stop.is_set():
        try:
            data = await reader.read(1024)
        except asyncio.CancelledError:
            return
        if not data:
            log.info("stdin EOF received; shutting down")
            stop.set()
            return


def _owner_pid_from_env() -> int:
    """Electron 通过 ``CHAHUA_PARENT_PID`` 显式喂自己的 PID 给 sidecar 用作 owner。

    返回 0 = 没有可监控的 owner（独立跑 ``uv run chahua-server`` 时常态）。**不**回退
    到 ``os.getppid()`` —— dev 模式 ppid 指向 wrapper（``uv.exe`` / shell），监它退出
    会让 sidecar 在不该退的时刻退（比如 PowerShell 关掉但 Electron 还活着）。
    """
    raw = os.environ.get("CHAHUA_PARENT_PID")
    if not raw:
        return 0
    try:
        pid = int(raw)
    except ValueError:
        logging.getLogger(__name__).debug("invalid CHAHUA_PARENT_PID=%r", raw)
        return 0
    return pid if pid > 0 else 0


async def _watch_parent_process(stop: asyncio.Event, parent_pid: int) -> None:
    """Windows 下监 owner 进程退出 → set stop。

    OpenProcess + WaitForSingleObject 的同步阻塞调用走 ``asyncio.to_thread`` 丢到
    执行线程，await 完成后回到事件循环主线程继续 set stop —— 不必走
    ``call_soon_threadsafe``。task 在 ``_serve.finally`` 里 cancel；CancelledError
    silent return，避免进程正常退出时这边补 ERROR 日志。
    """
    log = logging.getLogger(__name__)
    try:
        await asyncio.to_thread(_wait_for_parent_exit_windows, parent_pid)
    except asyncio.CancelledError:
        return
    except Exception as e:
        log.debug("parent watcher disabled: %s", e)
        return
    if not stop.is_set():
        log.info("parent process exited; shutting down")
        stop.set()


def _wait_for_parent_exit_windows(parent_pid: int) -> None:
    """阻塞等待 Windows owner 进程退出。仅由 :func:`_watch_parent_process` via to_thread 调用。

    ctypes argtypes / restype 必须显式声明：64 位 Windows 上 ``HANDLE`` 是指针
    （8 字节），ctypes 默认按 C ``int``（4 字节）截断，handle 高位被砍后
    ``WaitForSingleObject`` 拿到坏 handle 立刻返 ``WAIT_FAILED`` 让 sidecar 启动
    秒退。这是隐性 bug，不写 argtypes 在小 PID 下偶然能跑、handle 高位非零时翻车。
    """
    if parent_pid <= 0:
        return

    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    k32.WaitForSingleObject.restype = wintypes.DWORD
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    k32.CloseHandle.restype = wintypes.BOOL

    SYNCHRONIZE = 0x00100000
    INFINITE = 0xFFFFFFFF

    handle = k32.OpenProcess(SYNCHRONIZE, False, parent_pid)
    if not handle:
        # PID 不存在 / 权限不足 —— 没法监控就不监，不当 ERROR（owner 已经死了
        # 也走这条路径，调用方靠 stop 没被 set 来推断）。
        return
    try:
        k32.WaitForSingleObject(handle, INFINITE)
    finally:
        k32.CloseHandle(handle)


def main() -> None:
    """``chahua-server`` 命令入口。"""
    args = _parse_args(sys.argv[1:])
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    try:
        rc = asyncio.run(_serve(args))
    except KeyboardInterrupt:
        rc = 130
    sys.exit(rc)


if __name__ == "__main__":
    main()
