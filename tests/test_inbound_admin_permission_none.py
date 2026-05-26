"""ADD_GUEST / CREATE_ROOM inbound 保留 ``permission=None`` 透传到 admin 层（P12 C3）。

承重契约：plan §"承重不变量"第 6 条 —— frontend/inbound 全链路用 None 表示"用户未
显式选"，默认值合一仅在 admin 层。**禁止**在 inbound 写 ``data.get("permission") or
DEFAULT_MODE`` —— manifest defaults 永远拿不到机会。

覆盖：
- ADD_GUEST 未带 permission 键 → admin 收到 None
- ADD_GUEST 带 permission=null → admin 收到 None
- ADD_GUEST 带 permission="ask" → 透传
- ADD_GUEST 带 permission=123（非 str + 非 None）→ WARN + ignore（不调 admin）
- CREATE_ROOM 同口径
"""

from __future__ import annotations

import pytest

from chahua import admin
from chahua.server import (
    ChahuaServer,
    INBOUND_ADD_GUEST,
    INBOUND_CREATE_ROOM,
    _bind_inbound_handlers,
    _install_handler_slots,
)
from chahua.session import build_room_session


@pytest.fixture
def session_and_srv(env_paths):
    rc = admin.create_room(
        paths=env_paths, room_id="t1", name="t1",
        guests=[{"persona": "chahua/personas/宝总/宝总.md", "name": "宝总"}],
    )
    session = build_room_session(rc.room_dir, env_paths)
    srv = object.__new__(ChahuaServer)
    srv._session = session
    srv._paths = env_paths
    srv._inflight_turn_task = None
    srv._inflight_kind = None
    _install_handler_slots(srv)
    srv._inbound_handlers = _bind_inbound_handlers(srv)

    # 不真跑 _cancel_and_drain_all_foreground —— 让 inbound 触发到 admin 层
    async def _noop():
        return None

    srv._cancel_and_drain_all_foreground = _noop  # type: ignore[method-assign]
    yield session, srv
    session.close()


# ── 捕获 _add_guest / _create_room 入参的 mock 装置 ───────────────────────


class CaptureAdmin:
    """替 admin.AdminHandlers 的 _add_guest / _create_room mock，记录入参 kwargs。"""

    def __init__(self) -> None:
        self.add_calls: list[dict] = []
        self.create_calls: list[dict] = []

    def _add_guest(self, *, persona, name, permission, sink) -> None:
        self.add_calls.append({
            "persona": persona, "name": name, "permission": permission,
        })

    def _create_room(self, *, room_id, name, topic, rules, guests, sink) -> None:
        self.create_calls.append({
            "room_id": room_id, "name": name, "guests": guests,
        })


@pytest.fixture
def session_capture(session_and_srv, monkeypatch):
    session, srv = session_and_srv
    cap = CaptureAdmin()
    # 替 admin handlers 上挂的方法 —— srv.admin 是 AdminHandlers 实例
    monkeypatch.setattr(srv.admin, "_add_guest", cap._add_guest)
    monkeypatch.setattr(srv.admin, "_create_room", cap._create_room)
    return session, srv, cap


# ── ADD_GUEST ─────────────────────────────────────────────────────────────


async def test_add_guest_no_permission_key_passes_none(session_capture):
    _, srv, cap = session_capture
    await srv._handle_inbound(
        {"type": INBOUND_ADD_GUEST, "persona": "chahua/personas/汪小姐/汪小姐.md"},
        lambda e: None,
    )
    assert len(cap.add_calls) == 1
    assert cap.add_calls[0]["permission"] is None


async def test_add_guest_explicit_null_passes_none(session_capture):
    _, srv, cap = session_capture
    await srv._handle_inbound(
        {"type": INBOUND_ADD_GUEST, "persona": "chahua/personas/汪小姐/汪小姐.md", "permission": None},
        lambda e: None,
    )
    assert len(cap.add_calls) == 1
    assert cap.add_calls[0]["permission"] is None


async def test_add_guest_explicit_string_passes_through(session_capture):
    _, srv, cap = session_capture
    await srv._handle_inbound(
        {"type": INBOUND_ADD_GUEST, "persona": "chahua/personas/汪小姐/汪小姐.md", "permission": "ask"},
        lambda e: None,
    )
    assert len(cap.add_calls) == 1
    assert cap.add_calls[0]["permission"] == "ask"


async def test_add_guest_non_str_non_none_is_ignored(session_capture):
    _, srv, cap = session_capture
    await srv._handle_inbound(
        {"type": INBOUND_ADD_GUEST, "persona": "chahua/personas/汪小姐/汪小姐.md", "permission": 42},
        lambda e: None,
    )
    # 整帧被忽略 —— admin 不调用。
    assert len(cap.add_calls) == 0


# ── CREATE_ROOM ───────────────────────────────────────────────────────────


async def test_create_room_no_permission_key_passes_through(session_capture):
    _, srv, cap = session_capture
    await srv._handle_inbound(
        {
            "type": INBOUND_CREATE_ROOM,
            "room_id": "r2",
            "name": "r2",
            "guests": [{"persona": "chahua/personas/宝总/宝总.md", "name": "宝总"}],
        },
        lambda e: None,
    )
    assert len(cap.create_calls) == 1
    g0 = cap.create_calls[0]["guests"][0]
    assert "permission" not in g0  # 透传原 dict，admin 层做 coalesce


async def test_create_room_null_permission_passes_through(session_capture):
    _, srv, cap = session_capture
    await srv._handle_inbound(
        {
            "type": INBOUND_CREATE_ROOM,
            "room_id": "r2",
            "name": "r2",
            "guests": [
                {"persona": "chahua/personas/宝总/宝总.md", "name": "宝总", "permission": None},
            ],
        },
        lambda e: None,
    )
    assert len(cap.create_calls) == 1
    assert cap.create_calls[0]["guests"][0]["permission"] is None


async def test_create_room_string_permission_passes_through(session_capture):
    _, srv, cap = session_capture
    await srv._handle_inbound(
        {
            "type": INBOUND_CREATE_ROOM,
            "room_id": "r2",
            "name": "r2",
            "guests": [
                {"persona": "chahua/personas/宝总/宝总.md", "name": "宝总", "permission": "ask"},
            ],
        },
        lambda e: None,
    )
    assert len(cap.create_calls) == 1
    assert cap.create_calls[0]["guests"][0]["permission"] == "ask"


async def test_create_room_guest_non_str_permission_ignored(session_capture):
    _, srv, cap = session_capture
    await srv._handle_inbound(
        {
            "type": INBOUND_CREATE_ROOM,
            "room_id": "r2",
            "name": "r2",
            "guests": [
                {"persona": "chahua/personas/宝总/宝总.md", "name": "宝总", "permission": 42},
            ],
        },
        lambda e: None,
    )
    # 整帧被忽略 —— admin 不调用。
    assert len(cap.create_calls) == 0
