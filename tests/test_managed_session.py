"""P8.3 托管任务会话（MTS）—— 承重段回归（docs/P8.3-原生自动推进.md §13）。

覆盖 §2.2 的每个停止条件 + ``_advance_managed_session_after_turn`` 分流 +
``_intercept_task_proposal`` 自动入队 + drain 步序回归。
"""

from __future__ import annotations

import asyncio

import pytest

from chahua.cursor import GuestCursor
from chahua.events import (
    TASK_PROPOSAL_KIND_DECISION,
    TASK_PROPOSAL_KIND_HANDOFF_DELEGATE,
    TASK_PROPOSAL_KIND_HANDOFF_PANEL,
    TASK_PROPOSAL_KIND_HANDOFF_REVIEW,
    ChahuaEnvelope,
    ChahuaEventType,
)
from chahua.handoff import (
    MANAGED_SESSION_DEFAULT_BUDGET,
    MAX_MANAGED_BUDGET,
    HandoffItem,
    HandoffKind,
    ManagedSession,
)
from chahua.orchestrator import Orchestrator, OrchestratorConfig
from chahua.room import Room
from chahua.user_md import USER_SPEAKER_ID, UserConfig

from conftest import NoopScorer, NoopSummarizer, SpeakingStubGuest


# ── fake task store（_managed_session_stop_reason 的 task_closed 检查用）──────


class _FakeTask:
    def __init__(self, status: str) -> None:
        self.status = status


class _FakeStore:
    """只实现 ``get_task`` —— orchestrator 在 MTS 路径上只读这一个 API。"""

    def __init__(self) -> None:
        self.tasks: dict[str, _FakeTask] = {}
        self.active_task_id = None

    def get_task(self, task_id):
        return self.tasks.get(task_id)

    def list_tasks(self):
        # ArtifactDetector.__init__ seed 用 —— MTS 测试不关心产物，返空。
        return []


# ── manager / worker stub guests ─────────────────────────────────────────


class _MtsManagerGuest(SpeakingStubGuest):
    """管理者替身：发言后按 ``proposals`` 逐回合「提议」一位 worker。

    走真 ``_intercept_task_proposal`` hook（构造一条 TASK_PROPOSAL envelope 喂它）
    —— 同时把自动入队通道也覆盖到。``proposals`` 每项是 worker 名或 ``None``
    （None = 这一回合不派活）。
    """

    def __init__(self, name, room, orch_box, proposals) -> None:
        super().__init__(name, room)
        self._orch_box = orch_box
        self._proposals = list(proposals)

    async def speak(self, ctx, *, turn_id, sink, cancellation_token=None, task_id=None):
        msg = await super().speak(
            ctx, turn_id=turn_id, sink=sink, task_id=task_id,
        )
        if self._proposals:
            worker = self._proposals.pop(0)
            if worker is not None:
                orch = self._orch_box[0]
                env = ChahuaEnvelope(
                    room_id=self._room.name, turn_id=turn_id,
                    guest_name=self.name, message_id=None,
                    type=ChahuaEventType.TASK_PROPOSAL,
                    data={
                        "proposer": self.name,
                        "kind": TASK_PROPOSAL_KIND_HANDOFF_DELEGATE,
                        "payload": {"target": worker},
                    },
                )
                orch._intercept_task_proposal(env, sink)
        return msg


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


def _delegate(target: str) -> HandoffItem:
    return HandoffItem(kind=HandoffKind.DELEGATE, target=target)


async def _run_mts(orch, *, manager, budget, proposals, store_task_id="task-1"):
    """开启 MTS（直接装态，模拟 inbound）+ enqueue kickoff + drain。返回 envelopes。"""
    orch_box = [orch]
    # 把管理者换成会派活的替身。
    mgr = _MtsManagerGuest(manager, orch.room, orch_box, proposals)
    orch._guests[manager].guest = mgr  # type: ignore[attr-defined]
    captured: list[ChahuaEnvelope] = []
    orch.start_managed_session(
        captured.append, task_id=store_task_id,
        manager_guest=manager, budget=budget,
    )
    orch.enqueue_handoff(
        HandoffItem(kind=HandoffKind.DELEGATE, target=manager, reason="托管启动")
    )
    await orch.run_pending_handoff(captured.append, task_id=store_task_id)
    return captured


