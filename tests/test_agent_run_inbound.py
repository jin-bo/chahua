"""P11 C8：``agent_run_start`` / ``agent_run_cancel`` inbound 校验 + 登记 + race 回归。"""

from __future__ import annotations

import asyncio
from typing import Any, Optional

import pytest

from chahua.agent_run import AgentRun, create as create_agent_run
from chahua.events import (
    ChahuaEnvelope,
    ChahuaEventType,
    NOOP_SINK,
    NOTICE_LEVEL_ERROR,
)
from chahua.room_runtime import RoomEventRouter, RoomRuntime
from chahua.server import ChahuaServer
from chahua.server_inbound_agent_run import (
    AgentRunHandlers,
    INBOUND_AGENT_RUN_CANCEL,
    INBOUND_AGENT_RUN_START,
    MAX_AGENT_RUNS_PER_ROOM,
)


# ── 测试夹具 ─────────────────────────────────────────────────────────────────


class _FakeOrchestrator:
    def __init__(self, guests: tuple[str, ...]) -> None:
        self.guest_names: tuple[str, ...] = guests
        self.active_guest_names: Optional[set[str]] = None
        # P11.2.X F9：_start_agent_run 读 ``managed_session`` 公开 @property（不再
        # ``getattr`` 兜底）—— 测试 fake 必须显式提供同名 attr，None 即「无 MTS」。
        self.managed_session = None


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


class _FakeSession:
    def __init__(
        self, *, guests: tuple[str, ...] = ("Bob",),
        tasks: Optional[dict[str, _FakeTask]] = None,
    ) -> None:
        self.orchestrator = _FakeOrchestrator(guests)
        self.room = _FakeRoom()
        self.tasks_store: Optional[_FakeTasksStore] = (
            _FakeTasksStore(tasks) if tasks is not None else None
        )


def _make_server(
    sink_log: list[ChahuaEnvelope],
    *,
    guests: tuple[str, ...] = ("Bob", "Alice"),
    tasks: Optional[dict[str, _FakeTask]] = None,
    wrapper_spy: Optional[list[AgentRun]] = None,
) -> tuple[ChahuaServer, RoomRuntime]:
    srv = object.__new__(ChahuaServer)
    rt = RoomRuntime(
        room_id="rid",
        session=_FakeSession(guests=guests, tasks=tasks),  # type: ignore[arg-type]
        router=RoomEventRouter(sink_log.append),
    )
    # P11 C2：让 active_guest_names 与 orchestrator 同 set。
    rt.session.orchestrator.active_guest_names = rt.active_guest_names
    srv._runtimes = {"rid": rt}
    srv._foreground_id = "rid"
    srv.agent_run = AgentRunHandlers(srv)
    notices: list[tuple[str, str]] = []

    def emit_notice(sink: Any, *, level: str, text: str) -> None:
        notices.append((level, text))
        sink_log.append(ChahuaEnvelope(
            room_id="test-room", turn_id=None, guest_name=None,
            message_id=None, type=ChahuaEventType.NOTICE,
            data={"level": level, "text": text},
        ))

    srv._emit_notice = emit_notice  # type: ignore[assignment]
    srv._reject_unknown_keys = (  # type: ignore[assignment]
        lambda data, allowed, *, where, sink: _reject_unknown_keys(
            data, allowed, where=where, emit_notice=emit_notice, sink=sink,
        )
    )
    srv._notices_log = notices  # type: ignore[attr-defined]

    # _run_agent_background 替换为 spy —— 不真跑 wrapper（外层测它）。
    async def fake_wrapper(runtime: RoomRuntime, run: AgentRun) -> None:
        if wrapper_spy is not None:
            wrapper_spy.append(run)
        # 模拟 wrapper finally 行为：保留 entry 直到外部测试断言完。

    srv._run_agent_background = fake_wrapper  # type: ignore[assignment]
    return srv, rt


def _reject_unknown_keys(
    data: dict, allowed: frozenset, *, where: str,
    emit_notice, sink,
) -> bool:
    extra = set(data) - allowed
    if extra:
        emit_notice(
            sink, level=NOTICE_LEVEL_ERROR,
            text=f"{where}: unknown keys {sorted(extra)}",
        )
        return False
    return True


def _has_notice_containing(srv: ChahuaServer, sub: str) -> bool:
    notices = srv._notices_log  # type: ignore[attr-defined]
    return any(sub in text for _lvl, text in notices)


# ── agent_run_start 校验路径 ─────────────────────────────────────────────────


async def test_agent_run_start_rejects_unknown_keys() -> None:
    sink_log: list[ChahuaEnvelope] = []
    srv, rt = _make_server(sink_log)
    await srv.agent_run._inbound_agent_run_start(
        {"type": INBOUND_AGENT_RUN_START, "target": "Bob",
         "instruction": "x", "bogus": 1}, NOOP_SINK,
    )
    assert _has_notice_containing(srv, "unknown keys")
    assert rt.agent_runs == {}


