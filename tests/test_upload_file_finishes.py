"""恒发 FILE_UPLOADED：错误路径也必须 emit，前端串行上传循环靠 echo 推进队列。"""

from __future__ import annotations

import base64
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


@pytest.fixture
def session_and_srv(env_paths):
    from chahua.session import build_room_session

    rc = admin.create_room(
        paths=env_paths, room_id="t1", name="t1",
        guests=[{"persona": "chahua/personas/宝总.md", "name": "宝总"}],
    )
    session = build_room_session(rc.room_dir, env_paths)
    srv = object.__new__(ChahuaServer)
    srv._session = session
    srv._paths = env_paths
    srv._inflight_turn_task = None
    yield session, srv
    session.close()


def _types(envs: list[dict]) -> list[str]:
    return [e["type"] for e in envs]


def test_upload_success_emits_file_uploaded(session_and_srv):
    session, srv = session_and_srv
    captured: list[dict] = []
    srv._upload_file(
        filename="a.txt",
        content_b64=base64.b64encode(b"hello").decode(),
        sink=lambda env: captured.append(env.to_dict()),
    )
    types = _types(captured)
    assert types.count(ChahuaEventType.FILE_UPLOADED.value) == 1
    ok_env = next(e for e in captured if e["type"] == ChahuaEventType.FILE_UPLOADED.value)
    assert ok_env["data"]["rel"] == "share/a.txt"
    assert "error" not in ok_env["data"]


def test_upload_bad_filename_still_emits_file_uploaded(session_and_srv):
    """文件名非法 → NOTICE + 失败 echo（无 rel，含 error）。"""
    session, srv = session_and_srv
    captured: list[dict] = []
    srv._upload_file(
        filename="...",  # sanitize_fs_name 拒
        content_b64=base64.b64encode(b"x").decode(),
        sink=lambda env: captured.append(env.to_dict()),
    )
    types = _types(captured)
    assert ChahuaEventType.NOTICE.value in types
    assert ChahuaEventType.FILE_UPLOADED.value in types
    failed = next(e for e in captured if e["type"] == ChahuaEventType.FILE_UPLOADED.value)
    assert "rel" not in failed["data"]
    assert "error" in failed["data"]
    assert failed["data"]["original"] == "..."


def test_upload_bad_base64_still_emits_file_uploaded(session_and_srv):
    session, srv = session_and_srv
    captured: list[dict] = []
    srv._upload_file(
        filename="a.txt",
        content_b64="!!!not base64!!!",
        sink=lambda env: captured.append(env.to_dict()),
    )
    failed = next(
        e for e in captured if e["type"] == ChahuaEventType.FILE_UPLOADED.value
    )
    assert "error" in failed["data"]
    assert failed["data"]["original"] == "a.txt"


async def test_inbound_upload_zero_byte_file_emits_file_uploaded(session_and_srv):
    """零字节文件 content_b64="" —— inbound 不能因为 _require_str 拒空串而早返不发 echo。"""
    session, srv = session_and_srv
    captured: list[dict] = []
    await srv._inbound_upload_file(
        {"type": "upload_file", "filename": "empty.txt", "content_b64": ""},
        lambda env: captured.append(env.to_dict()),
    )
    assert ChahuaEventType.FILE_UPLOADED.value in _types(captured)
    # 零字节是合法上传 —— 应当落盘成功而不是 error。
    ok = next(e for e in captured if e["type"] == ChahuaEventType.FILE_UPLOADED.value)
    assert ok["data"].get("rel") == "share/empty.txt"


async def test_inbound_upload_missing_filename_emits_file_uploaded(session_and_srv):
    """filename 缺失 → inbound 早返但仍发 file_uploaded(error)。"""
    session, srv = session_and_srv
    captured: list[dict] = []
    await srv._inbound_upload_file(
        {"type": "upload_file", "content_b64": "aGk="},
        lambda env: captured.append(env.to_dict()),
    )
    failed = next(
        e for e in captured if e["type"] == ChahuaEventType.FILE_UPLOADED.value
    )
    assert "error" in failed["data"]
    assert "rel" not in failed["data"]


async def test_inbound_upload_missing_content_b64_emits_file_uploaded(session_and_srv):
    """content_b64 字段缺失（非 str）—— inbound 应当发失败 echo，前端队列才能推进。"""
    session, srv = session_and_srv
    captured: list[dict] = []
    await srv._inbound_upload_file(
        {"type": "upload_file", "filename": "x.txt"},
        lambda env: captured.append(env.to_dict()),
    )
    failed = next(
        e for e in captured if e["type"] == ChahuaEventType.FILE_UPLOADED.value
    )
    assert "error" in failed["data"]


def test_upload_too_large_still_emits_file_uploaded(session_and_srv, monkeypatch):
    """模拟 size 超限 —— file_uploaded(error) 仍发，避免前端 await echo 永挂。"""
    session, srv = session_and_srv
    import chahua.server_inbound_io as io_mod
    monkeypatch.setattr(io_mod, "_UPLOAD_MAX_BYTES", 1)
    captured: list[dict] = []
    srv._upload_file(
        filename="a.txt",
        content_b64=base64.b64encode(b"too big").decode(),
        sink=lambda env: captured.append(env.to_dict()),
    )
    types = _types(captured)
    assert types.count(ChahuaEventType.FILE_UPLOADED.value) == 1
