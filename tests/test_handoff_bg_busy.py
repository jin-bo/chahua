"""P11 C12（+ P8.4.9 Codex round 3 narrow）：handoff 全家拒 **bg-run busy** 茶客
（防 ``let_speak`` 与 bg ``arun`` 并发跑在同一 ``agentao.Agentao`` 实例）。

**P8.4.9 narrow（Codex round 3 P2）**：inbound 准入校验只看 ``guest_in_bg_run``，
不看 ``active_guest_names`` 全集。前台 turn / handoff drain 占用让
``_enqueue_handoff_and_maybe_start`` 抢占；只有 bg run 是不可抢占的真忙。drain
自身的 busy-winner 守卫（``_advance_to_runnable_handoff``）仍用 ``active_guest_names``
全集兜底（覆盖 inbound→drain race / MTS auto-enqueue 间瞬态）。

覆盖五处 fix：

1. ``_inbound_handoff_delegate``：target 在跑 bg run → NOTICE error + 不入队。
2. ``_inbound_handoff_review``：target 在跑 bg run → NOTICE error + 不入队。
3. ``_inbound_handoff_panel``：任一 panelist / summarizer 在跑 bg run → NOTICE
   error + 不入队。
4. ``_advance_to_runnable_handoff``（drain 兜底）：队首项 winner 在 active_guest_names
   全集中 → drop + emit ``HANDOFF_ENQUEUED`` 快照。覆盖 inbound→drain race +
   MTS auto-enqueue 路径。
5. ``ManagedSessionOps._handoff_item_from_proposal``（MTS intercept 路径）：target /
   panelist / summarizer 在 active 全集中 → 返 ``None`` 让 hook 降级渲采纳卡。
"""

from __future__ import annotations

import asyncio
import contextlib

import pytest

from chahua import admin
from chahua.agent_run import create as create_agent_run
from chahua.cursor import GuestCursor
from chahua.events import ChahuaEnvelope, ChahuaEventType
from chahua.handoff import HandoffItem, HandoffKind
from chahua.orchestrator import Orchestrator, OrchestratorConfig
from chahua.room import Room
from chahua.server import (
    ChahuaServer,
    INBOUND_HANDOFF_DELEGATE,
    INBOUND_HANDOFF_PANEL,
    INBOUND_HANDOFF_REVIEW,
    _bind_inbound_handlers,
    _install_handler_slots,
)
from chahua.session import build_room_session
from chahua.user_md import USER_SPEAKER_ID, UserConfig

from conftest import NoopScorer, NoopSummarizer, SpeakingStubGuest


_PERSONAS = ["宝总", "范总", "玲子", "汪小姐", "爷叔"]


