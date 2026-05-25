"""P11.2.X：MTS × ``spawn_agent_run`` 续命闭环回归。

承重不变量（CLAUDE.md §「后台 Agent × MTS 续命」）：

- ``_start_agent_run`` 创建 bg run 时按 ``source_guest == ms.manager_guest`` 快照
  ``mts_managed`` —— 用户手起 / 非管理者 spawn / 无 MTS 三种都 False。
- ``ManagedSessionOps.advance_after_bg_completion`` 是 bg wrapper finally 的续命
  入口：MTS 活则扣 1 budget + enqueue 管理者复查 DELEGATE + emit advanced 并返 True；
  MTS 已被独立结束（cancel / task_closed / budget_exhausted）则跳过返 False。
- ``RoomRuntime.has_pending_mts_bg()`` 真值即「有 ``mts_managed=True`` 的存活 run」，
  drain 收尾兜底（``run_pending_handoff``）凭此判断「MTS 还要不要等」。
- drain 收尾兜底带 ``_has_pending_mts_bg()`` 守卫：守卫 True 时**不**触发
  MANAGER_FINISHED 收尾、安静退出 drain；守卫 False 时回到 P8.3 既有 MANAGER_FINISHED。
"""

from __future__ import annotations

from typing import Optional

import pytest

from chahua.agent_run import (
    AGENT_RUN_ISSUED_BY_AGENT,
    AGENT_RUN_ISSUED_BY_USER,
    AgentRun,
    create as create_agent_run,
)
from chahua.cursor import GuestCursor
from chahua.events import NOOP_SINK, ChahuaEnvelope, ChahuaEventType
from chahua.handoff import (
    MANAGED_SESSION_REASON_MANAGER_FINISHED,
    HandoffItem,
    HandoffKind,
    ManagedSession,
)
from chahua.orchestrator import Orchestrator, OrchestratorConfig
from chahua.room import Room
from chahua.room_runtime import RoomRuntime, RoomEventRouter
from chahua.server import ChahuaServer, _install_handler_slots
from chahua.task import Task
from chahua.user_md import USER_SPEAKER_ID, UserConfig

from conftest import NoopScorer, NoopSummarizer, SpeakingStubGuest


# ── 共享桩 ─────────────────────────────────────────────────────────────────


class _FakeStore:
    """复用 :class:`chahua.task.Task` 真模型 —— ``_FakeTask`` 占位字段太少
    （缺 title / goal 让 context_renderer onboarding 路径炸）。"""

    def __init__(self) -> None:
        self.tasks: dict[str, Task] = {}

    def get_task(self, task_id):
        return self.tasks.get(task_id)

    def list_tasks(self):
        return []

    def list_decisions(self, task_id):
        # onboarding 路径必读 —— 空列表代表「该 task 还没决策」。
        return []

    def list_artifacts(self, task_id):
        # onboarding 路径必读 —— 空列表代表「该 task 还没产物」。
        return []


def _make_store_with_task(task_id: str = "t1") -> _FakeStore:
    """``stop_reason`` 在 ``tasks_store is not None`` 时必经 ``get_task``，缺则
    返 ``TASK_CLOSED`` 提前收 MTS —— 凡 MTS 测试都给 store 装一条 doing 任务。"""
    store = _FakeStore()
    store.tasks[task_id] = Task(
        id=task_id, title="t", goal="g", status="doing", owner=None,
        created_at_ms=0, updated_at_ms=0, closed_at_ms=None,
    )
    return store


def _build_orch(*names, max_ai: int = 12, store=None):
    """裸 Orchestrator + Room —— 与 test_managed_session._build 同构（避免循环 import 直复制）。"""
    room = Room(name="t")
    room.add_participant(USER_SPEAKER_ID)
    orch = Orchestrator(
        room=room,
        user_config=UserConfig(display_name="老金", full_md=None, source=None),
        scorer=NoopScorer(),
        summarizer=NoopSummarizer(),
        cursor=GuestCursor(),
        config=OrchestratorConfig(
            max_consecutive_ai_turns=max_ai,
            max_speakers_per_pick=1,
            speaker_cooldown_turns=0,
            summary_block_size=999,
        ),
        tasks_store=store,
    )
    for n in names:
        orch.register(SpeakingStubGuest(n, room), persona_md=f"{n} persona")
    return orch, room


