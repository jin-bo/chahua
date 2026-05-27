"""P8.4 MTS 待机闭环 —— 新增承重段回归（docs/P8.4-MTS 待机闭环.md §11）。

覆盖 P8.4 引入的新行为：

1. drain 收尾队列空 + MTS 活 → MTS **仍活**（dormant），不 emit
   ``managed_session_ended``。`MANAGER_FINISHED` reason 已退役（§3.1）。
2. cap 不双扣：chain 跑到接近 cap + 管理者派活 → re-drain 因 cap 撞顶留队首，
   不再清零跑满。``run_pending_handoff(..., reset_cap=False)`` 是承重契约（§4.1）。
3. ``check_after_task_change`` helper：MTS dormant + task 状态变更（4 种入口）
   命中 ``stop_reason()`` 时统一收尾（§4.2）。
4. P8.4 行为改进：关**非** MTS 任务时 MTS 继续 dormant（旧 P8.3 强行
   ``USER_CANCEL`` 是过度反应）。
"""

from __future__ import annotations

import pytest

from chahua import admin
from chahua.cursor import GuestCursor
from chahua.events import (
    ChahuaEnvelope,
    ChahuaEventType,
    TASK_PROPOSAL_KIND_HANDOFF_DELEGATE,
)
from chahua.handoff import HandoffItem, HandoffKind, ManagedSession
from chahua.orchestrator import Orchestrator, OrchestratorConfig
from chahua.room import Room
from chahua.server import (
    ChahuaServer,
    _bind_inbound_handlers,
    _install_handler_slots,
)
from chahua.session import build_room_session
from chahua.task import Task
from chahua.user_md import USER_SPEAKER_ID, UserConfig

from conftest import NoopScorer, NoopSummarizer, SpeakingStubGuest


# ── 共享桩 ─────────────────────────────────────────────────────────────────


class _FakeStore:
    """复用 :class:`chahua.task.Task` 真模型（context_renderer 要 title/goal 等
    完整字段）。与 ``test_mts_bg_spawn_reentry._FakeStore`` 同口径。

    P8.4 ``stop_reason`` 新增「MTS 任务不再 active」判定 → 必有 ``active_task_id``。
    """

    def __init__(self) -> None:
        self.tasks: dict[str, Task] = {}
        self.active_task_id: str | None = None

    def get_task(self, task_id):
        return self.tasks.get(task_id)

    def list_tasks(self):
        return []

    def list_decisions(self, task_id):
        return []

    def list_artifacts(self, task_id):
        return []

    def close_task(self, task_id, *, status):
        t = self.tasks[task_id]
        self.tasks[task_id] = Task(
            id=t.id, title=t.title, goal=t.goal, status=status, owner=t.owner,
            created_at_ms=t.created_at_ms, updated_at_ms=t.updated_at_ms,
            closed_at_ms=0,
        )
        return self.tasks[task_id]


def _build(*names, max_ai: int = 12, store=None):
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


def _make_store(task_id: str, active: bool = True) -> _FakeStore:
    store = _FakeStore()
    store.tasks[task_id] = Task(
        id=task_id, title="t", goal="g", status="doing", owner=None,
        created_at_ms=0, updated_at_ms=0, closed_at_ms=None,
    )
    store.active_task_id = task_id if active else None
    return store


# ── 1) drain 收尾 dormant —— 显式拎出来钉一遍 ────────────────────────────


async def test_drain_finishes_empty_queue_stays_dormant() -> None:
    """管理者 kickoff 不派活 → 队列空 → drain ``return``，MTS 保持 dormant。

    与 ``test_managed_session.test_manager_no_delegate_stays_dormant`` 同义，再
    钉一次以确保 P8.4 后续 refactor 不让 dormant 变 ended（docs/P8.4 §3.1）。
    """
    orch, _ = _build("Maya", "Wei", store=_make_store("t1"))
    captured: list[ChahuaEnvelope] = []
    orch.start_managed_session(
        captured.append, task_id="t1", manager_guest="Maya", budget=3,
    )
    orch.enqueue_handoff(
        HandoffItem(kind=HandoffKind.DELEGATE, target="Maya", reason="kickoff")
    )
    await orch.run_pending_handoff(captured.append, task_id="t1")

    assert orch.managed_session is not None
    assert len(orch._handoff_queue) == 0
    ended = [
        e for e in captured if e.type is ChahuaEventType.MANAGED_SESSION_ENDED
    ]
    assert ended == []


# ── 2) cap 不双扣（最承重）────────────────────────────────────────────────


