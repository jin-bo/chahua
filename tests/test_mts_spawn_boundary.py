"""P11.2 C12：MTS × ``spawn_agent_run(s)`` 边界回归。

承重不变量（docs/P11 §「MTS × spawn_agent_run」）：

- ``ManagedSessionOps.intercept_task_proposal`` **只**拦 ``TASK_PROPOSAL``、
  **不**碰 ``AGENT_RUN_*`` —— transport hook 在 envelope 类型层即过滤
  （``transport_bridge.py``：``if type is TASK_PROPOSAL and hook is not None``）。
- spawn 工具创建的 bg run **不消耗** MTS budget、**不增加** ``_consecutive_ai_turns``
  —— budget 控制管理者**串行**复查深度；``spawn_*`` 是**并行**后台调度，两者正交。
- spawn 工具创建的 bg run **仍占** ``MAX_AGENT_RUNS_PER_ROOM`` —— 共享同一资源池。
- ``<managed_session>`` prompt 块第 ④ 条显式告知管理者「需要并发用
  spawn_agent_runs，不用 propose_panel」。
"""

from __future__ import annotations

import asyncio
from typing import Optional

import pytest

from chahua.agent_run import AGENT_RUN_ISSUED_BY_AGENT
from chahua.context_renderer import render_managed_session_block
from chahua.events import (
    ChahuaEnvelope,
    ChahuaEventType,
    NOOP_SINK,
    TASK_PROPOSAL_KIND_HANDOFF_DELEGATE,
)
from chahua.room_runtime import RoomEventRouter, RoomRuntime
from chahua.server import ChahuaServer, _install_handler_slots


# ── 复用 helper 测试夹具 ──────────────────────────────────────────────────────


class _FakeOrch:
    def __init__(self, guest_names: tuple[str, ...]) -> None:
        self.guest_names: tuple[str, ...] = guest_names
        self.active_guest_names: Optional[set[str]] = None
        self._managed_session: Optional[object] = None
        # _make_start_agent_run 闭包在 task_id=None 时默认填 active task_id。
        # MTS 边界测试默认 None（与"无活跃任务"一致）。
        self._active_task_id: Optional[str] = None

    @property
    def managed_session(self) -> Optional[object]:
        # P11.2.X F9：_start_agent_run 走 ``managed_session`` 公开 @property；测试夹具
        # 仍按既有约定写 ``_managed_session``，此处 forward 保兼容。
        return self._managed_session

    def snapshot_active_task_id(self) -> Optional[str]:
        return self._active_task_id


class _FakeRoom:
    name = "mts-room"


class _FakeTasksStore:
    def __init__(self) -> None:
        self._tasks: dict[str, object] = {}

    def get_task(self, task_id: str):
        return self._tasks.get(task_id)


class _FakeSession:
    def __init__(self, guest_names: tuple[str, ...]) -> None:
        self.orchestrator = _FakeOrch(guest_names)
        self.room = _FakeRoom()
        self.tasks_store = _FakeTasksStore()
        self.guests = []


def _make_server(
    sink_log: list[ChahuaEnvelope],
    *,
    guest_names: tuple[str, ...] = ("Carol", "Bob", "Alice"),
) -> tuple[ChahuaServer, RoomRuntime]:
    srv = object.__new__(ChahuaServer)
    rt = RoomRuntime(
        room_id="rid",
        session=_FakeSession(guest_names),  # type: ignore[arg-type]
        router=RoomEventRouter(sink_log.append),
    )
    srv._runtimes = {"rid": rt}
    srv._foreground_id = "rid"
    # P11 slot 拆分：``_make_start_agent_run`` / ``_start_agent_run`` 转发到 slot。
    _install_handler_slots(srv)

    async def fake_wrapper(runtime: RoomRuntime, run) -> None:
        return None

    srv._run_agent_background = fake_wrapper  # type: ignore[assignment]
    srv._attach_runtime_state(rt)
    return srv, rt


async def _drain(rt: RoomRuntime) -> None:
    for t in list(rt.agent_run_tasks.values()):
        t.cancel()
    for t in list(rt.agent_run_tasks.values()):
        try:
            await t
        except asyncio.CancelledError:
            pass


# ── MTS hook 不碰 AGENT_RUN_* envelope ────────────────────────────────────────


