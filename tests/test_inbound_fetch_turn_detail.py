"""``fetch_turn_detail`` inbound（P6.3.A，docs/P6.3 §4.2 / §5.3）。

覆盖：

- happy path：``{turn_id}`` → TURN_DETAIL{found=true, turn, prompts}。
- missing turn / debug 关闭 → ``{found=false}``，**不发 NOTICE**（前端协议过期是预期）。
- 非法 turn_id（穿越 / 非 hex / 空串）→ NOTICE error，不读盘。
- 未知字段 → NOTICE error。

走 ``_handle_inbound`` 真分发路径，挂真实 RoomSession + 真 TurnRecorder（``debug.enabled
= true`` 默认）。
"""

from __future__ import annotations

import json

import pytest

from chahua import admin
from chahua.events import ChahuaEventType
from chahua.server import (
    ChahuaServer,
    INBOUND_FETCH_TURN_DETAIL,
)


@pytest.fixture
def session_and_srv(env_paths):
    from chahua.session import build_room_session
    from chahua.server import _bind_inbound_handlers, _install_handler_slots

    rc = admin.create_room(
        paths=env_paths, room_id="t1", name="t1",
        guests=[{"persona": "chahua/personas/宝总.md", "name": "宝总"}],
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


def _seed_jsonl_row(recorder, row):
    recorder._turns_path.parent.mkdir(parents=True, exist_ok=True)
    with recorder._turns_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False))
        f.write("\n")


async def test_happy_path(session_and_srv):
    session, srv = session_and_srv
    recorder = session.recorder
    assert recorder.enabled
    _seed_jsonl_row(recorder, {
        "turn_id": "turn_0123456789abcdef0011", "ts_ms": 1,
        "task_id": None, "trigger": {"kind": "user_msg"},
        "scoring_path": "scoring",
        "scoring": {"winners": [], "results": []},
        "messages": [],
    })
    captured = []
    await srv._handle_inbound(
        {"type": INBOUND_FETCH_TURN_DETAIL, "turn_id": "turn_0123456789abcdef0011"},
        lambda env: captured.append(env.to_dict()),
    )
    details = _by_type(captured, ChahuaEventType.TURN_DETAIL.value)
    assert len(details) == 1
    data = details[0]["data"]
    assert data["found"] is True
    assert data["turn"]["turn_id"] == "turn_0123456789abcdef0011"
    assert data["prompts"] == {}
    # 顶层 turn_id 与实时 envelope 同款。
    assert details[0]["turn_id"] == "turn_0123456789abcdef0011"
    # 不发 NOTICE。
    assert _by_type(captured, ChahuaEventType.NOTICE.value) == []


async def test_missing_turn_returns_found_false(session_and_srv):
    session, srv = session_and_srv
    captured = []
    await srv._handle_inbound(
        {"type": INBOUND_FETCH_TURN_DETAIL, "turn_id": "turn_deadbeef"},
        lambda env: captured.append(env.to_dict()),
    )
    details = _by_type(captured, ChahuaEventType.TURN_DETAIL.value)
    assert len(details) == 1
    assert details[0]["data"] == {"found": False}
    # 找不到不是错，不发 NOTICE。
    assert _by_type(captured, ChahuaEventType.NOTICE.value) == []


async def test_debug_disabled_returns_found_false(session_and_srv, monkeypatch):
    """``debug.enabled=False`` 也回 found=false，不发 NOTICE。"""
    session, srv = session_and_srv
    # 直接把 enabled flip 掉模拟启动期 debug 关闭。
    monkeypatch.setattr(session.recorder, "enabled", False)
    captured = []
    await srv._handle_inbound(
        {"type": INBOUND_FETCH_TURN_DETAIL, "turn_id": "turn_abcd"},
        lambda env: captured.append(env.to_dict()),
    )
    details = _by_type(captured, ChahuaEventType.TURN_DETAIL.value)
    assert len(details) == 1
    assert details[0]["data"] == {"found": False}
    assert _by_type(captured, ChahuaEventType.NOTICE.value) == []


async def test_illegal_turn_id_rejected_path_traversal(session_and_srv):
    session, srv = session_and_srv
    captured = []
    await srv._handle_inbound(
        {"type": INBOUND_FETCH_TURN_DETAIL, "turn_id": "../etc/passwd"},
        lambda env: captured.append(env.to_dict()),
    )
    notices = _by_type(captured, ChahuaEventType.NOTICE.value)
    assert notices and notices[0]["data"]["level"] == "error"
    # 非法不应该读盘 → 没有 TURN_DETAIL envelope
    assert _by_type(captured, ChahuaEventType.TURN_DETAIL.value) == []


async def test_illegal_turn_id_non_hex(session_and_srv):
    session, srv = session_and_srv
    captured = []
    await srv._handle_inbound(
        {"type": INBOUND_FETCH_TURN_DETAIL, "turn_id": "turn_NOT_HEX_zz"},
        lambda env: captured.append(env.to_dict()),
    )
    notices = _by_type(captured, ChahuaEventType.NOTICE.value)
    assert notices and notices[0]["data"]["level"] == "error"
    assert _by_type(captured, ChahuaEventType.TURN_DETAIL.value) == []


async def test_illegal_turn_id_empty(session_and_srv):
    session, srv = session_and_srv
    captured = []
    await srv._handle_inbound(
        {"type": INBOUND_FETCH_TURN_DETAIL, "turn_id": ""},
        lambda env: captured.append(env.to_dict()),
    )
    # 空串 _require_str 会返 None + WARN，整个 handler 早退；没有 NOTICE / TURN_DETAIL。
    # 实际上空串拒后服务端不需要回 NOTICE（_require_str WARN 路径走 log），所以保险只断"无 TURN_DETAIL"。
    assert _by_type(captured, ChahuaEventType.TURN_DETAIL.value) == []


async def test_unknown_field_rejected(session_and_srv):
    session, srv = session_and_srv
    captured = []
    await srv._handle_inbound(
        {
            "type": INBOUND_FETCH_TURN_DETAIL,
            "turn_id": "turn_abcd",
            "novelfield": "x",
        },
        lambda env: captured.append(env.to_dict()),
    )
    notices = _by_type(captured, ChahuaEventType.NOTICE.value)
    assert notices and notices[0]["data"]["level"] == "error"
    assert _by_type(captured, ChahuaEventType.TURN_DETAIL.value) == []
