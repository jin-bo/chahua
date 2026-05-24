"""P11.2 C11：``server._start_agent_run`` 共调 helper + ``_attach_runtime_state``
把 ``start_agent_run`` 回调绑到 guests 的回归。

覆盖：
- 校验 4 道（target 在场 / task_id 未关闭 / 不 busy / 不超 ``MAX_AGENT_RUNS_PER_ROOM``）。
- 成功路径：登记 ``agent_runs`` + ``active_guest_names.add`` **先于** ``create_task`` +
  emit ``AGENT_RUN_STARTED`` 走 ``runtime.router``。
- ``_attach_runtime_state`` 把 ``start_agent_run`` 槽位绑到每个 guest，且切换 runtime
  时同一 guest 槽位被重写为新 runtime 的回调（**闭合契约**）。
"""

from __future__ import annotations

import asyncio
from typing import Any, Optional

import pytest

from chahua.agent_run import AGENT_RUN_ISSUED_BY_AGENT, AGENT_RUN_ISSUED_BY_USER
from chahua.events import (
    ChahuaEnvelope,
    ChahuaEventType,
    NOOP_SINK,
)
from chahua.room_runtime import RoomEventRouter, RoomRuntime
from chahua.server import ChahuaServer
from chahua.server_inbound_agent_run import MAX_AGENT_RUNS_PER_ROOM


# ── 测试夹具 ─────────────────────────────────────────────────────────────────


class _FakeOrch:
    def __init__(self, guest_names: tuple[str, ...]) -> None:
        self.guest_names: tuple[str, ...] = guest_names
        self.active_guest_names: Optional[set[str]] = None


class _FakeRoom:
    name = "test-room"


class _FakeTask:
    def __init__(self, status: str = "open") -> None:
        self.status = status


class _FakeTasksStore:
    def __init__(self, by_id: dict[str, _FakeTask]) -> None:
        self._by_id = by_id

    def get_task(self, task_id: str) -> Optional[_FakeTask]:
        return self._by_id.get(task_id)


class _FakeGuest:
    def __init__(self, name: str) -> None:
        self.name = name
        self.start_agent_run = None  # 由 _attach_runtime_state 装


class _FakeSession:
    def __init__(
        self, *,
        guest_names: tuple[str, ...] = ("Bob", "Alice"),
        guests: Optional[list[_FakeGuest]] = None,
        tasks: Optional[dict[str, _FakeTask]] = None,
    ) -> None:
        self.orchestrator = _FakeOrch(guest_names)
        self.room = _FakeRoom()
        self.tasks_store = _FakeTasksStore(tasks or {})
        self.guests = guests if guests is not None else [
            _FakeGuest(n) for n in guest_names
        ]


def _make_server(
    sink_log: list[ChahuaEnvelope],
    *,
    guest_names: tuple[str, ...] = ("Bob", "Alice"),
    guests: Optional[list[_FakeGuest]] = None,
    tasks: Optional[dict[str, _FakeTask]] = None,
) -> tuple[ChahuaServer, RoomRuntime]:
    srv = object.__new__(ChahuaServer)
    rt = RoomRuntime(
        room_id="rid",
        session=_FakeSession(  # type: ignore[arg-type]
            guest_names=guest_names, guests=guests, tasks=tasks,
        ),
        router=RoomEventRouter(sink_log.append),
    )
    srv._runtimes = {"rid": rt}
    srv._foreground_id = "rid"

    # 替 _run_agent_background：不真跑 wrapper（外层测它），返一个 done task。
    async def fake_wrapper(runtime: RoomRuntime, run: Any) -> None:
        return None

    srv._run_agent_background = fake_wrapper  # type: ignore[assignment]
    srv._attach_runtime_state(rt)  # 同时挂 active_guest_names + start_agent_run
    return srv, rt


# ── _attach_runtime_state 绑定槽位 ───────────────────────────────────────────


