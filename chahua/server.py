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
import os
import signal
import sys
from pathlib import Path
from typing import Callable, Optional

from websockets import CloseCode
from websockets.asyncio.server import ServerConnection, serve
from websockets.exceptions import ConnectionClosed

from . import admin, persona_import
from ._paths import Paths, resolve_under
from .config import RoomConfigError
from .events import (
    ChahuaEnvelope,
    ChahuaEventType,
    EnvelopeSink,
    NOTICE_LEVEL_ERROR,
    NOTICE_LEVEL_INFO,
)
from .permissions import DEFAULT_MODE
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

# 入站帧上限。``websockets`` 默认 1MB —— 头像 PNG 上限 1.5MB（admin._AVATAR_MAX_BYTES）
# × 4/3 base64 ≈ 2MB，再加 JSON quoting 必爆默认。设 4MB 给上传留头：用户传 1.5MB
# 上限的 PNG 不会被默默断线（websockets 会发 1009 + close 链接，sidecar 看上去是
# 中途挂掉，排查噩梦）。
_WS_MAX_INBOUND_BYTES = 4 * 1024 * 1024

# 客户端 → 服务端 message type 字段值。
INBOUND_USER_MESSAGE = "user_message"
INBOUND_SWITCH_ROOM = "switch_room"
INBOUND_CLEAR_ROOM = "clear_room"
INBOUND_CANCEL = "cancel"
INBOUND_ADD_GUEST = "add_guest"
INBOUND_REMOVE_GUEST = "remove_guest"
INBOUND_CREATE_ROOM = "create_room"
INBOUND_DELETE_ROOM = "delete_room"
INBOUND_UPDATE_USER_MD = "update_user_md"
INBOUND_UPDATE_USER_AVATAR = "update_user_avatar"
INBOUND_IMPORT_PERSONA_FOLDER = "import_persona_folder"
INBOUND_IMPORT_PERSONA_GITHUB = "import_persona_github"