def test_mts_intercept_task_proposal_ignores_agent_run_envelopes() -> None:
    """``intercept_task_proposal`` 只看 ``data.proposer / kind`` —— 喂它
    一条 AGENT_RUN_STARTED envelope 也应返 False（不拦）。

    更深一层的不变量在 ``ChahuaTransport.emit_chahua``：hook 经
    ``if type is TASK_PROPOSAL and hook is not None`` 入口门，AGENT_RUN_* 根本
    走不到 hook。这条单测把 MTS hook 直接喂 AGENT_RUN_STARTED 兜底 envelope 层
    防御：即便未来某 emit 路径出 bug 让 AGENT_RUN_STARTED 流到 hook，仍应放行。
    """
    from chahua._orchestrator_managed_session import ManagedSessionOps
    from chahua.handoff import ManagedSession

    # 最小桩：让 ops.orch._guests / _handoff_ops 可读，hook 不会真触达它们因为
    # 提议不是 TASK_PROPOSAL 形状。
    class _OrchStub:
        _guests = {"Carol": object(), "Bob": object()}
        _handoff_ops = type("_H", (), {"_queue": [],
                                       "enqueue_handoff": lambda *a, **k: None,
                                       "emit_handoff_queue_snapshot": lambda *a: None})()

        # P11 C12：intercept_task_proposal busy 校验读 ``active_guest_names``；
        # 真 Orchestrator 装 None 兜底（裸构默认），stub 同口径补一行。
        active_guest_names = None

        class config:
            max_consecutive_ai_turns = 100

        @property
        def room(self):
            return _FakeRoom()

    ops = ManagedSessionOps(_OrchStub())
    ops._managed_session = ManagedSession(
        task_id="t1", manager_guest="Carol", budget=3,
    )

    env = ChahuaEnvelope(
        room_id="mts-room",
        turn_id=None,
        guest_name="Bob",
        message_id=None,
        type=ChahuaEventType.AGENT_RUN_STARTED,
        data={
            "run_id": "run_test",
            "guest_name": "Bob",
            "source_guest": "Carol",
            "issued_by": AGENT_RUN_ISSUED_BY_AGENT,
        },
    )
    # AGENT_RUN_STARTED envelope 的 data 没有 ``proposer`` / ``kind`` 字段。
    # hook 内 ``data.get("proposer") != ms.manager_guest`` 立刻返 False。
    assert ops.intercept_task_proposal(env, NOOP_SINK) is False


def test_mts_intercept_task_proposal_still_works_for_real_proposals() -> None:
    """肯定面：管理者发 ``TASK_PROPOSAL{kind=handoff_delegate, proposer=Carol}``
    时 hook 返 True（拦截、入队）—— 证明 hook 自己没被改坏，只是边界精确。"""
    from chahua._orchestrator_managed_session import ManagedSessionOps
    from chahua.handoff import ManagedSession

    enqueued: list = []

    class _HandoffOps:
        # ``_queue`` 在 ``__init__`` 里实例化 —— 不能用类级 ``_queue: list = []``
        # 默认（mutable class var）：如果未来某测在同进程内重跑会跨实例累积。
        def __init__(self) -> None:
            self._queue: list = []

        def enqueue_handoff(self, item):
            enqueued.append(item)
            self._queue.append(item)

        def emit_handoff_queue_snapshot(self, sink):
            pass

    class _OrchStub:
        _guests = {"Carol": object(), "Bob": object()}
        _handoff_ops = _HandoffOps()

        # P11 C12：intercept_task_proposal busy 校验读 ``active_guest_names``；
        # 真 Orchestrator 装 None 兜底（裸构默认），stub 同口径补一行。
        active_guest_names = None

        class config:
            max_consecutive_ai_turns = 100

        @property
        def room(self):
            return _FakeRoom()

    ops = ManagedSessionOps(_OrchStub())
    ops._managed_session = ManagedSession(
        task_id="t1", manager_guest="Carol", budget=3,
    )

    env = ChahuaEnvelope(
        room_id="mts-room", turn_id=None, guest_name="Carol", message_id=None,
        type=ChahuaEventType.TASK_PROPOSAL,
        data={
            "proposer": "Carol",
            "kind": TASK_PROPOSAL_KIND_HANDOFF_DELEGATE,
            "payload": {"target": "Bob"},
        },
    )
    assert ops.intercept_task_proposal(env, NOOP_SINK) is True
    assert len(enqueued) == 1


# ── spawn 不耗 budget / 不增 _consecutive_ai_turns（共享 cap 仍受限）──────────


async def test_spawn_during_mts_does_not_decrement_budget() -> None:
    """**核心 C12 不变量**：MTS 管理者在自己回合内调 spawn → bg run 创建成功，但
    ``budget`` 不变（budget 是「worker 回合走完 → 管理者复查」扣 1 的 drain-loop
    层概念，spawn 走独立 wrapper、不进 drain queue）。

    **测试覆盖范围**：本测试只验证 ``_start_agent_run`` 路径自身不写
    ``orchestrator._managed_session.budget``。完整的 drain-loop 级覆盖（即
    spawn 完成后下一个真 turn 调 ``advance_after_turn`` 时 budget 不被错误扣减）
    属于 ``test_managed_session.py`` 的范畴 —— C12 单测只验 spawn 路径的局部
    不变量。
    """
    from chahua.handoff import ManagedSession

    sink: list[ChahuaEnvelope] = []
    srv, rt = _make_server(sink, guest_names=("Carol", "Bob"))

    # 装一份 MTS state（直接挂到 fake orch；真 flow 走 start_managed_session）。
    rt.session.orchestrator._managed_session = ManagedSession(  # type: ignore[attr-defined]
        task_id="t1", manager_guest="Carol", budget=5,
    )
    initial_budget = rt.session.orchestrator._managed_session.budget  # type: ignore[attr-defined]

    # Carol（管理者）调 spawn → Bob 起 bg run。
    cb = srv._make_start_agent_run(rt)
    run_id, err = cb(
        target="Bob", instruction="并发审一下方案",
        task_id=None, source_guest="Carol",
    )
    assert err is None
    assert run_id is not None
    assert run_id in rt.agent_runs

    # budget 未减。
    assert rt.session.orchestrator._managed_session.budget == initial_budget  # type: ignore[attr-defined]

    await _drain(rt)


