"""P5.1.6: ``ChahuaServer._emit_task_info`` envelope shape 回归。

测两件事：
- 空房间下发 ``{tasks: [], active_task_id: null}``（前端用这帧确认协议生效）
- 开任务后下发包含 task entry + artifacts + decisions
"""

from __future__ import annotations

from pathlib import Path

import pytest

from chahua import admin
from chahua._paths import ENV_APP_ROOT, ENV_USER_DATA_ROOT, Paths
from chahua.events import ChahuaEventType
from chahua.server import ChahuaServer


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


def _build_session_and_server(env_paths):
    from chahua.session import build_room_session

    rc = admin.create_room(
        paths=env_paths, room_id="t1", name="t1",
        guests=[{"persona": "chahua/personas/宝总.md", "name": "宝总"}],
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
