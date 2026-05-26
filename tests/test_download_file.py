"""download_file inbound：恒发 FILE_DOWNLOAD（成功 / 失败），且只接受 share/ +
tasks/<id>/artifacts/ 前缀的 rel，拒绝路径穿越。"""

from __future__ import annotations

import base64

import pytest

from chahua import admin
from chahua.events import ChahuaEventType
from chahua.server import ChahuaServer, _install_handler_slots
from chahua.session import build_room_session


@pytest.fixture
def session_and_srv(env_paths):
    rc = admin.create_room(
        paths=env_paths, room_id="dl1", name="dl1",
        guests=[{"persona": "chahua/personas/宝总/宝总.md", "name": "宝总"}],
    )
    session = build_room_session(rc.room_dir, env_paths)
    srv = object.__new__(ChahuaServer)
    srv._session = session
    srv._paths = env_paths
    srv._inflight_turn_task = None
    _install_handler_slots(srv)
    yield session, srv
    session.close()


def _types(envs: list[dict]) -> list[str]:
    return [e["type"] for e in envs]


def test_download_share_file_success(session_and_srv):
    """先上传一个文件到 share/，再下载回来 —— 字节流原样回吐。"""
    session, srv = session_and_srv
    payload = b"hello world\n"
    captured: list[dict] = []
    srv.io._upload_file(
        filename="hi.txt",
        content_b64=base64.b64encode(payload).decode(),
        sink=lambda env: captured.append(env.to_dict()),
    )
    captured.clear()
    srv.io._download_file(
        rel="share/hi.txt",
        sink=lambda env: captured.append(env.to_dict()),
    )
    ok = next(e for e in captured if e["type"] == ChahuaEventType.FILE_DOWNLOAD.value)
    assert "error" not in ok["data"]
    assert ok["data"]["rel"] == "share/hi.txt"
    assert ok["data"]["name"] == "hi.txt"
    assert ok["data"]["size"] == len(payload)
    assert base64.b64decode(ok["data"]["content_b64"]) == payload


def test_download_artifact_success(session_and_srv):
    """开任务 → attach share/ 文件做 artifact → 按 tasks/<id>/artifacts/ rel 下回。"""
    session, srv = session_and_srv
    # 先准备 share/ 源文件
    srv.io._upload_file(
        filename="a.bin",
        content_b64=base64.b64encode(b"\x00\x01\x02").decode(),
        sink=lambda env: None,
    )
    store = session.tasks_store
    task = store.open_task(title="t", goal="g")
    share_root = session.room_config.room_dir / "share"
    info = store.attach_artifact(
        task_id=task.id, share_rel="share/a.bin", share_root=share_root,
    )
    rel = info["rel"]
    captured: list[dict] = []
    srv.io._download_file(rel=rel, sink=lambda env: captured.append(env.to_dict()))
    ok = next(e for e in captured if e["type"] == ChahuaEventType.FILE_DOWNLOAD.value)
    assert "error" not in ok["data"]
    assert ok["data"]["rel"] == rel
    assert base64.b64decode(ok["data"]["content_b64"]) == b"\x00\x01\x02"


def test_download_rejects_path_traversal(session_and_srv):
    """rel 含 .. → NOTICE error + FILE_DOWNLOAD(error)，**不读盘**。"""
    session, srv = session_and_srv
    captured: list[dict] = []
    srv.io._download_file(
        rel="share/../room.toml",
        sink=lambda env: captured.append(env.to_dict()),
    )
    types = _types(captured)
    assert ChahuaEventType.NOTICE.value in types
    err = next(e for e in captured if e["type"] == ChahuaEventType.FILE_DOWNLOAD.value)
    assert "error" in err["data"]
    assert "content_b64" not in err["data"]


def test_download_rejects_non_whitelist_prefix(session_and_srv):
    """rel 不以 share/ 或 tasks/ 起手 → 拒。"""
    session, srv = session_and_srv
    captured: list[dict] = []
    srv.io._download_file(rel="room.toml", sink=lambda env: captured.append(env.to_dict()))
    err = next(e for e in captured if e["type"] == ChahuaEventType.FILE_DOWNLOAD.value)
    assert "error" in err["data"]


def test_download_rejects_tasks_state_json(session_and_srv):
    """tasks/state.json 是内部元数据，``tasks/`` 前缀单看会放进来 → 必须额外段形校验拒。"""
    session, srv = session_and_srv
    store = session.tasks_store
    task = store.open_task(title="t", goal="g")  # 触发 state.json 落盘
    # 即便 state.json 真存在盘上，下载也不该把它给出去。
    captured: list[dict] = []
    srv.io._download_file(
        rel="tasks/state.json",
        sink=lambda env: captured.append(env.to_dict()),
    )
    err = next(e for e in captured if e["type"] == ChahuaEventType.FILE_DOWNLOAD.value)
    assert "error" in err["data"]
    assert "content_b64" not in err["data"]