def _import_success_text(result: "persona_import.ImportedPersona") -> str:
    """导入成功的 notice 文案 —— 含 persona 名 + 头像状态 + sidecar 文件数提示。"""
    parts = [f"已导入 persona「{result.name}」"]
    parts.append("（含头像）" if result.has_avatar else "（无头像）")
    if result.extras:
        parts.append(
            f"另有 {len(result.extras)} 个 sidecar 文件被保留（mcp.json / skills 等，目前运行时尚未消费）"
        )
    return "".join(parts)


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
                    # 完整 USER.md 原文 + source 路径 —— 前端"编辑配置"modal 拿来 prefill
                    # textarea。无 USER.md（user_config.full_md=None）→ 空串 + source=null，
                    # 编辑器从空白起步，保存时 server 落到 user_data_root/USER.md。
                    "user_md_content": self._session.user_config.full_md or "",
                    "user_md_source": (
                        str(self._session.user_config.source)
                        if self._session.user_config.source is not None
                        else None
                    ),
                    "current_room_id": rc.room_dir.name,
                    "rooms_available": discover_rooms(self._paths),
                    # 已在场茶客的 name 也在 personas_available 里 —— 前端按
                    # guests[].name 去重显示，避免 picker 列出重复人选。
                    "personas_available": admin.discover_personas(self._paths),
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

    def _add_guest(
        self,
        *,
        persona: str,
        name: Optional[str],
        permission: str,
        sink: EnvelopeSink,
    ) -> None:
        """往当前房间加一位茶客：改 room.toml + 重装 session + 重发 snapshot。"""
        room_dir = self._session.room_config.room_dir
        try:
            admin.add_guest(
                paths=self._paths,
                room_dir=room_dir,
                persona=persona,
                name=name,
                permission=permission,
            )
        except Exception:
            _log.exception("add_guest: persona=%r name=%r 失败", persona, name)
            # 写 toml 失败 / 校验失败：room.toml 已被回滚到 snapshot，session 还是旧的。
            # 重发 snapshot 让前端 UI 复位（添加按钮的 loading 等状态消除）。
            self._emit_room_snapshot(sink)
            return
        if not self._replace_session(room_dir, sink, label="add_guest"):
            return
        _log.info("add_guest: %r 加入 room=%r", name or persona, room_dir.name)
        self._emit_room_snapshot(sink)

    def _remove_guest(self, *, name: str, sink: EnvelopeSink) -> None:
        """从当前房间移除一位茶客：改 room.toml + 重装 session + 重发 snapshot。"""
        room_dir = self._session.room_config.room_dir
        try:
            admin.remove_guest(paths=self._paths, room_dir=room_dir, name=name)
        except Exception:
            _log.exception("remove_guest: name=%r 失败", name)
            self._emit_room_snapshot(sink)
            return
        if not self._replace_session(room_dir, sink, label="remove_guest"):
            return
        _log.info("remove_guest: %r 离开 room=%r", name, room_dir.name)
        self._emit_room_snapshot(sink)

    def _create_room(
        self,
        *,
        room_id: str,
        name: str,
        topic: str,
        rules: str,
        guests: list,
        sink: EnvelopeSink,
    ) -> None:
        """新建房间 + 切换到它：mkdir + 写 toml + load 校验 + replace session + emit snapshot。"""
        try:
            rc = admin.create_room(
                paths=self._paths,
                room_id=room_id,
                name=name,
                topic=topic,
                rules=rules,
                guests=guests,
            )
        except Exception:
            _log.exception("create_room: room_id=%r 失败", room_id)
            # 创建失败：磁盘已 rmtree 回滚（admin.create_room 内部）；前端没切走，重发当
            # 前 snapshot 让"创建中…"按钮态归位。
            self._emit_room_snapshot(sink)
            return
        if not self._replace_session(rc.room_dir, sink, label=f"create_room→{rc.room_dir.name!r}"):
            return
        _log.info("create_room: → %r", rc.room_dir.name)
        self._emit_room_snapshot(sink)

    def _update_user_md(self, *, content: str, sink: EnvelopeSink) -> None:
        """覆盖 USER.md + 原地 reload user_config（不重装整个 session）+ 重发 snapshot。

        优先沿用 user_config.source（若用户用了 room 级 USER.md 或 explicit override，
        编辑就改那个；不偷偷新建 user_data_root/USER.md 让两份并存）。

        在 ``reload_user_config`` 之前不需要 cancel inflight —— ``_handle_inbound`` 已经
        cancel 过了，且 user_config 是纯数据 swap，没有"半个茶客"的中间态。
        """
        try:
            admin.update_user_md(
                self._paths,
                content,
                source=self._session.user_config.source,
            )
        except Exception:
            _log.exception("update_user_md 失败")
            self._emit_room_snapshot(sink)
            return
        self._session.reload_user_config(self._paths)
        _log.info("update_user_md: %d 字节已落盘", len(content))
        self._emit_room_snapshot(sink)

    def _update_user_avatar(self, *, data_uri: str, sink: EnvelopeSink) -> None:
        """覆盖 USER.png；不重装 session（avatar 不是 UserConfig 字段，靠 sidebar 重发即可）。

        admin 层 cache_clear 已让下次 read_avatar_data_uri 拿到新文件；这里只发 room_info
        让前端拿到新 user_avatar_data_uri，transcript 不动 —— 用 _emit_room_info 而非全
        snapshot，省一次历史回放。
        """
        try:
            png_bytes = admin.parse_png_data_uri(data_uri)
            admin.update_user_avatar(
                self._paths,
                png_bytes,
                source=self._session.user_config.source,
            )
        except Exception:
            _log.exception("update_user_avatar 失败")
            self._emit_room_info(sink)
            return
        _log.info("update_user_avatar: %d 字节已落盘", len(png_bytes))
        self._emit_room_info(sink)

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

    def _run_import(
        self,
        label: str,
        op: Callable[[], "persona_import.ImportedPersona"],
        sink: EnvelopeSink,
    ) -> None:
        """跑一次 persona import + 统一 notice/room_info emit。

        统一三态：``PersonaImportError`` 拿用户可见原因；其它异常吞成"内部错误"避免
        把 traceback / 路径泄到前端；成功打 success 文案。失败也重发 room_info 让前端
        modal 状态复位（与 add_guest / create_room 同款）。
        """
        try:
            result = op()
        except persona_import.PersonaImportError as e:
            _log.info("%s 失败：%s", label, e)
            self._emit_notice(sink, level=NOTICE_LEVEL_ERROR, text=str(e))
            self._emit_room_info(sink)
            return
        except Exception as e:
            _log.exception("%s 意外错", label)
            self._emit_notice(
                sink, level=NOTICE_LEVEL_ERROR, text=f"导入失败（内部错误）：{e}"
            )
            self._emit_room_info(sink)
            return
        _log.info("%s → %s", label, result.persona_rel)
        self._emit_notice(
            sink, level=NOTICE_LEVEL_INFO, text=_import_success_text(result)
        )
        self._emit_room_info(sink)

    def _delete_room(self, *, room_id: str, sink: EnvelopeSink) -> None:
        """删除一个非当前房间。当前房间在 admin.delete_room 那层硬拒。"""
        current = self._session.room_config.room_dir.name
        try:
            admin.delete_room(
                paths=self._paths, room_id=room_id, current_room_id=current
            )
        except Exception:
            _log.exception("delete_room: room_id=%r 失败", room_id)
            self._emit_room_snapshot(sink)
            return
        _log.info("delete_room: %r 已删", room_id)
        # 房间没动当前 session —— 只重发 room_info 让 sidebar 列表更新（rooms_available 少一项）。
        self._emit_room_info(sink)

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
        if msg_type == INBOUND_ADD_GUEST:
            persona = data.get("persona")
            if not isinstance(persona, str) or not persona:
                _log.warning("ignoring add_guest with missing/empty persona")
                return
            name = data.get("name")
            if name is not None and not isinstance(name, str):
                _log.warning("add_guest.name 必须是字符串或 null，收到 %r", type(name))
                return
            permission = data.get("permission") or DEFAULT_MODE
            if not isinstance(permission, str):
                _log.warning("add_guest.permission 必须是字符串")
                return
            await self._cancel_and_drain_inflight()
            self._add_guest(
                persona=persona, name=name, permission=permission, sink=sink
            )
            return
        if msg_type == INBOUND_REMOVE_GUEST:
            name = data.get("name")
            if not isinstance(name, str) or not name:
                _log.warning("ignoring remove_guest with missing/empty name")
                return
            await self._cancel_and_drain_inflight()
            self._remove_guest(name=name, sink=sink)
            return
        if msg_type == INBOUND_CREATE_ROOM:
            room_id = data.get("room_id")
            name = data.get("name")
            if not isinstance(room_id, str) or not room_id:
                _log.warning("ignoring create_room with missing/empty room_id")
                return
            if not isinstance(name, str) or not name:
                _log.warning("ignoring create_room with missing/empty name")
                return
            topic = data.get("topic") or ""
            rules = data.get("rules") or ""
            guests = data.get("guests")
            if not isinstance(guests, list) or not guests:
                _log.warning("ignoring create_room with missing/empty guests")
                return
            # 防御：guests 每项至少要 persona 字段 —— admin.create_room 也校验，这里
            # 早一步报"前端协议不对"而不是被动等到 KeyError。
            if not all(isinstance(g, dict) and isinstance(g.get("persona"), str) and g["persona"]
                       for g in guests):
                _log.warning("create_room.guests 每项必须含 persona:str")
                return
            await self._cancel_and_drain_inflight()
            self._create_room(
                room_id=room_id,
                name=name,
                topic=topic if isinstance(topic, str) else "",
                rules=rules if isinstance(rules, str) else "",
                guests=guests,
                sink=sink,
            )
            return
        if msg_type == INBOUND_DELETE_ROOM:
            room_id = data.get("room_id")
            if not isinstance(room_id, str) or not room_id:
                _log.warning("ignoring delete_room with missing/empty room_id")
                return
            await self._cancel_and_drain_inflight()
            self._delete_room(room_id=room_id, sink=sink)
            return
        if msg_type == INBOUND_UPDATE_USER_MD:
            content = data.get("content")
            if not isinstance(content, str):
                _log.warning("ignoring update_user_md with non-string content")
                return
            await self._cancel_and_drain_inflight()
            self._update_user_md(content=content, sink=sink)
            return
        if msg_type == INBOUND_IMPORT_PERSONA_FOLDER:
            src = data.get("path")
            if not isinstance(src, str) or not src:
                _log.warning("ignoring import_persona_folder with missing/empty path")
                return
            # 导入不动 session，无需 cancel inflight。
            self._run_import(
                f"import_persona_folder src={src!r}",
                lambda: persona_import.import_from_folder(self._paths, Path(src)),
                sink,
            )
            return
        if msg_type == INBOUND_IMPORT_PERSONA_GITHUB:
            url = data.get("url")
            if not isinstance(url, str) or not url:
                _log.warning("ignoring import_persona_github with missing/empty url")
                return
            self._run_import(
                f"import_persona_github url={url!r}",
                lambda: persona_import.import_from_github(self._paths, url),
                sink,
            )
            return
        if msg_type == INBOUND_UPDATE_USER_AVATAR:
            data_uri = data.get("data_uri")
            if not isinstance(data_uri, str) or not data_uri:
                _log.warning("ignoring update_user_avatar with missing/empty data_uri")
                return
            # 头像写不动 session，无需 cancel inflight —— 让正在跑的 turn 自然结束。
            self._update_user_avatar(data_uri=data_uri, sink=sink)
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