async def test_agent_run_start_rejects_missing_instruction() -> None:
    sink_log: list[ChahuaEnvelope] = []
    srv, rt = _make_server(sink_log)
    await srv.agent_run._inbound_agent_run_start(
        {"type": INBOUND_AGENT_RUN_START, "target": "Bob"}, NOOP_SINK,
    )
    assert rt.agent_runs == {}


async def test_agent_run_start_rejects_empty_instruction() -> None:
    sink_log: list[ChahuaEnvelope] = []
    srv, rt = _make_server(sink_log)
    await srv.agent_run._inbound_agent_run_start(
        {"type": INBOUND_AGENT_RUN_START, "target": "Bob",
         "instruction": "  \t\n"}, NOOP_SINK,
    )
    assert _has_notice_containing(srv, "instruction 不能为空")
    assert rt.agent_runs == {}


async def test_agent_run_start_rejects_target_not_in_room() -> None:
    sink_log: list[ChahuaEnvelope] = []
    srv, rt = _make_server(sink_log)
    await srv.agent_run._inbound_agent_run_start(
        {"type": INBOUND_AGENT_RUN_START, "target": "Zhang",
         "instruction": "x"}, NOOP_SINK,
    )
    assert _has_notice_containing(srv, "不在场")
    assert rt.agent_runs == {}


async def test_agent_run_start_accepts_valid_open_task_id() -> None:
    """task_id 存在 + 未关闭 → 走成功路径 + run.task_id 被冻结。"""
    sink_log: list[ChahuaEnvelope] = []
    srv, rt = _make_server(sink_log, tasks={"task_t": _FakeTask("doing")})
    await srv.agent_run._inbound_agent_run_start(
        {"type": INBOUND_AGENT_RUN_START, "target": "Bob",
         "instruction": "x", "task_id": "task_t"}, sink_log.append,
    )
    runs = list(rt.agent_runs.values())
    assert len(runs) == 1
    assert runs[0].task_id == "task_t"
    await asyncio.gather(*rt.agent_run_tasks.values(), return_exceptions=True)


async def test_agent_run_start_rejects_task_id_unknown() -> None:
    sink_log: list[ChahuaEnvelope] = []
    srv, rt = _make_server(sink_log, tasks={})
    await srv.agent_run._inbound_agent_run_start(
        {"type": INBOUND_AGENT_RUN_START, "target": "Bob",
         "instruction": "x", "task_id": "task_zzz"}, NOOP_SINK,
    )
    assert _has_notice_containing(srv, "不存在")
    assert rt.agent_runs == {}


async def test_agent_run_start_rejects_task_id_closed() -> None:
    sink_log: list[ChahuaEnvelope] = []
    srv, rt = _make_server(sink_log, tasks={"task_done": _FakeTask("done")})
    await srv.agent_run._inbound_agent_run_start(
        {"type": INBOUND_AGENT_RUN_START, "target": "Bob",
         "instruction": "x", "task_id": "task_done"}, NOOP_SINK,
    )
    assert _has_notice_containing(srv, "已关闭")
    assert rt.agent_runs == {}


async def test_agent_run_start_rejects_target_busy() -> None:
    sink_log: list[ChahuaEnvelope] = []
    srv, rt = _make_server(sink_log)
    rt.active_guest_names.add("Bob")  # 模拟 Bob 正在前台 speak
    await srv.agent_run._inbound_agent_run_start(
        {"type": INBOUND_AGENT_RUN_START, "target": "Bob",
         "instruction": "x"}, NOOP_SINK,
    )
    assert _has_notice_containing(srv, "正忙")
    assert rt.agent_runs == {}


async def test_agent_run_start_rejects_when_room_at_max() -> None:
    sink_log: list[ChahuaEnvelope] = []
    srv, rt = _make_server(sink_log, guests=("Bob", "A", "B", "C", "D"))
    # 把 runtime 推到上限。
    for i in range(MAX_AGENT_RUNS_PER_ROOM):
        run = create_agent_run(
            room_id="rid", guest_name=f"X{i}", instruction="x",
        )
        rt.agent_runs[run.run_id] = run

    await srv.agent_run._inbound_agent_run_start(
        {"type": INBOUND_AGENT_RUN_START, "target": "Bob",
         "instruction": "x"}, NOOP_SINK,
    )
    assert _has_notice_containing(srv, "上限")
    assert len(rt.agent_runs) == MAX_AGENT_RUNS_PER_ROOM  # 未新增


# ── 成功路径：登记顺序 + emit started ────────────────────────────────────────


