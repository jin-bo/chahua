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
    INBOUND_UPDATE_GUEST_PERMISSION,
    INBOUND_UPDATE_ROOM_TOML,
    INBOUND_UPDATE_USER_AVATAR,
    INBOUND_UPDATE_USER_MD,
    INBOUND_UPLOAD_FILE,
    INBOUND_EXPORT_ROOM,
    INBOUND_USER_MESSAGE,
)


# ── 测试夹具 ──────────────────────────────────────────────────────────────


class _SpyServer(ChahuaServer):
    """绕开 __init__，只挂 ``_handle_inbound`` 真正访问的状态 + 记录器。

    刻意**不**填 ``_session`` / ``_paths`` / ``_host`` 等：路由测里这些不该被读到，
    若将来 ``_handle_inbound`` 开始访问它们应该 ``AttributeError`` 让我们立刻注意到
    新增的依赖，而不是悄悄拿到 dummy 值绕过测试。
    """

    def __init__(self) -> None:  # type: ignore[override]
        self._inflight_turn_task: asyncio.Task | None = None
        self.calls: list[tuple[str, dict]] = []
        self.cancel_drain_count = 0
        self.emit_room_info_count = 0
        self.emit_snapshot_count = 0
        self.notices: list[tuple[str, str]] = []
        self.run_turn_args: list[str] = []

    async def _cancel_and_drain_inflight(self) -> None:  # type: ignore[override]
        self.cancel_drain_count += 1

    def _cancel_inflight(self) -> None:  # type: ignore[override]
        self.calls.append(("_cancel_inflight", {}))

    def _emit_room_info(self, sink) -> None:  # type: ignore[override]
        self.emit_room_info_count += 1

    def _emit_room_snapshot(self, sink) -> None:  # type: ignore[override]
        self.emit_snapshot_count += 1

    def _emit_notice(self, sink, *, level: str, text: str) -> None:  # type: ignore[override]
        self.notices.append((level, text))

    # 每个 mutator 同款：拍下 kwargs。
    def _switch_room(self, room_id, sink):  # type: ignore[override]
        self.calls.append(("_switch_room", {"room_id": room_id}))

    def _clear_room(self, sink):  # type: ignore[override]
        self.calls.append(("_clear_room", {}))

    def _add_guest(self, *, persona, name, permission, sink):  # type: ignore[override]
        self.calls.append((
            "_add_guest",
            {"persona": persona, "name": name, "permission": permission},
        ))

    def _remove_guest(self, *, name, sink):  # type: ignore[override]
        self.calls.append(("_remove_guest", {"name": name}))

    def _set_persona_mcp_trust(self, *, persona_rel, trusted, sink):  # type: ignore[override]
        self.calls.append((
            "_set_persona_mcp_trust",
            {"persona_rel": persona_rel, "trusted": trusted},
        ))

    def _update_guest_permission(self, *, name, permission, sink):  # type: ignore[override]
        self.calls.append((
            "_update_guest_permission",
            {"name": name, "permission": permission},
        ))

    def _create_room(self, *, room_id, name, topic, rules, guests, sink):  # type: ignore[override]
        self.calls.append((
            "_create_room",
            {
                "room_id": room_id,
                "name": name,
                "topic": topic,
                "rules": rules,
                "guests": guests,
            },
        ))

    def _delete_room(self, *, room_id, sink):  # type: ignore[override]
        self.calls.append(("_delete_room", {"room_id": room_id}))

    def _update_user_md(self, *, content, sink):  # type: ignore[override]
        self.calls.append(("_update_user_md", {"content": content}))

    def _update_room_toml(self, *, content, sink):  # type: ignore[override]
        self.calls.append(("_update_room_toml", {"content": content}))

    def _update_user_avatar(self, *, data_uri, sink):  # type: ignore[override]
        self.calls.append(("_update_user_avatar", {"data_uri": data_uri}))

    def _upload_file(self, *, filename, content_b64, sink):  # type: ignore[override]
        self.calls.append((
            "_upload_file",
            {"filename": filename, "content_b64": content_b64},
        ))

    def _export_room(self, sink):  # type: ignore[override]
        self.calls.append(("_export_room", {}))

    def _run_import(self, label, op, sink):  # type: ignore[override]
        # 把 op 留下来，让测试断言它是 partial / lambda 即可，不真跑导入。
        self.calls.append(("_run_import", {"label": label}))

    async def _run_turn(self, text, sink):  # type: ignore[override]
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
    assert srv.cancel_drain_count == 1
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
            "persona": "chahua/personas/宝总.md",
            "name": None,  # 允许 null
            "permission": "workspace-write",
        },
        _sink,
    )
    assert srv.cancel_drain_count == 1
    assert srv.calls == [(
        "_add_guest",
        {"persona": "chahua/personas/宝总.md", "name": None, "permission": "workspace-write"},
    )]


async def test_add_guest_default_permission(srv: _SpyServer):
    """permission 缺 / falsy → DEFAULT_MODE。"""
    from chahua.permissions import DEFAULT_MODE

    await srv._handle_inbound(
        {"type": INBOUND_ADD_GUEST, "persona": "p.md"},
        _sink,
    )
    assert srv.calls == [(
        "_add_guest", {"persona": "p.md", "name": None, "permission": DEFAULT_MODE}
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
    {"type": INBOUND_UPLOAD_FILE, "filename": "a", "content_b64": ""},
])
async def test_upload_file_bad_payload(srv: _SpyServer, payload):
    await srv._handle_inbound(payload, _sink)
    assert srv.calls == []


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