async def test_spawn_during_mts_does_not_increment_consecutive_ai_turns() -> None:
    """``_consecutive_ai_turns`` 是 drain loop 维护的「连续 AI 回合数」cap 计数。
    spawn 走 ``_run_agent_background`` wrapper、显式不动 ``_consecutive_ai_turns``
    （docs/P11 §「后台执行路径」第 ③ 条）—— budget cap 与 consecutive cap 都
    保护「串行深度」不被并发后台稀释。

    测试间接验证：spawn 完成后 orchestrator 没有 _consecutive_ai_turns 字段被
    改写（fake orch 无此字段；真实 orchestrator 的 ``_run_ai_chain`` /
    drain loop 才维护它）。这里只断言 spawn 不去写。
    """
    sink: list[ChahuaEnvelope] = []
    srv, rt = _make_server(sink, guest_names=("Carol", "Bob"))

    # _consecutive_ai_turns 不在 fake orch 上 —— 用 sentinel 探测：spawn 不会
    # 凭空创建 / 修改它。
    assert not hasattr(rt.session.orchestrator, "_consecutive_ai_turns")

    cb = srv._make_start_agent_run(rt)
    run_id, err = cb(
        target="Bob", instruction="审", task_id=None, source_guest="Carol",
    )
    assert err is None
    assert run_id is not None

    # spawn 路径不应往 orchestrator 装 _consecutive_ai_turns 属性。
    assert not hasattr(rt.session.orchestrator, "_consecutive_ai_turns")

    await _drain(rt)


async def test_spawn_still_counts_against_max_agent_runs_per_room() -> None:
    """正交性的另一面：spawn 仍占 ``MAX_AGENT_RUNS_PER_ROOM`` —— budget bypass
    不是「无限并发」的免死金牌。MTS 与非 MTS 在这条 cap 上一致。"""
    from chahua.server_inbound_agent_run import MAX_AGENT_RUNS_PER_ROOM
    from chahua.handoff import ManagedSession

    sink: list[ChahuaEnvelope] = []
    # G0 = 管理者；workers = G1..G_MAX 占满 cap，G_(MAX+1) 试探拒。
    # 不让 G0 当 spawn target（管理者把自己当 worker 是奇怪场景；C8 inbound 也
    # 不显式禁，但避免误测「manager-as-target 拒绝」当成 cap 拒绝）。
    workers = tuple(f"G{i}" for i in range(1, MAX_AGENT_RUNS_PER_ROOM + 2))
    guest_names = ("G0",) + workers
    srv, rt = _make_server(sink, guest_names=guest_names)
    rt.session.orchestrator._managed_session = ManagedSession(  # type: ignore[attr-defined]
        task_id="t1", manager_guest="G0", budget=99,
    )

    cb = srv._make_start_agent_run(rt)
    # 占满 max_agent_runs_per_room（前 N 个 worker）。
    for i in range(MAX_AGENT_RUNS_PER_ROOM):
        run_id, err = cb(
            target=workers[i], instruction="x",
            task_id=None, source_guest="G0",
        )
        assert err is None, f"i={i} target={workers[i]}: 应放行"

    # 第 N+1 条（workers[-1]）：在 MTS 内仍拒（cap 不被 MTS bypass）。
    run_id, err = cb(
        target=workers[-1], instruction="x",
        task_id=None, source_guest="G0",
    )
    assert run_id is None
    assert err is not None and "已达上限" in err

    await _drain(rt)


# ── prompt 块文案覆盖 ────────────────────────────────────────────────────────


def test_managed_session_block_mentions_spawn_agent_runs() -> None:
    """C12 改动：``<managed_session>`` 块第 ④ 条告知管理者 spawn_agent_runs
    用法 + 「别用 propose_panel 并发」纠偏。"""
    block = render_managed_session_block("Carol", 5)
    assert "spawn_agent_runs" in block
    assert "并发" in block
    # 反向：不要把 propose_panel 当并发工具用。
    assert "propose_panel" in block
    assert "panel 仍是串行" in block
    # XML 边界 + manager 转义保留。
    assert "<managed_session" in block and "</managed_session>" in block
    assert 'manager="Carol"' in block
    # remaining_budget 仍显式打出（与既有断言口径一致，让前端 / LLM 都能直读）。
    assert 'remaining_budget="5"' in block
