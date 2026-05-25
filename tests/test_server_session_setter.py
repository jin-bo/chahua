"""P9 阶段 9.2.2a：``ChahuaServer._session`` setter 前台原地替换回归。

setter 是 ``_replace_session``（同房重建）/ ``_create_room`` 的换 session 入口。
P9 多 runtime 后它**只能动前台那一条**注册表项 —— 切走后留在注册表的 background
runtime 不能被连带丢掉（旧 setter 整体重建 ``_runtimes`` 会泄漏它们）。
"""

from __future__ import annotations

from chahua.events import NOOP_SINK
from chahua.room_runtime import RoomEventRouter, RoomRuntime
from chahua.server import ChahuaServer, _install_handler_slots


class _FakeRoomConfig:
    def __init__(self, name: str) -> None:
        # room_dir 只需 .name —— setter 取 room_dir.name 当注册表 key。
        self.room_dir = type("_Dir", (), {"name": name})()


class _FakeOrchestrator:
    """C2 起 setter 经 ``_attach_runtime_state`` 写 ``orchestrator.active_guest_names``。"""

    active_guest_names: set[str] | None = None


class _FakeSession:
    def __init__(self, name: str) -> None:
        self.room_config = _FakeRoomConfig(name)
        self.orchestrator = _FakeOrchestrator()


def _runtime(room_id: str) -> RoomRuntime:
    return RoomRuntime(
        room_id=room_id,
        session=_FakeSession(room_id),  # type: ignore[arg-type]
        router=RoomEventRouter(NOOP_SINK),
    )


def _server(foreground: str, *background: str) -> ChahuaServer:
    srv = object.__new__(ChahuaServer)
    srv._runtimes = {foreground: _runtime(foreground)}
    for bg in background:
        srv._runtimes[bg] = _runtime(bg)
    srv._foreground_id = foreground
    # P11 slot 拆分：``_session`` setter 调 ``_attach_runtime_state`` → 经
    # ``_make_start_agent_run`` 走 ``_agent_run_ops``，必装。
    _install_handler_slots(srv)
    return srv


def test_setter_swaps_foreground_session_in_place() -> None:
    """同房重建：换前台 session，前台 key / 指针不变。"""
    srv = _server("A")
    new_sess = _FakeSession("A")

    srv._session = new_sess  # type: ignore[assignment]

    assert srv._foreground_id == "A"
    assert srv._runtimes["A"].session is new_sess
    assert srv._session is new_sess


def test_setter_keeps_background_runtimes() -> None:
    """换前台 session 不该丢掉切走后仍在后台的 runtime。"""
    srv = _server("A", "B", "C")
    bg_b, bg_c = srv._runtimes["B"], srv._runtimes["C"]

    srv._session = _FakeSession("A")  # type: ignore[assignment]

    assert srv._runtimes["B"] is bg_b
    assert srv._runtimes["C"] is bg_c


def test_setter_rekeys_on_new_room_id() -> None:
    """新建房：新 session 的 room_id 与旧前台不同 → 换 key + 移前台指针。"""
    srv = _server("A", "B")
    bg_b = srv._runtimes["B"]
    new_sess = _FakeSession("D")

    srv._session = new_sess  # type: ignore[assignment]

    assert srv._foreground_id == "D"
    assert "A" not in srv._runtimes  # 旧前台 key 被 pop
    assert srv._runtimes["D"].session is new_sess
    assert srv._runtimes["B"] is bg_b  # 后台仍在