# ── 1. RoomRuntime.has_pending_mts_bg() ─────────────────────────────────────


def test_has_pending_mts_bg_false_when_empty() -> None:
    """无 bg run → False。"""
    rt = RoomRuntime(
        room_id="r", session=None,  # type: ignore[arg-type]
        router=RoomEventRouter(lambda env: None),
    )
    assert rt.has_pending_mts_bg() is False


def test_has_pending_mts_bg_false_when_only_non_mts_runs() -> None:
    """有 bg run 但 mts_managed=False（用户手起 / 非管理者 spawn）→ False。"""
    rt = RoomRuntime(
        room_id="r", session=None,  # type: ignore[arg-type]
        router=RoomEventRouter(lambda env: None),
    )
    rt.agent_runs["run_1"] = create_agent_run(
        room_id="r", guest_name="Bob", instruction="x",
        issued_by=AGENT_RUN_ISSUED_BY_USER,
        # mts_managed 默认 False
    )
    rt.agent_runs["run_2"] = create_agent_run(
        room_id="r", guest_name="Carol", instruction="y",
        issued_by=AGENT_RUN_ISSUED_BY_AGENT, source_guest="Alice",
        # 非管理者 spawn 时也 False（由 _start_agent_run 决定，这里桩接显式 False）
    )
    assert rt.has_pending_mts_bg() is False


def test_has_pending_mts_bg_true_when_at_least_one_mts_run() -> None:
    """混合：有任一 mts_managed=True 的存活 run → True。"""
    rt = RoomRuntime(
        room_id="r", session=None,  # type: ignore[arg-type]
        router=RoomEventRouter(lambda env: None),
    )
    rt.agent_runs["run_user"] = create_agent_run(
        room_id="r", guest_name="Bob", instruction="x",
        issued_by=AGENT_RUN_ISSUED_BY_USER,
    )
    rt.agent_runs["run_mts"] = create_agent_run(
        room_id="r", guest_name="Carol", instruction="y",
        issued_by=AGENT_RUN_ISSUED_BY_AGENT, source_guest="Alice",
        mts_managed=True,
    )
    assert rt.has_pending_mts_bg() is True


# ── 2. ManagedSessionOps.advance_after_bg_completion ─────────────────────────


def test_advance_after_bg_completion_decrements_and_enqueues() -> None:
    """MTS 活 → 扣 1 budget + enqueue DELEGATE(manager) + emit advanced + 返 True。"""
    orch, _ = _build_orch("Alice", "Bob", "Carol", store=_make_store_with_task())
    sink: list[ChahuaEnvelope] = []
    orch.start_managed_session(
        sink.append, task_id="t1", manager_guest="Alice", budget=3,
    )
    sink.clear()  # 不关心 MANAGED_SESSION_STARTED 帧

    continued = orch._mts_ops.advance_after_bg_completion(
        sink.append, bg_guest_name="Bob",
    )

    assert continued is True
    assert orch._managed_session.budget == 2  # 3 → 2
    assert len(orch._handoff_queue) == 1
    item = orch._handoff_queue[0]
    assert item.kind is HandoffKind.DELEGATE
    assert item.target == "Alice"  # manager
    assert "bg Bob 已完成" in item.reason
    # emit 序列：HANDOFF_ENQUEUED 快照 + MANAGED_SESSION_ADVANCED。
    types = [env.type for env in sink]
    assert ChahuaEventType.HANDOFF_ENQUEUED in types
    assert ChahuaEventType.MANAGED_SESSION_ADVANCED in types


def test_advance_after_bg_completion_skips_when_mts_dead() -> None:
    """MTS 已被独立结束（user_cancel / task_closed / budget_exhausted）→ 返
    False，不动 budget 不动 queue 不 emit advanced。

    spawn 时刻的 mts_managed=True 快照只反映「spawn 时 MTS 活」，bg 跑期间 MTS
    可能被独立路径结束 —— wrapper finally 续命必须有 None 守卫。
    """
    orch, _ = _build_orch("Alice", "Bob", store=_make_store_with_task())
    sink: list[ChahuaEnvelope] = []
    # 直接装 MTS 又立刻擦 —— 模拟 bg 跑期间 MTS 被独立结束的局面。
    orch._managed_session = None

    continued = orch._mts_ops.advance_after_bg_completion(
        sink.append, bg_guest_name="Bob",
    )

    assert continued is False
    assert len(orch._handoff_queue) == 0
    assert sink == []  # 不 emit 任何 envelope


