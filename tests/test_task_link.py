"""P5.2.11 通道 2：茶客 cwd 下 ``./task/`` 软链跟随 active task。

走真实 ``build_room_session`` + ``_handle_inbound``，确认装配 / open / set_active /
close 四条改 active 的路径都让 guest workspace 下 ``./task/`` 对到当前 active 的
``tasks/<id>/artifacts/`` 上。Windows junction 路径在 CI 跳过（pytestmark）。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from chahua import admin
from chahua.server import (
    ChahuaServer,
    INBOUND_CLOSE_TASK,
    INBOUND_OPEN_TASK,
    INBOUND_SET_ACTIVE_TASK,
    _bind_inbound_handlers,
    _install_handler_slots,
)
from chahua.session import TASK_LINK_DIRNAME, build_room_session, relink_task_dirs

pytestmark = pytest.mark.skipif(
    os.name == "nt", reason="Windows junction 走 mklink /J，单独手验",
)


# env_paths fixture 在 tests/conftest.py。


@pytest.fixture
def session_and_srv(env_paths):
    rc = admin.create_room(
        paths=env_paths, room_id="t1", name="t1",
        guests=[
            {"persona": "chahua/personas/宝总.md", "name": "宝总"},
            {"persona": "chahua/personas/汪小姐.md", "name": "汪小姐"},
        ],
    )
    session = build_room_session(rc.room_dir, env_paths)
    srv = object.__new__(ChahuaServer)
    srv._session = session  # type: ignore[attr-defined]
    srv._paths = env_paths  # type: ignore[attr-defined]
    srv._inflight_turn_task = None  # type: ignore[attr-defined]
    _install_handler_slots(srv)
    srv._inbound_handlers = _bind_inbound_handlers(srv)  # type: ignore[attr-defined]
    yield session, srv
    session.close()


def _assert_all_linked_to(session, expected: Path | None) -> None:
    """每位茶客 cwd 下的 ``./task/`` 与 ``expected`` 对齐 —— ``None`` = 整链不存在。"""
    for g in session.guests:
        link = g.working_directory / TASK_LINK_DIRNAME
        if expected is None:
            assert not link.exists() and not link.is_symlink(), (
                f"guest {g.name}: 应无链但 {link} 存在"
            )
        else:
            assert link.is_symlink(), f"guest {g.name}: {link} 不是软链"
            assert link.readlink() == expected, f"guest {g.name}: {link} → {link.readlink()} ≠ {expected}"


def _expected_target(session, task_id: str) -> Path:
    return session.tasks_store.artifacts_dir(task_id)


async def test_no_active_no_link(session_and_srv):
    session, _ = session_and_srv
    assert session.tasks_store.active_task_id is None
    _assert_all_linked_to(session, None)


async def test_open_task_creates_link_for_each_guest(session_and_srv):
    session, srv = session_and_srv
    await srv._handle_inbound(
        {"type": INBOUND_OPEN_TASK, "title": "写 README", "goal": "..."},
        lambda _env: None,
    )
    active_id = session.tasks_store.active_task_id
    assert active_id is not None
    _assert_all_linked_to(session, _expected_target(session, active_id))


async def test_set_active_none_removes_link(session_and_srv):
    session, srv = session_and_srv
    await srv._handle_inbound(
        {"type": INBOUND_OPEN_TASK, "title": "t", "goal": "g"},
        lambda _env: None,
    )
    await srv._handle_inbound(
        {"type": INBOUND_SET_ACTIVE_TASK, "task_id": None},
        lambda _env: None,
    )
    _assert_all_linked_to(session, None)


async def test_set_active_retargets_link(session_and_srv):
    session, srv = session_and_srv
    t1 = session.tasks_store.open_task(title="一", goal="...")
    # 直接走 store 不发 inbound，手动 relink 以与初始装配口径同步。
    relink_task_dirs(session)
    await srv._handle_inbound(
        {"type": INBOUND_OPEN_TASK, "title": "二", "goal": "..."},
        lambda _env: None,
    )
    t2 = session.tasks_store.get_active_task()
    assert t2 is not None and t2.id != t1.id
    _assert_all_linked_to(session, _expected_target(session, t2.id))
    await srv._handle_inbound(
        {"type": INBOUND_SET_ACTIVE_TASK, "task_id": t1.id},
        lambda _env: None,
    )
    _assert_all_linked_to(session, _expected_target(session, t1.id))


async def test_close_active_removes_link(session_and_srv):
    session, srv = session_and_srv
    await srv._handle_inbound(
        {"type": INBOUND_OPEN_TASK, "title": "t", "goal": "g"},
        lambda _env: None,
    )
    active_id = session.tasks_store.active_task_id
    assert active_id is not None
    await srv._handle_inbound(
        {"type": INBOUND_CLOSE_TASK, "task_id": active_id, "status": "done"},
        lambda _env: None,
    )
    assert session.tasks_store.active_task_id is None
    _assert_all_linked_to(session, None)


async def test_close_non_active_keeps_link(session_and_srv):
    """关一个非 active 的旧任务 → active 不动、./task/ 链保持。"""
    session, srv = session_and_srv
    t1 = session.tasks_store.open_task(title="旧", goal="...")
    await srv._handle_inbound(
        {"type": INBOUND_OPEN_TASK, "title": "新", "goal": "..."},
        lambda _env: None,
    )
    t2 = session.tasks_store.get_active_task()
    assert t2 is not None and t2.id != t1.id
    await srv._handle_inbound(
        {"type": INBOUND_CLOSE_TASK, "task_id": t1.id, "status": "abandoned"},
        lambda _env: None,
    )
    assert session.tasks_store.active_task_id == t2.id
    _assert_all_linked_to(session, _expected_target(session, t2.id))


async def test_link_survives_session_close(session_and_srv, env_paths):
    """重启 session：旧 task.json 在盘上，装配后链按当前 active 重建。"""
    session, srv = session_and_srv
    await srv._handle_inbound(
        {"type": INBOUND_OPEN_TASK, "title": "p", "goal": "g"},
        lambda _env: None,
    )
    active_id = session.tasks_store.active_task_id
    room_dir = session.room_config.room_dir
    session.close()
    session2 = build_room_session(room_dir, env_paths)
    try:
        assert session2.tasks_store.active_task_id == active_id
        _assert_all_linked_to(session2, session2.tasks_store.artifacts_dir(active_id))
    finally:
        session2.close()
