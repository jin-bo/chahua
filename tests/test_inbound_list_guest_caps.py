"""``list_guest_caps`` inbound —— /tools / /skills slash 查询回包。

覆盖：

- happy path：``{guest}`` → GUEST_CAPS_INFO{guest, permission, tools, skills}。
- guest 不在场 → NOTICE error，不回 GUEST_CAPS_INFO。
- 未知字段 → NOTICE error。
- 已 remove 的茶客查不到 → NOTICE error（盯「走 orchestrator 不走 boot 快照」）。

走 ``_handle_inbound`` 真分发路径，挂真实 RoomSession。
"""

from __future__ import annotations

import pytest

from chahua import admin
from chahua.events import ChahuaEventType
from chahua.server import ChahuaServer, INBOUND_LIST_GUEST_CAPS


@pytest.fixture
def session_and_srv(env_paths):
    from chahua.server import _bind_inbound_handlers, _install_handler_slots
    from chahua.session import build_room_session

    rc = admin.create_room(
        paths=env_paths, room_id="t1", name="t1",
        guests=[
            {"persona": "chahua/personas/宝总/宝总.md", "name": "宝总"},
            {"persona": "chahua/personas/汪小姐/汪小姐.md", "name": "汪小姐"},
        ],
    )
    session = build_room_session(rc.room_dir, env_paths)
    srv = object.__new__(ChahuaServer)
    srv._session = session
    srv._paths = env_paths
    srv._inflight_turn_task = None
    _install_handler_slots(srv)
    srv._inbound_handlers = _bind_inbound_handlers(srv)
    yield session, srv
    session.close()


def _by_type(captured, t):
    return [e for e in captured if e["type"] == t]


async def _run(srv, frame):
    captured = []
    await srv._handle_inbound(frame, lambda env: captured.append(env.to_dict()))
    return captured


async def test_happy_path(session_and_srv):
    _, srv = session_and_srv
    captured = await _run(
        srv, {"type": INBOUND_LIST_GUEST_CAPS, "guest": "宝总", "view": "tools"}
    )
    caps = _by_type(captured, ChahuaEventType.GUEST_CAPS_INFO.value)
    assert len(caps) == 1
    data = caps[0]["data"]
    assert data["guest"] == "宝总"
    assert data["permission"]
    assert {t["name"] for t in data["tools"]} >= {"task_list_artifacts", "propose_panel"}
    assert isinstance(data["skills"], list)
    assert data["view"] == "tools"
    assert _by_type(captured, ChahuaEventType.NOTICE.value) == []


async def test_view_echoed_back(session_and_srv):
    """view 经响应原样回声 —— 前端据此裁剪，多查询并发不串台。"""
    _, srv = session_and_srv
    captured = await _run(
        srv, {"type": INBOUND_LIST_GUEST_CAPS, "guest": "宝总", "view": "skills"}
    )
    data = _by_type(captured, ChahuaEventType.GUEST_CAPS_INFO.value)[0]["data"]
    assert data["view"] == "skills"
    # view 缺省 / 非法值 → 规范化成 "tools"。
    captured2 = await _run(srv, {"type": INBOUND_LIST_GUEST_CAPS, "guest": "宝总"})
    assert _by_type(captured2, ChahuaEventType.GUEST_CAPS_INFO.value)[0]["data"]["view"] == "tools"


async def test_guest_absent_emits_notice(session_and_srv):
    _, srv = session_and_srv
    captured = await _run(srv, {"type": INBOUND_LIST_GUEST_CAPS, "guest": "查无此人"})
    notices = _by_type(captured, ChahuaEventType.NOTICE.value)
    assert notices and notices[0]["data"]["level"] == "error"
    assert _by_type(captured, ChahuaEventType.GUEST_CAPS_INFO.value) == []


async def test_unknown_field_rejected(session_and_srv):
    _, srv = session_and_srv
    captured = await _run(
        srv, {"type": INBOUND_LIST_GUEST_CAPS, "guest": "宝总", "extra": 1}
    )
    notices = _by_type(captured, ChahuaEventType.NOTICE.value)
    assert notices and notices[0]["data"]["level"] == "error"
    assert _by_type(captured, ChahuaEventType.GUEST_CAPS_INFO.value) == []


async def test_reads_live_guests_not_boot_snapshot(session_and_srv):
    """从 orchestrator 活字典里抹掉茶客后查 caps → 不在场。

    盯「handler 走 orchestrator.get_guest，不读 RoomSession.guests boot 快照」——
    后者是 frozen list，运行时增删不会反映。"""
    session, srv = session_and_srv
    del session.orchestrator._guests["汪小姐"]
    captured = await _run(srv, {"type": INBOUND_LIST_GUEST_CAPS, "guest": "汪小姐"})
    notices = _by_type(captured, ChahuaEventType.NOTICE.value)
    assert notices and notices[0]["data"]["level"] == "error"
    assert _by_type(captured, ChahuaEventType.GUEST_CAPS_INFO.value) == []
