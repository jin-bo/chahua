"""``clear_room`` 必须同步擦 debug 取证（P6.3.A 回归）。

口径：``room.clear`` 把 transcript / summary / cursor 清掉之后，``debug/turns.jsonl``
与 ``debug/prompts/<turn_id>/`` 必须一起被擦 —— 否则随后的 ``room_history`` snapshot
仍会把"已清"房间的老 ``turns_index`` 喂回前端，``fetch_turn_detail`` 还能解出之前的
prompt 内容。

走 ``_clear_room`` 真路径（``_cancel_and_drain_inflight`` 已在 inbound 串行循环里干掉
in-flight turn，所以 ``self._current`` 不需要构造）。
"""

from __future__ import annotations

import json

import pytest

from chahua import admin
from chahua.events import ChahuaEventType
from chahua.server import ChahuaServer, INBOUND_FETCH_TURN_DETAIL


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


def _seed_row(recorder, turn_id: str) -> None:
    recorder._turns_path.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "turn_id": turn_id, "ts_ms": 1,
        "task_id": None, "trigger": {"kind": "user_msg"},
        "scoring_path": "scoring",
        "scoring": {
            "winners": [],
            "results": [{
                "guest": "宝总", "score": 0.0, "kind": "scored", "raw": "{}",
                "prompt_file": f"prompts/{turn_id}/scoring_宝总.txt",
            }],
        },
        "messages": [],
    }
    with recorder._turns_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False))
        f.write("\n")
    # 也写一份 prompt 文件，验证 prompts/ 整子树被擦。
    pf = recorder._debug_dir / "prompts" / turn_id / "scoring_宝总.txt"
    pf.parent.mkdir(parents=True, exist_ok=True)
    pf.write_text("secret-prompt-body", encoding="utf-8")


def _capture_room_history(srv) -> dict:
    captured = []
    srv._emit_room_history(lambda env: captured.append(env.to_dict()))
    assert len(captured) == 1
    assert captured[0]["type"] == ChahuaEventType.ROOM_HISTORY.value
    return captured[0]["data"]


def test_clear_room_drops_turns_jsonl_and_prompts_subtree(session_and_srv):
    session, srv = session_and_srv
    recorder = session.recorder
    assert recorder.enabled
    _seed_row(recorder, "turn_0011")
    _seed_row(recorder, "turn_0022")
    # 计数器走真实 increment 路径（绕过启动期 seed 直接补 2）。
    recorder._turn_count = 2

    # 清房间前 —— turns_index 应该看得到两条。
    data_before = _capture_room_history(srv)
    assert len(data_before.get("turns_index", [])) == 2

    # 真清。
    srv._clear_room(lambda _env: None)

    # 盘上 jsonl 与 prompts/ 子树都没了。
    assert not recorder._turns_path.exists()
    assert not (recorder._debug_dir / "prompts").exists()
    # 内存计数器同步归零。
    assert recorder._turn_count == 0

    # room_history 不再下发 turns_index 字段（**不变量"整字段缺省"**）。
    data_after = _capture_room_history(srv)
    assert "turns_index" not in data_after


async def test_clear_room_makes_fetch_turn_detail_return_found_false(session_and_srv):
    session, srv = session_and_srv
    recorder = session.recorder
    _seed_row(recorder, "turn_aabbccdd")
    recorder._turn_count = 1

    srv._clear_room(lambda _env: None)

    # 老 turn 的 prompt 即便有 ``prompt_file`` 字段也读不到了 —— 整体 found=false。
    captured = []
    await srv._handle_inbound(
        {"type": INBOUND_FETCH_TURN_DETAIL, "turn_id": "turn_aabbccdd"},
        lambda env: captured.append(env.to_dict()),
    )
    details = [e for e in captured if e["type"] == ChahuaEventType.TURN_DETAIL.value]
    assert len(details) == 1
    assert details[0]["data"] == {"found": False}


def test_clear_room_with_disabled_recorder_is_noop(session_and_srv, monkeypatch):
    """``enabled=False`` 时 :meth:`TurnRecorder.clear` early return —— 不抛、不动盘。"""
    session, srv = session_and_srv
    monkeypatch.setattr(session.recorder, "enabled", False)
    # 没 seed jsonl，整调用应当 silent。
    srv._clear_room(lambda _env: None)
