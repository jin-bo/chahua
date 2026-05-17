"""P5.1.7: task inbound handler 协议 + 副作用回归。

每条 inbound 测三件事：
- 未知字段 → NOTICE error 不动 store
- 业务错（已有任务 / 不存在 / 越界）→ NOTICE error + 不动 store / 不发 hint
- 合法 payload → 落盘 + emit hint + 重发 task_info

走 ``_handle_inbound`` 真分发路径，挂真实 RoomSession + TasksStore，envelope 通过
sink list 捕获。
"""

from __future__ import annotations

import pytest

from chahua import admin
from chahua.events import ChahuaEventType
from chahua.server import (
    ChahuaServer,
    INBOUND_ADD_DECISION,
    INBOUND_ATTACH_ARTIFACT,
    INBOUND_OPEN_TASK,
    INBOUND_UPDATE_TASK,
)


# env_paths fixture 在 tests/conftest.py。


@pytest.fixture
def session_and_srv(env_paths):
    from chahua.session import build_room_session

    rc = admin.create_room(
        paths=env_paths, room_id="t1", name="t1",
        guests=[{"persona": "chahua/personas/宝总.md", "name": "宝总"}],
    )
    session = build_room_session(rc.room_dir, env_paths)
    from chahua.server import _bind_inbound_handlers, _install_handler_slots
    srv = object.__new__(ChahuaServer)
    srv._session = session  # type: ignore[attr-defined]
    srv._paths = env_paths  # type: ignore[attr-defined]
    srv._inflight_turn_task = None  # type: ignore[attr-defined]
    # P5.2 inbound handler 切到 slot + 预绑 dispatch 表；object.__new__ 跳 __init__ 后
    # 沿用同一对 helper 装回去，与 ChahuaServer.__init__ 唯一真理源对齐。
    _install_handler_slots(srv)
    srv._inbound_handlers = _bind_inbound_handlers(srv)  # type: ignore[attr-defined]
    yield session, srv
    session.close()


def _types(captured: list[dict]) -> list[str]:
    return [e["type"] for e in captured]


def _by_type(captured: list[dict], t: str) -> list[dict]:
    return [e for e in captured if e["type"] == t]


# ── open_task ───────────────────────────────────────────────────────────────


async def test_open_task_oserror_becomes_notice(session_and_srv, monkeypatch):
    """磁盘满 / 不可写 → NOTICE error，不让异常逃逸把 ws 断线。"""
    session, srv = session_and_srv
    def _fail(*_a, **_kw):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(session.tasks_store, "open_task", _fail)
    captured: list[dict] = []
    await srv._handle_inbound(
        {"type": INBOUND_OPEN_TASK, "title": "t", "goal": "g"},
        lambda env: captured.append(env.to_dict()),
    )
    notices = _by_type(captured, ChahuaEventType.NOTICE.value)
    assert notices and notices[0]["data"]["level"] == "error"
    assert "落盘失败" in notices[0]["data"]["text"]


async def test_open_task_ok(session_and_srv):
    session, srv = session_and_srv
    captured: list[dict] = []
    await srv._handle_inbound(
        {"type": INBOUND_OPEN_TASK, "title": "写 README", "goal": "..."},
        lambda env: captured.append(env.to_dict()),
    )
    types = _types(captured)
    # task_open hint + 权威 task_info
    assert ChahuaEventType.TASK_OPEN.value in types
    assert ChahuaEventType.TASK_INFO.value in types
    # task_info 是最后一帧（权威）—— 与 §4.2 事件分工口径一致
    assert types[-1] == ChahuaEventType.TASK_INFO.value
    # store 状态
    assert session.tasks_store.active_task_id is not None
    task = session.tasks_store.get_active_task()
    assert task is not None and task.title == "写 README"


async def test_open_task_unknown_field_notice(session_and_srv):
    session, srv = session_and_srv
    captured: list[dict] = []
    await srv._handle_inbound(
        {
            "type": INBOUND_OPEN_TASK, "title": "t", "goal": "g",
            "extra_field": "bad",
        },
        lambda env: captured.append(env.to_dict()),
    )
    notices = _by_type(captured, ChahuaEventType.NOTICE.value)
    assert notices and notices[0]["data"]["level"] == "error"
    assert "extra_field" in notices[0]["data"]["text"]
    # store 不动
    assert session.tasks_store.active_task_id is None


