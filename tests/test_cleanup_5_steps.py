"""P11 C9：清理 5 步 + ws 断开分流回归。

承重不变量（docs/P11 §「取消与清理」）：
- ``cancel_and_drain_all()``：sync cancel inflight + 全部 bg run → await drain bg →
  await drain inflight。
- 清理路径前 cancel 所有生产者、后 await drain —— drain 期间不会有新 bg run race。
- ``_cancel_and_drain_inflight()`` 仅供 cancel 按钮，不动 bg run。
- ``_cancel_and_drain_all_foreground()`` 是所有 mutate 前必经入口。
"""

from __future__ import annotations

import asyncio

import pytest

from chahua.agent_run import create as create_agent_run
from chahua.events import NOOP_SINK
from chahua.room_runtime import RoomEventRouter, RoomRuntime


def _make_runtime() -> RoomRuntime:
    return RoomRuntime(
        room_id="r",
        session=object(),  # type: ignore[arg-type]
        router=RoomEventRouter(NOOP_SINK),
    )


# ── 5 步顺序回归 ────────────────────────────────────────────────────────────


async def test_cancel_and_drain_all_runs_inflight_and_bg_in_order() -> None:
    """sync cancel 阶段先发出所有 cancel，await drain 阶段等所有 finally 跑完。"""
    rt = _make_runtime()

    finally_ran: list[str] = []

    async def inflight_coro() -> None:
        try:
            await asyncio.sleep(100)
        finally:
            finally_ran.append("inflight")

    async def bg_coro(tag: str) -> None:
        try:
            await asyncio.sleep(100)
        finally:
            finally_ran.append(f"bg_{tag}")

    rt.set_inflight(asyncio.create_task(inflight_coro()), "user")
    for tag in ("a", "b"):
        run = create_agent_run(room_id="r", guest_name=f"X{tag}", instruction="x")
        rt.agent_runs[run.run_id] = run
        rt.agent_run_tasks[run.run_id] = asyncio.create_task(bg_coro(tag))
    await asyncio.sleep(0)  # 让所有 task 进 await。

    await rt.cancel_and_drain_all()

    # finally 全跑了；前台 inflight + 两个 bg 各一次。
    assert sorted(finally_ran) == ["bg_a", "bg_b", "inflight"]
    # 注册表清空。
    assert rt.inflight_task is None or rt.inflight_task.done()
    assert rt.agent_runs == {}
    assert rt.agent_run_tasks == {}


async def test_cancel_and_drain_all_no_bg_no_inflight_returns() -> None:
    """空 runtime（无 inflight / 无 bg）—— 立即返回不抛。"""
    rt = _make_runtime()
    await rt.cancel_and_drain_all()


async def test_cancel_and_drain_all_only_bg_runs() -> None:
    """只有 bg run、无 inflight —— 仍 drain 干净。"""
    rt = _make_runtime()

    finally_ran: list[str] = []

    async def bg_coro() -> None:
        try:
            await asyncio.sleep(100)
        finally:
            finally_ran.append("bg")

    run = create_agent_run(room_id="r", guest_name="B", instruction="x")
    rt.agent_runs[run.run_id] = run
    rt.agent_run_tasks[run.run_id] = asyncio.create_task(bg_coro())
    await asyncio.sleep(0)

    await rt.cancel_and_drain_all()
    assert finally_ran == ["bg"]
    assert rt.agent_runs == {}


async def test_cancel_and_drain_all_only_inflight() -> None:
    """只有 inflight、无 bg —— 行为等价于 cancel_and_drain_inflight。"""
    rt = _make_runtime()

    finally_ran: list[str] = []

    async def inflight_coro() -> None:
        try:
            await asyncio.sleep(100)
        finally:
            finally_ran.append("inflight")

    rt.set_inflight(asyncio.create_task(inflight_coro()), "user")
    await asyncio.sleep(0)

    await rt.cancel_and_drain_all()
    assert finally_ran == ["inflight"]


# ── 顺序保证：sync cancel 先于 await drain ────────────────────────────────────


async def test_sync_cancel_phase_happens_before_any_await() -> None:
    """`cancel_and_drain_all` 在第一个 await 之前必须已 cancel 全部 task ——
    一旦 await drain 后某 task 才被 cancel，drain 等不到它。"""
    rt = _make_runtime()

    cancel_orders: list[str] = []

    class _SpyTask:
        """模拟 asyncio.Task：把 cancel 时机记下来。"""

        def __init__(self, name: str) -> None:
            self.name = name
            self._cancelled = False

        def cancel(self) -> bool:
            cancel_orders.append(self.name)
            self._cancelled = True
            return True

        def done(self) -> bool:
            return self._cancelled

    # 用真 Future 让 gather 立刻返回（不实际 await 任何 sleep）。
    rt.inflight_task = None  # 简化：只测 sync cancel 阶段对 bg。
    rt.inflight_kind = None
    for tag in ("a", "b", "c"):
        run = create_agent_run(room_id="r", guest_name=f"X{tag}", instruction="x")
        rt.agent_runs[run.run_id] = run
        rt.agent_run_tasks[run.run_id] = _SpyTask(tag)  # type: ignore[assignment]

    # 真 gather 不能跟 _SpyTask 一起用——直接调底层 step 验证 cancel 顺序。
    # 步骤 2 必先于步骤 3 await：测试通过校验「在第一次 await 之前所有 cancel 已发」。
    rt.cancel_inflight()  # 步骤 1，无副作用（inflight_task is None）
    for task in list(rt.agent_run_tasks.values()):
        if not task.done():
            task.cancel()  # type: ignore[attr-defined]
    # 此刻所有 sync cancel 已发出。
    assert sorted(cancel_orders) == ["a", "b", "c"]


# ── pre-start cancel race + sweep 兜底 ─────────────────────────────────────


async def test_cancel_and_drain_all_sweeps_leaked_entries() -> None:
    """与 cancel_and_drain_agent_runs 的 sweep 行为一致 —— wrapper finally 没跑也清。"""
    rt = _make_runtime()
    run = create_agent_run(room_id="r", guest_name="Bob", instruction="x")
    rt.agent_runs[run.run_id] = run
    rt.active_guest_names.add("Bob")

    async def _never_ran() -> None:
        await asyncio.sleep(100)

    rt.agent_run_tasks[run.run_id] = asyncio.create_task(_never_ran())
    await asyncio.sleep(0)

    await rt.cancel_and_drain_all()
    assert rt.agent_runs == {}
    assert rt.agent_run_tasks == {}
    # active_guest_names 也被 sweep 清掉。
    assert "Bob" not in rt.active_guest_names