def test_advance_after_bg_completion_can_drive_budget_to_zero() -> None:
    """budget=1 时续命一次 → budget=0；下一轮 drain 的 advance_after_turn 会按
    BUDGET_EXHAUSTED 收尾（这条不直接测，留给 drain 集成）。本测试只验 budget 真的
    被扣到 0、续命动作仍执行（管理者复查 turn 仍能跑一次）。
    """
    orch, _ = _build_orch("Alice", "Bob", store=_make_store_with_task())
    sink: list[ChahuaEnvelope] = []
    orch.start_managed_session(
        sink.append, task_id="t1", manager_guest="Alice", budget=1,
    )

    continued = orch._mts_ops.advance_after_bg_completion(
        sink.append, bg_guest_name="Bob",
    )

    assert continued is True
    assert orch._managed_session.budget == 0
    assert len(orch._handoff_queue) == 1  # 管理者复查仍 enqueue


# ── 3. drain 收尾守卫 ────────────────────────────────────────────────────────


async def test_drain_skips_manager_finished_when_bg_pending() -> None:
    """``_has_pending_mts_bg() == True`` 时 drain 收尾不触发 MANAGER_FINISHED。

    场景：管理者刚跑完、队列空、MTS 活、但仍有 manager-attributed bg 在跑 —— drain
    安静退出，让 bg 完成回调（``advance_after_bg_completion``）来 enqueue 复查。
    """
    orch, _ = _build_orch("Alice", "Bob", store=_make_store_with_task())
    sink: list[ChahuaEnvelope] = []
    orch.start_managed_session(
        sink.append, task_id="t1", manager_guest="Alice", budget=3,
    )
    # kickoff manager turn 入队 + drain。
    orch.enqueue_handoff(
        HandoffItem(kind=HandoffKind.DELEGATE, target="Alice", reason="kickoff")
    )
    # 注入守卫：假装管理者已 spawn 1 个 bg 还在跑。
    orch._has_pending_mts_bg = lambda: True
    sink.clear()

    await orch.run_pending_handoff(sink.append, task_id="t1")

    # MTS 不应被收尾 —— ENDED 帧不该出现。
    types = [env.type for env in sink]
    assert ChahuaEventType.MANAGED_SESSION_ENDED not in types
    assert orch._managed_session is not None


async def test_drain_ends_mts_when_no_bg_pending_regression() -> None:
    """守卫 False（既有行为）—— drain 仍按 MANAGER_FINISHED 收尾，回归保护。

    这是 P11.2.X 改动前的既有行为：管理者没派活 + 队列空 + MTS 活 → MANAGER_FINISHED。
    """
    orch, _ = _build_orch("Alice", "Bob", store=_make_store_with_task())
    sink: list[ChahuaEnvelope] = []
    orch.start_managed_session(
        sink.append, task_id="t1", manager_guest="Alice", budget=3,
    )
    orch.enqueue_handoff(
        HandoffItem(kind=HandoffKind.DELEGATE, target="Alice", reason="kickoff")
    )
    # 守卫默认 False（无 runtime 注入），P11.2.X 之前的行为应保留。
    assert orch._has_pending_mts_bg() is False
    sink.clear()

    await orch.run_pending_handoff(sink.append, task_id="t1")

    # MTS 应按 MANAGER_FINISHED 收尾。
    ended = [
        env for env in sink
        if env.type is ChahuaEventType.MANAGED_SESSION_ENDED
    ]
    assert len(ended) == 1
    assert ended[0].data.get("reason") == MANAGED_SESSION_REASON_MANAGER_FINISHED
    assert orch._managed_session is None


# ── 4. _start_agent_run 的 mts_managed 快照 ─────────────────────────────────


class _MinimalSession:
    """``_start_agent_run`` 读 orchestrator / tasks_store / room / guests 四件。"""

    def __init__(self, orch) -> None:
        self.orchestrator = orch
        self.tasks_store = orch.tasks_store
        self.room = orch.room  # _emit_agent_run_started 用
        self.guests = []  # _attach_runtime_state 用，本测试不调


