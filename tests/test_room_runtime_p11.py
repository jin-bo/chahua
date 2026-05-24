"""P11 C1：``RoomRuntime`` bg run 扩展回归。

覆盖：

- 新字段默认值（空 dict / 空 set）；
- ``has_active_runs()`` / ``busy_alive()`` / ``guest_busy()`` 真值表；
- ``cancel_and_drain_agent_runs()`` 空 dict → 立即返回、不抛；
- ``cancel_and_drain_agent_runs()`` 含一个已 cancel task：等到 wrapper 退出、
  ``CancelledError`` 被 ``return_exceptions=True`` 吞、不向调用方传播；
- ``cancel_and_drain_agent_runs()`` 含一个抛非 CancelledError 的 task：WARN 后继续 drain，
  调用方不感知异常；
- ``inflight_alive()`` 与 bg run 解耦：只有 bg run、无 foreground turn → False。
"""

from __future__ import annotations

import asyncio

import pytest

from chahua.agent_run import AgentRun, create as create_agent_run
from chahua.events import NOOP_SINK
from chahua.room_runtime import RoomEventRouter, RoomRuntime


def _make_runtime() -> RoomRuntime:
    return RoomRuntime(
        room_id="r",
        session=object(),  # type: ignore[arg-type]
        router=RoomEventRouter(NOOP_SINK),
    )


def test_agent_run_direct_construction_gets_real_timestamp() -> None:
    """直接构造 AgentRun（不经 create 工厂）也必须拿到真实时间戳，不是 0。"""
    run = AgentRun(
        run_id="run_abc",
        room_id="r",
        guest_name="Bob",
        instruction="ping",
    )
    assert run.created_at_ms > 0


def test_default_fields_empty() -> None:
    rt = _make_runtime()
    assert rt.agent_runs == {}
    assert rt.agent_run_tasks == {}
    assert rt.active_guest_names == set()
    assert rt.has_active_runs() is False
    assert rt.busy_alive() is False
    assert rt.guest_busy("Alice") is False
    assert rt.inflight_alive() is False


def test_has_active_runs_and_busy_alive_track_agent_runs() -> None:
    rt = _make_runtime()
    run = create_agent_run(room_id="r", guest_name="Bob", instruction="ping")
    rt.agent_runs[run.run_id] = run
    assert rt.has_active_runs() is True
    assert rt.busy_alive() is True
    # 但 inflight_alive() 不能被 bg run 污染（P11 设计：前台 turn 控制语义保留）。
    assert rt.inflight_alive() is False


def test_guest_busy_reads_active_guest_names() -> None:
    rt = _make_runtime()
    rt.active_guest_names.add("Bob")
    assert rt.guest_busy("Bob") is True
    assert rt.guest_busy("Alice") is False
    rt.active_guest_names.discard("Bob")
    assert rt.guest_busy("Bob") is False


async def test_cancel_and_drain_agent_runs_empty_returns() -> None:
    rt = _make_runtime()
    # 不抛、立即返回。
    await rt.cancel_and_drain_agent_runs()


async def test_cancel_and_drain_agent_runs_cancels_running_task() -> None:
    rt = _make_runtime()

    finally_ran: list[str] = []

    async def _wrapper() -> None:
        try:
            await asyncio.sleep(100)
        finally:
            finally_ran.append("ok")

    task = asyncio.create_task(_wrapper())
    rt.agent_run_tasks["run_x"] = task
    # 让 task 真正进 await。
    await asyncio.sleep(0)

    await rt.cancel_and_drain_agent_runs()
    # wrapper finally 必跑过、CancelledError 不向调用方传播。
    assert finally_ran == ["ok"]
    assert task.cancelled() or task.done()


async def test_cancel_and_drain_agent_runs_swallows_non_cancel_exception(
    caplog: pytest.LogCaptureFixture,
) -> None:
    rt = _make_runtime()

    async def _wrapper() -> None:
        raise RuntimeError("boom")

    task = asyncio.create_task(_wrapper())
    rt.agent_run_tasks["run_y"] = task
    await asyncio.sleep(0)  # 让 task 跑到抛点。

    with caplog.at_level("WARNING", logger="chahua.room_runtime"):
        # 调用方不感知异常 —— 否则会冒泡到 clear_room/aclose finally。
        await rt.cancel_and_drain_agent_runs()

    assert any("bg run wrapper raised" in rec.message for rec in caplog.records)


async def test_cancel_and_drain_agent_runs_handles_already_done_task() -> None:
    rt = _make_runtime()

    async def _done() -> None:
        return None

    task = asyncio.create_task(_done())
    rt.agent_run_tasks["run_z"] = task
    await task  # 提前 await 让 task done。
    # done task 进入 helper：不再 cancel，gather 立刻返回。
    await rt.cancel_and_drain_agent_runs()