@pytest.fixture
def session_and_srv(env_paths):
    rc = admin.create_room(
        paths=env_paths, room_id="t1", name="t1",
        guests=[
            {"persona": f"chahua/personas/{n}/{n}.md", "name": n}
            for n in _PERSONAS
        ],
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


def _by_type(captured: list[dict], t: str) -> list[dict]:
    return [e for e in captured if e["type"] == t]


def _assert_rejected_with(captured, queue, *, contains: str) -> None:
    notices = _by_type(captured, ChahuaEventType.NOTICE.value)
    assert notices and notices[0]["data"]["level"] == "error"
    assert contains in notices[0]["data"]["text"]
    assert len(queue) == 0


# ── 1) handoff_delegate inbound：busy target → NOTICE error + 不入队 ──────────


def _add_bg_run(srv, name: str) -> None:
    """模拟 bg run 占用：往 ``agent_runs`` + ``active_guest_names`` 同步落一份。
    与 P11 真实 inbound 入口同口径（``_start_agent_run``：先 add(target) 再
    ``create_task``）。"""
    run = create_agent_run(room_id="t1", guest_name=name, instruction="x")
    srv._foreground_runtime.agent_runs[run.run_id] = run
    srv._foreground_runtime.active_guest_names.add(name)


async def test_delegate_busy_target_notice_error(session_and_srv) -> None:
    """target 在跑 bg run → 拒（``guest_in_bg_run`` 命中）。"""
    session, srv = session_and_srv
    _add_bg_run(srv, "宝总")

    captured: list[dict] = []
    await srv._handle_inbound(
        {"type": INBOUND_HANDOFF_DELEGATE, "target": "宝总"},
        lambda e: captured.append(e.to_dict()),
    )
    _assert_rejected_with(
        captured, session.orchestrator._handoff_queue, contains="后台任务",
    )
    assert srv._inflight_turn_task is None


# ── 2) handoff_review inbound：busy target → NOTICE error + 不入队 ──────────


async def test_review_busy_target_notice_error(session_and_srv) -> None:
    """target 在跑 bg run → 拒。"""
    session, srv = session_and_srv
    msg = session.room.append("宝总", "占位")
    _add_bg_run(srv, "宝总")

    captured: list[dict] = []
    await srv._handle_inbound(
        {
            "type": INBOUND_HANDOFF_REVIEW,
            "target": "宝总",
            "message_id": msg.message_id,
        },
        lambda e: captured.append(e.to_dict()),
    )
    _assert_rejected_with(
        captured, session.orchestrator._handoff_queue, contains="后台任务",
    )
    assert srv._inflight_turn_task is None


# ── 3) handoff_panel inbound：busy panelist / summarizer → NOTICE error + 不入队


async def test_panel_busy_panelist_notice_error(session_and_srv) -> None:
    """panelist 在跑 bg run → 拒。"""
    session, srv = session_and_srv
    _add_bg_run(srv, "范总")

    captured: list[dict] = []
    await srv._handle_inbound(
        {"type": INBOUND_HANDOFF_PANEL, "targets": ["宝总", "范总"]},
        lambda e: captured.append(e.to_dict()),
    )
    _assert_rejected_with(
        captured, session.orchestrator._handoff_queue, contains="后台任务",
    )
    assert srv._inflight_turn_task is None


async def test_panel_busy_summarizer_notice_error(session_and_srv) -> None:
    """summarizer 在跑 bg run → 拒。"""
    session, srv = session_and_srv
    _add_bg_run(srv, "玲子")

    captured: list[dict] = []
    await srv._handle_inbound(
        {
            "type": INBOUND_HANDOFF_PANEL,
            "targets": ["宝总", "范总"],
            "summarizer": "玲子",
        },
        lambda e: captured.append(e.to_dict()),
    )
    _assert_rejected_with(
        captured, session.orchestrator._handoff_queue, contains="后台任务",
    )
    assert srv._inflight_turn_task is None


# ── 3b) P8.4.9 narrow：前台 / handoff 占用不被 inbound 误拒（Codex round 3 P2）──


async def test_delegate_active_but_not_bg_passes(session_and_srv) -> None:
    """target 在 active_guest_names 但**不**在 bg run → 不拒。

    用户 turn 中的 ``_let_speak`` 也会 add；handoff 抢占设计要求这种「正在前台讲话」
    的 target 允许被 cancel + drain 抢占。
    """
    session, srv = session_and_srv
    # 只 add active 标记，不挂 bg run。
    srv._foreground_runtime.active_guest_names.add("宝总")

    captured: list[dict] = []
    await srv._handle_inbound(
        {"type": INBOUND_HANDOFF_DELEGATE, "target": "宝总"},
        lambda e: captured.append(e.to_dict()),
    )
    notices = _by_type(captured, ChahuaEventType.NOTICE.value)
    error_notices = [n for n in notices if n["data"]["level"] == "error"]
    assert error_notices == []
    assert _by_type(captured, ChahuaEventType.HANDOFF_ENQUEUED.value)
    # 收尾：把启动起来的 drain wrapper 关掉
    if srv._inflight_turn_task is not None:
        srv._inflight_turn_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await srv._inflight_turn_task


async def test_panel_all_free_still_passes(session_and_srv) -> None:
    """sanity：没人 busy 时 panel 正常入队（防 P11 C12 误伤合法路径）。"""
    session, srv = session_and_srv
    captured: list[dict] = []
    await srv._handle_inbound(
        {
            "type": INBOUND_HANDOFF_PANEL,
            "targets": ["宝总", "范总"],
            "summarizer": "玲子",
        },
        lambda e: captured.append(e.to_dict()),
    )
    assert _by_type(captured, ChahuaEventType.HANDOFF_ENQUEUED.value)
    assert len(session.orchestrator._handoff_queue) == 1
    # 收尾：把启动起来的 drain wrapper 关掉
    if srv._inflight_turn_task is not None:
        srv._inflight_turn_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await srv._inflight_turn_task


# ── 4) drain _advance_to_runnable_handoff：busy winner → drop + emit ────────


def _build_orch(*names: str, max_ai: int = 4) -> tuple[Orchestrator, Room]:
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
    )
    for n in names:
        orch.register(SpeakingStubGuest(n, room), persona_md=f"{n} persona")
    return orch, room