def _make_minimal_runtime(orch) -> tuple[ChahuaServer, RoomRuntime]:
    rt = RoomRuntime(
        room_id="r",
        session=_MinimalSession(orch),  # type: ignore[arg-type]
        router=RoomEventRouter(lambda env: None),
    )
    srv = object.__new__(ChahuaServer)
    srv._runtimes = {"r": rt}
    srv._foreground_id = "r"
    # P11 slot 拆分：``_start_agent_run`` 转发到 ``_agent_run_ops``，必装。
    _install_handler_slots(srv)
    # 让 wrapper 立即返回，不要真起 bg 执行 —— 本测试只验登记瞬间的 mts_managed 标志。

    async def fake_wrapper(runtime: RoomRuntime, run) -> None:
        return None

    srv._run_agent_background = fake_wrapper  # type: ignore[assignment]
    return srv, rt


@pytest.mark.parametrize(
    "ms_active,source_guest,expected_mts_managed",
    [
        (False, None, False),                 # 无 MTS, user spawn
        (False, "Alice", False),              # 无 MTS, agent spawn
        (True, None, False),                  # 有 MTS, user spawn（issued_by="user"）
        (True, "Carol", False),               # 有 MTS, non-manager spawn
        (True, "Alice", True),                # 有 MTS, manager spawn ← ✅ 唯一 True
    ],
)
async def test_start_agent_run_snapshot_mts_managed(
    ms_active: bool,
    source_guest: Optional[str],
    expected_mts_managed: bool,
) -> None:
    """``_start_agent_run`` 按 ``(MTS 活, source_guest == manager)`` 真值表快照
    ``mts_managed``。Alice 是管理者；只有「Alice 自己 spawn」才标记。
    """
    orch, _ = _build_orch("Alice", "Bob", "Carol", store=_make_store_with_task())
    if ms_active:
        orch._managed_session = ManagedSession(
            task_id="t1", manager_guest="Alice", budget=3,
        )
    srv, rt = _make_minimal_runtime(orch)

    issued_by = (
        AGENT_RUN_ISSUED_BY_AGENT if source_guest is not None
        else AGENT_RUN_ISSUED_BY_USER
    )
    run, err = srv._start_agent_run(
        rt,
        target="Bob", instruction="x",
        task_id=None,
        issued_by=issued_by,
        source_guest=source_guest,
    )

    assert err is None
    assert run is not None
    assert run.mts_managed is expected_mts_managed
    # F3：mts_managed=True 时同步冻结 manager 身份；False 时为 None。
    if expected_mts_managed:
        assert run.mts_manager_at_spawn == "Alice"
    else:
        assert run.mts_manager_at_spawn is None


# ── 5. code-review F2：stop_reason / budget 守卫 ────────────────────────────


async def test_advance_after_bg_completion_task_closed_ends_mts() -> None:
    """F2：bg 跑期间任务被关闭 → stop_reason 返 TASK_CLOSED → advance_after_bg
    走 end_managed_session(TASK_CLOSED) 返 False，不再 enqueue 多余复查 turn。"""
    from chahua.handoff import MANAGED_SESSION_REASON_TASK_CLOSED

    store = _make_store_with_task()
    orch, _ = _build_orch("Alice", "Bob", store=store)
    sink: list[ChahuaEnvelope] = []
    orch.start_managed_session(
        sink.append, task_id="t1", manager_guest="Alice", budget=3,
    )
    # 模拟任务被关闭。
    store.tasks["t1"] = Task(
        id="t1", title="t", goal="g", status="done", owner=None,
        created_at_ms=0, updated_at_ms=0, closed_at_ms=1,
    )
    sink.clear()

    continued = orch._mts_ops.advance_after_bg_completion(
        sink.append, bg_guest_name="Bob",
    )

    assert continued is False
    assert orch._managed_session is None  # end_managed_session 已跑
    assert len(orch._handoff_queue) == 0  # 队列被 end 清空
    ended = [e for e in sink if e.type is ChahuaEventType.MANAGED_SESSION_ENDED]
    assert len(ended) == 1
    assert ended[0].data["reason"] == MANAGED_SESSION_REASON_TASK_CLOSED