async def test_open_task_second_promotes_active(session_and_srv):
    """P5.2.1：第二次 open_task 不再被拒，新任务成为 active，旧任务保留。"""
    session, srv = session_and_srv
    t1 = session.tasks_store.open_task(title="一", goal="...")
    captured: list[dict] = []
    await srv._handle_inbound(
        {"type": INBOUND_OPEN_TASK, "title": "二", "goal": "..."},
        lambda env: captured.append(env.to_dict()),
    )
    # 没有 NOTICE error；TASK_OPEN + TASK_INFO 都发
    assert _by_type(captured, ChahuaEventType.NOTICE.value) == []
    assert ChahuaEventType.TASK_OPEN.value in _types(captured)
    assert ChahuaEventType.TASK_INFO.value in _types(captured)
    # store 现在有 2 个 task，active 是新建那个
    tasks = session.tasks_store.list_tasks()
    assert {t.id for t in tasks} == {t1.id, session.tasks_store.active_task_id}
    assert session.tasks_store.active_task_id != t1.id


async def test_open_task_missing_title(session_and_srv):
    session, srv = session_and_srv
    captured: list[dict] = []
    await srv._handle_inbound(
        {"type": INBOUND_OPEN_TASK, "goal": "g"},
        lambda env: captured.append(env.to_dict()),
    )
    # title 缺失 → _require_str WARN + 早返（无 NOTICE / 无 emit）
    assert captured == []
    assert session.tasks_store.active_task_id is None


async def test_open_task_owner_non_str(session_and_srv):
    session, srv = session_and_srv
    captured: list[dict] = []
    await srv._handle_inbound(
        {"type": INBOUND_OPEN_TASK, "title": "t", "goal": "g", "owner": 42},
        lambda env: captured.append(env.to_dict()),
    )
    notices = _by_type(captured, ChahuaEventType.NOTICE.value)
    assert notices and "owner" in notices[0]["data"]["text"]


# ── update_task ─────────────────────────────────────────────────────────────


async def test_update_task_ok(session_and_srv):
    session, srv = session_and_srv
    t = session.tasks_store.open_task(title="旧", goal="老")
    captured: list[dict] = []
    await srv._handle_inbound(
        {
            "type": INBOUND_UPDATE_TASK,
            "task_id": t.id,
            "patch": {"title": "新"},
        },
        lambda env: captured.append(env.to_dict()),
    )
    types = _types(captured)
    assert ChahuaEventType.TASK_UPDATE.value in types
    assert types[-1] == ChahuaEventType.TASK_INFO.value
    assert session.tasks_store.get_task(t.id).title == "新"


async def test_update_task_patch_unknown_field(session_and_srv):
    """P5.2 起 patch 白名单为 {title, goal, owner, status}；其它键 → NOTICE。"""
    session, srv = session_and_srv
    t = session.tasks_store.open_task(title="t", goal="g")
    captured: list[dict] = []
    await srv._handle_inbound(
        {
            "type": INBOUND_UPDATE_TASK,
            "task_id": t.id,
            "patch": {"hacker": "x"},
        },
        lambda env: captured.append(env.to_dict()),
    )
    notices = _by_type(captured, ChahuaEventType.NOTICE.value)
    assert notices and "hacker" in notices[0]["data"]["text"]
    assert session.tasks_store.get_task(t.id).title == "t"  # 不动


async def test_update_task_patch_terminal_status_rejected(session_and_srv):
    """update_task patch.status 仅接受非终结态；done / abandoned 必须走 close_task。"""
    session, srv = session_and_srv
    t = session.tasks_store.open_task(title="t", goal="g")
    captured: list[dict] = []
    await srv._handle_inbound(
        {
            "type": INBOUND_UPDATE_TASK,
            "task_id": t.id,
            "patch": {"status": "done"},
        },
        lambda env: captured.append(env.to_dict()),
    )
    notices = _by_type(captured, ChahuaEventType.NOTICE.value)
    assert notices and "close_task" in notices[0]["data"]["text"]
    assert session.tasks_store.get_task(t.id).status == "open"


