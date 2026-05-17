"""P5.1.7: task inbound handler 协议 + 副作用回归。

每条 inbound 测三件事：
- 未知字段 → NOTICE error 不动 store
- 业务错（已有任务 / 不存在 / 越界）→ NOTICE error + 不动 store / 不发 hint
- 合法 payload → 落盘 + emit hint + 重发 task_info

走 ``_handle_inbound`` 真分发路径，挂真实 RoomSession + TasksStore，envelope 通过
sink list 捕获。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from chahua import admin
from chahua._paths import ENV_APP_ROOT, ENV_USER_DATA_ROOT, Paths
from chahua.events import ChahuaEventType
from chahua.server import (
    ChahuaServer,
    INBOUND_ADD_DECISION,
    INBOUND_ATTACH_ARTIFACT,
    INBOUND_OPEN_TASK,
    INBOUND_UPDATE_TASK,
)


REPO_ROOT = Path(__file__).resolve().parent.parent


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
    from chahua.session import build_room_session

    rc = admin.create_room(
        paths=env_paths, room_id="t1", name="t1",
        guests=[{"persona": "chahua/personas/宝总.md", "name": "宝总"}],
    )
    session = build_room_session(rc.room_dir, env_paths)
    srv = object.__new__(ChahuaServer)
    srv._session = session  # type: ignore[attr-defined]
    srv._paths = env_paths  # type: ignore[attr-defined]
    srv._inflight_turn_task = None  # type: ignore[attr-defined]
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


async def test_open_task_already_has_task_rejected(session_and_srv):
    session, srv = session_and_srv
    session.tasks_store.open_task(title="一", goal="...")
    captured: list[dict] = []
    await srv._handle_inbound(
        {"type": INBOUND_OPEN_TASK, "title": "二", "goal": "..."},
        lambda env: captured.append(env.to_dict()),
    )
    notices = _by_type(captured, ChahuaEventType.NOTICE.value)
    assert notices and notices[0]["data"]["level"] == "error"
    # 重发 task_info 让前端复位 disabled 按钮
    assert ChahuaEventType.TASK_INFO.value in _types(captured)
    # store 仍是一个 task
    assert len(session.tasks_store.list_tasks()) == 1


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
    session, srv = session_and_srv
    t = session.tasks_store.open_task(title="t", goal="g")
    captured: list[dict] = []
    await srv._handle_inbound(
        {
            "type": INBOUND_UPDATE_TASK,
            "task_id": t.id,
            "patch": {"status": "done"},  # P5.1 不接 status
        },
        lambda env: captured.append(env.to_dict()),
    )
    notices = _by_type(captured, ChahuaEventType.NOTICE.value)
    assert notices and "status" in notices[0]["data"]["text"]
    assert session.tasks_store.get_task(t.id).title == "t"  # 不动


async def test_update_task_patch_owner_rejected(session_and_srv):
    session, srv = session_and_srv
    t = session.tasks_store.open_task(title="t", goal="g")
    captured: list[dict] = []
    await srv._handle_inbound(
        {
            "type": INBOUND_UPDATE_TASK,
            "task_id": t.id,
            "patch": {"owner": "user"},  # P5.1 锁定 owner 不能改
        },
        lambda env: captured.append(env.to_dict()),
    )
    notices = _by_type(captured, ChahuaEventType.NOTICE.value)
    assert notices and "owner" in notices[0]["data"]["text"]


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
    import chahua.server as server_mod

    def _boom(_room_dir):
        raise OSError(13, "Permission denied")

    monkeypatch.setattr(server_mod, "ensure_room_share_dir", _boom)
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
