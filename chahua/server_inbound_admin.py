"""房间 / 茶客 / persona 管理 inbound mixin（P5.2 重构，docs §7.2）。

涵盖最大的一类 inbound：guest add/remove/permission/llm/isolation/extra_mcp、room
create/delete、orchestrator 编排参数、房间默认 LLM、persona MCP 信任。

依赖核心 ``ChahuaServer`` 提供：``self._session`` / ``self._paths`` / ``self._emit_notice``
/ ``self._emit_room_info`` / ``self._emit_room_snapshot`` / ``self._replace_session``
/ ``self._cancel_and_drain_inflight``。
"""

from __future__ import annotations

import logging
from typing import Optional

from . import admin, trust
from ._server_helpers import (
    check_optional_dict,
    require_bool,
    require_list,
    require_str,
)
from .events import EnvelopeSink
from .permissions import DEFAULT_MODE
from .persona_assets import persona_relative

_log = logging.getLogger(__name__)


INBOUND_ADD_GUEST = "add_guest"
INBOUND_REMOVE_GUEST = "remove_guest"
INBOUND_UPDATE_GUEST_PERMISSION = "update_guest_permission"
INBOUND_SET_PERSONA_MCP_TRUST = "set_persona_mcp_trust"
INBOUND_CREATE_ROOM = "create_room"
INBOUND_DELETE_ROOM = "delete_room"
INBOUND_UPDATE_ROOM_ORCHESTRATOR = "update_room_orchestrator"
INBOUND_UPDATE_ROOM_LLM = "update_room_llm"
INBOUND_UPDATE_GUEST_LLM = "update_guest_llm"
INBOUND_UPDATE_GUEST_ISOLATION = "update_guest_isolation"
INBOUND_UPDATE_GUEST_EXTRA_MCP = "update_guest_extra_mcp"


class AdminHandlersMixin:
    """房间 / 茶客管理 inbound + worker 集合。

    多数 worker 形如 "改 toml → ``_replace_session`` → emit snapshot" —— admin 层做配置
    校验 + 磁盘回滚，本 mixin 只负责 cancel / replace / emit 三件事。
    """

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

    # ── inbound 帧 ────────────────────────────────────────────────────────

    async def _inbound_add_guest(self, data: dict, sink: EnvelopeSink) -> None:
        persona = require_str(data, "persona", where=INBOUND_ADD_GUEST)
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
        name = require_str(data, "name", where=INBOUND_REMOVE_GUEST)
        if name is None:
            return
        await self._cancel_and_drain_inflight()
        self._remove_guest(name=name, sink=sink)

    async def _inbound_set_persona_mcp_trust(
        self, data: dict, sink: EnvelopeSink
    ) -> None:
        persona_rel = require_str(
            data, "persona_rel", where=INBOUND_SET_PERSONA_MCP_TRUST
        )
        if persona_rel is None:
            return
        trusted = require_bool(data, "trusted", where=INBOUND_SET_PERSONA_MCP_TRUST)
        if trusted is None:
            return
        await self._cancel_and_drain_inflight()
        self._set_persona_mcp_trust(
            persona_rel=persona_rel, trusted=trusted, sink=sink
        )

    async def _inbound_update_guest_permission(
        self, data: dict, sink: EnvelopeSink
    ) -> None:
        name = require_str(data, "name", where=INBOUND_UPDATE_GUEST_PERMISSION)
        if name is None:
            return
        permission = require_str(
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
        if not check_optional_dict(data, "spec", where=INBOUND_UPDATE_ROOM_LLM):
            return
        await self._cancel_and_drain_inflight()
        self._update_room_llm(
            section=section, spec_dict=data.get("spec"), sink=sink
        )

    async def _inbound_update_guest_llm(
        self, data: dict, sink: EnvelopeSink
    ) -> None:
        name = require_str(data, "name", where=INBOUND_UPDATE_GUEST_LLM)
        if name is None:
            return
        if not check_optional_dict(data, "spec", where=INBOUND_UPDATE_GUEST_LLM):
            return
        await self._cancel_and_drain_inflight()
        self._update_guest_llm(name=name, spec_dict=data.get("spec"), sink=sink)

    async def _inbound_update_guest_isolation(
        self, data: dict, sink: EnvelopeSink
    ) -> None:
        name = require_str(data, "name", where=INBOUND_UPDATE_GUEST_ISOLATION)
        if name is None:
            return
        isolation = require_str(
            data, "isolation", where=INBOUND_UPDATE_GUEST_ISOLATION
        )
        if isolation is None:
            return
        await self._cancel_and_drain_inflight()
        self._update_guest_isolation(name=name, isolation=isolation, sink=sink)

    async def _inbound_update_guest_extra_mcp(
        self, data: dict, sink: EnvelopeSink
    ) -> None:
        name = require_str(data, "name", where=INBOUND_UPDATE_GUEST_EXTRA_MCP)
        if name is None:
            return
        # 每项 dict 内字段校验在 admin 层 _build_extra_mcp_servers；这里只挡 wire 层。
        servers = require_list(data, "servers", where=INBOUND_UPDATE_GUEST_EXTRA_MCP)
        if servers is None:
            return
        await self._cancel_and_drain_inflight()
        self._update_guest_extra_mcp(name=name, servers=servers, sink=sink)

    async def _inbound_create_room(self, data: dict, sink: EnvelopeSink) -> None:
        room_id = require_str(data, "room_id", where=INBOUND_CREATE_ROOM)
        if room_id is None:
            return
        name = require_str(data, "name", where=INBOUND_CREATE_ROOM)
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
        room_id = require_str(data, "room_id", where=INBOUND_DELETE_ROOM)
        if room_id is None:
            return
        await self._cancel_and_drain_inflight()
        self._delete_room(room_id=room_id, sink=sink)