async def test_update_task_patch_owner_accepted(session_and_srv):
    """P5.2.5 起 owner 进 patch 白名单 —— 合法 str 直接落盘。"""
    session, srv = session_and_srv
    t = session.tasks_store.open_task(title="t", goal="g", owner=None)
    captured: list[dict] = []
    await srv._handle_inbound(
        {
            "type": INBOUND_UPDATE_TASK,
            "task_id": t.id,
            "patch": {"owner": "宝总"},
        },
        lambda env: captured.append(env.to_dict()),
    )
    assert _by_type(captured, ChahuaEventType.NOTICE.value) == []
    assert session.tasks_store.get_task(t.id).owner == "宝总"


async def test_update_task_patch_owner_null_clears(session_and_srv):
    """owner=null 显式清归属（sentinel 区分"不传"与"清"）。"""
    session, srv = session_and_srv
    t = session.tasks_store.open_task(title="t", goal="g", owner="宝总")
    captured: list[dict] = []
    await srv._handle_inbound(
        {
            "type": INBOUND_UPDATE_TASK,
            "task_id": t.id,
            "patch": {"owner": None},
        },
        lambda env: captured.append(env.to_dict()),
    )
    assert _by_type(captured, ChahuaEventType.NOTICE.value) == []
    assert session.tasks_store.get_task(t.id).owner is None


async def test_update_task_patch_owner_non_str_rejected(session_and_srv):
    session, srv = session_and_srv
    t = session.tasks_store.open_task(title="t", goal="g")
    captured: list[dict] = []
    await srv._handle_inbound(
        {
            "type": INBOUND_UPDATE_TASK,
            "task_id": t.id,
            "patch": {"owner": 42},
        },
        lambda env: captured.append(env.to_dict()),
    )
    notices = _by_type(captured, ChahuaEventType.NOTICE.value)
    assert notices and "owner" in notices[0]["data"]["text"]


async def test_update_task_patch_status_non_terminal_ok(session_and_srv):
    session, srv = session_and_srv
    t = session.tasks_store.open_task(title="t", goal="g")
    captured: list[dict] = []
    await srv._handle_inbound(
        {
            "type": INBOUND_UPDATE_TASK,
            "task_id": t.id,
            "patch": {"status": "in_progress"},
        },
        lambda env: captured.append(env.to_dict()),
    )
    assert _by_type(captured, ChahuaEventType.NOTICE.value) == []
    assert session.tasks_store.get_task(t.id).status == "in_progress"


async def test_update_task_empty_title_rejected(session_and_srv):
    """与 open_task 同口径：title 必填非空（goal 仍允许空）。"""
    session, srv = session_and_srv
    t = session.tasks_store.open_task(title="t", goal="g")
    captured: list[dict] = []
    await srv._handle_inbound(
        {
            "type": INBOUND_UPDATE_TASK,
            "task_id": t.id,
            "patch": {"title": ""},
        },
        lambda env: captured.append(env.to_dict()),
    )
    notices = _by_type(captured, ChahuaEventType.NOTICE.value)
    assert notices and "title" in notices[0]["data"]["text"]
    # store 不动
    assert session.tasks_store.get_task(t.id).title == "t"


async def test_update_task_empty_goal_allowed(session_and_srv):
    """goal 允许 patch 成空串 —— 与 open_task 接受空 goal 同口径。"""
    session, srv = session_and_srv
    t = session.tasks_store.open_task(title="t", goal="原目标")
    captured: list[dict] = []
    await srv._handle_inbound(
        {
            "type": INBOUND_UPDATE_TASK,
            "task_id": t.id,
            "patch": {"goal": ""},
        },
        lambda env: captured.append(env.to_dict()),
    )
    assert session.tasks_store.get_task(t.id).goal == ""