async def test_drain_drops_busy_delegate_and_emits_snapshot() -> None:
    """队首是 busy 的 delegate（典型：MTS auto-enqueue 后 worker 刚被 bg-spawn）
    → drain pop + WARN + 重发 HANDOFF_ENQUEUED 快照；后续非 busy 项继续消费。"""
    orch, _ = _build_orch("A", "B")
    # 模拟 runtime 装的 active_guest_names —— bare orch 默认 None，手动装一个 set
    orch.active_guest_names = {"A"}
    orch.enqueue_handoff(HandoffItem(kind=HandoffKind.DELEGATE, target="A"))
    orch.enqueue_handoff(HandoffItem(kind=HandoffKind.DELEGATE, target="B"))

    captured: list[ChahuaEnvelope] = []
    await orch.run_pending_handoff(captured.append, task_id=None)

    # A 被 drop，B 被消费
    assert len(orch._handoff_queue) == 0
    consumed = [
        e for e in captured if e.type is ChahuaEventType.HANDOFF_CONSUMED
    ]
    assert len(consumed) == 1
    assert consumed[0].data["item"]["target"] == "B"
    # drop 后 emit 了一次 HANDOFF_ENQUEUED 权威快照（队列预览不残留死项）
    snapshots = [
        e for e in captured if e.type is ChahuaEventType.HANDOFF_ENQUEUED
    ]
    assert len(snapshots) >= 1


async def test_drain_drops_panel_with_busy_panelist() -> None:
    """panel 任一 panelist busy → 整项 drop（串行 N(+1) 缺一不可，不做 partial）。"""
    orch, _ = _build_orch("A", "B", "C", max_ai=4)
    orch.active_guest_names = {"B"}
    orch.enqueue_handoff(HandoffItem(
        kind=HandoffKind.PANEL, targets=("A", "B"),
    ))
    # 再塞一条 delegate(C) 验证 drain 不被 panel drop 卡死
    orch.enqueue_handoff(HandoffItem(kind=HandoffKind.DELEGATE, target="C"))

    captured: list[ChahuaEnvelope] = []
    await orch.run_pending_handoff(captured.append, task_id=None)

    assert len(orch._handoff_queue) == 0
    consumed = [
        e for e in captured if e.type is ChahuaEventType.HANDOFF_CONSUMED
    ]
    assert len(consumed) == 1
    assert consumed[0].data["item"]["target"] == "C"


# ── 5) MTS intercept：busy target → 返 None 让 hook 降级渲采纳卡 ────────────


async def test_mts_intercept_busy_target_falls_back_to_card() -> None:
    """MTS 管理者 propose_delegate(busy_target) → _handoff_item_from_proposal 返
    None → intercept_task_proposal 返 False → 不拦截、envelope 照常下发渲采纳卡
    （busy 茶客在 inbound 采纳时再被拒，与 inbound 同口径）。"""
    from chahua._orchestrator_managed_session import (
        TASK_PROPOSAL_KIND_HANDOFF_DELEGATE,
    )

    orch, _ = _build_orch("M", "W")  # M = manager, W = worker
    orch.active_guest_names = {"W"}  # 模拟 W 正在 bg run
    orch.start_managed_session(
        lambda _e: None, task_id="t1", manager_guest="M", budget=2,
    )

    # 构造一条 manager 提议 delegate(W) 的 TASK_PROPOSAL envelope
    env = ChahuaEnvelope(
        room_id="t", turn_id="x", guest_name="M", message_id="m",
        type=ChahuaEventType.TASK_PROPOSAL,
        data={
            "proposer": "M",
            "kind": TASK_PROPOSAL_KIND_HANDOFF_DELEGATE,
            "payload": {"target": "W"},
        },
    )
    captured: list[ChahuaEnvelope] = []
    intercepted = orch._mts_ops.intercept_task_proposal(env, captured.append)

    # 关键断言：未拦截（False）→ envelope 走原路径渲采纳卡
    assert intercepted is False
    # 队列里不该多任何东西
    assert len(orch._handoff_queue) == 0


async def test_mts_intercept_busy_summarizer_falls_back_to_card() -> None:
    """panel 路径同：summarizer busy → 返 False 降级渲卡。"""
    from chahua._orchestrator_managed_session import (
        TASK_PROPOSAL_KIND_HANDOFF_PANEL,
    )

    orch, _ = _build_orch("M", "A", "B", "S", max_ai=4)
    orch.active_guest_names = {"S"}  # summarizer busy
    orch.start_managed_session(
        lambda _e: None, task_id="t1", manager_guest="M", budget=2,
    )

    env = ChahuaEnvelope(
        room_id="t", turn_id="x", guest_name="M", message_id="m",
        type=ChahuaEventType.TASK_PROPOSAL,
        data={
            "proposer": "M",
            "kind": TASK_PROPOSAL_KIND_HANDOFF_PANEL,
            "payload": {"targets": ["A", "B"], "summarizer": "S"},
        },
    )
    captured: list[ChahuaEnvelope] = []
    intercepted = orch._mts_ops.intercept_task_proposal(env, captured.append)

    assert intercepted is False
    assert len(orch._handoff_queue) == 0