def test_attach_runtime_state_binds_start_agent_run_on_guests() -> None:
    """``_attach_runtime_state`` 把 ``_make_start_agent_run`` 回调绑到每个 guest 槽位。"""
    sink: list[ChahuaEnvelope] = []
    guests = [_FakeGuest("Bob"), _FakeGuest("Alice")]
    srv, rt = _make_server(sink, guests=guests)

    assert guests[0].start_agent_run is not None
    assert guests[1].start_agent_run is not None
    # 同一 runtime → 同一回调对象（_make_start_agent_run 返一个 closure）。
    assert guests[0].start_agent_run is guests[1].start_agent_run


def test_attach_runtime_state_rebinds_on_runtime_swap() -> None:
    """re-attach（同 runtime 二次调用 / 切房新 runtime）—— guest.start_agent_run
    被重写到新回调（**闭合契约**：同 Tool 实例下次 call 自动走新 runtime）。"""
    sink: list[ChahuaEnvelope] = []
    guests = [_FakeGuest("Bob")]
    srv, rt_a = _make_server(sink, guests=guests)
    cb_a = guests[0].start_agent_run

    # 构造另一个 runtime，re-attach。
    rt_b = RoomRuntime(
        room_id="rid_b",
        session=_FakeSession(  # type: ignore[arg-type]
            guest_names=("Bob",), guests=guests,
        ),
        router=RoomEventRouter(NOOP_SINK),
    )
    srv._attach_runtime_state(rt_b)
    cb_b = guests[0].start_agent_run

    assert cb_a is not None
    assert cb_b is not None
    assert cb_a is not cb_b  # 槽位被重写


# ── _start_agent_run 校验路径 ────────────────────────────────────────────────


def test_start_agent_run_rejects_unknown_target() -> None:
    sink: list[ChahuaEnvelope] = []
    srv, rt = _make_server(sink, guest_names=("Bob",))
    run, err = srv._start_agent_run(
        rt, target="Carol", instruction="x", task_id=None,
        issued_by=AGENT_RUN_ISSUED_BY_USER, source_guest=None,
    )
    assert run is None
    assert err is not None and "不在场" in err


def test_start_agent_run_rejects_closed_task() -> None:
    sink: list[ChahuaEnvelope] = []
    srv, rt = _make_server(
        sink,
        guest_names=("Bob",),
        tasks={"t1": _FakeTask(status="done")},
    )
    run, err = srv._start_agent_run(
        rt, target="Bob", instruction="x", task_id="t1",
        issued_by=AGENT_RUN_ISSUED_BY_USER, source_guest=None,
    )
    assert run is None
    assert err is not None and "已关闭" in err


def test_start_agent_run_rejects_missing_task() -> None:
    sink: list[ChahuaEnvelope] = []
    srv, rt = _make_server(sink, guest_names=("Bob",), tasks={})
    run, err = srv._start_agent_run(
        rt, target="Bob", instruction="x", task_id="t-unknown",
        issued_by=AGENT_RUN_ISSUED_BY_USER, source_guest=None,
    )
    assert run is None
    assert err is not None and "不存在" in err


def test_start_agent_run_rejects_busy_target() -> None:
    sink: list[ChahuaEnvelope] = []
    srv, rt = _make_server(sink, guest_names=("Bob",))
    rt.active_guest_names.add("Bob")  # 模拟前台 speak / handoff drain 占用
    run, err = srv._start_agent_run(
        rt, target="Bob", instruction="x", task_id=None,
        issued_by=AGENT_RUN_ISSUED_BY_USER, source_guest=None,
    )
    assert run is None
    assert err is not None and "正忙" in err


async def _drain(rt: RoomRuntime) -> None:
    """收尾 pending wrapper task 避免 RuntimeWarning。"""
    for t in list(rt.agent_run_tasks.values()):
        t.cancel()
    for t in list(rt.agent_run_tasks.values()):
        try:
            await t
        except asyncio.CancelledError:
            pass