def test_download_rejects_task_metadata_files(session_and_srv):
    """tasks/<id>/task.json / decisions.jsonl / events.jsonl 是内部元数据 → 拒。
    白名单只能放产物（artifacts/<name>）。"""
    session, srv = session_and_srv
    store = session.tasks_store
    task = store.open_task(title="t", goal="g")
    for rel in (
        f"tasks/{task.id}/task.json",
        f"tasks/{task.id}/decisions.jsonl",
        f"tasks/{task.id}/events.jsonl",
    ):
        captured: list[dict] = []
        srv.io._download_file(rel=rel, sink=lambda env: captured.append(env.to_dict()))
        err = next(e for e in captured if e["type"] == ChahuaEventType.FILE_DOWNLOAD.value)
        assert "error" in err["data"], f"{rel} 该被拒"
        assert "content_b64" not in err["data"]


def test_download_rejects_tasks_id_only(session_and_srv):
    """tasks/<id> / tasks/<id>/ 路径不足 4 段 → 拒（不能列目录）。"""
    session, srv = session_and_srv
    store = session.tasks_store
    task = store.open_task(title="t", goal="g")
    captured: list[dict] = []
    srv.io._download_file(
        rel=f"tasks/{task.id}",
        sink=lambda env: captured.append(env.to_dict()),
    )
    err = next(e for e in captured if e["type"] == ChahuaEventType.FILE_DOWNLOAD.value)
    assert "error" in err["data"]


def test_download_rejects_symlink_escape_to_metadata(session_and_srv):
    """share/<symlink> → ../tasks/state.json 形的逃逸：rel 看起来合法（share/ 前缀 +
    段形 OK），但 resolve 后落到 tasks/state.json。光检查 room_dir 子树不够 ——
    必须确认 resolved target 仍在**具体白名单子目录**（share/ 或 artifacts/）下。"""
    session, srv = session_and_srv
    store = session.tasks_store
    task = store.open_task(title="t", goal="g")  # 触发 state.json 落盘
    room_dir = session.room_config.room_dir
    share_dir = room_dir / "share"
    share_dir.mkdir(parents=True, exist_ok=True)
    target = room_dir / "tasks" / "state.json"
    assert target.exists(), "state.json 应已被 open_task 落盘"
    link = share_dir / "escape.lnk"
    link.symlink_to(target)
    captured: list[dict] = []
    srv.io._download_file(
        rel="share/escape.lnk",
        sink=lambda env: captured.append(env.to_dict()),
    )
    err = next(e for e in captured if e["type"] == ChahuaEventType.FILE_DOWNLOAD.value)
    assert "error" in err["data"]
    assert "content_b64" not in err["data"]


def test_download_rejects_artifact_symlink_escape(session_and_srv):
    """artifacts/<symlink> → ../task.json 同理：resolved 路径必须仍在该任务 artifacts/ 下。"""
    session, srv = session_and_srv
    store = session.tasks_store
    task = store.open_task(title="t", goal="g")
    adir = store.artifacts_dir(task.id)
    adir.mkdir(parents=True, exist_ok=True)
    target = adir.parent / "task.json"
    assert target.exists()
    link = adir / "leak.lnk"
    link.symlink_to(target)
    captured: list[dict] = []
    srv.io._download_file(
        rel=f"tasks/{task.id}/artifacts/leak.lnk",
        sink=lambda env: captured.append(env.to_dict()),
    )
    err = next(e for e in captured if e["type"] == ChahuaEventType.FILE_DOWNLOAD.value)
    assert "error" in err["data"]
    assert "content_b64" not in err["data"]


def test_download_rejects_absolute_path(session_and_srv):
    session, srv = session_and_srv
    captured: list[dict] = []
    srv.io._download_file(rel="/etc/passwd", sink=lambda env: captured.append(env.to_dict()))
    err = next(e for e in captured if e["type"] == ChahuaEventType.FILE_DOWNLOAD.value)
    assert "error" in err["data"]


def test_download_missing_file_emits_error(session_and_srv):
    session, srv = session_and_srv
    captured: list[dict] = []
    srv.io._download_file(
        rel="share/does-not-exist.txt",
        sink=lambda env: captured.append(env.to_dict()),
    )
    err = next(e for e in captured if e["type"] == ChahuaEventType.FILE_DOWNLOAD.value)
    assert "error" in err["data"]


async def test_inbound_download_missing_rel_emits_error(session_and_srv):
    """rel 字段缺失 → inbound 不能静默吞，必须发 FILE_DOWNLOAD(error)。"""
    session, srv = session_and_srv
    captured: list[dict] = []
    await srv.io._inbound_download_file(
        {"type": "download_file"},
        lambda env: captured.append(env.to_dict()),
    )
    err = next(e for e in captured if e["type"] == ChahuaEventType.FILE_DOWNLOAD.value)
    assert "error" in err["data"]


def test_download_too_large_rejected(session_and_srv, monkeypatch):
    """超 _DOWNLOAD_MAX_BYTES 拒 —— 避免对端 ws max_size 中途断线。"""
    session, srv = session_and_srv
    srv.io._upload_file(
        filename="big.txt",
        content_b64=base64.b64encode(b"abcdef").decode(),
        sink=lambda env: None,
    )
    import chahua.server_inbound_io as io_mod
    monkeypatch.setattr(io_mod, "_DOWNLOAD_MAX_BYTES", 1)
    captured: list[dict] = []
    srv.io._download_file(rel="share/big.txt", sink=lambda env: captured.append(env.to_dict()))
    err = next(e for e in captured if e["type"] == ChahuaEventType.FILE_DOWNLOAD.value)
    assert "error" in err["data"]