# ── 数据契约 ──────────────────────────────────────────────────────────────


def test_managed_session_construction() -> None:
    ms = ManagedSession(task_id="t1", manager_guest="Maya", budget=6)
    assert ms.task_id == "t1"
    assert ms.manager_guest == "Maya"
    assert ms.budget == 6
    # 非 frozen —— budget 可变（每次回调扣 1）。
    ms.budget -= 1
    assert ms.budget == 5


def test_managed_session_constants() -> None:
    assert MAX_MANAGED_BUDGET == 20
    assert 1 <= MANAGED_SESSION_DEFAULT_BUDGET <= MAX_MANAGED_BUDGET


# ── _intercept_task_proposal ─────────────────────────────────────────────


def test_intercept_no_mts_returns_false() -> None:
    orch, _ = _build("A", "B")
    env = ChahuaEnvelope(
        room_id="t", turn_id="x", guest_name="A", message_id=None,
        type=ChahuaEventType.TASK_PROPOSAL,
        data={"proposer": "A", "kind": TASK_PROPOSAL_KIND_HANDOFF_DELEGATE,
              "payload": {"target": "B"}},
    )
    assert orch._intercept_task_proposal(env, lambda _e: None) is False
    assert len(orch._handoff_queue) == 0


def test_intercept_manager_delegate_enqueues_and_intercepts() -> None:
    orch, _ = _build("Maya", "Wei")
    orch._managed_session = ManagedSession(
        task_id="t1", manager_guest="Maya", budget=3,
    )
    captured: list[ChahuaEnvelope] = []
    env = ChahuaEnvelope(
        room_id="t", turn_id="x", guest_name="Maya", message_id=None,
        type=ChahuaEventType.TASK_PROPOSAL,
        data={"proposer": "Maya", "kind": TASK_PROPOSAL_KIND_HANDOFF_DELEGATE,
              "payload": {"target": "Wei", "reason": "做数据清洗"}},
    )
    assert orch._intercept_task_proposal(env, captured.append) is True
    # 入队 + 重发队列快照（不下发 TASK_PROPOSAL）。
    assert len(orch._handoff_queue) == 1
    assert orch._handoff_queue[0].target == "Wei"
    assert "托管 · Maya 指派" in (orch._handoff_queue[0].reason or "")
    assert "做数据清洗" in (orch._handoff_queue[0].reason or "")
    assert any(
        e.type is ChahuaEventType.HANDOFF_ENQUEUED for e in captured
    )
    assert not any(
        e.type is ChahuaEventType.TASK_PROPOSAL for e in captured
    )


def test_intercept_delegate_absent_target_not_auto_enqueued() -> None:
    """管理者 propose_delegate 到不在场茶客 → 不拦截、照常渲采纳卡（Codex review P2）。
    否则坏指派被静默吞、drain drop 后 MTS 无报错地收尾。"""
    orch, _ = _build("Maya", "Wei")
    orch._managed_session = ManagedSession(
        task_id="t1", manager_guest="Maya", budget=3,
    )
    env = ChahuaEnvelope(
        room_id="t", turn_id="x", guest_name="Maya", message_id=None,
        type=ChahuaEventType.TASK_PROPOSAL,
        data={"proposer": "Maya", "kind": TASK_PROPOSAL_KIND_HANDOFF_DELEGATE,
              "payload": {"target": "Ghost"}},
    )
    assert orch._intercept_task_proposal(env, lambda _e: None) is False
    assert len(orch._handoff_queue) == 0


def test_intercept_non_manager_proposal_not_intercepted() -> None:
    orch, _ = _build("Maya", "Wei")
    orch._managed_session = ManagedSession(
        task_id="t1", manager_guest="Maya", budget=3,
    )
    env = ChahuaEnvelope(
        room_id="t", turn_id="x", guest_name="Wei", message_id=None,
        type=ChahuaEventType.TASK_PROPOSAL,
        data={"proposer": "Wei", "kind": TASK_PROPOSAL_KIND_HANDOFF_DELEGATE,
              "payload": {"target": "Maya"}},
    )
    assert orch._intercept_task_proposal(env, lambda _e: None) is False
    assert len(orch._handoff_queue) == 0


