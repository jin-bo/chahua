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


async def test_cancel_and_drain_agent_runs_sweeps_leaked_entries() -> None:
    """pre-start cancel race：wrapper never ran → finally 不执行 → 三件套留死。
    drain 兜底 sweep 把死条目清掉。
    """
    rt = _make_runtime()

    # 模拟 wrapper 还没启动就被 cancel 的尾态：注册表里有 run + task，
    # active_guest_names 也有名字（inbound 同步 add 的），但 finally 没跑。
    run = create_agent_run(room_id="r", guest_name="Bob", instruction="x")
    rt.agent_runs[run.run_id] = run
    rt.active_guest_names.add("Bob")

    async def _never_ran() -> None:
        # 不进 try/finally —— 模拟 throw 在 body 之前就触发。
        await asyncio.sleep(100)

    task = asyncio.create_task(_never_ran())
    rt.agent_run_tasks[run.run_id] = task

    # 此外加一个正常完成的 task（wrapper 跑过 finally 已清掉自己条目）：
    # 验证 sweep 不会误伤已清的部分。
    async def _ok() -> None:
        return None

    done_task = asyncio.create_task(_ok())
    await done_task  # 让它完成；但保留在 agent_run_tasks 里模拟 wrapper finally 未 pop。
    rt.agent_run_tasks["run_done"] = done_task

    await rt.cancel_and_drain_agent_runs()

    # sweep 后三件套清空（Bob 也被 discard）。
    assert rt.agent_runs == {}
    assert rt.agent_run_tasks == {}
    assert "Bob" not in rt.active_guest_names


async def test_drain_sweep_preserves_foreground_only_active_guest_names() -> None:
    """sweep 只清「bg run 注册表里」的 guest_name —— 不动 foreground let_speak 加的 entry。"""
    rt = _make_runtime()

    # bg run 留死条目：guest_name=Bob
    leaked = create_agent_run(room_id="r", guest_name="Bob", instruction="x")
    rt.agent_runs[leaked.run_id] = leaked
    rt.active_guest_names.add("Bob")

    # 同时 foreground let_speak 加了 Alice（无对应 bg run）
    rt.active_guest_names.add("Alice")

    async def _never_ran() -> None:
        await asyncio.sleep(100)

    rt.agent_run_tasks[leaked.run_id] = asyncio.create_task(_never_ran())

    await rt.cancel_and_drain_agent_runs()

    # Bob 被 sweep 清掉，Alice 保留（foreground 还在 speak，不能误伤）。
    assert "Bob" not in rt.active_guest_names
    assert "Alice" in rt.active_guest_names


async def test_cancel_and_drain_agent_runs_handles_already_done_task() -> None:
    rt = _make_runtime()

    async def _done() -> None:
        return None

    task = asyncio.create_task(_done())
    rt.agent_run_tasks["run_z"] = task
    await task  # 提前 await 让 task done。
    # done task 进入 helper：不再 cancel，gather 立刻返回。
    await rt.cancel_and_drain_agent_runs()
