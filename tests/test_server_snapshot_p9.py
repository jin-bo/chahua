"""P9 阶段 9.3.2：房间快照补发 MTS + 房间列表 ``busy`` 标志回归。

覆盖（docs/P9 §8 / §6）：

- ``orchestrator.emit_managed_session_snapshot``：``_managed_session`` 非空 → 补发一帧
  ``managed_session_started``（``budget`` 是当前剩余值）；无 MTS → 空操作。
- ``_rooms_available_with_busy``：``room_id ∈ _runtimes`` 且 ``inflight_alive()`` →
  ``busy=True``，否则 ``False``。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from chahua import admin
from chahua._paths import ENV_APP_ROOT, ENV_USER_DATA_ROOT, Paths
from chahua.events import ChahuaEventType, NOOP_SINK
from chahua.room_runtime import (
    ROUTER_MODE_BACKGROUND,
    ROUTER_MODE_FOREGROUND,
    RoomEventRouter,
    RoomRuntime,
)
from chahua.server import ChahuaServer
from chahua.server_room_snapshot import _emit_inflight_message, _rooms_available_with_busy
from chahua.session import build_room_session

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def env_paths(tmp_path, monkeypatch):
    """user_data_root 在 tmp 下；app_root 指真仓库（带 ship persona）。"""
    user_data = tmp_path / "userdata"
    user_data.mkdir()
    monkeypatch.setenv(ENV_APP_ROOT, str(REPO_ROOT))
    monkeypatch.setenv(ENV_USER_DATA_ROOT, str(user_data))
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("OPENAI_MODEL", "gpt-5.4")
    return Paths.from_env()


# ── emit_managed_session_snapshot ──────────────────────────────────────────


def test_emit_mts_snapshot_replays_started_when_active(env_paths):
    """MTS 在跑 → emit_managed_session_snapshot 补发一帧 managed_session_started。"""
    rc = admin.create_room(
        paths=env_paths, room_id="mts", name="mts",
        guests=[{"persona": "chahua/personas/宝总/宝总.md", "name": "宝总"}],
    )
    session = build_room_session(rc.room_dir, env_paths)
    try:
        orch = session.orchestrator
        orch.start_managed_session(
            NOOP_SINK, task_id="t1", manager_guest="宝总", budget=6,
        )
        # 模拟已推进过两轮 —— budget 被扣到 4。
        orch._managed_session.budget = 4

        captured: list = []
        orch.emit_managed_session_snapshot(captured.append)

        assert len(captured) == 1
        env = captured[0]
        assert env.type is ChahuaEventType.MANAGED_SESSION_STARTED
        assert env.data["manager_guest"] == "宝总"
        assert env.data["task_id"] == "t1"
        # budget 是当前剩余值，不是初始值。
        assert env.data["budget"] == 4
    finally:
        session.close()


def test_emit_mts_snapshot_noop_when_no_session(env_paths):
    """无 MTS → emit_managed_session_snapshot 空操作。"""
    rc = admin.create_room(
        paths=env_paths, room_id="nomts", name="nomts",
        guests=[{"persona": "chahua/personas/宝总/宝总.md", "name": "宝总"}],
    )
    session = build_room_session(rc.room_dir, env_paths)
    try:
        captured: list = []
        session.orchestrator.emit_managed_session_snapshot(captured.append)
        assert captured == []
    finally:
        session.close()


# ── _rooms_available_with_busy ─────────────────────────────────────────────


class _FakeTask:
    def done(self) -> bool:
        return False


class _FakeSession:
    def __init__(self, name: str) -> None:
        self.room_config = type(
            "_RC", (), {"room_dir": type("_D", (), {"name": name})()},
        )()
        # P8.4.8：``RoomRuntime.has_managed_session`` 读 ``session.orchestrator.managed_session``。
        self.orchestrator = type(
            "_O", (), {"managed_session": None},
        )()


def _runtime(room_id: str, *, busy: bool) -> RoomRuntime:
    rt = RoomRuntime(
        room_id=room_id,
        session=_FakeSession(room_id),  # type: ignore[arg-type]
        router=RoomEventRouter(NOOP_SINK, mode=ROUTER_MODE_FOREGROUND),
    )
    if busy:
        rt.set_inflight(_FakeTask(), "user")  # type: ignore[arg-type]
    return rt


def _server_with_runtimes(env_paths, *runtimes: RoomRuntime) -> ChahuaServer:
    srv = object.__new__(ChahuaServer)
    srv._paths = env_paths  # type: ignore[attr-defined]
    srv._runtimes = {rt.room_id: rt for rt in runtimes}  # type: ignore[attr-defined]
    return srv


def test_rooms_available_busy_flag(env_paths):
    """注册表里 inflight_alive 的房标 busy；其余房 busy=False。"""
    admin.create_room(
        paths=env_paths, room_id="alpha", name="alpha",
        guests=[{"persona": "chahua/personas/宝总/宝总.md", "name": "宝总"}],
    )
    admin.create_room(
        paths=env_paths, room_id="beta", name="beta",
        guests=[{"persona": "chahua/personas/宝总/宝总.md", "name": "宝总"}],
    )
    admin.create_room(
        paths=env_paths, room_id="gamma", name="gamma",
        guests=[{"persona": "chahua/personas/宝总/宝总.md", "name": "宝总"}],
    )
    # alpha 后台续跑（busy），beta 在注册表但 idle，gamma 不在注册表。
    srv = _server_with_runtimes(
        env_paths,
        _runtime("alpha", busy=True),
        _runtime("beta", busy=False),
    )

    rooms = {r["room_id"]: r["busy"] for r in _rooms_available_with_busy(srv)}

    assert rooms["alpha"] is True
    assert rooms["beta"] is False
    assert rooms["gamma"] is False


def test_rooms_available_busy_flag_empty_registry(env_paths):
    """注册表为空 → 所有房 busy=False。"""
    admin.create_room(
        paths=env_paths, room_id="solo", name="solo",
        guests=[{"persona": "chahua/personas/宝总/宝总.md", "name": "宝总"}],
    )
    srv = _server_with_runtimes(env_paths)
    rooms = _rooms_available_with_busy(srv)
    assert rooms and all(r["busy"] is False for r in rooms)


# ── _emit_inflight_message ─────────────────────────────────────────────────


class _FakeOrch:
    def __init__(self, snap):
        self._snap = snap
        # P8.4.8：``RoomRuntime.has_managed_session`` 读公开 ``managed_session``。
        self.managed_session = None

    def snapshot_inflight_message(self):
        return self._snap


class _FakeSnapSession:
    def __init__(self, snap):
        self.orchestrator = _FakeOrch(snap)
        self.room = type("_Room", (), {"name": "黄河路"})()


def _srv_with_inflight(snap):
    # 直接装注册表 —— 绕开 _session setter（它要 room_config.room_dir.name）。
    srv = object.__new__(ChahuaServer)
    rt = RoomRuntime(
        room_id="x",
        session=_FakeSnapSession(snap),  # type: ignore[arg-type]
        router=RoomEventRouter(NOOP_SINK),
    )
    srv._runtimes = {"x": rt}  # type: ignore[attr-defined]
    srv._foreground_id = "x"  # type: ignore[attr-defined]
    return srv


def test_emit_inflight_message_replays_start_and_delta():
    """有 in-flight 消息 → 补发 message_start + message_delta(partial_text)。"""
    captured = []
    snap = {
        "turn_id": "turn_x", "message_id": "msg_x", "guest_name": "宝总",
        "task_id": "task_x", "partial_text": "你好，世",
    }
    _emit_inflight_message(_srv_with_inflight(snap), captured.append)

    assert [e.type for e in captured] == [
        ChahuaEventType.MESSAGE_START, ChahuaEventType.MESSAGE_DELTA,
    ]
    start, delta = captured
    assert start.room_id == "黄河路"
    assert start.message_id == "msg_x"
    assert start.turn_id == "turn_x"
    assert start.guest_name == "宝总"
    assert start.data == {"task_id": "task_x"}
    assert delta.message_id == "msg_x"
    assert delta.data == {"chunk": "你好，世"}


def test_emit_inflight_message_noop_when_none():
    """无 in-flight 消息 → 不发任何帧。"""
    captured = []
    _emit_inflight_message(_srv_with_inflight(None), captured.append)
    assert captured == []


def test_emit_inflight_message_start_only_when_partial_empty():
    """partial_text 为空（刚 bind 还没出字）→ 只发 message_start，不发空 delta。"""
    captured = []
    snap = {
        "turn_id": "turn_x", "message_id": "msg_x", "guest_name": "宝总",
        "task_id": None, "partial_text": "",
    }
    _emit_inflight_message(_srv_with_inflight(snap), captured.append)
    assert [e.type for e in captured] == [ChahuaEventType.MESSAGE_START]
    # task_id 为 None → message_start.data 不塞 task_id 键。
    assert captured[0].data == {}