@pytest.mark.parametrize("kind", [
    TASK_PROPOSAL_KIND_DECISION,
    TASK_PROPOSAL_KIND_HANDOFF_REVIEW,
])
def test_intercept_review_and_decision_not_auto_enqueued(kind) -> None:
    """只自动入队 delegate / panel —— review / decision / status 照常渲卡（docs §5.2）。"""
    orch, _ = _build("Maya", "Wei")
    orch._managed_session = ManagedSession(
        task_id="t1", manager_guest="Maya", budget=3,
    )
    env = ChahuaEnvelope(
        room_id="t", turn_id="x", guest_name="Maya", message_id=None,
        type=ChahuaEventType.TASK_PROPOSAL,
        data={"proposer": "Maya", "kind": kind, "payload": {}},
    )
    assert orch._intercept_task_proposal(env, lambda _e: None) is False
    assert len(orch._handoff_queue) == 0


def test_intercept_manager_self_delegate_swallowed() -> None:
    """管理者 propose_delegate(自己) → 吞掉空转，不入队不渲卡（docs §5.1）。"""
    orch, _ = _build("Maya", "Wei")
    orch._managed_session = ManagedSession(
        task_id="t1", manager_guest="Maya", budget=3,
    )
    captured: list[ChahuaEnvelope] = []
    env = ChahuaEnvelope(
        room_id="t", turn_id="x", guest_name="Maya", message_id=None,
        type=ChahuaEventType.TASK_PROPOSAL,
        data={"proposer": "Maya", "kind": TASK_PROPOSAL_KIND_HANDOFF_DELEGATE,
              "payload": {"target": "Maya"}},
    )
    assert orch._intercept_task_proposal(env, captured.append) is True
    assert len(orch._handoff_queue) == 0
    assert captured == []


def test_intercept_manager_self_delegate_swallowed_while_speaking() -> None:
    """P8.4.11（Codex round 5 P2）：管理者在自己 ``speak()`` 内提议 delegate(自己)
    —— ``let_speak`` 已把 manager 标 busy；自指派必须仍被早 swallow，**不**落渲采纳卡。

    没本 fix 时：``_handoff_item_from_proposal`` 走全集 busy 校验 → target=manager
    in busy → 返 None → ``intercept_task_proposal`` 退到「item is None: return False」
    → 渲采纳卡 → 用户点采纳真的 enqueue 一轮 manager（破坏 §5.1「自指派不入队」）。
    """
    orch, _ = _build("Maya", "Wei")
    orch._managed_session = ManagedSession(
        task_id="t1", manager_guest="Maya", budget=3,
    )
    # 模拟 _let_speak 加的 busy 标 —— manager 正在自己 speak()。
    orch.active_guest_names = {"Maya"}
    captured: list[ChahuaEnvelope] = []
    env = ChahuaEnvelope(
        room_id="t", turn_id="x", guest_name="Maya", message_id=None,
        type=ChahuaEventType.TASK_PROPOSAL,
        data={"proposer": "Maya", "kind": TASK_PROPOSAL_KIND_HANDOFF_DELEGATE,
              "payload": {"target": "Maya"}},
    )
    assert orch._intercept_task_proposal(env, captured.append) is True
    assert len(orch._handoff_queue) == 0
    assert captured == []


def test_intercept_manager_panel_enqueues() -> None:
    orch, _ = _build("Maya", "Wei", "Lin")
    orch._managed_session = ManagedSession(
        task_id="t1", manager_guest="Maya", budget=3,
    )
    env = ChahuaEnvelope(
        room_id="t", turn_id="x", guest_name="Maya", message_id=None,
        type=ChahuaEventType.TASK_PROPOSAL,
        data={"proposer": "Maya", "kind": TASK_PROPOSAL_KIND_HANDOFF_PANEL,
              "payload": {"targets": ["Wei", "Lin"]}},
    )
    assert orch._intercept_task_proposal(env, lambda _e: None) is True
    assert len(orch._handoff_queue) == 1
    assert orch._handoff_queue[0].kind is HandoffKind.PANEL
    assert orch._handoff_queue[0].targets == ("Wei", "Lin")


