"""P5.1.6: ``ChahuaServer._emit_task_info`` envelope shape 回归。

测两件事：
- 空房间下发 ``{tasks: [], active_task_id: null}``（前端用这帧确认协议生效）
- 开任务后下发包含 task entry + artifacts + decisions
"""

from __future__ import annotations

from chahua import admin
from chahua.events import ChahuaEventType
from chahua.server import ChahuaServer


# env_paths fixture 在 tests/conftest.py。


def _build_session_and_server(env_paths):
    from chahua.session import build_room_session

    rc = admin.create_room(
        paths=env_paths, room_id="t1", name="t1",
        guests=[{"persona": "chahua/personas/宝总/宝总.md", "name": "宝总"}],
    )
    from chahua.server import _install_handler_slots
    session = build_room_session(rc.room_dir, env_paths)
    srv = object.__new__(ChahuaServer)
    srv._session = session  # type: ignore[attr-defined]
    srv._paths = env_paths  # type: ignore[attr-defined]
    _install_handler_slots(srv)
    return session, srv


def _emit_task_info(srv) -> dict:
    captured: list[dict] = []
    # P5.2 起 _emit_task_info 挂 self.task slot。
    srv.task._emit_task_info(lambda env: captured.append(env.to_dict()))
    assert len(captured) == 1
    env = captured[0]
    assert env["type"] == ChahuaEventType.TASK_INFO.value
    assert env["turn_id"] is None
    assert env["message_id"] is None
    assert env["guest_name"] is None
    return env["data"]


def test_task_info_empty_room(env_paths):
    session, srv = _build_session_and_server(env_paths)
    try:
        data = _emit_task_info(srv)
        assert data == {"tasks": [], "active_task_id": None}
    finally:
        session.close()


def test_task_info_after_open_task(env_paths):
    session, srv = _build_session_and_server(env_paths)
    try:
        t = session.tasks_store.open_task(title="写 README", goal="不写完睡不着")
        data = _emit_task_info(srv)
        assert data["active_task_id"] == t.id
        assert len(data["tasks"]) == 1
        entry = data["tasks"][0]
        assert entry["id"] == t.id
        assert entry["title"] == "写 README"
        assert entry["status"] == "open"
        assert entry["artifacts"] == []
        assert entry["decisions"] == []
    finally:
        session.close()


def test_emit_room_snapshot_emits_load_warnings(env_paths):
    """P5.2.6：多 task + state.active = None → NOTICE info 通过 _emit_room_snapshot 下发。

    先在第一个 session 里写盘 2 个 task + 删 state.json，再起第二个 session 触发
    "多 task + state 缺" 加载路径 —— 这条路径上才会累计 load warning。
    """
    from chahua.session import build_room_session
    from chahua.tasks_store import TasksStore

    # 第一次 session：写盘 2 个 task，然后关
    session1, _ = _build_session_and_server(env_paths)
    room_dir = session1.room_config.room_dir
    session1.tasks_store.open_task(title="A", goal="g")
    session1.tasks_store.open_task(title="B", goal="g")
    session1.close()
    # 删 state.json 让第二次加载触发多任务无 active 分支
    (room_dir / "tasks" / "state.json").unlink()

    # 第二次 session：fresh build → tasks_store._load 触发 warning
    session2 = build_room_session(room_dir, env_paths)
    try:
        assert session2.tasks_store.active_task_id is None
        from chahua.server import _install_handler_slots
        srv = object.__new__(ChahuaServer)
        srv._session = session2  # type: ignore[attr-defined]
        srv._paths = env_paths  # type: ignore[attr-defined]
        _install_handler_slots(srv)
        captured: list[dict] = []
        srv._emit_room_snapshot(lambda env: captured.append(env.to_dict()))
        notices = [e for e in captured if e["type"] == ChahuaEventType.NOTICE.value]
        assert len(notices) == 1
        assert notices[0]["data"]["level"] == "info"
        assert "active" in notices[0]["data"]["text"]
        # 第二次 _emit_room_snapshot：warning 已 consume，不再 emit
        captured2: list[dict] = []
        srv._emit_room_snapshot(lambda env: captured2.append(env.to_dict()))
        assert [e for e in captured2 if e["type"] == ChahuaEventType.NOTICE.value] == []
    finally:
        session2.close()


def test_task_info_includes_decisions_and_artifacts(env_paths, tmp_path):
    session, srv = _build_session_and_server(env_paths)
    try:
        store = session.tasks_store
        t = store.open_task(title="t", goal="g")
        # 决策
        store.add_decision(
            t.id, supporting_message_ids=["m1"], summary="decided X"
        )
        # 产物（拷贝 share/ 文件）
        share_root = session.room_config.room_dir / "share"
        share_root.mkdir(exist_ok=True)
        (share_root / "plan.md").write_text("plan", encoding="utf-8")
        store.attach_artifact(
            t.id, share_rel="plan.md", share_root=share_root
        )

        data = _emit_task_info(srv)
        entry = data["tasks"][0]
        assert len(entry["decisions"]) == 1
        assert entry["decisions"][0]["summary"] == "decided X"
        assert len(entry["artifacts"]) == 1
        assert entry["artifacts"][0]["name"] == "plan.md"
        assert entry["artifacts"][0]["size"] == 4
    finally:
        session.close()
