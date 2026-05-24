"""P11 C7：``busy_alive()`` 窄替换（3 处）回归。

替换为 ``busy_alive()`` 的判定：
1. ``_switch_room`` 旧前台 demote 判断 —— 仅有 bg run、无前台 turn 时仍 demote 转后台。
2. ``_maybe_self_destruct_background_runtime`` 自毁判断 —— 后台房只剩 bg run 时不自毁。
3. ``_rooms_available_with_busy`` 房间 busy 标志 —— 有 bg run 的房在房间列表里也亮 busy。

保持 ``inflight_alive()`` 的判定（不被 bg run 污染）：
4. ``_inbound_cancel`` 前台 cancel 按钮 —— 不取消 bg run（用户后台自己拉起的并行工作）。
5. ``user_message`` 单 in-flight 防御 —— 只有 bg run 时新 user_message 仍正常入。
"""

from __future__ import annotations

import asyncio
from typing import Optional

import pytest

from chahua.agent_run import create as create_agent_run
from chahua.events import NOOP_SINK
from chahua.room_runtime import (
    ROUTER_MODE_BACKGROUND,
    ROUTER_MODE_FOREGROUND,
    RoomEventRouter,
    RoomRuntime,
)
from chahua.server import ChahuaServer


class _FakeRoom:
    name = "test"


class _FakeOrchestrator:
    active_guest_names: Optional[set[str]] = None
    _managed_session: Optional[object] = None


class _FakeSession:
    def __init__(self) -> None:
        self.orchestrator = _FakeOrchestrator()
        self.room = _FakeRoom()
        self.closed = False

    def close(self) -> None:
        self.closed = True


def _runtime(room_id: str, *, mode: str = ROUTER_MODE_FOREGROUND) -> RoomRuntime:
    return RoomRuntime(
        room_id=room_id,
        session=_FakeSession(),  # type: ignore[arg-type]
        router=RoomEventRouter(NOOP_SINK, mode=mode),  # type: ignore[arg-type]
    )


# ── 1) _maybe_self_destruct_background_runtime 自毁判定 ──────────────────────


def test_self_destruct_skipped_when_bg_run_present_no_inflight() -> None:
    """后台 runtime 只有 bg run、没前台 turn —— busy_alive() 应保住 runtime 不自毁。"""
    rt = _runtime("rid", mode=ROUTER_MODE_BACKGROUND)
    # 模拟仍在跑的 bg run。
    run = create_agent_run(room_id="rid", guest_name="Bob", instruction="x")
    rt.agent_runs[run.run_id] = run

    srv = object.__new__(ChahuaServer)
    srv._runtimes = {"rid": rt}
    srv._foreground_id = ""

    sink_log: list = []
    rt.router.ws_sink = sink_log.append

    srv._maybe_self_destruct_background_runtime(rt)

    # 没自毁：runtime 仍在注册表里，没 emit room_background_finished。
    assert "rid" in srv._runtimes
    assert sink_log == []


def test_self_destruct_runs_when_truly_idle() -> None:
    """后台 runtime 既无 bg run 也无前台 turn —— 走自毁路径。"""
    rt = _runtime("rid", mode=ROUTER_MODE_BACKGROUND)
    # 不放 bg run，也无 inflight task。

    srv = object.__new__(ChahuaServer)
    srv._runtimes = {"rid": rt}
    srv._foreground_id = ""

    sink_log: list = []
    rt.router.ws_sink = sink_log.append

    srv._maybe_self_destruct_background_runtime(rt)

    assert "rid" not in srv._runtimes
    assert len(sink_log) == 1  # emit 了 ROOM_BACKGROUND_FINISHED


# ── 2) _rooms_available_with_busy 含 bg run ──────────────────────────────────


def test_rooms_available_with_bg_run_marks_busy(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """房间只跑 bg run 时仍标 busy=true（让前端 sidebar「进行中」徽标亮）。"""
    from chahua import server_room_snapshot

    # discover_rooms 替身：返一房 rid。
    monkeypatch.setattr(
        server_room_snapshot, "discover_rooms",
        lambda paths: [{"room_id": "rid", "name": "test"}],
    )

    rt = _runtime("rid")
    run = create_agent_run(room_id="rid", guest_name="Bob", instruction="x")
    rt.agent_runs[run.run_id] = run

    srv = object.__new__(ChahuaServer)
    srv._runtimes = {"rid": rt}
    srv._paths = object()

    rooms = server_room_snapshot._rooms_available_with_busy(srv)
    assert rooms[0]["busy"] is True


# ── 3) _switch_room demote 判定（间接验证：构造场景下走 demote 而非 close）────────


def test_switch_room_demotes_when_only_bg_run() -> None:
    """直接验证 RoomRuntime.busy_alive() 在「仅 bg run、无 inflight」时返 True ——
    _switch_room 的 demote 分支基于此。"""
    rt = _runtime("rid")
    assert rt.busy_alive() is False  # 空 runtime 不 busy

    # 加一条 bg run，inflight 仍为空。
    run = create_agent_run(room_id="rid", guest_name="Bob", instruction="x")
    rt.agent_runs[run.run_id] = run

    assert rt.busy_alive() is True
    assert rt.inflight_alive() is False  # P11 不变量：bg run 不污染 inflight 语义