class _ProposeAfterSpeakManager(SpeakingStubGuest):
    """管理者替身：每次发言后经 ``_intercept_task_proposal`` 派一位 worker。

    与 ``test_managed_session._MtsManagerGuest`` 区别：此处用 ``proposals``
    无限循环（如 ``["Wei", "Wei", ...]``），让 cap 来兜底而非 proposal 耗尽。
    """

    def __init__(self, name, room, orch_box, target: str) -> None:
        super().__init__(name, room)
        self._orch_box = orch_box
        self._target = target

    async def speak(self, ctx, *, turn_id, sink, cancellation_token=None, task_id=None):
        msg = await super().speak(
            ctx, turn_id=turn_id, sink=sink, task_id=task_id,
        )
        env = ChahuaEnvelope(
            room_id=self._room.name, turn_id=turn_id,
            guest_name=self.name, message_id=None,
            type=ChahuaEventType.TASK_PROPOSAL,
            data={
                "proposer": self.name,
                "kind": TASK_PROPOSAL_KIND_HANDOFF_DELEGATE,
                "payload": {"target": self._target},
            },
        )
        self._orch_box[0]._intercept_task_proposal(env, sink)
        return msg


async def test_redrain_reset_cap_false_does_not_double_count() -> None:
    """P8.4 §4.1 cap 双扣陷阱钉锁：``reset_cap=False`` 让 chain + re-drain 段
    共享同一 cap 预算。

    构造：MTS 活、_consecutive_ai_turns 已逼近 cap。直接驱动 ``run_pending_handoff``
    两次：① 第一次 ``reset_cap=True``（首次 drain，清零计数）走完正常路径；
    ② 第二次 ``reset_cap=False`` —— 队首项 cost 已超 cap，应立即留队首并 return，
    **不**清零计数后再跑一轮。
    """
    orch, _ = _build("Maya", "Wei", max_ai=2, store=_make_store("t1"))
    captured: list[ChahuaEnvelope] = []
    orch.start_managed_session(
        captured.append, task_id="t1", manager_guest="Maya", budget=3,
    )
    # 直接将计数器拉到 cap-1，模拟 chain 已跑了 max_ai-1 轮 AI。
    orch._consecutive_ai_turns = 1
    orch.enqueue_handoff(
        HandoffItem(kind=HandoffKind.DELEGATE, target="Wei", reason="x")
    )
    # 当前 count=1，cost=1，cap=2 → 1+1=2 不撞顶；为构造撞顶用 cost=2 的 panel。
    # 但此处更直接：把计数推到 cap，让 cost=1 也撞顶。
    orch._consecutive_ai_turns = 2
    captured.clear()

    # reset_cap=False —— 不清零，cost=1 占 0 名额：2+1 > 2 → cost-break 留队首。
    await orch.run_pending_handoff(
        captured.append, task_id="t1", reset_cap=False,
    )

    # 队列项原样保留（cost-break 不弹），无 turn 跑、无 HANDOFF_CONSUMED 发出。
    assert len(orch._handoff_queue) == 1
    assert orch._handoff_queue[0].target == "Wei"
    consumed = [
        e for e in captured if e.type is ChahuaEventType.HANDOFF_CONSUMED
    ]
    assert consumed == []
    # 计数器**未**被清零（reset_cap=False 的根本目的）。
    assert orch._consecutive_ai_turns == 2


async def test_redrain_reset_cap_true_baseline() -> None:
    """对照：``reset_cap=True`` 默认 —— 计数清零后队首项可跑。

    钉住默认值行为不动（P7.1 / P8.3 / P11 既有调用零回归）。
    """
    orch, _ = _build("Maya", "Wei", max_ai=2, store=_make_store("t1"))
    captured: list[ChahuaEnvelope] = []
    orch.start_managed_session(
        captured.append, task_id="t1", manager_guest="Maya", budget=3,
    )
    orch._consecutive_ai_turns = 2  # 撞 cap
    orch.enqueue_handoff(
        HandoffItem(kind=HandoffKind.DELEGATE, target="Wei", reason="x")
    )
    captured.clear()

    # reset_cap=True（默认）—— 入口清零后能跑。
    await orch.run_pending_handoff(captured.append, task_id="t1")

    # 队列空（首项 Wei 跑完，advance_after_turn 又回填了 Maya 复查 + Maya 也跑完
    # 撞 cap 后留 dormant）；关键钉点是 Wei 真的跑了 —— P7.1 既有清零行为。
    consumed = [
        e for e in captured if e.type is ChahuaEventType.HANDOFF_CONSUMED
    ]
    targets = [e.data["item"]["target"] for e in consumed]
    assert "Wei" in targets


# ── 3) check_after_task_change helper ────────────────────────────────────


def test_check_after_task_change_noop_without_mts() -> None:
    """无 MTS → helper 立即返回，不发任何事件。"""
    orch, _ = _build("Maya", store=_make_store("t1"))
    captured: list[ChahuaEnvelope] = []
    orch._mts_ops.check_after_task_change(captured.append)
    assert captured == []


def test_check_after_task_change_ends_mts_when_active_diverges() -> None:
    """dormant MTS + active 切走 → helper 命中 stop_reason → end_managed_session。"""
    store = _make_store("t1")
    orch, _ = _build("Maya", store=store)
    orch._managed_session = ManagedSession(
        task_id="t1", manager_guest="Maya", budget=3,
    )
    # 模拟用户切 active 到别的任务（或 None）。
    store.active_task_id = "t2"
    captured: list[ChahuaEnvelope] = []
    orch._mts_ops.check_after_task_change(captured.append)
    assert orch.managed_session is None
    ended = [
        e for e in captured if e.type is ChahuaEventType.MANAGED_SESSION_ENDED
    ]
    assert len(ended) == 1
    assert ended[0].data["reason"] == "task_closed"


