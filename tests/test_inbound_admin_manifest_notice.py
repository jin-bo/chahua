"""坏 manifest 经 ADD_GUEST / CREATE_ROOM inbound 触发时转 NOTICE error 给前端（P12 C3）。

承重契约：plan §"承重不变量"第 7 条 —— 消费路径（add_guest / create_room）遇坏
manifest 必须抛 PersonaManifestError → inbound 转 NOTICE，**不**静默 fallback 回
DEFAULT_MODE。否则用户 typo 永远不可见、P12 等于白做。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from chahua import admin
from chahua.events import ChahuaEventType
from chahua.server import (
    ChahuaServer,
    INBOUND_ADD_GUEST,
    INBOUND_CREATE_ROOM,
    _bind_inbound_handlers,
    _install_handler_slots,
)
from chahua.session import build_room_session


PERSONAS_DIR_REL = Path("chahua/personas")


@pytest.fixture
def session_and_srv(env_paths):
    rc = admin.create_room(
        paths=env_paths, room_id="t1", name="t1",
        guests=[{"persona": "chahua/personas/宝总.md", "name": "宝总"}],
    )
    session = build_room_session(rc.room_dir, env_paths)
    srv = object.__new__(ChahuaServer)
    srv._session = session
    srv._paths = env_paths
    srv._inflight_turn_task = None
    srv._inflight_kind = None
    _install_handler_slots(srv)
    srv._inbound_handlers = _bind_inbound_handlers(srv)

    async def _noop():
        return None

    srv._cancel_and_drain_all_foreground = _noop  # type: ignore[method-assign]
    yield session, srv
    session.close()


def _make_bad_yvonne(paths) -> str:
    """造一个 user_data 下 dir-form persona Yvonne 带坏 manifest，返 rel-path。"""
    persona_dir = paths.user_data_root / PERSONAS_DIR_REL / "Yvonne"
    persona_dir.mkdir(parents=True)
    (persona_dir / "Yvonne.md").write_text("I am Yvonne", encoding="utf-8")
    (persona_dir / "persona.toml").write_text("schema_version = 2\n", encoding="utf-8")
    return "chahua/personas/Yvonne/Yvonne.md"


def _by_type(captured: list[dict], t: str) -> list[dict]:
    return [e for e in captured if e["type"] == t]


# ── ADD_GUEST：坏 manifest → NOTICE error ─────────────────────────────


async def test_add_guest_bad_manifest_emits_notice_error(session_and_srv, env_paths):
    session, srv = session_and_srv
    persona_rel = _make_bad_yvonne(env_paths)
    captured: list[dict] = []
    await srv._handle_inbound(
        {"type": INBOUND_ADD_GUEST, "persona": persona_rel, "name": "Yvonne"},
        lambda e: captured.append(e.to_dict()),
    )
    notices = _by_type(captured, ChahuaEventType.NOTICE.value)
    assert notices, "坏 manifest 应触发 NOTICE"
    assert notices[0]["data"]["level"] == "error"
    # NOTICE 文本应含错误原因（schema_version 关键词）—— 帮用户定位
    assert "schema_version" in notices[0]["data"]["text"]
    # 茶客**未**被加入（room.toml 没动）
    assert len(session.room_config.guests) == 1
    assert session.room_config.guests[0].name == "宝总"


async def test_add_guest_good_manifest_no_notice(session_and_srv, env_paths):
    """对照：合法 manifest 不应产 NOTICE error。"""
    session, srv = session_and_srv
    persona_dir = env_paths.user_data_root / PERSONAS_DIR_REL / "Maya"
    persona_dir.mkdir(parents=True)
    (persona_dir / "Maya.md").write_text("I am Maya", encoding="utf-8")
    (persona_dir / "persona.toml").write_text(
        'schema_version = 1\nsummary = "设计师"\n', encoding="utf-8"
    )
    captured: list[dict] = []
    await srv._handle_inbound(
        {
            "type": INBOUND_ADD_GUEST,
            "persona": "chahua/personas/Maya/Maya.md",
            "name": "Maya",
        },
        lambda e: captured.append(e.to_dict()),
    )
    error_notices = [
        e for e in _by_type(captured, ChahuaEventType.NOTICE.value)
        if e["data"]["level"] == "error"
    ]
    assert not error_notices, "合法 manifest 不应产 NOTICE error"


# ── CREATE_ROOM：坏 manifest → NOTICE error + 房间目录不创建 ─────────


async def test_create_room_bad_manifest_emits_notice_and_no_room_dir(
    session_and_srv, env_paths
):
    _, srv = session_and_srv
    persona_rel = _make_bad_yvonne(env_paths)
    captured: list[dict] = []
    await srv._handle_inbound(
        {
            "type": INBOUND_CREATE_ROOM,
            "room_id": "bad_room",
            "name": "bad_room",
            "guests": [{"persona": persona_rel, "name": "Yvonne"}],
        },
        lambda e: captured.append(e.to_dict()),
    )
    notices = _by_type(captured, ChahuaEventType.NOTICE.value)
    error_notices = [n for n in notices if n["data"]["level"] == "error"]
    assert error_notices, "坏 manifest 应触发 NOTICE error"
    assert "schema_version" in error_notices[0]["data"]["text"]
    # 房间目录**未**被创建（事务性 —— manifest 解析在 mkdir 之前）
    assert not (env_paths.user_data_root / "rooms" / "bad_room").exists()