async def test_advance_after_bg_completion_task_closed_ends_mts_even_with_queue() -> None:
    """Codex round 4 P2-A 修：``TASK_CLOSED`` 触发的 stop 不分流 queue —— 任务关闭后
    残留 review 跑也无意义（任务都关了），直接 end + 清队列。守卫仅对
    ``BUDGET_EXHAUSTED`` 多 bg race 场景分流。"""
    from chahua.handoff import MANAGED_SESSION_REASON_TASK_CLOSED

    store = _make_store_with_task()
    orch, _ = _build_orch("Alice", "Bob", store=store)
    sink: list[ChahuaEnvelope] = []
    orch.start_managed_session(
        sink.append, task_id="t1", manager_guest="Alice", budget=3,
    )
    # 队列预先有 review 项（模拟之前 bg 完成 enqueue 的）。
    orch.enqueue_handoff(
        HandoffItem(
            kind=HandoffKind.DELEGATE, target="Alice",
            reason="bg Bob 已完成 · 托管复查",
        )
    )
    # 任务被关闭。
    store.tasks["t1"] = Task(
        id="t1", title="t", goal="g", status="done", owner=None,
        created_at_ms=0, updated_at_ms=0, closed_at_ms=1,
    )
    sink.clear()

    continued = orch._mts_ops.advance_after_bg_completion(
        sink.append, bg_guest_name="Carol",
    )

    assert continued is False
    # TASK_CLOSED → 即使队列非空也 end + 清队列。
    assert orch._managed_session is None
    assert len(orch._handoff_queue) == 0
    ended = [e for e in sink if e.type is ChahuaEventType.MANAGED_SESSION_ENDED]
    assert len(ended) == 1
    assert ended[0].data["reason"] == MANAGED_SESSION_REASON_TASK_CLOSED


async def test_advance_after_bg_completion_budget_zero_does_not_clear_queue() -> None:
    """Codex P2 (round 3) 修：advance_after_bg 在 budget<=0 时仅当队列也空才 end，
    队列非空（前面 bg 的 review 已 enqueue 待跑）→ 跳过 end_managed_session 防误清。

    具体场景：两个 deferred bg callback 顺序跑。cb_bg1: budget=1→0 enqueue review_bob。
    cb_bg2: 看到 budget=0，但队列有 review_bob → 不 end，让 drain 跑完 review_bob 再
    通过 advance_after_turn 自然 BUDGET_EXHAUSTED 收尾。"""
    orch, _ = _build_orch("Alice", "Bob", "Carol", store=_make_store_with_task())
    sink: list[ChahuaEnvelope] = []
    orch.start_managed_session(
        sink.append, task_id="t1", manager_guest="Alice", budget=1,
    )
    # 模拟 cb_bg1 已跑：budget=0，队列已有 review_bob。
    orch._managed_session.budget = 0
    orch.enqueue_handoff(
        HandoffItem(
            kind=HandoffKind.DELEGATE, target="Alice",
            reason="bg Bob 已完成 · 托管复查",
        )
    )
    sink.clear()

    # cb_bg2 跑：budget=0 + queue 非空 → 守卫触发，不 end。
    continued = orch._mts_ops.advance_after_bg_completion(
        sink.append, bg_guest_name="Carol",
    )

    assert continued is False
    # MTS 仍活，review_bob 仍在队列等 drain 消费。
    assert orch._managed_session is not None
    assert len(orch._handoff_queue) == 1
    assert "bg Bob" in orch._handoff_queue[0].reason
    # 不应 emit ENDED（仅当 cb_bg2 错误地 end 才会）。
    assert ChahuaEventType.MANAGED_SESSION_ENDED not in [e.type for e in sink]


async def test_advance_after_bg_completion_budget_zero_ends_mts() -> None:
    """F2：bg 完成时 budget 已 ≤0（被并发桥 / 其他路径提前扣到 0）→ advance_after_bg
    直接 end_managed_session(BUDGET_EXHAUSTED)，不再扣到 -1 emit 反人类负预算。"""
    from chahua.handoff import MANAGED_SESSION_REASON_BUDGET_EXHAUSTED

    orch, _ = _build_orch("Alice", "Bob", store=_make_store_with_task())
    sink: list[ChahuaEnvelope] = []
    orch.start_managed_session(
        sink.append, task_id="t1", manager_guest="Alice", budget=1,
    )
    # 模拟 budget 已被另一桥扣完。
    orch._managed_session.budget = 0
    sink.clear()

    continued = orch._mts_ops.advance_after_bg_completion(
        sink.append, bg_guest_name="Bob",
    )

    assert continued is False
    assert orch._managed_session is None
    ended = [e for e in sink if e.type is ChahuaEventType.MANAGED_SESSION_ENDED]
    assert len(ended) == 1
    assert ended[0].data["reason"] == MANAGED_SESSION_REASON_BUDGET_EXHAUSTED
    # **不**应 emit advanced（避免「先 advanced(budget=-1) 后 ENDED」反人类序列）。
    assert ChahuaEventType.MANAGED_SESSION_ADVANCED not in [e.type for e in sink]


