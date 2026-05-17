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
import base64
import binascii
import ctypes
import json
import logging
import os
import signal
import sys
from pathlib import Path
from typing import Awaitable, Callable, Literal, Optional

from websockets import CloseCode
from websockets.asyncio.server import ServerConnection, serve
from websockets.exceptions import ConnectionClosed

from . import admin, exporter, persona_import, trust
from ._paths import Paths, resolve_under
from ._persist import write_bytes_atomic
from .admin import sanitize_fs_name
from .config import ORCH_FIELD_BOUNDS, RoomConfigError
from .events import (
    ChahuaEnvelope,
    ChahuaEventType,
    EnvelopeSink,
    NOTICE_LEVEL_ERROR,
    NOTICE_LEVEL_INFO,
)
from .orchestrator import OrchestratorConfig
from .permissions import DEFAULT_MODE
from .persona_assets import discover_assets, persona_relative
from .session import (
    DEFAULT_ROOM_REL,
    ROOM_SHARE_DIRNAME,
    RoomSession,
    build_room_session,
    discover_rooms,
    ensure_room_share_dir,
    load_env_files,
)
from .task import MARKED_BY_USER
from .tasks_store import (
    ArtifactSourceMissingError,
    TaskExistsError,
    TaskNotFoundError,
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
INBOUND_ADD_GUEST = "add_guest"
INBOUND_REMOVE_GUEST = "remove_guest"
INBOUND_UPDATE_GUEST_PERMISSION = "update_guest_permission"
INBOUND_SET_PERSONA_MCP_TRUST = "set_persona_mcp_trust"
INBOUND_CREATE_ROOM = "create_room"
INBOUND_DELETE_ROOM = "delete_room"
INBOUND_UPDATE_USER_MD = "update_user_md"
INBOUND_UPDATE_USER_AVATAR = "update_user_avatar"
INBOUND_UPDATE_ROOM_TOML = "update_room_toml"
INBOUND_UPDATE_ROOM_ORCHESTRATOR = "update_room_orchestrator"
INBOUND_UPDATE_ROOM_LLM = "update_room_llm"
INBOUND_UPDATE_GUEST_LLM = "update_guest_llm"
INBOUND_UPDATE_GUEST_ISOLATION = "update_guest_isolation"
INBOUND_UPDATE_GUEST_EXTRA_MCP = "update_guest_extra_mcp"
INBOUND_IMPORT_PERSONA_FOLDER = "import_persona_folder"
INBOUND_IMPORT_PERSONA_GITHUB = "import_persona_github"
INBOUND_UPLOAD_FILE = "upload_file"
INBOUND_EXPORT_ROOM = "export_room"
# P5.1.7 任务房间 inbound（docs/P5-任务房间.md §4.3）。P5.1 不接 set_active_task /
# close_task —— 严守"一房间最多 1 个任务"窄路径；这两条到 P5.2 再开放。
INBOUND_OPEN_TASK = "open_task"
INBOUND_UPDATE_TASK = "update_task"
INBOUND_ATTACH_ARTIFACT = "attach_artifact"
INBOUND_ADD_DECISION = "add_decision"

# 单文件上限。WS 入站帧 max=300MB（_WS_MAX_INBOUND_BYTES），base64 4/3 膨胀 → 原始
# 文件极限 ~225MB。设 200MB 让 JSON quoting + 字段开销有头。改大要同步抬 ws max_size。
_UPLOAD_MAX_BYTES = 200 * 1024 * 1024


def _read_room_toml(room_dir: Path) -> str:
    """读 ``room_dir/room.toml`` 原文。读盘失败 → 空串（room_info 不会因为这个炸掉）。"""
    toml_path = room_dir / "room.toml"
    try:
        return toml_path.read_text("utf-8")
    except OSError:
        _log.exception("read_room_toml: %s 读盘失败", toml_path)
        return ""


def _mcp_summary_list(
    servers: Optional[dict[str, dict]],
) -> list[dict]:
    """``{name -> cfg}`` → 前端 popover 展示用 list。

    只挑 ``name`` / ``command`` / ``args`` —— ``env`` 含可能敏感的 token，**绝不下发**。
    """
    if not servers:
        return []
    return [
        {
            "name": name,
            "command": str(cfg.get("command", "")),
            "args": [str(a) for a in (cfg.get("args") or [])],
        }
        for name, cfg in servers.items()
    ]


# `_llm_summary` 的 ``source`` 字段：guest 段是 ``guest``/``room_default``，房间段是
# ``room``/``default``。``room`` = toml 里有写 / ``default`` = 走上游 fallback（room 默认段
# 自身的 default 表示 env 推断，scoring/summary 的 default 表示落到 room_default）。
# 串字面挪到一处 + Literal 注解 → IDE 能挡 typo。
LlmSource = Literal["guest", "room_default", "room", "default"]


def _llm_summary(*, spec, source: LlmSource) -> dict:
    """``LLMSpec`` → 前端 envelope 字典。

    **绝不下发 ``api_key`` 本身**：envelope 一旦塞 key，前端 devtools / log dump 都能
    看到，跨用户 user_data 共享更糟（设计 §2.2）。``api_key_ready`` 是 server 端探测
    出的 bool —— ``ollama`` 本地不强鉴权所以永远 ready。

    ``temperature`` 走 ``spec.temperature``（原始值，None 表示本段没显式写 → UI 看到
    "继承默认"语义；custom 模式才填具体值）。
    """
    api_key_env = spec.default_api_key_env()
    api_key_ready = spec.provider == "ollama" or bool(os.environ.get(api_key_env))
    return {
        "model": spec.model_id,
        "base_url": spec.base_url,
        "api_key_env": api_key_env,
        "api_key_ready": api_key_ready,
        "temperature": spec.temperature,
        "source": source,
    }


def _orchestrator_effective_dict(config: OrchestratorConfig) -> dict[str, object]:
    """从当前 :class:`OrchestratorConfig` 实例摘出公开给前端的编排字段。

    键集派生自 :data:`chahua.config.ORCH_FIELD_BOUNDS` —— 与 config 解析认得的字段
    一一对应。其余 ``OrchestratorConfig`` 字段（``threshold_decay_per_turn`` /
    ``onboarding_recent_messages`` / ``summary_block_size`` 等）是内部调参，不进 toml
    schema 也不下发 —— 减少 envelope 噪音，避免用户以为它们也可改。
    """
    return {k: getattr(config, k) for k in ORCH_FIELD_BOUNDS}


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


def _require_str(
    data: dict, key: str, *, where: str, allow_empty: bool = False
) -> Optional[str]:
    """从入站 payload 取一个 str 字段，校验失败 → WARN + 返回 ``None``。

    ``where`` 取 :data:`INBOUND_*` 常量值 —— 让 WARN 日志一眼看出是哪条 wire 帧
    不合法。``allow_empty=True`` 给 ``content`` 这种允许空串的字段（用户清空
    USER.md 等）。
    """
    v = data.get(key)
    if not isinstance(v, str) or (not allow_empty and not v):
        _log.warning(
            "ignoring %s: %s 必须是%sstr，收到 %r",
            where, key, "" if allow_empty else "非空 ", v,
        )
        return None
    return v


def _require_bool(data: dict, key: str, *, where: str) -> Optional[bool]:
    """同 :func:`_require_str` —— 取 bool 字段，非 bool → WARN + None。"""
    v = data.get(key)
    if not isinstance(v, bool):
        _log.warning("ignoring %s: %s 必须是 bool，收到 %r", where, key, type(v))
        return None
    return v


def _require_list(data: dict, key: str, *, where: str) -> Optional[list]:
    """同 :func:`_require_str` —— 取 list 字段。空 list 是合法值（语义"清整段"），不拒。"""
    v = data.get(key)
    if not isinstance(v, list):
        _log.warning("ignoring %s: %s 必须是 list，收到 %r", where, key, type(v))
        return None
    return v


def _check_optional_dict(data: dict, key: str, *, where: str) -> bool:
    """``data[key]`` 必须是 dict 或缺/null；其它类型 → WARN + ``False``（让 caller 丢弃帧）。
    本身不取值 —— 用 ``data.get(key)`` 拿；只表态校验过了。
    """
    v = data.get(key)
    if v is not None and not isinstance(v, dict):
        _log.warning(
            "ignoring %s: %s 必须是对象 / null，收到 %r", where, key, type(v)
        )
        return False
    return True


def _check_keys_whitelist(
    data: dict,
    allowed: frozenset[str],
    *,
    where: str,
) -> Optional[str]:
    """严格白名单：payload 顶层只接 ``allowed`` 里的字段（``type`` 已被 dispatcher 吃）。

    合法 → ``None``；多余键 → 返回错误文案（caller emit NOTICE + 丢帧）。P5.1.7 任务
    inbound 用 —— 等价 ``_require_str`` 同款 "校验失败给个反馈" 接口。
    """
    extra = set(data) - allowed - {"type"}
    if not extra:
        return None
    return f"{where}: 未知字段 {sorted(extra)!r}"


def _import_success_text(result: "persona_import.ImportedPersona") -> str:
    """导入成功的 notice 文案 —— 含 persona 名 + 头像状态 + sidecar 文件数提示。"""
    parts = [f"已导入 persona「{result.name}」"]
    parts.append("（含头像）" if result.has_avatar else "（无头像）")
    if result.extras:
        parts.append(
            f"另有 {len(result.extras)} 个 sidecar 文件被保留（mcp.json / skills 等，目前运行时尚未消费）"
        )
    return "".join(parts)


# 任务 inbound payload 白名单（P5.1.7，docs §4.3）。集中在模块顶让"加字段就动这里"
# 一目了然；任何不在集合里的顶层键 → NOTICE error。``type`` 字段单独豁免（dispatcher
# 已消费）。`update_task` 的 patch 字段另设一组（嵌套不在顶层白名单里）。
_OPEN_TASK_ALLOWED: frozenset[str] = frozenset({"title", "goal", "owner"})
_UPDATE_TASK_ALLOWED: frozenset[str] = frozenset({"task_id", "patch"})
_UPDATE_TASK_PATCH_ALLOWED: frozenset[str] = frozenset({"title", "goal"})
_ATTACH_ARTIFACT_ALLOWED: frozenset[str] = frozenset({"task_id", "share_rel"})
_ADD_DECISION_ALLOWED: frozenset[str] = frozenset(
    {"task_id", "summary", "supporting_message_ids"}
)


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
        """ws 连上即下发一次 ``room_info`` —— 前端拿来装 sidebar / @ 补全候选。

        - **绝不下发 ``api_key`` 本身**：只出 ``api_key_env`` 名 + ``api_key_ready`` bool
          （设计 §2.2，避免前端 devtools / log dump 泄露）。
        - **persona vs 房间级 MCP 拆两块**：trust 门控不对称 —— persona sidecar 走 trust
          清单（持续生效），``[[guest.extra_mcp_servers]]`` 是用户在自己 toml 里手写自动
          信任。前端按各自语义渲染。``effective_mcp_names`` 是合并后实际装载（房间级覆盖
          persona 同名），纯展示。
        """
        rc = self._session.room_config
        # 一次性加载 trust 清单 —— 后面每 guest 走 ``in`` 查，避免 N 次 disk read。
        trusted_personas = trust.list_trusted(self._paths)
        guests: list[dict] = []
        for gc in rc.guests:
            assets = discover_assets(gc.persona_path)
            persona_rel = persona_relative(gc.persona_path, self._paths)
            persona_mcp_trusted = bool(
                assets.has_mcp and persona_rel in trusted_personas
            )
            persona_mcp_servers = _mcp_summary_list(assets.mcp_servers)
            room_mcp_servers = _mcp_summary_list(gc.extra_mcp_servers)
            # effective 装载顺序：persona (受信任时) + 房间级覆盖同名 —— 与
            # chahua.guest._merged_mcp_configs 的合并语义一致。
            effective: dict[str, None] = {}
            if persona_mcp_trusted:
                for s in persona_mcp_servers:
                    effective[s["name"]] = None
            for s in room_mcp_servers:
                effective[s["name"]] = None
            workspace_path = gc.workspace_in(
                paths=self._paths, room_dir=rc.room_dir,
            )
            guests.append({
                "name": gc.name,
                "permission": gc.permission,
                "isolation": gc.isolation,
                "workspace_path": str(workspace_path),
                "avatar_data_uri": gc.read_avatar_data_uri(),
                "persona_rel": persona_rel,
                "persona_mcp_trusted": persona_mcp_trusted,
                "persona_mcp_servers": persona_mcp_servers,
                "room_mcp_servers": room_mcp_servers,
                "effective_mcp_names": list(effective),
                "llm": _llm_summary(
                    spec=self._session.guest_specs[gc.name],
                    source="guest" if gc.llm is not None else "room_default",
                ),
                "skills_available": list(assets.skills_available),
            })
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
                    # 房间 room.toml 原文 + 路径 —— 前端「更改房间配置」modal prefill。
                    # 读盘失败（理论上不会，session 已经成功 load 过一次）兜底空串。
                    "room_toml_content": _read_room_toml(rc.room_dir),
                    "room_toml_source": str(rc.room_dir / "room.toml"),
                    # 编排参数：effective = 当前 Orchestrator 实际跑的值（默认 + 用户 override 合并后），
                    # overrides_keys = room.toml [room] 段里用户实际写过的键。前端按 keys 算出
                    # "哪些是默认 / 哪些是用户改过的"，无需自己重算默认值。
                    "orchestrator": _orchestrator_effective_dict(
                        self._session.orchestrator.config
                    ),
                    "orchestrator_overrides_keys": sorted(rc.orchestrator_overrides),
                    # 房间级 LLM section：与 guests[].llm 同构。
                    # ``source`` 是 "room" 表示 [scoring]/[summary] 段在 toml 里有写；
                    # "default" 表示该段缺失走的是房间默认。
                    # 房间默认 LLM —— "继承房间默认" UI 标签的字面来源（P4.5 modal）。
                    # P4.9 起 source = "room" 表示 [room.llm] 在 toml 里有写；"default"
                    # 表示走 env 推断（LLMSpec.try_from_env）。UI 据此区分"用户在房间
                    # 里配的默认" vs "全局 env 默认"。
                    "room_default_llm": _llm_summary(
                        spec=self._session.room_default_spec,
                        source="room" if rc.room_llm is not None else "default",
                    ),
                    "scoring_llm": _llm_summary(
                        spec=self._session.scoring_spec,
                        source="room" if rc.scoring_llm is not None else "default",
                    ),
                    "summary_llm": _llm_summary(
                        spec=self._session.summary_spec,
                        source="room" if rc.summary_llm is not None else "default",
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

    def _set_persona_mcp_trust(
        self, *, persona_rel: str, trusted: bool, sink: EnvelopeSink
    ) -> None:
        """改一份 persona 的 MCP 信任状态：写信任清单 + 重装当前 session + 重发 snapshot。

        信任是 user-level（跨房），但只有当前房间载着这位 persona 的茶客时改的"立刻生效"
        才有意义；本函数只重装当前 session，其它房间下次进房时自然拿到新状态。

        ``persona_rel`` 校验：必须命中当前房间某位茶客的 persona —— 防止前端发任意路径
        让我们写任意 trust 键（攻击面有限但口径要严）。
        """
        # 校验：本房间确实有这位 persona 的茶客，避免被注入任意 trust 键。
        rc = self._session.room_config
        known = {
            persona_relative(gc.persona_path, self._paths) for gc in rc.guests
        }
        if persona_rel not in known:
            _log.warning(
                "set_persona_mcp_trust: persona_rel=%r 不在本房间茶客列表 %s 内，拒绝",
                persona_rel, sorted(known),
            )
            self._emit_room_info(sink)
            return
        try:
            trust.set_mcp_trust(self._paths, persona_rel, trusted)
        except Exception:
            _log.exception(
                "set_persona_mcp_trust: persona_rel=%r trusted=%r 写盘失败",
                persona_rel, trusted,
            )
            self._emit_room_snapshot(sink)
            return
        _log.info(
            "set_persona_mcp_trust: %s → %s", persona_rel, "trusted" if trusted else "untrusted"
        )
        room_dir = rc.room_dir
        if not self._replace_session(room_dir, sink, label="set_persona_mcp_trust"):
            return
        self._emit_room_snapshot(sink)

    def _update_guest_permission(
        self, *, name: str, permission: str, sink: EnvelopeSink
    ) -> None:
        """改一位茶客 permission：改 room.toml + 重装 session + 重发 snapshot。

        session 重装是必要的 —— permission 挂在 agent.permission_engine + tool_runner 上
        （:func:`chahua.permissions.apply_permission_mode`），运行时切要么挨个茶客重新
        ``apply_permission_mode``、要么直接重建。后者复用 :meth:`_replace_session`
        与 add/remove guest 同口径，简单可靠。
        """
        room_dir = self._session.room_config.room_dir
        try:
            admin.update_guest_permission(
                paths=self._paths, room_dir=room_dir, name=name, permission=permission
            )
        except Exception:
            _log.exception(
                "update_guest_permission: name=%r permission=%r 失败", name, permission
            )
            self._emit_room_snapshot(sink)
            return
        if not self._replace_session(room_dir, sink, label="update_guest_permission"):
            return
        _log.info("update_guest_permission: %r → %r", name, permission)
        self._emit_room_snapshot(sink)

    def _update_room_orchestrator(
        self, *, overrides: dict, sink: EnvelopeSink
    ) -> None:
        """整体覆盖 ``[room]`` 段的编排参数键集 + 热替 ``orchestrator.config`` + 重发 snapshot。

        走 :meth:`RoomSession.swap_room_config` 一次 attribute swap —— 编排参数是纯数值
        声明（``OrchestratorConfig`` 的字段在 Orchestrator 热循环里都靠 ``self.config.X``
        live 读），无需 cancel in-flight turn / 重建茶客 / 重新 onboarding。校验失败回
        ``admin.update_room_orchestrator`` 那条路径，磁盘旧 bytes 已回滚，session 不动。
        """
        room_dir = self._session.room_config.room_dir
        try:
            new_rc = admin.update_room_orchestrator(
                paths=self._paths, room_dir=room_dir, overrides=overrides
            )
        except Exception:
            _log.exception(
                "update_room_orchestrator: overrides=%r 失败", overrides
            )
            self._emit_room_snapshot(sink)
            return
        self._session.swap_room_config(new_rc)
        _log.info("update_room_orchestrator: %r", overrides)
        self._emit_room_snapshot(sink)

    def _update_room_llm(
        self, *, section: str, spec_dict: Optional[dict], sink: EnvelopeSink
    ) -> None:
        """覆盖 ``[scoring]`` / ``[summary]`` 顶层 LLM 段 + 重装 session + 重发 snapshot。

        session 重装是必要的 —— LLMClient 在装配期注入 IntentScorer / Summarizer / Agentao
        实例内部，不像 OrchestratorConfig 那样可热替（agentao 没暴露热换 client 接口）。
        改完即 ``_replace_session`` 让茶客 / scorer / summarizer 全部带新 client 起。
        """
        room_dir = self._session.room_config.room_dir
        try:
            admin.update_room_llm(
                paths=self._paths, room_dir=room_dir,
                section=section, spec_dict=spec_dict,
            )
        except Exception:
            _log.exception(
                "update_room_llm: section=%r spec=%r 失败", section, spec_dict
            )
            self._emit_room_snapshot(sink)
            return
        if not self._replace_session(room_dir, sink, label="update_room_llm"):
            return
        _log.info("update_room_llm: section=%r spec=%r", section, spec_dict)
        self._emit_room_snapshot(sink)

    def _update_guest_isolation(
        self, *, name: str, isolation: str, sink: EnvelopeSink
    ) -> None:
        """改一位茶客的 isolation + 重装 session + 重发 snapshot。

        session 重装是必要的 —— cwd 路径变了，TeaGuest 持的 ``working_directory`` 是
        agentao 工具 sandbox 边界（无法热改）。改完即 ``_replace_session`` 让那位
        茶客以新 cwd 起。旧路径下的 ``.agentao/memory.db`` 不自动 rm（设计 §2.5）。
        """
        room_dir = self._session.room_config.room_dir
        try:
            admin.update_guest_isolation(
                paths=self._paths, room_dir=room_dir,
                name=name, isolation=isolation,
            )
        except Exception:
            _log.exception(
                "update_guest_isolation: name=%r isolation=%r 失败", name, isolation
            )
            self._emit_room_snapshot(sink)
            return
        if not self._replace_session(room_dir, sink, label="update_guest_isolation"):
            return
        _log.info("update_guest_isolation: %r → %r", name, isolation)
        self._emit_room_snapshot(sink)

    def _update_guest_llm(
        self, *, name: str, spec_dict: Optional[dict], sink: EnvelopeSink
    ) -> None:
        """覆盖一位茶客的 LLM 字段 + 重装 session + 重发 snapshot。

        ``spec_dict=None`` 即清掉该茶客的 model/base_url/api_key_env，回到房间默认。
        """
        room_dir = self._session.room_config.room_dir
        try:
            admin.update_guest_llm(
                paths=self._paths, room_dir=room_dir,
                name=name, spec_dict=spec_dict,
            )
        except Exception:
            _log.exception(
                "update_guest_llm: name=%r spec=%r 失败", name, spec_dict
            )
            self._emit_room_snapshot(sink)
            return
        if not self._replace_session(room_dir, sink, label="update_guest_llm"):
            return
        _log.info("update_guest_llm: name=%r spec=%r", name, spec_dict)
        self._emit_room_snapshot(sink)

    def _update_guest_extra_mcp(
        self, *, name: str, servers: list, sink: EnvelopeSink
    ) -> None:
        """覆盖一位茶客的 ``[[guest.extra_mcp_servers]]`` 数组段 + 重装 session + 重发 snapshot。

        ``servers=[]`` 即清掉该茶客的所有房间级 MCP entry（与 admin 层语义一致）。
        session 重装是必要的 —— Agentao 在 ``__init__`` 时把 mcp_manager 装进去，运行时
        无法热改；改完即 ``_replace_session`` 让那位茶客以新 MCP 装载起。
        """
        room_dir = self._session.room_config.room_dir
        try:
            admin.update_guest_extra_mcp(
                paths=self._paths, room_dir=room_dir,
                name=name, servers=servers,
            )
        except Exception:
            _log.exception(
                "update_guest_extra_mcp: name=%r servers=%r 失败", name, servers
            )
            self._emit_room_snapshot(sink)
            return
        if not self._replace_session(room_dir, sink, label="update_guest_extra_mcp"):
            return
        _log.info(
            "update_guest_extra_mcp: name=%r servers=%d 项", name, len(servers)
        )
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

    def _update_room_toml(self, *, content: str, sink: EnvelopeSink) -> None:
        """覆盖当前房间 room.toml 全文 + 重装 session + 重发 snapshot。

        校验失败（语法 / 白名单 / persona 找不到）→ emit error notice + 重发当前 snapshot
        让前端 UI 复位；admin.update_room_toml 已经把磁盘内容回滚到旧 toml。
        """
        room_dir = self._session.room_config.room_dir
        try:
            admin.update_room_toml(room_dir, content, paths=self._paths)
        except (ValueError, RoomConfigError) as e:
            _log.warning("update_room_toml: room=%r 校验失败：%s", room_dir.name, e)
            self._emit_notice(
                sink, level=NOTICE_LEVEL_ERROR, text=f"房间配置保存失败：{e}"
            )
            self._emit_room_snapshot(sink)
            return
        except Exception:
            _log.exception("update_room_toml: room=%r 失败", room_dir.name)
            self._emit_notice(
                sink, level=NOTICE_LEVEL_ERROR, text="房间配置保存失败（详见服务端日志）"
            )
            self._emit_room_snapshot(sink)
            return
        if not self._replace_session(room_dir, sink, label="update_room_toml"):
            return
        _log.info("update_room_toml: room=%r 已落盘", room_dir.name)
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

    def _upload_file(
        self, *, filename: str, content_b64: str, sink: EnvelopeSink
    ) -> None:
        """把前端传上来的文件落到房间共享目录 ``<room_dir>/share/``。

        - 文件名经 :func:`sanitize_fs_name` 洗一遍（防 ``../`` traversal 写到 share 外）。
          洗完为空 / 全点 → 拒。
        - base64 解码失败 / 超 :data:`_UPLOAD_MAX_BYTES` → 拒。
        - 重名直接覆盖 —— 用户主动选了同名文件意味着想替换，比"自动加 (1)"明确。
        - 写盘走 :func:`write_bytes_atomic` —— tmp+rename，写一半被 kill 不留残骸。

        **每次入站请求恒发一条 ``file_uploaded`` envelope**（成功 / 失败都发）——
        前端的串行上传循环靠这条 echo 推进队列，没 echo 就永远 await 卡住。data 形态：

        - 成功：``{rel, name, size, original}``
        - 失败：``{original, error}`` —— 无 ``rel``，前端 onServerEcho 跳过 pill 追加。

        失败路径同时 emit 一条 ``notice(level=error)`` 让用户看见可读原因（与 persona
        导入失败口径一致）。
        """
        original = filename
        try:
            safe_name = sanitize_fs_name(filename, label="filename")
        except ValueError as e:
            self._fail_upload(sink, original=original, text=f"文件名非法：{e}")
            return
        try:
            data = base64.b64decode(content_b64, validate=True)
        except (binascii.Error, ValueError) as e:
            self._fail_upload(
                sink, original=original, text=f"上传失败（base64 解码）：{e}",
            )
            return
        if len(data) > _UPLOAD_MAX_BYTES:
            self._fail_upload(
                sink, original=original,
                text=f"文件太大（{len(data)} bytes，上限 {_UPLOAD_MAX_BYTES}）",
            )
            return
        try:
            share_dir = ensure_room_share_dir(self._session.room_config.room_dir)
            target = share_dir / safe_name
            write_bytes_atomic(target, data)
        except Exception as e:
            _log.exception("upload_file: 写盘失败 name=%r", safe_name)
            self._fail_upload(sink, original=original, text=f"上传失败：{e}")
            return
        rel = f"{ROOM_SHARE_DIRNAME}/{safe_name}"
        _log.info("upload_file: %s (%d bytes)", rel, len(data))
        sink(
            ChahuaEnvelope(
                room_id=self._session.room.name,
                turn_id=None,
                guest_name=None,
                message_id=None,
                type=ChahuaEventType.FILE_UPLOADED,
                data={
                    "rel": rel,
                    "name": safe_name,
                    "size": len(data),
                    "original": original,
                },
            )
        )

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
        """下发 room_info + room_history + task_info —— 前端拿来全量复位 sidebar + 消息区
        + 任务面板。

        三处调用：首次连接（``_serve_one``）、换房成功（``_switch_room``）、清空房间
        （``_clear_room``）。前端 ``renderSidebar`` 一帧 ``messagesEl.replaceChildren()``
        清屏，再用 ``room_history`` 回放，最后用 ``task_info`` 装任务面板。
        ``_switch_room`` 的失败兜底只单发 room_info 让 sidebar 状态复位，不走这个 helper。

        ``task_info`` 即使房间没任务也下发（空 ``{tasks: [], active_task_id: null}``），
        让前端用这帧确认"P5.1 任务协议生效"，避免老 sidecar 不下发的情况下前端误判。
        """
        self._emit_room_info(sink)
        self._emit_room_history(sink)
        self._emit_task_info(sink)

    def _emit_task_envelope(
        self,
        sink: EnvelopeSink,
        *,
        type: ChahuaEventType,
        data: dict,
    ) -> None:
        """连接级任务 envelope（``turn_id`` / ``message_id`` / ``guest_name`` 全 None）。
        P5.1.7 四个 hint 事件与 ``task_info`` 共用 —— 避免每个 callsite 写一遍三个 None。
        """
        sink(
            ChahuaEnvelope(
                room_id=self._session.room.name,
                turn_id=None,
                guest_name=None,
                message_id=None,
                type=type,
                data=data,
            )
        )

    def _emit_task_info(self, sink: EnvelopeSink) -> None:
        """下发 ``task_info`` envelope —— 权威快照，前端任务状态以此为准（docs §4.2）。

        每次任务状态变更（open / update / decision / artifact）后重发整份
        ``{tasks, active_task_id}``。task entry 字段沿 :meth:`Task.to_jsonl_dict`，外加
        ``artifacts`` / ``decisions`` 子列表，让前端一次拿全。空房间也下发（空 tasks），
        前端用这帧确认任务协议生效。
        """
        store = self._session.tasks_store
        tasks_payload = [
            {
                **t.to_jsonl_dict(),
                "artifacts": store.list_artifacts(t.id),
                "decisions": [d.to_jsonl_dict() for d in store.list_decisions(t.id)],
            }
            for t in store.list_tasks()
        ]
        self._emit_task_envelope(
            sink,
            type=ChahuaEventType.TASK_INFO,
            data={
                "tasks": tasks_payload,
                "active_task_id": store.active_task_id,
            },
        )

    def _export_room(self, sink: EnvelopeSink) -> None:
        # read-only：不动 session、不写盘。导出物只活在用户的 Downloads/ 里（renderer
        # 端走 Blob + <a download>），房间目录 transcript.jsonl / summary.jsonl 不动。
        msgs = self._session.room.messages_since(0)
        filename, content = exporter.format_room_markdown(
            self._session.room_config,
            msgs,
            self._session.user_config.display_name,
        )
        sink(
            ChahuaEnvelope(
                room_id=self._session.room.name,
                turn_id=None,
                guest_name=None,
                message_id=None,
                type=ChahuaEventType.ROOM_EXPORT,
                data={"filename": filename, "markdown": content},
            )
        )
        _log.info(
            "export_room: room=%r %d msg → %s (%d bytes)",
            self._session.room.name, len(msgs), filename, len(content),
        )

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
        """
        msg_type = data.get("type")
        handler = _INBOUND_HANDLERS.get(msg_type)
        if handler is not None:
            await handler(self, data, sink)
            return
        # 友好容忍：未知 type 不断连，仅 WARN。前端在协议升级期发新 type 时
        # 服务端旧版本也不至于把它踢下线。
        _log.warning("ignoring inbound message of unknown type=%r", msg_type)

    # ── 各 inbound 帧的 handler；注册在类外 _INBOUND_HANDLERS。────────────

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

    async def _inbound_add_guest(self, data: dict, sink: EnvelopeSink) -> None:
        persona = _require_str(data, "persona", where=INBOUND_ADD_GUEST)
        if persona is None:
            return
        # name 是 optional（null 让 admin 端按 persona stem 推），但传了就必须是 str。
        name = data.get("name")
        if name is not None and not isinstance(name, str):
            _log.warning(
                "ignoring %s: name 必须是 str 或 null，收到 %r",
                INBOUND_ADD_GUEST, type(name),
            )
            return
        # permission 缺 / falsy → DEFAULT_MODE；显式传就必须是 str。
        permission = data.get("permission") or DEFAULT_MODE
        if not isinstance(permission, str):
            _log.warning("ignoring %s: permission 必须是 str", INBOUND_ADD_GUEST)
            return
        await self._cancel_and_drain_inflight()
        self._add_guest(persona=persona, name=name, permission=permission, sink=sink)

    async def _inbound_remove_guest(self, data: dict, sink: EnvelopeSink) -> None:
        name = _require_str(data, "name", where=INBOUND_REMOVE_GUEST)
        if name is None:
            return
        await self._cancel_and_drain_inflight()
        self._remove_guest(name=name, sink=sink)

    async def _inbound_set_persona_mcp_trust(
        self, data: dict, sink: EnvelopeSink
    ) -> None:
        persona_rel = _require_str(
            data, "persona_rel", where=INBOUND_SET_PERSONA_MCP_TRUST
        )
        if persona_rel is None:
            return
        trusted = _require_bool(data, "trusted", where=INBOUND_SET_PERSONA_MCP_TRUST)
        if trusted is None:
            return
        await self._cancel_and_drain_inflight()
        self._set_persona_mcp_trust(
            persona_rel=persona_rel, trusted=trusted, sink=sink
        )

    async def _inbound_update_guest_permission(
        self, data: dict, sink: EnvelopeSink
    ) -> None:
        name = _require_str(data, "name", where=INBOUND_UPDATE_GUEST_PERMISSION)
        if name is None:
            return
        permission = _require_str(
            data, "permission", where=INBOUND_UPDATE_GUEST_PERMISSION
        )
        if permission is None:
            return
        await self._cancel_and_drain_inflight()
        self._update_guest_permission(name=name, permission=permission, sink=sink)

    async def _inbound_update_room_orchestrator(
        self, data: dict, sink: EnvelopeSink
    ) -> None:
        overrides = data.get("overrides")
        if not isinstance(overrides, dict):
            _log.warning(
                "ignoring %s: overrides 必须是对象，收到 %r",
                INBOUND_UPDATE_ROOM_ORCHESTRATOR, type(overrides),
            )
            return
        # 不 cancel inflight —— 编排参数走 swap_room_config 热替 self.config，下一轮
        # 迭代当场生效，无需打断当前发言 / 评分。
        self._update_room_orchestrator(overrides=overrides, sink=sink)

    async def _inbound_update_room_llm(
        self, data: dict, sink: EnvelopeSink
    ) -> None:
        section = data.get("section")
        if section not in ("room", "scoring", "summary"):
            _log.warning(
                "ignoring %s: section 必须是 'room'/'scoring'/'summary'，收到 %r",
                INBOUND_UPDATE_ROOM_LLM, section,
            )
            return
        if not _check_optional_dict(data, "spec", where=INBOUND_UPDATE_ROOM_LLM):
            return
        await self._cancel_and_drain_inflight()
        self._update_room_llm(
            section=section, spec_dict=data.get("spec"), sink=sink
        )

    async def _inbound_update_guest_llm(
        self, data: dict, sink: EnvelopeSink
    ) -> None:
        name = _require_str(data, "name", where=INBOUND_UPDATE_GUEST_LLM)
        if name is None:
            return
        if not _check_optional_dict(data, "spec", where=INBOUND_UPDATE_GUEST_LLM):
            return
        await self._cancel_and_drain_inflight()
        self._update_guest_llm(name=name, spec_dict=data.get("spec"), sink=sink)

    async def _inbound_update_guest_isolation(
        self, data: dict, sink: EnvelopeSink
    ) -> None:
        name = _require_str(data, "name", where=INBOUND_UPDATE_GUEST_ISOLATION)
        if name is None:
            return
        isolation = _require_str(
            data, "isolation", where=INBOUND_UPDATE_GUEST_ISOLATION
        )
        if isolation is None:
            return
        await self._cancel_and_drain_inflight()
        self._update_guest_isolation(name=name, isolation=isolation, sink=sink)

    async def _inbound_update_guest_extra_mcp(
        self, data: dict, sink: EnvelopeSink
    ) -> None:
        name = _require_str(data, "name", where=INBOUND_UPDATE_GUEST_EXTRA_MCP)
        if name is None:
            return
        # 每项 dict 内字段校验在 admin 层 _build_extra_mcp_servers；这里只挡 wire 层。
        servers = _require_list(data, "servers", where=INBOUND_UPDATE_GUEST_EXTRA_MCP)
        if servers is None:
            return
        await self._cancel_and_drain_inflight()
        self._update_guest_extra_mcp(name=name, servers=servers, sink=sink)

    async def _inbound_create_room(self, data: dict, sink: EnvelopeSink) -> None:
        room_id = _require_str(data, "room_id", where=INBOUND_CREATE_ROOM)
        if room_id is None:
            return
        name = _require_str(data, "name", where=INBOUND_CREATE_ROOM)
        if name is None:
            return
        guests = data.get("guests")
        if not isinstance(guests, list) or not guests:
            _log.warning("ignoring %s: guests 缺 / 空", INBOUND_CREATE_ROOM)
            return
        # 防御：guests 每项至少要 persona:str —— admin.create_room 也校验，这里
        # 早一步报"前端协议不对"而不是被动等到 KeyError。
        if not all(
            isinstance(g, dict) and isinstance(g.get("persona"), str) and g["persona"]
            for g in guests
        ):
            _log.warning("ignoring %s: guests 每项必须含 persona:str", INBOUND_CREATE_ROOM)
            return
        # topic / rules 是可选 str；缺 / 非 str → 空串。一个表达式收 None / 其它类型。
        topic = data.get("topic") if isinstance(data.get("topic"), str) else ""
        rules = data.get("rules") if isinstance(data.get("rules"), str) else ""
        await self._cancel_and_drain_inflight()
        self._create_room(
            room_id=room_id,
            name=name,
            topic=topic,
            rules=rules,
            guests=guests,
            sink=sink,
        )

    async def _inbound_delete_room(self, data: dict, sink: EnvelopeSink) -> None:
        room_id = _require_str(data, "room_id", where=INBOUND_DELETE_ROOM)
        if room_id is None:
            return
        await self._cancel_and_drain_inflight()
        self._delete_room(room_id=room_id, sink=sink)

    async def _inbound_update_user_md(self, data: dict, sink: EnvelopeSink) -> None:
        # content 允许空串：用户清空 USER.md 也算合法状态。
        content = _require_str(
            data, "content", where=INBOUND_UPDATE_USER_MD, allow_empty=True
        )
        if content is None:
            return
        await self._cancel_and_drain_inflight()
        self._update_user_md(content=content, sink=sink)

    async def _inbound_update_room_toml(self, data: dict, sink: EnvelopeSink) -> None:
        # room.toml 内容理论上不该为空，但 admin 层会用 RoomConfigError 拦住，校验
        # 责任不在这层；allow_empty 让传 "" 也走到 admin 拿到结构化错误。
        content = _require_str(
            data, "content", where=INBOUND_UPDATE_ROOM_TOML, allow_empty=True
        )
        if content is None:
            return
        await self._cancel_and_drain_inflight()
        self._update_room_toml(content=content, sink=sink)

    async def _inbound_update_user_avatar(
        self, data: dict, sink: EnvelopeSink
    ) -> None:
        data_uri = _require_str(data, "data_uri", where=INBOUND_UPDATE_USER_AVATAR)
        if data_uri is None:
            return
        # 头像写不动 session，无需 cancel inflight —— 让正在跑的 turn 自然结束。
        self._update_user_avatar(data_uri=data_uri, sink=sink)

    async def _inbound_import_persona_folder(
        self, data: dict, sink: EnvelopeSink
    ) -> None:
        src = _require_str(data, "path", where=INBOUND_IMPORT_PERSONA_FOLDER)
        if src is None:
            return
        # 导入不动 session，无需 cancel inflight。
        self._run_import(
            f"import_persona_folder src={src!r}",
            lambda: persona_import.import_from_folder(self._paths, Path(src)),
            sink,
        )

    async def _inbound_import_persona_github(
        self, data: dict, sink: EnvelopeSink
    ) -> None:
        url = _require_str(data, "url", where=INBOUND_IMPORT_PERSONA_GITHUB)
        if url is None:
            return
        self._run_import(
            f"import_persona_github url={url!r}",
            lambda: persona_import.import_from_github(self._paths, url),
            sink,
        )

    async def _inbound_upload_file(self, data: dict, sink: EnvelopeSink) -> None:
        # 任意校验失败也要 emit FILE_UPLOADED(error) —— 前端串行上传循环靠 echo 推进
        # 队列；inbound 早返不发 echo 会让循环永挂（典型 case：零字节文件 content_b64
        # 为空，_require_str 没 allow_empty 时返 None）。
        raw_filename = data.get("filename")
        # 用 raw 当 echo.original：哪怕是 None / 非 str，转 str 也好让前端能匹配到 pill
        # 占位（虽然 valid 上传里不会触发；这条路径是 wscat 直发的兜底）。
        echo_original = (
            raw_filename if isinstance(raw_filename, str) else ""
        )
        filename = _require_str(data, "filename", where=INBOUND_UPLOAD_FILE)
        if filename is None:
            self._fail_upload(sink, original=echo_original, text="文件名缺失或非法")
            return
        content_b64 = data.get("content_b64")
        if not isinstance(content_b64, str):
            self._fail_upload(
                sink, original=echo_original, text="content_b64 缺失或非 str",
            )
            return
        # 上传不动 session、不挡 inflight turn —— 让正在跑的 turn 自然结束；
        # 文件落房间共享目录，下一条 user_message 才把它带进上下文。
        # 允许 content_b64 == ""（零字节文件）—— _upload_file 内 base64.b64decode("") = b""。
        self._upload_file(filename=filename, content_b64=content_b64, sink=sink)

    def _fail_upload(
        self, sink: EnvelopeSink, *, original: str, text: str,
    ) -> None:
        """上传请求失败的统一回吐：NOTICE 给用户看 + FILE_UPLOADED(error) 让前端推进队列。

        ``_upload_file`` 内部错误路径与 ``_inbound_upload_file`` 的早返路径共用 —— 任何
        UPLOAD_FILE 入帧都必须以一条 FILE_UPLOADED envelope 收尾（成功 / 失败）。
        """
        self._emit_notice(sink, level=NOTICE_LEVEL_ERROR, text=text)
        sink(
            ChahuaEnvelope(
                room_id=self._session.room.name,
                turn_id=None, guest_name=None, message_id=None,
                type=ChahuaEventType.FILE_UPLOADED,
                data={"original": original, "error": text},
            )
        )

    async def _inbound_export_room(self, data: dict, sink: EnvelopeSink) -> None:
        # read-only：不动 session、不挡 inflight turn。
        self._export_room(sink)

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

    async def _inbound_open_task(self, data: dict, sink: EnvelopeSink) -> None:
        if not self._reject_unknown_keys(
            data, _OPEN_TASK_ALLOWED, where=INBOUND_OPEN_TASK, sink=sink,
        ):
            return
        title = _require_str(data, "title", where=INBOUND_OPEN_TASK)
        if title is None:
            return
        goal = _require_str(
            data, "goal", where=INBOUND_OPEN_TASK, allow_empty=True,
        )
        if goal is None:
            return
        owner_raw = data.get("owner")
        if owner_raw is not None and not isinstance(owner_raw, str):
            self._emit_notice(
                sink, level=NOTICE_LEVEL_ERROR,
                text=f"{INBOUND_OPEN_TASK}: owner 必须是 str / null",
            )
            return
        try:
            task = self._session.tasks_store.open_task(
                title=title, goal=goal, owner=owner_raw,
            )
        except TaskExistsError as e:
            self._emit_notice(
                sink, level=NOTICE_LEVEL_ERROR, text=f"开任务失败：{e}",
            )
            # 重发让前端按 tasks.length 把 "+新任务" 按钮 disable（防前端误判）。
            self._emit_task_info(sink)
            return
        except OSError as e:
            self._notice_persist_failure(sink, INBOUND_OPEN_TASK, e)
            return
        _log.info("open_task: %r (id=%s)", title, task.id)
        self._emit_task_envelope(
            sink, type=ChahuaEventType.TASK_OPEN, data=task.to_jsonl_dict(),
        )
        self._emit_task_info(sink)

    async def _inbound_update_task(self, data: dict, sink: EnvelopeSink) -> None:
        if not self._reject_unknown_keys(
            data, _UPDATE_TASK_ALLOWED, where=INBOUND_UPDATE_TASK, sink=sink,
        ):
            return
        task_id = _require_str(data, "task_id", where=INBOUND_UPDATE_TASK)
        if task_id is None:
            return
        patch_raw = data.get("patch")
        if not isinstance(patch_raw, dict):
            self._emit_notice(
                sink, level=NOTICE_LEVEL_ERROR,
                text=f"{INBOUND_UPDATE_TASK}: patch 必须是对象",
            )
            return
        extra = set(patch_raw) - _UPDATE_TASK_PATCH_ALLOWED
        if extra:
            self._emit_notice(
                sink, level=NOTICE_LEVEL_ERROR,
                text=(
                    f"{INBOUND_UPDATE_TASK}: patch 含未知字段 {sorted(extra)!r}；"
                    "P5.1 只支持 title / goal"
                ),
            )
            return
        title = patch_raw.get("title")
        goal = patch_raw.get("goal")
        for key, val in (("title", title), ("goal", goal)):
            if val is not None and not isinstance(val, str):
                self._emit_notice(
                    sink, level=NOTICE_LEVEL_ERROR,
                    text=f"{INBOUND_UPDATE_TASK}: patch.{key} 必须是 str",
                )
                return
        # title 与 open_task 同口径不接受空串（避免改成"无标题"任务卡）；goal 允许空。
        if title == "":
            self._emit_notice(
                sink, level=NOTICE_LEVEL_ERROR,
                text=f"{INBOUND_UPDATE_TASK}: patch.title 不能为空",
            )
            return
        try:
            task = self._session.tasks_store.update_task(
                task_id, title=title, goal=goal,
            )
        except TaskNotFoundError as e:
            self._emit_notice(
                sink, level=NOTICE_LEVEL_ERROR, text=f"更新任务失败：{e}",
            )
            self._emit_task_info(sink)
            return
        except OSError as e:
            self._notice_persist_failure(sink, INBOUND_UPDATE_TASK, e)
            return
        _log.info("update_task: id=%s title=%r goal=%r", task.id, title, goal)
        applied_patch: dict = {}
        if title is not None:
            applied_patch["title"] = title
        if goal is not None:
            applied_patch["goal"] = goal
        self._emit_task_envelope(
            sink,
            type=ChahuaEventType.TASK_UPDATE,
            data={"task_id": task.id, "patch": applied_patch},
        )
        self._emit_task_info(sink)

    async def _inbound_attach_artifact(
        self, data: dict, sink: EnvelopeSink
    ) -> None:
        if not self._reject_unknown_keys(
            data, _ATTACH_ARTIFACT_ALLOWED, where=INBOUND_ATTACH_ARTIFACT, sink=sink,
        ):
            return
        task_id = _require_str(data, "task_id", where=INBOUND_ATTACH_ARTIFACT)
        if task_id is None:
            return
        share_rel = _require_str(
            data, "share_rel", where=INBOUND_ATTACH_ARTIFACT,
        )
        if share_rel is None:
            return
        try:
            # 移入 try：room_dir 只读 / 磁盘满时 mkdir 抛 OSError 也不能逃出 handler。
            share_root = ensure_room_share_dir(self._session.room_config.room_dir)
            info = self._session.tasks_store.attach_artifact(
                task_id, share_rel=share_rel, share_root=share_root,
            )
        except TaskNotFoundError as e:
            # 任务在 UI render 与 click 之间消失 —— 重发 task_info 让前端任务列表复位。
            self._emit_notice(
                sink, level=NOTICE_LEVEL_ERROR, text=f"挂产物失败：{e}",
            )
            self._emit_task_info(sink)
            return
        except ArtifactSourceMissingError as e:
            # 用户给了错路径 —— 任务列表本身没变化，仅 NOTICE 提示即可。
            self._emit_notice(
                sink, level=NOTICE_LEVEL_ERROR, text=f"挂产物失败：{e}",
            )
            return
        except OSError as e:
            self._notice_persist_failure(sink, INBOUND_ATTACH_ARTIFACT, e)
            return
        _log.info(
            "attach_artifact: task=%s share_rel=%r → %s",
            task_id, share_rel, info["rel"],
        )
        self._emit_task_envelope(
            sink,
            type=ChahuaEventType.TASK_ARTIFACT_ADDED,
            data={
                "task_id": task_id,
                "name": info["name"],
                "size": info["size"],
                "rel": info["rel"],
                # P5.1 仅 user 一种来源；P5.4 茶客自动归集会出现 "<guest_name>" 值。
                "created_by": MARKED_BY_USER,
            },
        )
        self._emit_task_info(sink)

    async def _inbound_add_decision(
        self, data: dict, sink: EnvelopeSink
    ) -> None:
        if not self._reject_unknown_keys(
            data, _ADD_DECISION_ALLOWED, where=INBOUND_ADD_DECISION, sink=sink,
        ):
            return
        task_id = _require_str(data, "task_id", where=INBOUND_ADD_DECISION)
        if task_id is None:
            return
        summary = _require_str(data, "summary", where=INBOUND_ADD_DECISION)
        if summary is None:
            return
        sup_raw = data.get("supporting_message_ids", [])
        if not isinstance(sup_raw, list):
            self._emit_notice(
                sink, level=NOTICE_LEVEL_ERROR,
                text=f"{INBOUND_ADD_DECISION}: supporting_message_ids 必须是 list",
            )
            return
        supporting = [x for x in sup_raw if isinstance(x, str)]
        # 防 wscat 绕过前端 maxlength 灌长文。
        summary = summary[:200]
        try:
            decision = self._session.tasks_store.add_decision(
                task_id, supporting_message_ids=supporting, summary=summary,
            )
        except TaskNotFoundError as e:
            self._emit_notice(
                sink, level=NOTICE_LEVEL_ERROR, text=f"记决策失败：{e}",
            )
            self._emit_task_info(sink)
            return
        except OSError as e:
            self._notice_persist_failure(sink, INBOUND_ADD_DECISION, e)
            return
        _log.info(
            "add_decision: task=%s decision=%s sup=%d",
            task_id, decision.decision_id, len(supporting),
        )
        self._emit_task_envelope(
            sink,
            type=ChahuaEventType.TASK_DECISION_ADDED,
            data=decision.to_jsonl_dict(),
        )
        self._emit_task_info(sink)

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
        snapshot_task_id = self._snapshot_active_task_id()
        self._inflight_turn_task = asyncio.create_task(
            self._run_turn(text, sink, task_id=snapshot_task_id),
            name="chahua-turn",
        )

    def _snapshot_active_task_id(self) -> Optional[str]:
        """接帧同步快照当前 active task —— 不能延到 _run_turn 里再读：从这次
        ``create_task`` 到 turn 被调度之间，inbound 队列里排在后面的 ``open_task`` 会改
        active，回追后已经在 transcript 里的用户消息会被错挂到新任务上。
        """
        return self._session.orchestrator.snapshot_active_task_id()


# inbound 帧类型 → handler unbound method 的注册表。``_handle_inbound`` 拿到 type
# 直接查表分派，加新 wire 帧只动 INBOUND_* 常量 + 一个 ``_inbound_<name>`` 方法 +
# 这张表一行；不必再去维护一个 200 行的 if-elif 链。
_InboundHandler = Callable[
    ["ChahuaServer", dict, EnvelopeSink], Awaitable[None]
]
_INBOUND_HANDLERS: dict[str, _InboundHandler] = {
    INBOUND_CANCEL: ChahuaServer._inbound_cancel,
    INBOUND_SWITCH_ROOM: ChahuaServer._inbound_switch_room,
    INBOUND_CLEAR_ROOM: ChahuaServer._inbound_clear_room,
    INBOUND_ADD_GUEST: ChahuaServer._inbound_add_guest,
    INBOUND_REMOVE_GUEST: ChahuaServer._inbound_remove_guest,
    INBOUND_SET_PERSONA_MCP_TRUST: ChahuaServer._inbound_set_persona_mcp_trust,
    INBOUND_UPDATE_GUEST_PERMISSION: ChahuaServer._inbound_update_guest_permission,
    INBOUND_CREATE_ROOM: ChahuaServer._inbound_create_room,
    INBOUND_DELETE_ROOM: ChahuaServer._inbound_delete_room,
    INBOUND_UPDATE_USER_MD: ChahuaServer._inbound_update_user_md,
    INBOUND_UPDATE_ROOM_TOML: ChahuaServer._inbound_update_room_toml,
    INBOUND_UPDATE_ROOM_ORCHESTRATOR: ChahuaServer._inbound_update_room_orchestrator,
    INBOUND_UPDATE_ROOM_LLM: ChahuaServer._inbound_update_room_llm,
    INBOUND_UPDATE_GUEST_LLM: ChahuaServer._inbound_update_guest_llm,
    INBOUND_UPDATE_GUEST_ISOLATION: ChahuaServer._inbound_update_guest_isolation,
    INBOUND_UPDATE_GUEST_EXTRA_MCP: ChahuaServer._inbound_update_guest_extra_mcp,
    INBOUND_UPDATE_USER_AVATAR: ChahuaServer._inbound_update_user_avatar,
    INBOUND_IMPORT_PERSONA_FOLDER: ChahuaServer._inbound_import_persona_folder,
    INBOUND_IMPORT_PERSONA_GITHUB: ChahuaServer._inbound_import_persona_github,
    INBOUND_UPLOAD_FILE: ChahuaServer._inbound_upload_file,
    INBOUND_EXPORT_ROOM: ChahuaServer._inbound_export_room,
    INBOUND_OPEN_TASK: ChahuaServer._inbound_open_task,
    INBOUND_UPDATE_TASK: ChahuaServer._inbound_update_task,
    INBOUND_ATTACH_ARTIFACT: ChahuaServer._inbound_attach_artifact,
    INBOUND_ADD_DECISION: ChahuaServer._inbound_add_decision,
    INBOUND_USER_MESSAGE: ChahuaServer._inbound_user_message,
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