def test_check_after_task_change_ends_mts_when_task_closed() -> None:
    """dormant MTS + task.status 落 CLOSED → helper 收尾 task_closed。"""
    store = _make_store("t1")
    orch, _ = _build("Maya", store=store)
    orch._managed_session = ManagedSession(
        task_id="t1", manager_guest="Maya", budget=3,
    )
    # 任务被关 —— 用 close_task 走真状态机生成新 Task 实例（frozen dataclass）。
    store.close_task("t1", status="done")
    captured: list[ChahuaEnvelope] = []
    orch._mts_ops.check_after_task_change(captured.append)
    assert orch.managed_session is None
    ended = [
        e for e in captured if e.type is ChahuaEventType.MANAGED_SESSION_ENDED
    ]
    assert len(ended) == 1
    assert ended[0].data["reason"] == "task_closed"


def test_check_after_task_change_keeps_mts_when_unchanged() -> None:
    """dormant MTS + task 状态没变 → helper 无命中，MTS 仍活、不发事件。"""
    store = _make_store("t1")
    orch, _ = _build("Maya", store=store)
    orch._managed_session = ManagedSession(
        task_id="t1", manager_guest="Maya", budget=3,
    )
    captured: list[ChahuaEnvelope] = []
    orch._mts_ops.check_after_task_change(captured.append)
    assert orch.managed_session is not None
    assert captured == []


# ── 4) close 别的任务时 MTS 继续 dormant（P8.4 改进）─────────────────────


@pytest.fixture
def session_and_srv(env_paths, monkeypatch):
    """与 ``test_server_managed_session_inbound`` 同口径夹具 —— 复用 monkeypatch
    防止真跑 LLM。"""

    async def _noop_drain(self, sink, *, task_id, reset_cap=True):
        return None

    monkeypatch.setattr(Orchestrator, "run_pending_handoff", _noop_drain)

    rc = admin.create_room(
        paths=env_paths, room_id="t1", name="t1",
        guests=[{"persona": "chahua/personas/宝总/宝总.md", "name": "宝总"}],
    )
    session = build_room_session(rc.room_dir, env_paths)
    srv = object.__new__(ChahuaServer)
    srv._session = session
    srv._paths = env_paths
    srv._inflight_turn_task = None
    srv._inflight_kind = None
    _install_handler_slots(srv)
    srv._inbound_handlers = _bind_inbound_handlers(srv)
    yield session, srv
    session.close()


async def _send(srv, frame):
    captured: list[dict] = []
    await srv._handle_inbound(frame, lambda e: captured.append(e.to_dict()))
    return captured


def _by_type(captured: list[dict], t: str) -> list[dict]:
    return [e for e in captured if e["type"] == t]


async def test_close_other_task_keeps_mts_dormant(session_and_srv) -> None:
    """P8.4 行为改进：MTS 活在任务 A、用户关任务 B → MTS **仍活在 A**（dormant）。

    P8.3 走 eager ``USER_CANCEL`` 把 MTS 杀掉是过度反应。P8.4 让 helper 跑
    ``stop_reason()``，任务 A 仍是 active / open → 无命中，MTS 继续待机。
    """
    session, srv = session_and_srv
    orch = srv._session.orchestrator
    # 任务 A = MTS 任务；任务 B = 之后被关的任务。
    task_a = session.tasks_store.open_task(title="A", goal="g")
    task_b = session.tasks_store.open_task(title="B", goal="g")
    # open_task 自动 set_active → 现在 active 是 B。手动切回 A。
    session.tasks_store.set_active(task_a.id)
    orch._managed_session = ManagedSession(
        task_id=task_a.id, manager_guest="宝总", budget=3,
    )

    cap = await _send(srv, {
        "type": "close_task", "task_id": task_b.id, "status": "done",
    })
    # MTS 保持活在任务 A 上 —— 关 B 与它无关。
    assert orch.managed_session is not None
    assert orch.managed_session.task_id == task_a.id
    ended = _by_type(cap, ChahuaEventType.MANAGED_SESSION_ENDED.value)
    assert ended == []


async def test_update_task_non_terminal_status_keeps_mts(session_and_srv) -> None:
    """P8.4 §4.2 占位调用钉锁：``update_task`` 改非终结 status（如 ``doing`` →
    ``review``，但当前 NON_TERMINAL_STATUSES 只一个 doing；用 update title 兼容）
    后 helper 跑、但 stop_reason 无命中 → MTS 仍活。
    """
    session, srv = session_and_srv
    orch = srv._session.orchestrator
    task = session.tasks_store.open_task(title="A", goal="g")
    orch._managed_session = ManagedSession(
        task_id=task.id, manager_guest="宝总", budget=3,
    )

    cap = await _send(srv, {
        "type": "update_task", "task_id": task.id,
        "patch": {"title": "改名"},
    })
    assert orch.managed_session is not None
    ended = _by_type(cap, ChahuaEventType.MANAGED_SESSION_ENDED.value)
    assert ended == []