# ── 6. code-review F3：manager 身份不匹配 ─────────────────────────────────


async def test_advance_after_bg_completion_skips_when_manager_changed() -> None:
    """F3：bg spawn 时 manager=Alice，bg 跑期间 MTS 换主 manager=Bob，bg 完成 →
    advance 守卫返 False 不入队，不把 Alice 的复查塞给 Bob。"""
    orch, _ = _build_orch("Alice", "Bob", store=_make_store_with_task())
    sink: list[ChahuaEnvelope] = []
    orch.start_managed_session(
        sink.append, task_id="t1", manager_guest="Bob", budget=3,
    )
    sink.clear()

    # mts_manager_at_spawn=Alice，当前 ms.manager_guest=Bob → 不匹配。
    continued = orch._mts_ops.advance_after_bg_completion(
        sink.append, bg_guest_name="Carol",
        mts_manager_at_spawn="Alice",
    )

    assert continued is False
    assert orch._managed_session is not None  # MTS 仍活
    assert orch._managed_session.budget == 3  # budget 未扣
    assert len(orch._handoff_queue) == 0  # 不入队
    assert sink == []  # 不 emit 任何 envelope


async def test_advance_after_bg_completion_rejects_old_mts_session() -> None:
    """Codex round 5 P2 修：用户 stop MTS A、重启同管理者的新 MTS B 时，A 的旧 bg
    完成不能续命到 B —— 即使 manager_guest 一致，``ManagedSession`` 是新实例
    (id 不同)。本字段 ``mts_session_id_at_spawn`` 严格区分两个实例。"""
    orch, _ = _build_orch("Alice", "Bob", store=_make_store_with_task())
    sink: list[ChahuaEnvelope] = []
    # 启动 MTS A，模拟 spawn 时刻 session id。
    orch.start_managed_session(
        sink.append, task_id="t1", manager_guest="Alice", budget=3,
    )
    old_session_id = orch._managed_session.session_id
    # 用户 stop MTS A、重启同 manager 的 MTS B。
    orch._mts_ops.end_managed_session(sink.append, reason="user_cancel")
    orch.start_managed_session(
        sink.append, task_id="t1", manager_guest="Alice", budget=3,
    )
    new_session_id = orch._managed_session.session_id
    assert old_session_id != new_session_id  # counter 单调递增保证不同
    sink.clear()

    # MTS A 的旧 bg 完成 → 守卫拦下，不动 B 的 budget / queue。
    continued = orch._mts_ops.advance_after_bg_completion(
        sink.append, bg_guest_name="Bob",
        mts_manager_at_spawn="Alice",
        mts_session_id_at_spawn=old_session_id,  # 旧 MTS 的 id
    )

    assert continued is False
    assert orch._managed_session is not None  # B 仍活
    assert orch._managed_session.budget == 3  # B 的 budget 未动
    assert len(orch._handoff_queue) == 0
    assert sink == []  # 不 emit


async def test_advance_after_bg_completion_proceeds_when_manager_matches() -> None:
    """F3 反向：spawn 时刻 manager 与当前一致 → 正常续命。"""
    orch, _ = _build_orch("Alice", "Bob", store=_make_store_with_task())
    sink: list[ChahuaEnvelope] = []
    orch.start_managed_session(
        sink.append, task_id="t1", manager_guest="Alice", budget=3,
    )
    sink.clear()

    continued = orch._mts_ops.advance_after_bg_completion(
        sink.append, bg_guest_name="Bob",
        mts_manager_at_spawn="Alice",  # 与当前 manager 一致
    )

    assert continued is True
    assert orch._managed_session.budget == 2