async def test_start_agent_run_rejects_at_room_cap() -> None:
    sink: list[ChahuaEnvelope] = []
    srv, rt = _make_server(
        sink, guest_names=("Bob", "Alice", "Carol", "Dan", "Eve"),
    )
    # 占满 max。
    for i in range(MAX_AGENT_RUNS_PER_ROOM):
        run, err = srv._start_agent_run(
            rt, target=("Bob", "Alice", "Carol", "Dan", "Eve")[i],
            instruction="x", task_id=None,
            issued_by=AGENT_RUN_ISSUED_BY_USER, source_guest=None,
        )
        assert err is None
    # 第 5 条拒。
    run, err = srv._start_agent_run(
        rt, target="Eve", instruction="x", task_id=None,
        issued_by=AGENT_RUN_ISSUED_BY_USER, source_guest=None,
    )
    assert run is None
    assert err is not None and "已达上限" in err
    await _drain(rt)


# ── _start_agent_run 成功路径 ─────────────────────────────────────────────────


async def test_start_agent_run_registers_before_create_task() -> None:
    """登记 ``agent_runs[run_id]=run`` + ``active_guest_names.add(target)`` 同步发生
    于 ``asyncio.create_task`` 之前 —— 该不变量保证同 target 第二条请求在 wrapper
    进 speak 之前就被 ``guest_busy`` 拒。

    断言间接验证：调用返回后 (`run, None`)，注册表 + active_guest_names 都已就位。
    """
    sink: list[ChahuaEnvelope] = []
    srv, rt = _make_server(sink, guest_names=("Bob",))
    run, err = srv._start_agent_run(
        rt, target="Bob", instruction="审", task_id=None,
        issued_by=AGENT_RUN_ISSUED_BY_USER, source_guest=None,
    )
    assert err is None
    assert run is not None
    assert run.run_id in rt.agent_runs
    assert run.run_id in rt.agent_run_tasks
    assert "Bob" in rt.active_guest_names

    # emit AGENT_RUN_STARTED 走 runtime.router。
    started = [e for e in sink if e.type is ChahuaEventType.AGENT_RUN_STARTED]
    assert len(started) == 1
    assert started[0].data["run_id"] == run.run_id
    assert started[0].data["guest_name"] == "Bob"
    assert started[0].data["issued_by"] == AGENT_RUN_ISSUED_BY_USER
    assert started[0].data["instruction_preview"] == "审"
    assert "task_id" not in started[0].data  # 没绑任务

    await _drain(rt)


async def test_start_agent_run_truncates_long_instruction_preview() -> None:
    """``instruction_preview`` 截 30 字 —— 前端列表条固定预算。"""
    sink: list[ChahuaEnvelope] = []
    srv, rt = _make_server(sink, guest_names=("Bob",))
    long = "a" * 100
    run, err = srv._start_agent_run(
        rt, target="Bob", instruction=long, task_id=None,
        issued_by=AGENT_RUN_ISSUED_BY_USER, source_guest=None,
    )
    assert err is None
    started = [e for e in sink if e.type is ChahuaEventType.AGENT_RUN_STARTED]
    assert started[0].data["instruction_preview"] == "a" * 30
    await _drain(rt)


async def test_start_agent_run_passes_source_guest_for_agent_issued() -> None:
    """``issued_by=AGENT`` + ``source_guest`` 进 AgentRun + envelope。"""
    sink: list[ChahuaEnvelope] = []
    srv, rt = _make_server(sink, guest_names=("Bob",))
    run, err = srv._start_agent_run(
        rt, target="Bob", instruction="审", task_id=None,
        issued_by=AGENT_RUN_ISSUED_BY_AGENT, source_guest="Carol",
    )
    assert err is None
    assert run is not None
    assert run.issued_by == AGENT_RUN_ISSUED_BY_AGENT
    assert run.source_guest == "Carol"
    started = [e for e in sink if e.type is ChahuaEventType.AGENT_RUN_STARTED]
    assert started[0].data["source_guest"] == "Carol"
    assert started[0].data["issued_by"] == AGENT_RUN_ISSUED_BY_AGENT
    await _drain(rt)


