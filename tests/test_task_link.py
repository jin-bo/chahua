"""P5.2.11 通道 2：茶客 cwd 下 ``./task/`` 软链跟随 active task。

走真实 ``build_room_session`` + ``_handle_inbound``，确认装配 / open / set_active /
close 四条改 active 的路径都让 guest workspace 下 ``./task/`` 软链对到当前 active 的
``tasks/<id>/artifacts/`` 上。Windows 走 junction（不在 CI 里测，单独手验）。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from chahua import admin
from chahua._paths import ENV_APP_ROOT, ENV_USER_DATA_ROOT, Paths
from chahua.server import (
    ChahuaServer,
    INBOUND_CLOSE_TASK,
    INBOUND_OPEN_TASK,
    INBOUND_SET_ACTIVE_TASK,
    _bind_inbound_handlers,
    _install_handler_slots,
)
from chahua.session import TASK_LINK_DIRNAME, build_room_session

REPO_ROOT = Path(__file__).resolve().parent.parent

pytestmark = pytest.mark.skipif(
    os.name == "nt", reason="Windows junction 走 mklink /J，单独手验",
)


@pytest.fixture
def env_paths(tmp_path, monkeypatch):
    user_data = tmp_path / "userdata"
    user_data.mkdir()
    monkeypatch.setenv(ENV_APP_ROOT, str(REPO_ROOT))
    monkeypatch.setenv(ENV_USER_DATA_ROOT, str(user_data))
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("OPENAI_MODEL", "gpt-5.4")
    return Paths.from_env()


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


def _task_link(session, guest_name: str) -> Path:
    """guest 工作目录下 ./task/ 软链路径（不解析）。"""
    for g in session.guests:
        if g.name == guest_name:
            return g.working_directory / TASK_LINK_DIRNAME
    raise AssertionError(f"未找到 guest {guest_name!r}")


def _expected_target(session, task_id: str) -> Path:
    return session.tasks_store.artifacts_dir(task_id)


async def test_no_active_no_link(session_and_srv):
    """装配后 active=None → guest cwd 下没有 ./task/。"""
    session, _ = session_and_srv
    assert session.tasks_store.active_task_id is None
    for g in session.guests:
        link = g.working_directory / TASK_LINK_DIRNAME
        assert not link.exists(), f"guest {g.name} 应无 task 链，但 {link} 存在"
        assert not link.is_symlink()


async def test_open_task_creates_link_for_each_guest(session_and_srv):
    session, srv = session_and_srv
    await srv._handle_inbound(
        {"type": INBOUND_OPEN_TASK, "title": "写 README", "goal": "..."},
        lambda _env: None,
    )
    active_id = session.tasks_store.active_task_id
    assert active_id is not None
    expected = _expected_target(session, active_id)
    for g in session.guests:
        link = g.working_directory / TASK_LINK_DIRNAME
        assert link.is_symlink(), f"guest {g.name}: {link} 不是软链"
        assert link.readlink() == expected


async def test_set_active_none_removes_link(session_and_srv):
    session, srv = session_and_srv
    await srv._handle_inbound(
        {"type": INBOUND_OPEN_TASK, "title": "t", "goal": "g"},
        lambda _env: None,
    )
    # sanity
    assert _task_link(session, "宝总").is_symlink()
    await srv._handle_inbound(
        {"type": INBOUND_SET_ACTIVE_TASK, "task_id": None},
        lambda _env: None,
    )
    for g in session.guests:
        link = g.working_directory / TASK_LINK_DIRNAME
        assert not link.exists() and not link.is_symlink()


async def test_set_active_retargets_link(session_and_srv):
    session, srv = session_and_srv
    t1 = session.tasks_store.open_task(title="一", goal="...")
    # 手挂 active → t1（open_task 内部已 set_active；显式 relink 以与初始装配口径同步）
    from chahua.session import relink_task_dirs
    relink_task_dirs(session)
    await srv._handle_inbound(
        {"type": INBOUND_OPEN_TASK, "title": "二", "goal": "..."},
        lambda _env: None,
    )
    t2 = session.tasks_store.get_active_task()
    assert t2 is not None and t2.id != t1.id
    # 链指向 t2 artifacts
    for g in session.guests:
        link = g.working_directory / TASK_LINK_DIRNAME
        assert link.readlink() == _expected_target(session, t2.id)
    # 切回 t1
    await srv._handle_inbound(
        {"type": INBOUND_SET_ACTIVE_TASK, "task_id": t1.id},
        lambda _env: None,
    )
    for g in session.guests:
        link = g.working_directory / TASK_LINK_DIRNAME
        assert link.readlink() == _expected_target(session, t1.id)


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
    for g in session.guests:
        link = g.working_directory / TASK_LINK_DIRNAME
        assert not link.exists() and not link.is_symlink()


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
    # 关掉旧任务（非 active）
    await srv._handle_inbound(
        {"type": INBOUND_CLOSE_TASK, "task_id": t1.id, "status": "abandoned"},
        lambda _env: None,
    )
    assert session.tasks_store.active_task_id == t2.id
    for g in session.guests:
        link = g.working_directory / TASK_LINK_DIRNAME
        assert link.readlink() == _expected_target(session, t2.id)


async def test_link_survives_session_close(session_and_srv, env_paths):
    """重启 session：旧 task.json 在盘上，装配后链按当前 active 重建。

    模拟用户开任务 → 关 app → 重开 → guest cwd 下 ./task/ 仍然有效（指向 artifacts/）。
    """
    session, srv = session_and_srv
    await srv._handle_inbound(
        {"type": INBOUND_OPEN_TASK, "title": "p", "goal": "g"},
        lambda _env: None,
    )
    active_id = session.tasks_store.active_task_id
    room_dir = session.room_config.room_dir
    session.close()
    # 重新装配 —— TasksStore 加载 task.json + state.json
    session2 = build_room_session(room_dir, env_paths)
    try:
        assert session2.tasks_store.active_task_id == active_id
        for g in session2.guests:
            link = g.working_directory / TASK_LINK_DIRNAME
            assert link.is_symlink()
            assert link.readlink() == session2.tasks_store.artifacts_dir(active_id)
    finally:
        session2.close()