def test_intercept_panel_summarizer_is_manager_enqueues() -> None:
    """P8.4.9（Codex round 3 P2）：manager 在自己 turn 内提议 panel + summarizer=自己
    → 仍要 auto-enqueue。

    本 hook 跑在 manager 的 ``speak()`` 内，``_let_speak`` 已把 manager 标 busy；
    但 panel 的 summarizer 槽位是 manager 这一轮结束**之后**才跑，那时 busy 已清。
    原 P11 C12 用全集 busy 把这种「workers 讨论、我汇总」MTS 模式当采纳卡降级，
    fix：从 summarizer busy 检查里剔除 manager。
    """
    orch, _ = _build("Maya", "Wei", "Lin")
    orch._managed_session = ManagedSession(
        task_id="t1", manager_guest="Maya", budget=3,
    )
    # manager 自己 speak 期间被 _let_speak 标 busy。
    orch.active_guest_names = {"Maya"}
    env = ChahuaEnvelope(
        room_id="t", turn_id="x", guest_name="Maya", message_id=None,
        type=ChahuaEventType.TASK_PROPOSAL,
        data={"proposer": "Maya", "kind": TASK_PROPOSAL_KIND_HANDOFF_PANEL,
              "payload": {
                  "targets": ["Wei", "Lin"],
                  "summarizer": "Maya",
              }},
    )
    assert orch._intercept_task_proposal(env, lambda _e: None) is True
    assert len(orch._handoff_queue) == 1
    item = orch._handoff_queue[0]
    assert item.kind is HandoffKind.PANEL
    assert item.targets == ("Wei", "Lin")
    assert item.summarizer == "Maya"


def test_intercept_panel_panelist_is_manager_still_rejected() -> None:
    """对照：manager 作为 panelist（不是 summarizer）仍按 P11 C12 拒绝（语义闭环）。

    panel 是 worker 间讨论；manager 把自己列进 panelists 在 P8.4.9 narrow 中**不**放行，
    避免「manager 在自己回合里再让自己讲一遍」的怪异语义。
    """
    orch, _ = _build("Maya", "Wei", "Lin")
    orch._managed_session = ManagedSession(
        task_id="t1", manager_guest="Maya", budget=3,
    )
    orch.active_guest_names = {"Maya"}
    env = ChahuaEnvelope(
        room_id="t", turn_id="x", guest_name="Maya", message_id=None,
        type=ChahuaEventType.TASK_PROPOSAL,
        data={"proposer": "Maya", "kind": TASK_PROPOSAL_KIND_HANDOFF_PANEL,
              "payload": {"targets": ["Wei", "Maya"]}},
    )
    assert orch._intercept_task_proposal(env, lambda _e: None) is False
    assert len(orch._handoff_queue) == 0


def _panel_proposal(payload: dict) -> ChahuaEnvelope:
    return ChahuaEnvelope(
        room_id="t", turn_id="x", guest_name="Maya", message_id=None,
        type=ChahuaEventType.TASK_PROPOSAL,
        data={"proposer": "Maya", "kind": TASK_PROPOSAL_KIND_HANDOFF_PANEL,
              "payload": payload},
    )


@pytest.mark.parametrize("payload", [
    {"targets": ["Wei", "Wei"]},                       # 重复
    {"targets": ["Wei", "Ghost"]},                     # 不在场
    {"targets": ["Wei", "Lin"], "summarizer": "Wei"},  # summarizer 自指
])
def test_intercept_invalid_panel_not_auto_enqueued(payload) -> None:
    """MTS 自动入队绕开 inbound —— 非法圆桌（重复 / 不在场 / summarizer 自指）必须
    自校验拦下，不直接入队（Codex review P2）。返 False → 照常渲采纳卡。"""
    orch, _ = _build("Maya", "Wei", "Lin")
    orch._managed_session = ManagedSession(
        task_id="t1", manager_guest="Maya", budget=3,
    )
    assert orch._intercept_task_proposal(
        _panel_proposal(payload), lambda _e: None,
    ) is False
    assert len(orch._handoff_queue) == 0


