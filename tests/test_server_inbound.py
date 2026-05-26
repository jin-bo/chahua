"""``ChahuaServer._handle_inbound`` 的协议分派回归。

测的是「wire msg_type → 哪个 mutator + 用啥参数」的口径，不测 mutator 内部行为
（那些归 ``test_admin.py`` / ``test_trust.py`` 等）。每个 ``INBOUND_*`` 一正一错，
错例覆盖最容易写错的 missing / 非法类型字段。

测法：用 ``object.__new__`` 绕过 ``ChahuaServer.__init__``（避免真起 session），
然后挂 recorder：

- 同步 mutator (``_switch_room`` / ``_add_guest`` / ... ) → 记录调用参数。
- ``_cancel_and_drain_inflight`` → 计数 await 次数。
- ``_emit_room_info`` / ``_emit_room_snapshot`` / ``_emit_notice`` → 计数，证明
  错路径有给前端回复位 envelope。
- ``_run_turn`` → 不真起 asyncio.task，``user_message`` 走 _handle_inbound
  里手工塞 ``_inflight_turn_task`` 验"drop while in-flight"。
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

import pytest

from chahua.server import (
    ChahuaServer,
    _bind_inbound_handlers,
    INBOUND_ADD_GUEST,
    INBOUND_CANCEL,
    INBOUND_CLEAR_ROOM,
    INBOUND_CREATE_ROOM,
    INBOUND_DELETE_ROOM,
    INBOUND_IMPORT_PERSONA_FOLDER,
    INBOUND_IMPORT_PERSONA_GITHUB,
    INBOUND_REMOVE_GUEST,
    INBOUND_SET_PERSONA_MCP_TRUST,
    INBOUND_SWITCH_ROOM,
    INBOUND_UPDATE_GUEST_EXTRA_MCP,
    INBOUND_UPDATE_GUEST_ISOLATION,
    INBOUND_UPDATE_GUEST_LLM,
    INBOUND_UPDATE_GUEST_PERMISSION,
    INBOUND_UPDATE_ROOM_LLM,
    INBOUND_UPDATE_ROOM_ORCHESTRATOR,
    INBOUND_UPDATE_ROOM_TOML,
    INBOUND_UPDATE_USER_AVATAR,
    INBOUND_UPDATE_USER_MD,
    INBOUND_UPLOAD_FILE,
    INBOUND_EXPORT_ROOM,
    INBOUND_USER_MESSAGE,
)


# ── 测试夹具 ──────────────────────────────────────────────────────────────


from chahua.events import NOOP_SINK
from chahua.room_runtime import RoomEventRouter, RoomRuntime
from chahua.server_inbound_admin import AdminHandlers
from chahua.server_inbound_handoff import HandoffHandlers
from chahua.server_inbound_io import IOHandlers
from chahua.server_inbound_settings import SettingsHandlers
from chahua.server_inbound_task import TaskHandlers


# 各 spy slot **继承**真实 handler，自动复用 ``_inbound_*`` payload 校验；只覆盖 worker
# 把调用参数记到 ``self.server.calls``，路由测拦截到这一层就够，不真改 toml / 装 session。


class _SpyAdmin(AdminHandlers):
    def _add_guest(self, *, persona, name, permission, sink):
        self.server.calls.append((
            "_add_guest",
            {"persona": persona, "name": name, "permission": permission},
        ))

    def _remove_guest(self, *, name, sink):
        self.server.calls.append(("_remove_guest", {"name": name}))

    def _set_persona_mcp_trust(self, *, persona_rel, trusted, sink):
        self.server.calls.append((
            "_set_persona_mcp_trust",
            {"persona_rel": persona_rel, "trusted": trusted},
        ))

    def _update_guest_permission(self, *, name, permission, sink):
        self.server.calls.append((
            "_update_guest_permission",
            {"name": name, "permission": permission},
        ))

    def _create_room(self, *, room_id, name, topic, rules, guests, sink):
        self.server.calls.append((
            "_create_room",
            {
                "room_id": room_id, "name": name, "topic": topic,
                "rules": rules, "guests": guests,
            },
        ))

    def _delete_room(self, *, room_id, sink):
        self.server.calls.append(("_delete_room", {"room_id": room_id}))

    def _update_room_orchestrator(self, *, overrides, sink):
        self.server.calls.append(("_update_room_orchestrator", {"overrides": overrides}))

    def _update_room_llm(self, *, section, spec_dict, sink):
        self.server.calls.append((
            "_update_room_llm", {"section": section, "spec_dict": spec_dict},
        ))

    def _update_guest_llm(self, *, name, spec_dict, sink):
        self.server.calls.append((
            "_update_guest_llm", {"name": name, "spec_dict": spec_dict},
        ))

    def _update_guest_isolation(self, *, name, isolation, sink):
        self.server.calls.append((
            "_update_guest_isolation", {"name": name, "isolation": isolation},
        ))

    def _update_guest_extra_mcp(self, *, name, servers, sink):
        self.server.calls.append((
            "_update_guest_extra_mcp", {"name": name, "servers": servers},
        ))


class _SpySettings(SettingsHandlers):
    def _update_user_md(self, *, content, sink):
        self.server.calls.append(("_update_user_md", {"content": content}))

    def _update_room_toml(self, *, content, sink):
        self.server.calls.append(("_update_room_toml", {"content": content}))

    def _update_user_avatar(self, *, data_uri, sink):
        self.server.calls.append(("_update_user_avatar", {"data_uri": data_uri}))


class _SpyIO(IOHandlers):
    def _upload_file(self, *, filename, content_b64, sink):
        self.server.calls.append((
            "_upload_file",
            {"filename": filename, "content_b64": content_b64},
        ))

    def _export_room(self, sink):
        self.server.calls.append(("_export_room", {}))

    def _run_import(self, label, op, sink):
        # 把 op 留下来，让测试断言它是 partial / lambda 即可，不真跑导入。
        self.server.calls.append(("_run_import", {"label": label}))

    def _fail_upload(self, sink, *, original: str, text: str) -> None:
        # 路由测不挂真 session；记 NOTICE 即可，envelope 构造跳过避免访问 self._session。
        self.server.notices.append(("error", text))


class _SpyTask(TaskHandlers):
    def _snapshot_active_task_id(self):
        # 路由测不挂真 session；user_message 路径需要它返回 None 才不去访问 self._session。
        return None


class _SpyServer(ChahuaServer):
    """绕开 __init__，只挂 ``_handle_inbound`` 真正访问的状态 + 记录器。

    刻意**不**填 ``_session`` / ``_paths`` / ``_host`` 等：路由测里这些不该被读到，
    若将来 ``_handle_inbound`` 开始访问它们应该 ``AttributeError`` 让我们立刻注意到
    新增的依赖，而不是悄悄拿到 dummy 值绕过测试。

    P5.2 起 inbound handler 拆 slot；spy slot 继承真实 handler 复用 ``_inbound_*`` 校验，
    只覆盖 worker 拦截记录。dispatch 表用 :func:`_bind_inbound_handlers` 同款解析。
    """

    def __init__(self) -> None:  # type: ignore[override]
        # P9 9.1.3：``_inflight_*`` / ``_session`` 已是读写前台 runtime 的 property。
        # 装一个 spy RoomRuntime（``session=None`` —— 路由测不该读 session，真读到
        # ``None._xxx`` 仍 AttributeError 暴露依赖），让那些 property 有处可落。
        self._runtimes = {
            "spy": RoomRuntime(
                room_id="spy",
                session=None,  # type: ignore[arg-type]
                router=RoomEventRouter(NOOP_SINK),
            )
        }
        self._foreground_id = "spy"
        self._inflight_turn_task = None
        self._inflight_kind = None
        self.calls: list[tuple[str, dict]] = []
        self.cancel_drain_count = 0
        self.emit_room_info_count = 0
        self.emit_snapshot_count = 0
        self.notices: list[tuple[str, str]] = []
        self.run_turn_args: list[str] = []
        # 装 spy slot —— 与 ChahuaServer.__init__ 同布局，但 handler 实例是 spy。
        self.admin = _SpyAdmin(self)
        self.io = _SpyIO(self)
        self.settings = _SpySettings(self)
        self.task = _SpyTask(self)
        # handoff slot 无 spy 子类 —— test_server_inbound 不测 handoff 路由，装真实
        # HandoffHandlers 只为让 _bind_inbound_handlers 能解析 handoff.* 路径。
        self.handoff = HandoffHandlers(self)
        # agent_run slot 同上 —— 装真实 AgentRunHandlers 仅为解析路径。
        from chahua.server_inbound_agent_run import AgentRunHandlers
        self.agent_run = AgentRunHandlers(self)
        # ``_agent_run_ops`` 也要装，否则后续若有路由测 dispatch INBOUND_AGENT_RUN_START 经
        # spy fixture 会触发 ``srv._start_agent_run`` forwarder → 读 ``self._agent_run_ops``
        # AttributeError（forwarder 不带 lazy fallback）。当前测试没走这条路径但显式装齐
        # 保未来零陷阱。
        from chahua._server_agent_run import AgentRunOps
        self._agent_run_ops = AgentRunOps(self)
        self._inbound_handlers = _bind_inbound_handlers(self)

    async def _cancel_and_drain_inflight(self) -> None:  # type: ignore[override]
        self.cancel_drain_count += 1

    async def _cancel_and_drain_all_foreground(self) -> None:  # type: ignore[override]
        # P11 C9：admin/settings/clear_room 路径改走新 5 步 helper；spy 把它当
        # 同一计数器，让既有 `cancel_drain_count == 1` 断言保持语义（「mutation 前
        # 必走 drain」）不变。
        self.cancel_drain_count += 1

    def _cancel_inflight(self) -> None:  # type: ignore[override]
        self.calls.append(("_cancel_inflight", {}))

    def _maybe_end_managed_session(self, sink, *, reason):  # type: ignore[override]
        # P8.3：路由 spy 刻意不挂 _session —— cancel 路由测不关心 MTS 收尾，
        # 覆盖成 no-op（真行为见 test_managed_session.py）。
        pass

    def _emit_room_info(self, sink) -> None:  # type: ignore[override]
        self.emit_room_info_count += 1

    def _emit_room_snapshot(self, sink) -> None:  # type: ignore[override]
        self.emit_snapshot_count += 1

    def _emit_notice(self, sink, *, level: str, text: str) -> None:  # type: ignore[override]
        self.notices.append((level, text))

    # ── core inbound workers 仍挂 ChahuaServer 自身：switch / clear。──────────

    def _foreground_session_has_global_guest(self) -> bool:  # type: ignore[override]
        # 路由 spy 不挂真 _session —— switch_room 路由测不关心 global 茶客分流。
        return False

    def _switch_room(self, room_id, sink):  # type: ignore[override]
        self.calls.append(("_switch_room", {"room_id": room_id}))

    def _clear_room(self, sink):  # type: ignore[override]
        self.calls.append(("_clear_room", {}))

    async def _run_turn(self, runtime, text, *, task_id=None):  # type: ignore[override]
        # P9 9.1.3：_run_turn 改为「针对某 runtime」—— spy 同步签名。
        self.run_turn_args.append(text)


def _sink(env) -> None:
    pass


@pytest.fixture
def srv() -> _SpyServer:
    return _SpyServer()


@asynccontextmanager
async def _fake_inflight(srv: _SpyServer):
    """给 srv 临时挂一个未完成的 task，让 ``_inflight_alive()`` 返 True。

    用 ``asyncio.sleep(3600)`` 当占位 —— 测试会在 ``finally`` 里 cancel 掉，sleep
    永远不会真等到时长。两个"in-flight 干扰路由"用例共用。
    """
    task = asyncio.create_task(asyncio.sleep(3600))
    srv._inflight_turn_task = task
    try:
        yield task
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


# ── cancel / switch_room / clear_room ────────────────────────────────────


async def test_cancel_ignored_when_no_inflight(srv: _SpyServer, caplog):
    caplog.set_level(logging.INFO, logger="chahua.server")
    await srv._handle_inbound({"type": INBOUND_CANCEL, "turn_id": "t1"}, _sink)
    assert srv.calls == []  # _cancel_inflight 不调
    assert any("cancel ignored" in r.message for r in caplog.records)


async def test_cancel_with_inflight_calls_cancel(srv: _SpyServer):
    async with _fake_inflight(srv):
        await srv._handle_inbound({"type": INBOUND_CANCEL, "turn_id": "t1"}, _sink)
        assert srv.calls == [("_cancel_inflight", {})]


async def test_switch_room_ok(srv: _SpyServer):
    await srv._handle_inbound(
        {"type": INBOUND_SWITCH_ROOM, "room_id": "p3"}, _sink
    )
    # P9：切房不再 cancel —— 旧前台若 busy 由 _switch_room 转后台续跑。
    assert srv.cancel_drain_count == 0
    assert srv.calls == [("_switch_room", {"room_id": "p3"})]


@pytest.mark.parametrize("payload", [
    {"type": INBOUND_SWITCH_ROOM},               # missing
    {"type": INBOUND_SWITCH_ROOM, "room_id": ""},  # empty
    {"type": INBOUND_SWITCH_ROOM, "room_id": 1},   # non-string
])
async def test_switch_room_bad_payload(srv: _SpyServer, payload):
    await srv._handle_inbound(payload, _sink)
    assert srv.calls == []
    assert srv.cancel_drain_count == 0


async def test_clear_room(srv: _SpyServer):
    await srv._handle_inbound({"type": INBOUND_CLEAR_ROOM}, _sink)
    assert srv.cancel_drain_count == 1
    assert srv.calls == [("_clear_room", {})]


# ── add_guest / remove_guest / update_permission ─────────────────────────


async def test_add_guest_ok(srv: _SpyServer):
    await srv._handle_inbound(
        {
            "type": INBOUND_ADD_GUEST,
            "persona": "chahua/personas/宝总/宝总.md",
            "name": None,  # 允许 null
            "permission": "workspace-write",
        },
        _sink,
    )
    assert srv.cancel_drain_count == 1
    assert srv.calls == [(
        "_add_guest",
        {"persona": "chahua/personas/宝总/宝总.md", "name": None, "permission": "workspace-write"},
    )]


async def test_add_guest_default_permission(srv: _SpyServer):
    """permission 缺 → 透传 None 给 admin 层。

    P12 C3 承重不变量第 6 条：默认值合一仅在 admin 层；inbound 全链路用 None 表示
    "用户未显式选"。manifest defaults → DEFAULT_MODE 三级 coalesce 在
    :func:`chahua.admin_room._build_guest_with_manifest_defaults` 内完成，不在 inbound。
    """
    await srv._handle_inbound(
        {"type": INBOUND_ADD_GUEST, "persona": "p.md"},
        _sink,
    )
    assert srv.calls == [(
        "_add_guest", {"persona": "p.md", "name": None, "permission": None}
    )]


@pytest.mark.parametrize("payload", [
    {"type": INBOUND_ADD_GUEST},                                    # missing persona
    {"type": INBOUND_ADD_GUEST, "persona": ""},                      # empty
    {"type": INBOUND_ADD_GUEST, "persona": 1},                       # non-string
    {"type": INBOUND_ADD_GUEST, "persona": "p.md", "name": 42},      # name not str/null
    {"type": INBOUND_ADD_GUEST, "persona": "p.md", "permission": 1}, # permission non-string
])
async def test_add_guest_bad_payload(srv: _SpyServer, payload):
    await srv._handle_inbound(payload, _sink)
    assert srv.calls == []


async def test_remove_guest_ok(srv: _SpyServer):
    await srv._handle_inbound(
        {"type": INBOUND_REMOVE_GUEST, "name": "宝总"}, _sink
    )
    assert srv.cancel_drain_count == 1
    assert srv.calls == [("_remove_guest", {"name": "宝总"})]


@pytest.mark.parametrize("payload", [
    {"type": INBOUND_REMOVE_GUEST},
    {"type": INBOUND_REMOVE_GUEST, "name": ""},
    {"type": INBOUND_REMOVE_GUEST, "name": 0},
])
async def test_remove_guest_bad_payload(srv: _SpyServer, payload):
    await srv._handle_inbound(payload, _sink)
    assert srv.calls == []


async def test_update_guest_permission_ok(srv: _SpyServer):
    await srv._handle_inbound(
        {
            "type": INBOUND_UPDATE_GUEST_PERMISSION,
            "name": "宝总",
            "permission": "read-only",
        },
        _sink,
    )
    assert srv.calls == [(
        "_update_guest_permission",
        {"name": "宝总", "permission": "read-only"},
    )]


@pytest.mark.parametrize("payload", [
    {"type": INBOUND_UPDATE_GUEST_PERMISSION, "permission": "read-only"},  # 没 name
    {"type": INBOUND_UPDATE_GUEST_PERMISSION, "name": "宝总"},               # 没 permission
    {"type": INBOUND_UPDATE_GUEST_PERMISSION, "name": "", "permission": "read-only"},
])
async def test_update_guest_permission_bad_payload(srv: _SpyServer, payload):
    await srv._handle_inbound(payload, _sink)
    assert srv.calls == []


# ── update_room_orchestrator（P4.0）────────────────────────────────────────


async def test_update_room_orchestrator_ok(srv: _SpyServer):
    """编排参数热替 ——`swap_room_config` 一次属性赋值生效，不需 cancel in-flight。"""
    await srv._handle_inbound(
        {
            "type": INBOUND_UPDATE_ROOM_ORCHESTRATOR,
            "overrides": {"want_threshold": 0.7, "max_consecutive_ai_turns": 6},
        },
        _sink,
    )
    assert srv.cancel_drain_count == 0
    assert srv.calls == [(
        "_update_room_orchestrator",
        {"overrides": {"want_threshold": 0.7, "max_consecutive_ai_turns": 6}},
    )]


async def test_update_room_orchestrator_empty_overrides_dispatches(srv: _SpyServer):
    """空 dict 是合法 payload —— 语义"清掉所有 override，让默认值接管"。"""
    await srv._handle_inbound(
        {"type": INBOUND_UPDATE_ROOM_ORCHESTRATOR, "overrides": {}}, _sink,
    )
    assert srv.calls == [("_update_room_orchestrator", {"overrides": {}})]


@pytest.mark.parametrize("payload", [
    {"type": INBOUND_UPDATE_ROOM_ORCHESTRATOR},                          # 没 overrides
    {"type": INBOUND_UPDATE_ROOM_ORCHESTRATOR, "overrides": None},        # null
    {"type": INBOUND_UPDATE_ROOM_ORCHESTRATOR, "overrides": [0.5]},       # list
    {"type": INBOUND_UPDATE_ROOM_ORCHESTRATOR, "overrides": "want=0.5"},  # str
])
async def test_update_room_orchestrator_bad_payload(srv: _SpyServer, payload):
    await srv._handle_inbound(payload, _sink)
    assert srv.calls == []
    assert srv.cancel_drain_count == 0


# ── persona mcp trust ────────────────────────────────────────────────────


async def test_set_persona_mcp_trust_ok(srv: _SpyServer):
    await srv._handle_inbound(
        {
            "type": INBOUND_SET_PERSONA_MCP_TRUST,
            "persona_rel": "chahua/personas/foo/foo.md",
            "trusted": True,
        },
        _sink,
    )
    assert srv.calls == [(
        "_set_persona_mcp_trust",
        {"persona_rel": "chahua/personas/foo/foo.md", "trusted": True},
    )]


@pytest.mark.parametrize("payload", [
    {"type": INBOUND_SET_PERSONA_MCP_TRUST, "trusted": True},
    {"type": INBOUND_SET_PERSONA_MCP_TRUST, "persona_rel": "x", "trusted": "yes"},  # 非 bool
    {"type": INBOUND_SET_PERSONA_MCP_TRUST, "persona_rel": "x"},  # 没 trusted
])
async def test_set_persona_mcp_trust_bad_payload(srv: _SpyServer, payload):
    await srv._handle_inbound(payload, _sink)
    assert srv.calls == []


# ── create_room / delete_room ────────────────────────────────────────────


async def test_create_room_ok(srv: _SpyServer):
    payload = {
        "type": INBOUND_CREATE_ROOM,
        "room_id": "p9",
        "name": "九号房",
        "topic": "测试",
        "rules": "",
        "guests": [{"persona": "p.md"}, {"persona": "q.md", "name": "q1"}],
    }
    await srv._handle_inbound(payload, _sink)
    assert srv.calls and srv.calls[0][0] == "_create_room"
    args = srv.calls[0][1]
    assert args["room_id"] == "p9"
    assert args["name"] == "九号房"
    assert args["topic"] == "测试"
    assert len(args["guests"]) == 2


async def test_create_room_topic_default_empty_when_missing(srv: _SpyServer):
    """topic / rules 缺省 → 空串（前端可不填）。"""
    await srv._handle_inbound(
        {
            "type": INBOUND_CREATE_ROOM,
            "room_id": "p9",
            "name": "九",
            "guests": [{"persona": "p.md"}],
        },
        _sink,
    )
    assert srv.calls[0][1]["topic"] == ""
    assert srv.calls[0][1]["rules"] == ""


@pytest.mark.parametrize("payload", [
    {"type": INBOUND_CREATE_ROOM, "name": "x", "guests": [{"persona": "p"}]},  # 没 room_id
    {"type": INBOUND_CREATE_ROOM, "room_id": "x", "guests": [{"persona": "p"}]},  # 没 name
    {"type": INBOUND_CREATE_ROOM, "room_id": "x", "name": "x"},                   # 没 guests
    {"type": INBOUND_CREATE_ROOM, "room_id": "x", "name": "x", "guests": []},     # 空 guests
    {"type": INBOUND_CREATE_ROOM, "room_id": "x", "name": "x", "guests": [{}]},   # guest 缺 persona
    {"type": INBOUND_CREATE_ROOM, "room_id": "x", "name": "x", "guests": [{"persona": ""}]},  # 空 persona
])
async def test_create_room_bad_payload(srv: _SpyServer, payload):
    await srv._handle_inbound(payload, _sink)
    assert srv.calls == []


async def test_delete_room_ok(srv: _SpyServer):
    await srv._handle_inbound(
        {"type": INBOUND_DELETE_ROOM, "room_id": "p9"}, _sink
    )
    assert srv.calls == [("_delete_room", {"room_id": "p9"})]


async def test_delete_room_bad_payload(srv: _SpyServer):
    await srv._handle_inbound({"type": INBOUND_DELETE_ROOM}, _sink)
    assert srv.calls == []


# ── update_user_md / update_room_toml / update_user_avatar ───────────────


async def test_update_user_md_ok(srv: _SpyServer):
    await srv._handle_inbound(
        {"type": INBOUND_UPDATE_USER_MD, "content": "# hi"}, _sink
    )
    assert srv.calls == [("_update_user_md", {"content": "# hi"})]


async def test_update_user_md_accepts_empty_string(srv: _SpyServer):
    """空串是合法 content（用户主动清空 USER.md）。"""
    await srv._handle_inbound(
        {"type": INBOUND_UPDATE_USER_MD, "content": ""}, _sink
    )
    assert srv.calls == [("_update_user_md", {"content": ""})]


async def test_update_user_md_bad_payload(srv: _SpyServer):
    await srv._handle_inbound(
        {"type": INBOUND_UPDATE_USER_MD, "content": 42}, _sink
    )
    assert srv.calls == []


async def test_update_room_toml_ok(srv: _SpyServer):
    await srv._handle_inbound(
        {"type": INBOUND_UPDATE_ROOM_TOML, "content": '[room]\nname = "X"\n'}, _sink
    )
    assert srv.calls and srv.calls[0][0] == "_update_room_toml"


async def test_update_room_toml_bad_payload(srv: _SpyServer):
    await srv._handle_inbound(
        {"type": INBOUND_UPDATE_ROOM_TOML, "content": None}, _sink
    )
    assert srv.calls == []


async def test_update_user_avatar_ok(srv: _SpyServer):
    await srv._handle_inbound(
        {"type": INBOUND_UPDATE_USER_AVATAR, "data_uri": "data:image/png;base64,XXX"},
        _sink,
    )
    assert srv.calls == [(
        "_update_user_avatar",
        {"data_uri": "data:image/png;base64,XXX"},
    )]
    # 头像不走 cancel-inflight。
    assert srv.cancel_drain_count == 0


@pytest.mark.parametrize("payload", [
    {"type": INBOUND_UPDATE_USER_AVATAR},
    {"type": INBOUND_UPDATE_USER_AVATAR, "data_uri": ""},
])
async def test_update_user_avatar_bad_payload(srv: _SpyServer, payload):
    await srv._handle_inbound(payload, _sink)
    assert srv.calls == []


# ── persona import / upload ──────────────────────────────────────────────


async def test_import_persona_folder_ok(srv: _SpyServer):
    await srv._handle_inbound(
        {"type": INBOUND_IMPORT_PERSONA_FOLDER, "path": "/tmp/persona-x"}, _sink
    )
    assert srv.calls and srv.calls[0][0] == "_run_import"
    assert "import_persona_folder" in srv.calls[0][1]["label"]
    # 不走 cancel-inflight（导入不动 session）。
    assert srv.cancel_drain_count == 0


async def test_import_persona_folder_bad_payload(srv: _SpyServer):
    await srv._handle_inbound({"type": INBOUND_IMPORT_PERSONA_FOLDER}, _sink)
    assert srv.calls == []


async def test_import_persona_github_ok(srv: _SpyServer):
    await srv._handle_inbound(
        {"type": INBOUND_IMPORT_PERSONA_GITHUB, "url": "https://github.com/x/y"},
        _sink,
    )
    assert srv.calls and srv.calls[0][0] == "_run_import"
    assert "import_persona_github" in srv.calls[0][1]["label"]


async def test_import_persona_github_bad_payload(srv: _SpyServer):
    await srv._handle_inbound({"type": INBOUND_IMPORT_PERSONA_GITHUB}, _sink)
    assert srv.calls == []


async def test_upload_file_ok(srv: _SpyServer):
    await srv._handle_inbound(
        {
            "type": INBOUND_UPLOAD_FILE,
            "filename": "a.txt",
            "content_b64": "aGk=",
        },
        _sink,
    )
    assert srv.calls == [(
        "_upload_file",
        {"filename": "a.txt", "content_b64": "aGk="},
    )]
    # 上传不挡 inflight。
    assert srv.cancel_drain_count == 0


@pytest.mark.parametrize("payload", [
    {"type": INBOUND_UPLOAD_FILE, "content_b64": "x"},          # 没 filename
    {"type": INBOUND_UPLOAD_FILE, "filename": "a.txt"},          # 没 content_b64
    {"type": INBOUND_UPLOAD_FILE, "filename": "", "content_b64": "x"},
    # 注：``filename="a", content_b64=""``（零字节文件）现在是合法上传 ——
    # _inbound_upload_file 会把它喂给 _upload_file，base64.b64decode("") = b""。
])
async def test_upload_file_bad_payload(srv: _SpyServer, payload):
    await srv._handle_inbound(payload, _sink)
    assert srv.calls == []


async def test_upload_file_zero_byte_passes_through(srv: _SpyServer):
    """零字节文件 content_b64="" 是合法上传 —— 透传到 _upload_file，前端串行循环靠
    FILE_UPLOADED echo 推进队列；inbound 早返不发 echo 会让前端永挂（codex round 10）。"""
    await srv._handle_inbound(
        {"type": INBOUND_UPLOAD_FILE, "filename": "empty.txt", "content_b64": ""},
        _sink,
    )
    assert srv.calls == [
        ("_upload_file", {"filename": "empty.txt", "content_b64": ""}),
    ]


async def test_export_room_dispatches(srv: _SpyServer):
    """EXPORT_ROOM 帧 → _export_room（不带 payload，直接读 server 端 session）。"""
    await srv._handle_inbound({"type": INBOUND_EXPORT_ROOM}, _sink)
    assert srv.calls == [("_export_room", {})]
    # 导出 read-only，不挡 inflight turn。
    assert srv.cancel_drain_count == 0


# ── user_message：text / files / drop while in-flight / 未知 type ────────


async def test_user_message_ok_creates_turn(srv: _SpyServer):
    await srv._handle_inbound(
        {"type": INBOUND_USER_MESSAGE, "text": "你好"}, _sink
    )
    # _inflight_turn_task 被 asyncio.create_task 包出来，要 await 完才能 inspect。
    assert srv._inflight_turn_task is not None
    await srv._inflight_turn_task
    assert srv.run_turn_args == ["你好"]


async def test_user_message_with_files_appends_refs(srv: _SpyServer):
    await srv._handle_inbound(
        {
            "type": INBOUND_USER_MESSAGE,
            "text": "看下",
            "files": ["share/a.txt", "share/b.png"],
        },
        _sink,
    )
    assert srv._inflight_turn_task is not None
    await srv._inflight_turn_task
    text = srv.run_turn_args[0]
    assert "看下" in text
    assert "<./share/a.txt>" in text
    assert "<./share/b.png>" in text


async def test_user_message_files_only_no_text(srv: _SpyServer):
    """没打字但附了文件 —— 仍走 turn，text 就是文件引用。"""
    await srv._handle_inbound(
        {"type": INBOUND_USER_MESSAGE, "text": "", "files": ["share/a.txt"]},
        _sink,
    )
    assert srv._inflight_turn_task is not None
    await srv._inflight_turn_task
    assert srv.run_turn_args == ["<./share/a.txt>"]


async def test_user_message_empty_no_files_dropped(srv: _SpyServer):
    await srv._handle_inbound(
        {"type": INBOUND_USER_MESSAGE, "text": ""}, _sink
    )
    assert srv._inflight_turn_task is None
    assert srv.run_turn_args == []


async def test_user_message_non_string_text_dropped(srv: _SpyServer):
    await srv._handle_inbound(
        {"type": INBOUND_USER_MESSAGE, "text": 42}, _sink
    )
    assert srv._inflight_turn_task is None


async def test_user_message_dropped_while_inflight(srv: _SpyServer):
    async with _fake_inflight(srv):
        await srv._handle_inbound(
            {"type": INBOUND_USER_MESSAGE, "text": "again"}, _sink
        )
        assert srv.run_turn_args == []


async def test_unknown_type_ignored(srv: _SpyServer, caplog):
    caplog.set_level(logging.WARNING, logger="chahua.server")
    await srv._handle_inbound({"type": "future_feature"}, _sink)
    assert srv.calls == []
    assert srv._inflight_turn_task is None
    assert any("unknown type" in r.message for r in caplog.records)


# ── update_room_llm / update_guest_llm（P4.1）─────────────────────────────


async def test_update_room_llm_ok(srv: _SpyServer):
    await srv._handle_inbound(
        {
            "type": INBOUND_UPDATE_ROOM_LLM,
            "section": "scoring",
            "spec": {"model": "openai/gpt-5.4-mini"},
        },
        _sink,
    )
    assert srv.cancel_drain_count == 1
    assert srv.calls == [(
        "_update_room_llm",
        {"section": "scoring", "spec_dict": {"model": "openai/gpt-5.4-mini"}},
    )]


async def test_update_room_llm_null_spec_clears(srv: _SpyServer):
    """spec=null 是合法 payload —— 语义"删整段"。"""
    await srv._handle_inbound(
        {"type": INBOUND_UPDATE_ROOM_LLM, "section": "summary", "spec": None},
        _sink,
    )
    assert srv.calls == [(
        "_update_room_llm", {"section": "summary", "spec_dict": None},
    )]


@pytest.mark.parametrize("payload", [
    {"type": INBOUND_UPDATE_ROOM_LLM, "spec": {"model": "x/y"}},               # 缺 section
    {"type": INBOUND_UPDATE_ROOM_LLM, "section": "foo", "spec": {}},            # 非法 section
    {"type": INBOUND_UPDATE_ROOM_LLM, "section": "scoring", "spec": "hello"},   # spec 是 str
    {"type": INBOUND_UPDATE_ROOM_LLM, "section": "scoring", "spec": [1, 2]},    # spec 是 list
])
async def test_update_room_llm_bad_payload(srv: _SpyServer, payload):
    await srv._handle_inbound(payload, _sink)
    assert srv.calls == []
    assert srv.cancel_drain_count == 0


async def test_update_guest_llm_ok(srv: _SpyServer):
    await srv._handle_inbound(
        {
            "type": INBOUND_UPDATE_GUEST_LLM,
            "name": "宝总",
            "spec": {"model": "anthropic/claude-opus-4-7", "base_url": "https://api.anthropic.com"},
        },
        _sink,
    )
    assert srv.cancel_drain_count == 1
    assert srv.calls == [(
        "_update_guest_llm",
        {
            "name": "宝总",
            "spec_dict": {"model": "anthropic/claude-opus-4-7", "base_url": "https://api.anthropic.com"},
        },
    )]


async def test_update_guest_llm_null_spec_clears(srv: _SpyServer):
    await srv._handle_inbound(
        {"type": INBOUND_UPDATE_GUEST_LLM, "name": "宝总", "spec": None}, _sink,
    )
    assert srv.calls == [("_update_guest_llm", {"name": "宝总", "spec_dict": None})]


@pytest.mark.parametrize("payload", [
    {"type": INBOUND_UPDATE_GUEST_LLM, "spec": {"model": "x/y"}},               # 缺 name
    {"type": INBOUND_UPDATE_GUEST_LLM, "name": "", "spec": None},                # 空 name
    {"type": INBOUND_UPDATE_GUEST_LLM, "name": "宝总", "spec": "x"},              # spec 是 str
    {"type": INBOUND_UPDATE_GUEST_LLM, "name": "宝总", "spec": [{"model": "a/b"}]},  # spec 是 list
])
async def test_update_guest_llm_bad_payload(srv: _SpyServer, payload):
    await srv._handle_inbound(payload, _sink)
    assert srv.calls == []
    assert srv.cancel_drain_count == 0


# ── update_guest_isolation（P4.2）─────────────────────────────────────────


async def test_update_guest_isolation_ok(srv: _SpyServer):
    await srv._handle_inbound(
        {
            "type": INBOUND_UPDATE_GUEST_ISOLATION,
            "name": "宝总",
            "isolation": "global",
        },
        _sink,
    )
    assert srv.cancel_drain_count == 1
    assert srv.calls == [(
        "_update_guest_isolation",
        {"name": "宝总", "isolation": "global"},
    )]


@pytest.mark.parametrize("payload", [
    {"type": INBOUND_UPDATE_GUEST_ISOLATION, "isolation": "room"},   # 缺 name
    {"type": INBOUND_UPDATE_GUEST_ISOLATION, "name": "宝总"},          # 缺 isolation
    {"type": INBOUND_UPDATE_GUEST_ISOLATION, "name": "", "isolation": "room"},
    {"type": INBOUND_UPDATE_GUEST_ISOLATION, "name": "宝总", "isolation": ""},
    {"type": INBOUND_UPDATE_GUEST_ISOLATION, "name": "宝总", "isolation": 1},
])
async def test_update_guest_isolation_bad_payload(srv: _SpyServer, payload):
    """非法 isolation 值（如 'rogue'）由 admin 层在 _update_guest_isolation 内拒；
    这里只盯 wire 层的"必字段缺 / 类型错"。"""
    await srv._handle_inbound(payload, _sink)
    assert srv.calls == []
    assert srv.cancel_drain_count == 0


# ── update_guest_extra_mcp（P4.4）─────────────────────────────────────────


async def test_update_guest_extra_mcp_ok(srv: _SpyServer):
    await srv._handle_inbound(
        {
            "type": INBOUND_UPDATE_GUEST_EXTRA_MCP,
            "name": "宝总",
            "servers": [
                {"name": "web", "command": "npx", "args": ["-y", "@mcp/web"]},
                {"name": "fs", "command": "fs-mcp", "env": {"ROOT": "/tmp"}},
            ],
        },
        _sink,
    )
    assert srv.cancel_drain_count == 1
    assert srv.calls == [(
        "_update_guest_extra_mcp",
        {
            "name": "宝总",
            "servers": [
                {"name": "web", "command": "npx", "args": ["-y", "@mcp/web"]},
                {"name": "fs", "command": "fs-mcp", "env": {"ROOT": "/tmp"}},
            ],
        },
    )]


async def test_update_guest_extra_mcp_empty_list_clears(srv: _SpyServer):
    """空 list 是合法 payload —— 语义"清掉所有 entry"，与 admin 层一致。"""
    await srv._handle_inbound(
        {"type": INBOUND_UPDATE_GUEST_EXTRA_MCP, "name": "宝总", "servers": []},
        _sink,
    )
    assert srv.calls == [(
        "_update_guest_extra_mcp", {"name": "宝总", "servers": []},
    )]


@pytest.mark.parametrize("payload", [
    {"type": INBOUND_UPDATE_GUEST_EXTRA_MCP, "servers": []},                # 缺 name
    {"type": INBOUND_UPDATE_GUEST_EXTRA_MCP, "name": "", "servers": []},     # 空 name
    {"type": INBOUND_UPDATE_GUEST_EXTRA_MCP, "name": "宝总"},                  # 缺 servers
    {"type": INBOUND_UPDATE_GUEST_EXTRA_MCP, "name": "宝总", "servers": None},  # null
    {"type": INBOUND_UPDATE_GUEST_EXTRA_MCP, "name": "宝总", "servers": {}},    # dict 不是 list
    {"type": INBOUND_UPDATE_GUEST_EXTRA_MCP, "name": "宝总", "servers": "x"},   # str
])
async def test_update_guest_extra_mcp_bad_payload(srv: _SpyServer, payload):
    """每项内字段非法（缺 name / command / 重名）由 admin 层 ``_build_extra_mcp_servers``
    在 mutator 里拒；这里只盯 wire 层的"必字段缺 / 类型错"。"""
    await srv._handle_inbound(payload, _sink)
    assert srv.calls == []
    assert srv.cancel_drain_count == 0