# ── 7. code-review F12：_start_agent_run 拒 target == manager ──────────────


async def test_start_agent_run_rejects_target_eq_mts_manager() -> None:
    """F12：MTS 活 + target == manager_guest → 拒绝并返 Error，防 bg 内 propose 经
    intercept hook 自动入 MTS 队列污染托管。"""
    orch, _ = _build_orch("Alice", "Bob", store=_make_store_with_task())
    orch._managed_session = ManagedSession(
        task_id="t1", manager_guest="Alice", budget=3,
    )
    srv, rt = _make_minimal_runtime(orch)

    # 用户手起 bg 指向管理者 Alice。
    run, err = srv._start_agent_run(
        rt,
        target="Alice", instruction="audit X",
        task_id=None,
        issued_by=AGENT_RUN_ISSUED_BY_USER,
        source_guest=None,
    )

    assert run is None
    assert err is not None
    assert "MTS 管理者" in err  # 文案命中
    assert "Alice" in err


async def test_start_agent_run_allows_target_eq_manager_when_no_mts() -> None:
    """F12 反向：无 MTS 时，target 是任何茶客都没问题（管理者概念不存在）。"""
    orch, _ = _build_orch("Alice", "Bob", store=_make_store_with_task())
    # 无 MTS。
    srv, rt = _make_minimal_runtime(orch)

    run, err = srv._start_agent_run(
        rt,
        target="Alice", instruction="x",
        task_id=None,
        issued_by=AGENT_RUN_ISSUED_BY_USER,
        source_guest=None,
    )

    assert err is None
    assert run is not None


# ── 8. F11：set_inflight 防覆盖未跑完 finally 的旧 task ─────────────────────


async def test_set_inflight_rejects_overwrite_of_live_slot() -> None:
    """F11：set_inflight 写入新非空 task 时，旧槽必须为空 / done() —— 防止覆盖
    一个未跑完 finally 的旧 task 引用，让旧 finally 的 set_inflight(None) 把新
    task 引用清掉、新 task 失去 strong ref。"""
    import asyncio

    rt = RoomRuntime(
        room_id="r", session=None,  # type: ignore[arg-type]
        router=RoomEventRouter(lambda env: None),
    )

    # 起一个真正在跑的 task 占住槽。
    async def long_task() -> None:
        await asyncio.sleep(10)

    t1 = asyncio.create_task(long_task())
    rt.set_inflight(t1, "user")

    # 尝试覆盖一个仍在跑的 task 应该 assert 失败。
    async def other_task() -> None:
        await asyncio.sleep(0)

    t2 = asyncio.create_task(other_task())
    try:
        with pytest.raises(AssertionError, match="overwriting live slot"):
            rt.set_inflight(t2, "handoff")
    finally:
        # 清理。
        t1.cancel()
        t2.cancel()
        try:
            await t1
        except (asyncio.CancelledError, BaseException):
            pass
        try:
            await t2
        except (asyncio.CancelledError, BaseException):
            pass
        # 直接重置槽，不经 set_inflight（避免再次触发 assert）。
        rt.inflight_task = None
        rt.inflight_kind = None


def test_busy_alive_includes_pending_mts_continuations() -> None:
    """Codex P1 (round 2) 修：``busy_alive`` 应包含 ``pending_mts_continuations``
    —— 防 deferred 续命 callback 跑前 background runtime 被 self_destruct 误销。

    具体场景：bg 已 pop（agent_runs 空），inflight 也已清（drain 已 done），但
    deferred 续命 callback 尚未起新 drain → counter=1 让 busy_alive 真，让
    ``_maybe_self_destruct_background_runtime`` 不动 runtime。
    """
    rt = RoomRuntime(
        room_id="r", session=None,  # type: ignore[arg-type]
        router=RoomEventRouter(lambda env: None),
    )
    # 三件套都空 → busy_alive=False。
    assert rt.inflight_alive() is False
    assert rt.has_active_runs() is False
    assert rt.busy_alive() is False
    # counter>0 → busy_alive 应真。
    rt.pending_mts_continuations = 1
    assert rt.busy_alive() is True
    rt.pending_mts_continuations = 0
    assert rt.busy_alive() is False


