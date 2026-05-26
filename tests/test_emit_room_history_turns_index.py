"""``emit_room_history.data.turns_index``（P6.3.A，docs/P6.3 §4.1 / §5.2）。

覆盖：

- 有 turn → ``turns_index`` 倒序投影（最新在前）。
- ``enabled=False`` → 字段整缺省（不下发空数组，让前端区分"关了" vs "开着但空"）。
- 启动期 jsonl 不存在 → 字段缺省（同上口径）。
"""

from __future__ import annotations

import json

import pytest

from chahua import admin
from chahua.events import ChahuaEventType
from chahua.server import ChahuaServer


@pytest.fixture
def session_and_srv(env_paths):
    from chahua.session import build_room_session
    from chahua.server import _bind_inbound_handlers, _install_handler_slots

    rc = admin.create_room(
        paths=env_paths, room_id="t1", name="t1",
        guests=[{"persona": "chahua/personas/宝总/宝总.md", "name": "宝总"}],
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


def _capture_room_history(srv) -> dict:
    captured = []
    srv._emit_room_history(lambda env: captured.append(env.to_dict()))
    assert len(captured) == 1
    assert captured[0]["type"] == ChahuaEventType.ROOM_HISTORY.value
    return captured[0]["data"]


def _seed_jsonl_row(recorder, row):
    recorder._turns_path.parent.mkdir(parents=True, exist_ok=True)
    with recorder._turns_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False))
        f.write("\n")


def test_turns_index_present_and_reversed(session_and_srv):
    session, srv = session_and_srv
    recorder = session.recorder
    for i in range(5):
        _seed_jsonl_row(recorder, {
            "turn_id": f"turn_{i:020x}", "ts_ms": 1000 + i,
            "task_id": None, "trigger": {"kind": "user_msg"},
            "scoring_path": "scoring",
            "scoring": {"winners": [], "results": []},
            "messages": [],
        })
    data = _capture_room_history(srv)
    assert "turns_index" in data
    idx = data["turns_index"]
    assert len(idx) == 5
    # 倒序：最新在前
    assert idx[0]["turn_id"] == f"turn_{4:020x}"
    assert idx[-1]["turn_id"] == f"turn_{0:020x}"


def test_turns_index_absent_when_disabled(session_and_srv, monkeypatch):
    session, srv = session_and_srv
    monkeypatch.setattr(session.recorder, "enabled", False)
    data = _capture_room_history(srv)
    # **整字段缺省** —— 不下发空数组。
    assert "turns_index" not in data


def test_turns_index_absent_when_empty_jsonl(session_and_srv):
    """enabled=True 但 turns.jsonl 不存在 / 空 → 字段缺省。

    避免前端误认为"开了但确实空"是与"关了"等价 —— 真空 = 字段缺，符合不变量
    "整字段缺省（不下发空数组）"。
    """
    session, srv = session_and_srv
    # turns.jsonl 没人写过。
    data = _capture_room_history(srv)
    assert "turns_index" not in data