def test_intercept_oversize_panel_not_auto_enqueued() -> None:
    """超 cap 的圆桌（受 MAX_PANEL_TARGETS / max_consecutive_ai_turns 约束）不入队。"""
    orch, _ = _build("Maya", "Wei", "Lin", "Bob", max_ai=2)
    orch._managed_session = ManagedSession(
        task_id="t1", manager_guest="Maya", budget=3,
    )
    # cap = min(MAX_PANEL_TARGETS=4, max_ai=2) = 2；3 个 target 超额。
    assert orch._intercept_task_proposal(
        _panel_proposal({"targets": ["Wei", "Lin", "Bob"]}), lambda _e: None,
    ) is False
    assert len(orch._handoff_queue) == 0


# ── _managed_session_stop_reason ─────────────────────────────────────────


def test_stop_reason_budget_exhausted() -> None:
    orch, _ = _build("Maya")
    orch._managed_session = ManagedSession(
        task_id="t1", manager_guest="Maya", budget=0,
    )
    assert orch._managed_session_stop_reason() == "budget_exhausted"


def test_stop_reason_task_closed() -> None:
    store = _FakeStore()
    store.tasks["t1"] = _FakeTask("done")
    orch, _ = _build("Maya", store=store)
    orch._managed_session = ManagedSession(
        task_id="t1", manager_guest="Maya", budget=3,
    )
    assert orch._managed_session_stop_reason() == "task_closed"


def test_stop_reason_cap_reached() -> None:
    orch, _ = _build("Maya", max_ai=2)
    orch._managed_session = ManagedSession(
        task_id="t1", manager_guest="Maya", budget=3,
    )
    orch._consecutive_ai_turns = 2
    assert orch._managed_session_stop_reason() == "cap_reached"


def test_stop_reason_none_when_healthy() -> None:
    orch, _ = _build("Maya", max_ai=12)
    orch._managed_session = ManagedSession(
        task_id="t1", manager_guest="Maya", budget=3,
    )
    assert orch._managed_session_stop_reason() is None


# ── drain 全链路 ──────────────────────────────────────────────────────────


async def test_worker_turn_callbacks_manager_and_decrements_budget() -> None:
    """worker 回合后回调管理者复查 + budget 扣 1 + emit managed_session_advanced。"""
    orch, _ = _build("Maya", "Wei")
    cap = await _run_mts(orch, manager="Maya", budget=3, proposals=["Wei", None])

    consumed = [e for e in cap if e.type is ChahuaEventType.HANDOFF_CONSUMED]
    targets = [e.data["item"]["target"] for e in consumed]
    # Maya kickoff → Wei → Maya 复查
    assert targets == ["Maya", "Wei", "Maya"]

    advanced = [
        e for e in cap if e.type is ChahuaEventType.MANAGED_SESSION_ADVANCED
    ]
    assert len(advanced) == 1
    assert advanced[0].data["manager_guest"] == "Maya"
    assert advanced[0].data["remaining_budget"] == 2


async def test_manager_no_delegate_stays_dormant() -> None:
    """P8.4：管理者 kickoff 不派活 → 队列空 → MTS **保持 dormant**（不再终结）。

    P8.3 把这条归 ``manager_finished`` —— P8.4 改为待机：管理者「没派活」≠「事情
    完事」，由用户下一句话经 ``submit_user_message`` re-drain 续上。
    """
    orch, _ = _build("Maya", "Wei")
    cap = await _run_mts(orch, manager="Maya", budget=3, proposals=[None])

    consumed = [e for e in cap if e.type is ChahuaEventType.HANDOFF_CONSUMED]
    assert [e.data["item"]["target"] for e in consumed] == ["Maya"]
    ended = [e for e in cap if e.type is ChahuaEventType.MANAGED_SESSION_ENDED]
    assert ended == []
    # MTS 仍活，队列已空 —— dormant 标识。
    assert orch.managed_session is not None
    assert len(orch._handoff_queue) == 0