def test_has_pending_mts_bg_true_via_counter() -> None:
    """Codex P2 修：``has_pending_mts_bg`` 真值包括 ``pending_mts_continuations``
    计数 —— 即使 agent_runs 已 pop 完，只要 deferred 续命未跑，仍返 True 让 drain
    守卫等住。"""
    rt = RoomRuntime(
        room_id="r", session=None,  # type: ignore[arg-type]
        router=RoomEventRouter(lambda env: None),
    )
    assert rt.has_pending_mts_bg() is False
    rt.pending_mts_continuations = 1
    assert rt.has_pending_mts_bg() is True
    rt.pending_mts_continuations = 0
    assert rt.has_pending_mts_bg() is False


async def test_codex_p2_deferral_keeps_review_alive() -> None:
    """Codex P2 finding 集成回归：bg 完成时若有 inflight handoff drain → defer
    advance 到 drain done callback，避免「drain 的 advance_after_turn 见 budget=0
    触发 BUDGET_EXHAUSTED 清队列 → review 丢失」。

    场景模拟：bg 已 pop（step ③）+ 待跑续命 counter=1 + 调度 callback 在 drain done
    后跑 → drain 期间 MANAGER_FINISHED 兜底守卫见 counter>0 不收尾、callback 才扣
    budget enqueue review。
    """
    import asyncio

    orch, _ = _build_orch("Alice", "Bob", store=_make_store_with_task())
    sink: list[ChahuaEnvelope] = []
    orch.start_managed_session(
        sink.append, task_id="t1", manager_guest="Alice", budget=1,
    )
    # 模拟「bg Bob 在跑中，drain 在跑 manager turn」状态：
    # 用 counter=1 模拟 bg 已 pop 但 deferred 续命未跑。
    rt_proxy = RoomRuntime(
        room_id="r",
        session=type("_S", (), {"orchestrator": orch})(),  # type: ignore[arg-type]
        router=RoomEventRouter(sink.append),
    )
    rt_proxy.pending_mts_continuations = 1
    # 把 has_pending 注入 orch（模拟 _attach_runtime_state）。
    orch._has_pending_mts_bg = rt_proxy.has_pending_mts_bg

    # drain kickoff manager 入队 + drain。
    orch.enqueue_handoff(
        HandoffItem(kind=HandoffKind.DELEGATE, target="Alice", reason="kickoff")
    )
    sink.clear()

    await orch.run_pending_handoff(sink.append, task_id="t1")

    # drain 应安静退出（守卫 has_pending=True → 不 MANAGER_FINISHED）。
    types_after_drain = [e.type for e in sink]
    assert ChahuaEventType.MANAGED_SESSION_ENDED not in types_after_drain
    assert orch._managed_session is not None
    assert orch._managed_session.budget == 1  # 还没动 budget

    # 模拟 callback 跑：counter -= 1 + 调 advance。
    continued = orch._mts_ops.advance_after_bg_completion(
        sink.append, bg_guest_name="Bob",
        mts_manager_at_spawn="Alice",
    )
    rt_proxy.pending_mts_continuations = 0

    assert continued is True
    assert orch._managed_session.budget == 0
    # review 已 enqueue，未被 drain 清掉。
    assert len(orch._handoff_queue) == 1
    assert orch._handoff_queue[0].target == "Alice"
    assert "bg Bob" in orch._handoff_queue[0].reason


async def test_set_inflight_allows_overwrite_when_old_done() -> None:
    """F11 反向：旧槽里是 done() 的 task → 允许覆盖。"""
    import asyncio

    rt = RoomRuntime(
        room_id="r", session=None,  # type: ignore[arg-type]
        router=RoomEventRouter(lambda env: None),
    )

    async def quick_task() -> None:
        return None

    t1 = asyncio.create_task(quick_task())
    await t1  # 等它跑完

    rt.set_inflight(t1, "user")  # 用 done() 的 task 占位

    async def other_task() -> None:
        await asyncio.sleep(0)

    t2 = asyncio.create_task(other_task())
    rt.set_inflight(t2, "handoff")  # 旧槽 done() → 允许覆盖
    assert rt.inflight_task is t2
    assert rt.inflight_kind == "handoff"

    t2.cancel()
    try:
        await t2
    except (asyncio.CancelledError, BaseException):
        pass
    rt.inflight_task = None
    rt.inflight_kind = None