async def test_update_task_not_found(session_and_srv):
    session, srv = session_and_srv
    captured: list[dict] = []
    await srv._handle_inbound(
        {
            "type": INBOUND_UPDATE_TASK,
            "task_id": "task_ghost",
            "patch": {"title": "x"},
        },
        lambda env: captured.append(env.to_dict()),
    )
    notices = _by_type(captured, ChahuaEventType.NOTICE.value)
    assert notices and notices[0]["data"]["level"] == "error"


# ── attach_artifact ─────────────────────────────────────────────────────────


async def test_attach_artifact_ok(session_and_srv):
    session, srv = session_and_srv
    t = session.tasks_store.open_task(title="t", goal="g")
    # 放一个 share/ 文件
    share = session.room_config.room_dir / "share"
    share.mkdir(exist_ok=True)
    (share / "plan.md").write_text("plan", encoding="utf-8")

    captured: list[dict] = []
    await srv._handle_inbound(
        {
            "type": INBOUND_ATTACH_ARTIFACT,
            "task_id": t.id,
            "share_rel": "plan.md",
        },
        lambda env: captured.append(env.to_dict()),
    )
    types = _types(captured)
    assert ChahuaEventType.TASK_ARTIFACT_ADDED.value in types
    assert types[-1] == ChahuaEventType.TASK_INFO.value
    added = _by_type(captured, ChahuaEventType.TASK_ARTIFACT_ADDED.value)[0]
    assert added["data"]["name"] == "plan.md"
    assert added["data"]["created_by"] == "user"
    # share/ 副本仍在
    assert (share / "plan.md").is_file()
    # task artifacts/ 副本
    arts = session.tasks_store.list_artifacts(t.id)
    assert [a["name"] for a in arts] == ["plan.md"]


async def test_attach_artifact_share_dir_oserror_becomes_notice(
    session_and_srv, monkeypatch,
):
    """ensure_room_share_dir 失败（只读 / 满）不能逃出 handler 把 ws 断了。"""
    session, srv = session_and_srv
    t = session.tasks_store.open_task(title="t", goal="g")
    import chahua.server_inbound_task as task_mod

    def _boom(_room_dir):
        raise OSError(13, "Permission denied")

    monkeypatch.setattr(task_mod, "ensure_room_share_dir", _boom)
    captured: list[dict] = []
    await srv._handle_inbound(
        {
            "type": INBOUND_ATTACH_ARTIFACT,
            "task_id": t.id,
            "share_rel": "x.md",
        },
        lambda env: captured.append(env.to_dict()),
    )
    notices = _by_type(captured, ChahuaEventType.NOTICE.value)
    assert notices and notices[0]["data"]["level"] == "error"
    assert "落盘失败" in notices[0]["data"]["text"]


async def test_attach_artifact_path_traversal_rejected(session_and_srv):
    session, srv = session_and_srv
    t = session.tasks_store.open_task(title="t", goal="g")
    captured: list[dict] = []
    await srv._handle_inbound(
        {
            "type": INBOUND_ATTACH_ARTIFACT,
            "task_id": t.id,
            "share_rel": "../../etc/passwd",
        },
        lambda env: captured.append(env.to_dict()),
    )
    notices = _by_type(captured, ChahuaEventType.NOTICE.value)
    assert notices and notices[0]["data"]["level"] == "error"


# ── add_decision ────────────────────────────────────────────────────────────


async def test_add_decision_ok(session_and_srv):
    session, srv = session_and_srv
    t = session.tasks_store.open_task(title="t", goal="g")
    captured: list[dict] = []
    await srv._handle_inbound(
        {
            "type": INBOUND_ADD_DECISION,
            "task_id": t.id,
            "summary": "用 Electron 打包",
            "supporting_message_ids": ["m1", "m2"],
        },
        lambda env: captured.append(env.to_dict()),
    )
    types = _types(captured)
    assert ChahuaEventType.TASK_DECISION_ADDED.value in types
    assert types[-1] == ChahuaEventType.TASK_INFO.value
    decs = session.tasks_store.list_decisions(t.id)
    assert len(decs) == 1
    assert decs[0].summary == "用 Electron 打包"


