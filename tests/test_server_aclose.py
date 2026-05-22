"""P9 阶段 9.2.1：:meth:`ChahuaServer.aclose` 异步遍历清理回归。

覆盖：

- ``aclose`` 遍历**所有** RoomRuntime 做 cancel+drain+close，清完移出注册表。
- 重复 ``aclose`` 幂等（空注册表 → noop，不报错）。
- 带 MTS 的 runtime：先 ``end_managed_session`` 再 cancel drain（§8 拆除顺序）。
- in-flight turn task 被 cancel+drain。
- 同步 ``close`` 兜底：仅 close session，不 drain。
"""

from __future__ import annotations

import asyncio

from chahua.events import NOOP_SINK
from chahua.handoff import MANAGED_SESSION_REASON_USER_CANCEL
from chahua.room_runtime import RoomEventRouter, RoomRuntime
from chahua.server import ChahuaServer


# ── 极简 fake：只挂 aclose / close 真正会访问的接口 ─────────────────────────


class _FakeOrch:
    def __init__(self, *, has_managed_session: bool = False) -> None:
        self.managed_session: object | None = (
            object() if has_managed_session else None
        )
        self.ended_reason: str | None = None
        # aclose 拆除顺序见证：end_managed_session 触发时 inflight task 是否还活着。
        self.session_alive_at_end: bool | None = None
        self._runtime: RoomRuntime | None = None

    def end_managed_session(self, sink: object, *, reason: str) -> None:
        self.ended_reason = reason
        self.managed_session = None
        if self._runtime is not None:
            self.session_alive_at_end = self._runtime.inflight_alive()


class _FakeSession:
    def __init__(self, *, has_managed_session: bool = False) -> None:
        self.orchestrator = _FakeOrch(has_managed_session=has_managed_session)
        self.closed = False

    def close(self) -> None:
        self.closed = True


def _runtime(room_id: str, *, has_managed_session: bool = False) -> RoomRuntime:
    session = _FakeSession(has_managed_session=has_managed_session)
    rt = RoomRuntime(
        room_id=room_id,
        session=session,  # type: ignore[arg-type]
        router=RoomEventRouter(NOOP_SINK),
    )
    session.orchestrator._runtime = rt
    return rt


def _server(*runtimes: RoomRuntime) -> ChahuaServer:
    srv = object.__new__(ChahuaServer)
    srv._runtimes = {rt.room_id: rt for rt in runtimes}
    srv._foreground_id = runtimes[0].room_id if runtimes else ""
    return srv


# ── aclose ────────────────────────────────────────────────────────────────


async def test_aclose_closes_all_runtimes() -> None:
    """前台 + 多个后台 runtime 都被 close 并移出注册表。"""
    fg, bg1, bg2 = _runtime("fg"), _runtime("bg1"), _runtime("bg2")
    srv = _server(fg, bg1, bg2)

    await srv.aclose()

    assert fg.session.closed and bg1.session.closed and bg2.session.closed
    assert srv._runtimes == {}


async def test_aclose_idempotent() -> None:
    """重复 aclose —— 第二次遍历空注册表，不报错。"""
    srv = _server(_runtime("fg"))
    await srv.aclose()
    await srv.aclose()  # 不应抛
    assert srv._runtimes == {}


async def test_aclose_ends_managed_session_before_drain() -> None:
    """带 MTS 的 runtime：end_managed_session 先于 cancel drain（§8 拆除顺序）。"""
    rt = _runtime("fg", has_managed_session=True)
    task = asyncio.create_task(asyncio.sleep(3600))
    rt.set_inflight(task, "handoff")
    srv = _server(rt)

    await srv.aclose()

    orch = rt.session.orchestrator
    assert orch.managed_session is None
    assert orch.ended_reason == MANAGED_SESSION_REASON_USER_CANCEL
    # end_managed_session 触发时 in-flight drain task 还活着 —— 顺序正确。
    assert orch.session_alive_at_end is True
    assert task.cancelled()


async def test_aclose_cancels_inflight_turn() -> None:
    """in-flight turn task 被 aclose cancel+drain。"""
    rt = _runtime("fg")
    task = asyncio.create_task(asyncio.sleep(3600))
    rt.set_inflight(task, "user")
    srv = _server(rt)

    await srv.aclose()

    assert task.done()
    assert rt.session.closed


async def test_aclose_continues_past_failing_runtime() -> None:
    """一个 runtime close 抛错不该挡住其余 runtime 清理。"""
    bad = _runtime("bad")

    def _boom() -> None:
        raise RuntimeError("close failed")

    bad.session.close = _boom  # type: ignore[method-assign]
    good = _runtime("good")
    srv = _server(bad, good)

    await srv.aclose()

    assert good.session.closed
    assert srv._runtimes == {}  # bad 也被移出（finally 兜底）


async def test_aclose_closes_session_when_end_mts_raises() -> None:
    """end_managed_session 抛错不该挡住 runtime.close() —— 否则该房 agentao /
    MCP 子进程在进程退出时变孤儿泄漏（三步各自 try）。"""
    rt = _runtime("fg", has_managed_session=True)

    def _boom(sink: object, *, reason: str) -> None:
        raise RuntimeError("end MTS failed")

    rt.session.orchestrator.end_managed_session = _boom  # type: ignore[method-assign]
    srv = _server(rt)

    await srv.aclose()

    assert rt.session.closed  # end MTS 抛错后 close 仍跑到
    assert srv._runtimes == {}


# ── 同步 close 兜底 ────────────────────────────────────────────────────────


def test_close_closes_all_sessions_sync() -> None:
    """同步 close 兜底：close 所有 runtime 的 session。"""
    fg, bg = _runtime("fg"), _runtime("bg")
    srv = _server(fg, bg)

    srv.close()

    assert fg.session.closed and bg.session.closed
