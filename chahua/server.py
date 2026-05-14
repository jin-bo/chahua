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
    find_repo_root,
    load_env_files,
)

_log = logging.getLogger(__name__)


DEFAULT_PORT = 7860
DEFAULT_HOST = "127.0.0.1"

# 客户端 → 服务端 message type 字段值。"用户发言"——唯一在 P2.3 实装的。
INBOUND_USER_MESSAGE = "user_message"


# ── server ────────────────────────────────────────────────────────────────


class ChahuaServer:
    """单房间 ws server。

    在同一 session 上跨多次客户端连接复用 —— 客户端断开后房间状态保留，下次连上
    就是续聊（与 :mod:`chahua.cli` 的 ``/quit`` → 重启 → 续聊一个意思）。
    """

    def __init__(self, session: RoomSession, *, host: str, port: int) -> None:
        self._session = session
        self._host = host
        self._port = port
        # 当前在线的客户端句柄。``None`` 表示空闲；非 ``None`` 时第二个连接被拒。
        self._active: Optional[ServerConnection] = None

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
            self._emit_room_info(sink)
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
            writer.cancel()
            # 给 writer 一个机会观察 cancel —— 不阻塞断线流程。
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

        ``avatar_data_uri``：P3.2.x 加的茶客头像，与 persona md sibling 同名 ``.png``，
        base64 嵌进 envelope。缺图返 ``None``，前端按无头像渲染。

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
                },
            )
        )

    async def _handle_inbound(self, data: dict, sink: EnvelopeSink) -> None:
        """分派一条客户端消息。本期只处理 ``user_message``。"""
        msg_type = data.get("type")
        if msg_type != INBOUND_USER_MESSAGE:
            # 友好容忍：未知 type 不断连，仅 WARN。前端在协议升级期发新 type 时
            # 服务端旧版本也不至于把它踢下线。
            _log.warning("ignoring inbound message of unknown type=%r", msg_type)
            return
        text = data.get("text")
        if not isinstance(text, str) or not text:
            _log.warning("ignoring user_message with missing/empty text")
            return
        try:
            await self._session.orchestrator.submit_user_message(text, sink=sink)
        except Exception:
            # submit_user_message 内部已对单个茶客失败做了兜底；这里只能是真出意外
            # （asyncio.Cancelled / 资源问题）。留 stacktrace。
            _log.exception("submit_user_message crashed")


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

    server = ChahuaServer(session, host=args.host, port=port)
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
        session.close()
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