async def test_agent_run_start_registers_synchronously_before_create_task() -> None:
    """race 回归：登记 agent_runs + add(target) 同步发生于 create_task 之前 ——
    第二条同 target inbound 在 wrapper 进 speak 之前到达仍被 guest_busy 拒。
    """
    sink_log: list[ChahuaEnvelope] = []
    wrapper_calls: list[AgentRun] = []
    srv, rt = _make_server(sink_log, wrapper_spy=wrapper_calls)

    await srv.agent_run._inbound_agent_run_start(
        {"type": INBOUND_AGENT_RUN_START, "target": "Bob",
         "instruction": "ping"}, NOOP_SINK,
    )

    # 登记 entry 已存在（即便 wrapper 还没运行）。
    assert len(rt.agent_runs) == 1
    assert "Bob" in rt.active_guest_names
    # agent_run_tasks 在 create_task 之后写——已挂上。
    assert len(rt.agent_run_tasks) == 1

    # 同 target 第二条立刻到来 —— 必被 guest_busy 拒。
    await srv.agent_run._inbound_agent_run_start(
        {"type": INBOUND_AGENT_RUN_START, "target": "Bob",
         "instruction": "second"}, NOOP_SINK,
    )
    assert _has_notice_containing(srv, "正忙")
    assert len(rt.agent_runs) == 1  # 第二条没进表

    # 等 wrapper spy（spy 直接返回 None，task 完成）。
    await asyncio.gather(*rt.agent_run_tasks.values(), return_exceptions=True)
    assert len(wrapper_calls) == 1
    assert wrapper_calls[0].guest_name == "Bob"
    assert wrapper_calls[0].instruction == "ping"


async def test_agent_run_start_emits_started_envelope() -> None:
    sink_log: list[ChahuaEnvelope] = []
    srv, rt = _make_server(sink_log)
    await srv.agent_run._inbound_agent_run_start(
        {"type": INBOUND_AGENT_RUN_START, "target": "Bob",
         "instruction": "审稿 v1"}, sink_log.append,
    )

    started = [
        e for e in sink_log if e.type is ChahuaEventType.AGENT_RUN_STARTED
    ]
    assert len(started) == 1
    data = started[0].data
    assert data["guest_name"] == "Bob"
    assert data["instruction_preview"] == "审稿 v1"
    assert data["issued_by"] == "user"
    assert "task_id" not in data
    assert "source_guest" not in data
    assert "created_at_ms" in data

    await asyncio.gather(*rt.agent_run_tasks.values(), return_exceptions=True)


async def test_agent_run_start_strips_instruction_whitespace() -> None:
    sink_log: list[ChahuaEnvelope] = []
    srv, rt = _make_server(sink_log)
    await srv.agent_run._inbound_agent_run_start(
        {"type": INBOUND_AGENT_RUN_START, "target": "Bob",
         "instruction": "  hi there  "}, NOOP_SINK,
    )
    run = next(iter(rt.agent_runs.values()))
    assert run.instruction == "hi there"
    await asyncio.gather(*rt.agent_run_tasks.values(), return_exceptions=True)


# ── agent_run_cancel ────────────────────────────────────────────────────────


async def test_agent_run_cancel_cancels_named_task_only() -> None:
    """agent_run_cancel 只 cancel 指定 run，不动其它 bg run / 前台 in-flight。"""
    sink_log: list[ChahuaEnvelope] = []
    srv, rt = _make_server(sink_log)

    cancelled_flags: dict[str, bool] = {}

    async def long_lived(name: str) -> None:
        try:
            await asyncio.sleep(100)
        except asyncio.CancelledError:
            cancelled_flags[name] = True
            raise

    # 手动塞两条 bg run + 真 task —— 不走 _inbound_agent_run_start 简化。
    for name in ("a", "b"):
        run = create_agent_run(
            room_id="rid", guest_name=f"G{name}", instruction="x",
        )
        rt.agent_runs[run.run_id] = run
        rt.agent_run_tasks[run.run_id] = asyncio.create_task(long_lived(name))

    target_run_id = next(iter(rt.agent_run_tasks))
    target_name = "a" if any(rt.agent_run_tasks.keys()) else None

    await srv.agent_run._inbound_agent_run_cancel(
        {"type": INBOUND_AGENT_RUN_CANCEL, "run_id": target_run_id},
        NOOP_SINK,
    )

    # 给被 cancel 的 task 跑一次 step。
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    # 只有 target_run_id 那条被 cancel。
    target_task = rt.agent_run_tasks[target_run_id]
    other_task = next(
        t for rid, t in rt.agent_run_tasks.items() if rid != target_run_id
    )
    assert target_task.cancelled() or target_task.done()
    assert not other_task.done()  # 其它 bg run 仍在跑

    # 清理 other task。
    other_task.cancel()
    await asyncio.gather(*rt.agent_run_tasks.values(), return_exceptions=True)


async def test_agent_run_cancel_unknown_run_id_silent() -> None:
    """run_id 未知（已自然完成 / 拼错） —— WARN + 静默放过，不发 NOTICE。"""
    sink_log: list[ChahuaEnvelope] = []
    srv, rt = _make_server(sink_log)
    await srv.agent_run._inbound_agent_run_cancel(
        {"type": INBOUND_AGENT_RUN_CANCEL, "run_id": "run_nope"},
        NOOP_SINK,
    )
    notices = srv._notices_log  # type: ignore[attr-defined]
    assert notices == []
    assert sink_log == []