async def test_budget_one_runs_one_worker_then_exhausted() -> None:
    """budget=1：跑完 1 worker + 1 复查后 budget_exhausted；管理者最后一轮再派的活
    被 _end_managed_session 清掉、不多跑（docs §4.3）。"""
    orch, _ = _build("Maya", "Wei", "Lin")
    # kickoff 派 Wei、复查回合再派 Lin —— Lin 应被 budget_exhausted 清掉。
    cap = await _run_mts(orch, manager="Maya", budget=1, proposals=["Wei", "Lin"])

    consumed = [e for e in cap if e.type is ChahuaEventType.HANDOFF_CONSUMED]
    targets = [e.data["item"]["target"] for e in consumed]
    assert "Wei" in targets
    assert "Lin" not in targets  # 预算耗尽后没多跑

    ended = [e for e in cap if e.type is ChahuaEventType.MANAGED_SESSION_ENDED]
    assert len(ended) == 1
    assert ended[0].data["reason"] == "budget_exhausted"
    assert len(orch._handoff_queue) == 0
    assert orch.managed_session is None


async def test_cap_reached_ends_session() -> None:
    """连续 AI 轮数撞 max_consecutive_ai_turns → cap_reached。"""
    orch, _ = _build("Maya", "Wei", max_ai=2)
    cap = await _run_mts(orch, manager="Maya", budget=9, proposals=["Wei", "Wei"])
    ended = [e for e in cap if e.type is ChahuaEventType.MANAGED_SESSION_ENDED]
    assert len(ended) == 1
    assert ended[0].data["reason"] == "cap_reached"


async def test_task_closed_ends_session() -> None:
    """MTS 驱动的任务被关 → task_closed。"""
    store = _FakeStore()
    store.tasks["t1"] = _FakeTask("done")
    orch, _ = _build("Maya", "Wei", store=store)
    cap = await _run_mts(
        orch, manager="Maya", budget=5, proposals=["Wei"], store_task_id="t1",
    )
    ended = [e for e in cap if e.type is ChahuaEventType.MANAGED_SESSION_ENDED]
    assert len(ended) == 1
    assert ended[0].data["reason"] == "task_closed"


async def test_started_envelope_emitted() -> None:
    orch, _ = _build("Maya")
    captured: list[ChahuaEnvelope] = []
    orch.start_managed_session(
        captured.append, task_id="t1", manager_guest="Maya", budget=4,
    )
    started = [
        e for e in captured if e.type is ChahuaEventType.MANAGED_SESSION_STARTED
    ]
    assert len(started) == 1
    assert started[0].data == {
        "task_id": "t1", "manager_guest": "Maya", "budget": 4,
    }


async def test_end_managed_session_clears_queue() -> None:
    """end_managed_session 任意路径都清 _handoff_queue（docs §4.2 硬约束）。"""
    orch, _ = _build("Maya", "Wei")
    orch._managed_session = ManagedSession(
        task_id="t1", manager_guest="Maya", budget=3,
    )
    orch.enqueue_handoff(_delegate("Wei"))
    captured: list[ChahuaEnvelope] = []
    orch.end_managed_session(captured.append, reason="user_stopped")
    assert len(orch._handoff_queue) == 0
    assert orch.managed_session is None
    ended = [
        e for e in captured if e.type is ChahuaEventType.MANAGED_SESSION_ENDED
    ]
    assert len(ended) == 1
    assert ended[0].data["reason"] == "user_stopped"
    # 清队列后重发空快照让前端预览复位。
    enq = [e for e in captured if e.type is ChahuaEventType.HANDOFF_ENQUEUED]
    assert enq and enq[-1].data["queue"] == []


def test_end_managed_session_noop_without_session() -> None:
    orch, _ = _build("Maya")
    captured: list[ChahuaEnvelope] = []
    orch.end_managed_session(captured.append, reason="user_cancel")
    assert captured == []


def test_reset_room_clears_managed_session() -> None:
    orch, _ = _build("Maya")
    orch._managed_session = ManagedSession(
        task_id="t1", manager_guest="Maya", budget=3,
    )
    orch.reset_room()
    assert orch.managed_session is None