async def test_add_decision_summary_truncated(session_and_srv):
    session, srv = session_and_srv
    t = session.tasks_store.open_task(title="t", goal="g")
    long = "字" * 300
    captured: list[dict] = []
    await srv._handle_inbound(
        {
            "type": INBOUND_ADD_DECISION,
            "task_id": t.id,
            "summary": long,
            "supporting_message_ids": [],
        },
        lambda env: captured.append(env.to_dict()),
    )
    decs = session.tasks_store.list_decisions(t.id)
    assert len(decs[0].summary) == 200


async def test_add_decision_unknown_field(session_and_srv):
    session, srv = session_and_srv
    t = session.tasks_store.open_task(title="t", goal="g")
    captured: list[dict] = []
    await srv._handle_inbound(
        {
            "type": INBOUND_ADD_DECISION,
            "task_id": t.id,
            "summary": "s",
            "marked_by": "宝总",  # P5.1 服务端固定 user，前端不能传
        },
        lambda env: captured.append(env.to_dict()),
    )
    notices = _by_type(captured, ChahuaEventType.NOTICE.value)
    assert notices and "marked_by" in notices[0]["data"]["text"]


# ── P5.2.5: set_active_task / close_task ───────────────────────────────────


async def test_set_active_task_switches(session_and_srv):
    """两个 task，set_active_task 切到第一个 —— state.active 跟着变 + 重发 task_info。"""
    from chahua.server import INBOUND_SET_ACTIVE_TASK

    session, srv = session_and_srv
    t1 = session.tasks_store.open_task(title="A", goal="g")
    t2 = session.tasks_store.open_task(title="B", goal="g")
    assert session.tasks_store.active_task_id == t2.id
    captured: list[dict] = []
    await srv._handle_inbound(
        {"type": INBOUND_SET_ACTIVE_TASK, "task_id": t1.id},
        lambda env: captured.append(env.to_dict()),
    )
    assert _by_type(captured, ChahuaEventType.NOTICE.value) == []
    # 只发 task_info（沿 §4.2：切 active 不发独立 hint event）
    types = _types(captured)
    assert types[-1] == ChahuaEventType.TASK_INFO.value
    assert ChahuaEventType.TASK_OPEN.value not in types
    assert ChahuaEventType.TASK_CLOSE.value not in types
    assert session.tasks_store.active_task_id == t1.id


async def test_set_active_task_to_null(session_and_srv):
    """task_id=null → 清回房间级。"""
    from chahua.server import INBOUND_SET_ACTIVE_TASK

    session, srv = session_and_srv
    session.tasks_store.open_task(title="t", goal="g")
    captured: list[dict] = []
    await srv._handle_inbound(
        {"type": INBOUND_SET_ACTIVE_TASK, "task_id": None},
        lambda env: captured.append(env.to_dict()),
    )
    assert _by_type(captured, ChahuaEventType.NOTICE.value) == []
    assert session.tasks_store.active_task_id is None


async def test_set_active_task_noop_does_not_cancel_inflight(session_and_srv):
    """切到当前 active 是 no-op —— 不能借机 cancel 一个正在跑的 turn。"""
    import asyncio
    from chahua.server import INBOUND_SET_ACTIVE_TASK

    session, srv = session_and_srv
    t = session.tasks_store.open_task(title="t", goal="g")
    # 模拟一个 inflight turn（任意 sleep 当占位）
    async def _fake_turn() -> None:
        await asyncio.sleep(1.0)
    inflight = asyncio.create_task(_fake_turn(), name="chahua-turn")
    srv._inflight_turn_task = inflight  # type: ignore[attr-defined]
    captured: list[dict] = []
    await srv._handle_inbound(
        {"type": INBOUND_SET_ACTIVE_TASK, "task_id": t.id},
        lambda env: captured.append(env.to_dict()),
    )
    # No-op：inflight 仍在跑
    assert not inflight.done()
    # 没 NOTICE 没 envelope（最纯粹的 no-op；前端已经知道是 active 没必要重发 task_info）
    assert captured == []
    inflight.cancel()
    try:
        await inflight
    except asyncio.CancelledError:
        pass


