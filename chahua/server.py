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
import json
import logging
import os
import signal
import sys
from pathlib import Path
from typing import Optional

from websockets import CloseCode
from websockets.asyncio.server import ServerConnection, serve
from websockets.exceptions import ConnectionClosed

from ._paths import resolve_under
from .config import RoomConfigError
from .events import ChahuaEnvelope, ChahuaEventType, EnvelopeSink
from .session import (
    DEFAULT_ROOM_REL,
    RoomSession,
    build_room_session,
    discover_rooms,
    find_repo_root,
    load_env_files,
)

_log = logging.getLogger(__name__)


DEFAULT_PORT = 7860
DEFAULT_HOST = "127.0.0.1"

# 客户端 → 服务端 message type 字段值。
INBOUND_USER_MESSAGE = "user_message"
INBOUND_SWITCH_ROOM = "switch_room"
INBOUND_CLEAR_ROOM = "clear_room"
INBOUND_CANCEL = "cancel"


# ── server ────────────────────────────────────────────────────────────────


class ChahuaServer:
    """单房间 ws server。

    在同一 session 上跨多次客户端连接复用 —— 客户端断开后房间状态保留，下次连上
    就是续聊（与 :mod:`chahua.cli` 的 ``/quit`` → 重启 → 续聊一个意思）。

    P3.2.x 加 :meth:`_switch_room` 支持运行时换房：tear down 当前 session（
    所有茶客 agentao close），按新 ``room_id`` 装配新 session 替换 ``_session``，
    复用 ws 连接 + ``_emit_room_info`` / ``_emit_room_history`` —— 客户端拿到
    新 room_info + 全量历史回放，DOM ``replaceChildren`` 自动清掉前一个房间残留。
    """

    def __init__(
        self,
        session: RoomSession,
        *,
        host: str,
        port: int,
        repo_root: Path,
    ) -> None:
        self._session = session
        self._host = host
        self._port = port
        self._repo_root = repo_root
        # 当前在线的客户端句柄。``None`` 表示空闲；非 ``None`` 时第二个连接被拒。
        self._active: Optional[ServerConnection] = None
        # 当前在跑的 turn task —— P3.3 cancel 入口对这个 task 做 ``task.cancel()``。
        # 单 client + 单 in-flight 策略下（``user_message`` 在 task 未结束前 drop），
        # 同时只会有一个。
        self._inflight_turn_task: Optional[asyncio.Task[None]] = None

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
        async with serve(self._handle, self._host, self._port):
            # 这行 "监听 ws://" 措辞被 app/main/sidecar.js 的 SIDECAR_READY_RE
            # 字符串匹配 —— 改文案时同步那边的正则。
            print(
                f"茶话室 server 监听 ws://{self._host}:{self._port}",
                file=sys.stderr,
            )
            print(
                f"房间：{self._session.room_config.name}  "
                f"({self._session.room.latest_seq} 条历史)  "
                f"provider：{self._session.provider}",
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
        """ws 连上即下发一次 ``room_info`` —— 前端拿来装 sidebar / @ 补全候选。

        茶客信息从 ``room_config.guests``（``GuestConfig``）取而非运行时
        ``session.guests``（``TeaGuest``）—— 与 ``rc.name`` / ``rc.topic`` 同走声明源；
        P4 加 ``[[guest]].isolation`` 字段后这里读真值，前端按 ``data-isolation`` 渲染
        无需改。P3.2.2 内 ``isolation="room"`` hardcode 占位。

        ``avatar_data_uri`` / ``user_avatar_data_uri``：茶客 / 用户头像，与对应 md sibling
        同名 ``.png``，base64 嵌进 envelope。缺图返 ``None``，前端按无头像渲染。

        ``rooms_available`` / ``current_room_id``：P3.2.x 切房功能的 wire 输入 —— 前端
        sidebar 列其它房间供切换。``current_room_id`` 用房间目录名（P4 才有
        ``[room].id`` 稳定字段，与 envelope ``room_id`` 占位口径一致）。

        副作用：runtime 期 ``permission`` 切换（P4 才支持）不会反映到 sidebar —— 届时加
        ``room_info_delta`` wire 帧增量下发。
        """
        rc = self._session.room_config
        guests = [
            {
                "name": gc.name,
                "permission": gc.permission,
                "isolation": "room",
                "avatar_data_uri": gc.read_avatar_data_uri(),
            }
            for gc in rc.guests
        ]
        sink(
            ChahuaEnvelope(
                room_id=self._session.room.name,
                turn_id=None,
                guest_name=None,
                message_id=None,
                type=ChahuaEventType.ROOM_INFO,
                data={
                    "room_name": rc.name,
                    "topic": rc.topic,
                    "guests": guests,
                    "user_display_name": self._session.user_config.display_name,
                    "user_avatar_data_uri": self._session.user_config.read_avatar_data_uri(),
                    "current_room_id": rc.room_dir.name,
                    "rooms_available": discover_rooms(self._repo_root),
                },
            )
        )

    def _emit_room_history(self, sink: EnvelopeSink) -> None:
        """ws 连上 / room_info 之后立刻下发 transcript.jsonl 历史。

        一帧把 ``Room._messages`` 全部塞下去（``Message.to_jsonl_dict()`` 同款字段：
        ``seq / speaker_id / text / ts_ms / message_id``）。前端按 ``speaker_id``：
        ``"user"`` → 用户气泡（取 ``user_avatar_data_uri``）；其余 → 茶客气泡，名字
        即 ``speaker_id``、头像走 guests 名册查找（不在册的茶客退化成无头像，正常）。

        所有历史一次性下发的取舍：实现简单；目前 transcript 体量（数百条）单帧没压力；
        将来 ws 默认 max_size（~1MB）不够时再改成分页 / 后向滚动懒拉。
        """
        msgs = self._session.room.messages_since(0)
        sink(
            ChahuaEnvelope(
                room_id=self._session.room.name,
                turn_id=None,
                guest_name=None,
                message_id=None,
                type=ChahuaEventType.ROOM_HISTORY,
                data={"messages": [m.to_jsonl_dict() for m in msgs]},
            )
        )

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
        new_room_dir = self._repo_root / "rooms" / room_id
        if not new_room_dir.is_dir():
            _log.warning("switch_room: room_id=%r 目录不存在：%s", room_id, new_room_dir)
            self._emit_room_info(sink)
            return
        try:
            new_session = build_room_session(new_room_dir, repo_root=self._repo_root)
        except Exception:
            _log.exception("switch_room: build_room_session 失败 room_id=%r", room_id)
            self._emit_room_info(sink)
            return
        old = self._session
        self._session = new_session
        try:
            old.close()
        except Exception:
            _log.exception("switch_room: 旧 session close 出错（已切换，忽略）")
        _log.info("switch_room: → %r", room_id)
        self._emit_room_snapshot(sink)

    def _emit_room_snapshot(self, sink: EnvelopeSink) -> None:
        """下发 room_info + room_history —— 前端拿来全量复位 sidebar + 消息区。

        三处调用：首次连接（``_serve_one``）、换房成功（``_switch_room``）、清空房间
        （``_clear_room``）。前端 ``renderSidebar`` 一帧 ``messagesEl.replaceChildren()``
        清屏，再用 ``room_history`` 回放。``_switch_room`` 的失败兜底只单发 room_info
        让 sidebar 状态复位，不走这个 helper。
        """
        self._emit_room_info(sink)
        self._emit_room_history(sink)

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

    async def _run_turn(self, text: str, sink: EnvelopeSink) -> None:
        """承载一条 user_message 的整个 AI 链。挂在 ``_inflight_turn_task`` 上让 cancel
        入口能 ``task.cancel()`` 它。

        - CancelledError：``orchestrator._run_ai_chain`` 在补完 ``turn_end(cancelled)``
          后 reraise；这里 swallow 让 task 正常完成。
        - 其它异常：兜底 log + swallow，避免 task 异常逃逸触发 asyncio "Task exception
          was never retrieved" warning。
        """
        try:
            await self._session.orchestrator.submit_user_message(text, sink=sink)
        except asyncio.CancelledError:
            _log.info("turn cancelled by user")
        except Exception:
            _log.exception("submit_user_message crashed")
        finally:
            self._inflight_turn_task = None

    async def _handle_inbound(self, data: dict, sink: EnvelopeSink) -> None:
        """分派一条客户端消息。"""
        msg_type = data.get("type")
        if msg_type == INBOUND_CANCEL:
            # turn_id 由前端塞，服务端只记日志：单 in-flight 模型下当前 task 必定就是
            # 前端能看到 turn_id 的那个；race 窗口（前一 turn 刚 end / 下一 turn 刚
            # start 之间）下错杀也只是少说半句话，无 transcript 污染。
            turn_id = data.get("turn_id")
            if not self._inflight_alive():
                _log.info("cancel ignored: no in-flight turn (turn_id=%r)", turn_id)
                return
            _log.info("cancel: turn_id=%r", turn_id)
            self._cancel_inflight()
            return
        if msg_type == INBOUND_SWITCH_ROOM:
            room_id = data.get("room_id")
            if not isinstance(room_id, str) or not room_id:
                _log.warning("ignoring switch_room with missing/empty room_id")
                return
            await self._cancel_and_drain_inflight()
            self._switch_room(room_id, sink)
            return
        if msg_type == INBOUND_CLEAR_ROOM:
            await self._cancel_and_drain_inflight()
            self._clear_room(sink)
            return
        if msg_type != INBOUND_USER_MESSAGE:
            # 友好容忍：未知 type 不断连，仅 WARN。前端在协议升级期发新 type 时
            # 服务端旧版本也不至于把它踢下线。
            _log.warning("ignoring inbound message of unknown type=%r", msg_type)
            return
        text = data.get("text")
        if not isinstance(text, str) or not text:
            _log.warning("ignoring user_message with missing/empty text")
            return
        if self._inflight_alive():
            # 单 in-flight 严格策略：当前 turn 没结束前 drop 后续 user_message。前端
            # composer 在 turn_start / turn_end 之间禁用，正常情况打不到这条；防御性保护
            # 老前端 / wscat 直发场景。
            _log.warning("user_message dropped: previous turn still in flight")
            return
        self._inflight_turn_task = asyncio.create_task(
            self._run_turn(text, sink), name="chahua-turn"
        )


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
            f"相对路径相对 repo_root，绝对路径原样。"
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
    repo_root = find_repo_root()
    load_env_files(repo_root)

    room_dir = resolve_under(repo_root, args.room)
    try:
        session = build_room_session(room_dir, repo_root=repo_root)
    except RoomConfigError as e:
        print(f"房间配置错误：\n{e}", file=sys.stderr)
        return 2

    # 端口优先级：CLI > env > 默认。
    port = args.port
    if port is None:
        env_port = os.environ.get("CHAHUA_WS_PORT")
        port = int(env_port) if env_port else DEFAULT_PORT

    server = ChahuaServer(session, host=args.host, port=port, repo_root=repo_root)
    stop = asyncio.Event()

    # add_signal_handler 在 Windows 不支持 —— 茶话室目前定位 macOS / Linux，
    # Windows 走 P3 Electron 拉子进程再说。
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            # Windows fallback：靠 KeyboardInterrupt 上浮捕获。
            pass

    try:
        await server.serve_forever(stop)
    finally:
        # 关 server 持有的当前 session（换房后 self._session 已不是局部 `session`）。
        server.close()
    return 0


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