# ── _make_start_agent_run 回调结合工具 ────────────────────────────────────────


async def test_make_start_agent_run_callback_returns_run_id_tuple() -> None:
    """工厂返回的回调签名 ``(target, instruction, task_id, source_guest)`` →
    ``(run_id, None)`` / ``(None, error)``。工具按此协议解析。"""
    sink: list[ChahuaEnvelope] = []
    srv, rt = _make_server(sink, guest_names=("Bob",))
    cb = srv._make_start_agent_run(rt)
    run_id, err = cb(
        target="Bob", instruction="x", task_id=None, source_guest="Carol",
    )
    assert err is None
    assert run_id is not None
    assert run_id in rt.agent_runs
    # source_guest 透传到 AgentRun。
    assert rt.agent_runs[run_id].source_guest == "Carol"
    assert rt.agent_runs[run_id].issued_by == AGENT_RUN_ISSUED_BY_AGENT
    await _drain(rt)


# ── 端到端：spawn 工具经 _attach_runtime_state 绑定后调真 _start_agent_run ────


async def test_spawn_tool_end_to_end_via_attach_runtime_state() -> None:
    """**关键回归**：工具继承 AsyncToolBase 后，``async_execute`` 在 event loop 内
    跑，调真 ``_start_agent_run`` → ``asyncio.create_task`` 不再抛 ``no running event
    loop``（这是 C11 review 发现的 critical bug，B1）。

    场景：build _FakeGuest 装好 spawn tool，server._attach_runtime_state 绑 callback，
    `await tool.async_execute(...)` 走全链 → agent_runs 真出现 run 条目。
    """
    from chahua.agent_run_tools import SpawnAgentRunTool, register_agent_run_tools

    class _FakeTools:
        def __init__(self) -> None:
            self.registered: list = []

        def register(self, tool) -> None:
            self.registered.append(tool)

    class _FakeAgent:
        def __init__(self) -> None:
            self.tools = _FakeTools()

    class _TeaGuestStub:
        def __init__(self, name: str) -> None:
            self.name = name
            self.start_agent_run = None
            self.agent = _FakeAgent()
            register_agent_run_tools(
                self.agent,
                source_guest=self.name,
                get_start_agent_run=lambda: self.start_agent_run,
            )

    sink: list[ChahuaEnvelope] = []
    bob_stub = _TeaGuestStub("Bob")
    carol_stub = _TeaGuestStub("Carol")
    srv, rt = _make_server(
        sink, guest_names=("Bob", "Carol"),
        guests=[bob_stub, carol_stub],  # type: ignore[list-item]
    )

    # _attach_runtime_state 已在 _make_server 末尾调过 —— bob/carol 都拿到 callback。
    assert bob_stub.start_agent_run is not None
    assert carol_stub.start_agent_run is not None

    # Carol 调 spawn_agent_run 派 Bob —— 模拟茶客侧并发调度。
    spawn = next(
        t for t in carol_stub.agent.tools.registered
        if isinstance(t, SpawnAgentRunTool)
    )
    out = await spawn.async_execute(target="Bob", instruction="审一下")
    assert out.startswith("Successfully spawned bg run")

    # bg run 真登记，AgentRun.source_guest 是 Carol（工具调用方）。
    assert len(rt.agent_runs) == 1
    run = next(iter(rt.agent_runs.values()))
    assert run.guest_name == "Bob"
    assert run.source_guest == "Carol"
    assert run.issued_by == AGENT_RUN_ISSUED_BY_AGENT
    assert "Bob" in rt.active_guest_names

    # AGENT_RUN_STARTED 走 runtime.router 落 sink。
    started = [e for e in sink if e.type is ChahuaEventType.AGENT_RUN_STARTED]
    assert len(started) == 1
    assert started[0].data["source_guest"] == "Carol"

    await _drain(rt)