async def test_set_active_task_missing_id_field_rejected(session_and_srv):
    """task_id 必传（可为 null）—— 整段缺失 → NOTICE。"""
    from chahua.server import INBOUND_SET_ACTIVE_TASK

    session, srv = session_and_srv
    captured: list[dict] = []
    await srv._handle_inbound(
        {"type": INBOUND_SET_ACTIVE_TASK},
        lambda env: captured.append(env.to_dict()),
    )
    notices = _by_type(captured, ChahuaEventType.NOTICE.value)
    assert notices and "task_id" in notices[0]["data"]["text"]


async def test_set_active_task_unknown_id_notice(session_and_srv):
    """task_id 不在 store 中 → NOTICE + 重发 task_info 让前端复位 dropdown。"""
    from chahua.server import INBOUND_SET_ACTIVE_TASK

    session, srv = session_and_srv
    captured: list[dict] = []
    await srv._handle_inbound(
        {"type": INBOUND_SET_ACTIVE_TASK, "task_id": "task_ghost"},
        lambda env: captured.append(env.to_dict()),
    )
    notices = _by_type(captured, ChahuaEventType.NOTICE.value)
    assert notices and notices[0]["data"]["level"] == "error"
    assert ChahuaEventType.TASK_INFO.value in _types(captured)


async def test_close_task_done_emits_close_and_clears_active(session_and_srv):
    from chahua.server import INBOUND_CLOSE_TASK

    session, srv = session_and_srv
    t = session.tasks_store.open_task(title="t", goal="g")
    captured: list[dict] = []
    await srv._handle_inbound(
        {"type": INBOUND_CLOSE_TASK, "task_id": t.id, "status": "done"},
        lambda env: captured.append(env.to_dict()),
    )
    assert _by_type(captured, ChahuaEventType.NOTICE.value) == []
    # 发 TASK_CLOSE + TASK_INFO
    types = _types(captured)
    assert ChahuaEventType.TASK_CLOSE.value in types
    assert types[-1] == ChahuaEventType.TASK_INFO.value
    # 关闭当前 active → state.active = None
    assert session.tasks_store.active_task_id is None
    assert session.tasks_store.get_task(t.id).status == "done"


async def test_close_task_invalid_status_rejected(session_and_srv):
    from chahua.server import INBOUND_CLOSE_TASK

    session, srv = session_and_srv
    t = session.tasks_store.open_task(title="t", goal="g")
    captured: list[dict] = []
    await srv._handle_inbound(
        {"type": INBOUND_CLOSE_TASK, "task_id": t.id, "status": "in_progress"},
        lambda env: captured.append(env.to_dict()),
    )
    notices = _by_type(captured, ChahuaEventType.NOTICE.value)
    assert notices and "status" in notices[0]["data"]["text"]
    assert session.tasks_store.get_task(t.id).status == "open"


async def test_close_task_already_closed(session_and_srv):
    """已 closed task 再 close → NOTICE error + 重发 task_info。"""
    from chahua.server import INBOUND_CLOSE_TASK

    session, srv = session_and_srv
    t = session.tasks_store.open_task(title="t", goal="g")
    session.tasks_store.close_task(t.id, status="done")
    captured: list[dict] = []
    await srv._handle_inbound(
        {"type": INBOUND_CLOSE_TASK, "task_id": t.id, "status": "abandoned"},
        lambda env: captured.append(env.to_dict()),
    )
    notices = _by_type(captured, ChahuaEventType.NOTICE.value)
    assert notices and "已经" in notices[0]["data"]["text"]


async def test_close_task_unknown_id(session_and_srv):
    from chahua.server import INBOUND_CLOSE_TASK

    session, srv = session_and_srv
    captured: list[dict] = []
    await srv._handle_inbound(
        {"type": INBOUND_CLOSE_TASK, "task_id": "task_ghost", "status": "done"},
        lambda env: captured.append(env.to_dict()),
    )
    notices = _by_type(captured, ChahuaEventType.NOTICE.value)
    assert notices and notices[0]["data"]["level"] == "error"