async def test_drain_break_path_stays_dormant() -> None:
    """P8.4：drain 经 break 退出（队首死项被 drop 光）时 MTS **保持 dormant**。

    P8.3 把 break 路径归 ``manager_finished`` 收尾兜底 —— P8.4 同样退役。死项被
    ``_advance_to_runnable_handoff`` drop 后队列空，drain 安静 ``return``；MTS
    待机等用户下一句话。
    """
    orch, _ = _build("Maya", "A", "B", max_ai=1)
    orch._managed_session = ManagedSession(
        task_id="t1", manager_guest="Maya", budget=3,
    )
    # panel cost=2 > max_ai=1 → _advance_to_runnable_handoff 当死项 drop → 队列空
    # → while 经 `head is None` break 退出。
    orch.enqueue_handoff(
        HandoffItem(kind=HandoffKind.PANEL, targets=("A", "B"))
    )
    captured: list[ChahuaEnvelope] = []
    await orch.run_pending_handoff(captured.append, task_id="t1")

    # MTS 仍活、队列空 —— dormant。
    assert orch.managed_session is not None
    assert len(orch._handoff_queue) == 0
    ended = [e for e in captured if e.type is ChahuaEventType.MANAGED_SESSION_ENDED]
    assert ended == []


class _MtsPanelManagerGuest(SpeakingStubGuest):
    """管理者替身：kickoff 回合提议一个 panel（圆桌），之后不再派活。"""

    def __init__(self, name, room, orch_box, targets) -> None:
        super().__init__(name, room)
        self._orch_box = orch_box
        self._targets = list(targets)
        self._fired = False

    async def speak(self, ctx, *, turn_id, sink, cancellation_token=None, task_id=None):
        msg = await super().speak(
            ctx, turn_id=turn_id, sink=sink, task_id=task_id,
        )
        if not self._fired:
            self._fired = True
            env = ChahuaEnvelope(
                room_id=self._room.name, turn_id=turn_id,
                guest_name=self.name, message_id=None,
                type=ChahuaEventType.TASK_PROPOSAL,
                data={
                    "proposer": self.name,
                    "kind": TASK_PROPOSAL_KIND_HANDOFF_PANEL,
                    "payload": {"targets": self._targets},
                },
            )
            self._orch_box[0]._intercept_task_proposal(env, sink)
        return msg


async def test_cap_blocked_handoff_keeps_mts_and_queue() -> None:
    """管理者派下的 panel 因剩余 cap 不够而暂不能跑（cost-break、非死项）——
    drain 收尾兜底不能当 manager_finished 收尾并清队，该项须留队等下次 drain
    （Codex review P2）。否则管理者真派的活被静默丢弃。"""
    orch, _ = _build("Maya", "A", "B", max_ai=2)
    orch_box = [orch]
    mgr = _MtsPanelManagerGuest("Maya", orch.room, orch_box, ["A", "B"])
    orch._guests["Maya"].guest = mgr  # type: ignore[attr-defined]
    captured: list[ChahuaEnvelope] = []
    orch.start_managed_session(
        captured.append, task_id="t1", manager_guest="Maya", budget=5,
    )
    orch.enqueue_handoff(
        HandoffItem(kind=HandoffKind.DELEGATE, target="Maya", reason="托管启动")
    )
    # kickoff 后 _consecutive_ai_turns=1；panel cost=2 ≤ max_ai=2（非死项），但
    # 1+2=3 > 2 → cost-break，留队首。
    await orch.run_pending_handoff(captured.append, task_id="t1")

    assert orch.managed_session is not None
    assert len(orch._handoff_queue) == 1
    assert orch._handoff_queue[0].kind is HandoffKind.PANEL
    ended = [e for e in captured if e.type is ChahuaEventType.MANAGED_SESSION_ENDED]
    assert ended == []


async def test_advance_in_drain_runs_before_peek() -> None:
    """drain 步序：_advance_managed_session_after_turn 在 peek/turn_end 之前 ——
    worker 回合的 advanced envelope 必须早于该 turn 的 turn_end。"""
    orch, _ = _build("Maya", "Wei")
    cap = await _run_mts(orch, manager="Maya", budget=3, proposals=["Wei", None])
    types = [e.type for e in cap]
    adv_idx = types.index(ChahuaEventType.MANAGED_SESSION_ADVANCED)
    # advanced 之后才是 worker turn 的 turn_end
    turn_ends_after = [
        i for i, t in enumerate(types)
        if t is ChahuaEventType.TURN_END and i > adv_idx
    ]
    assert turn_ends_after, "advanced 必须在某个 turn_end 之前 emit"
