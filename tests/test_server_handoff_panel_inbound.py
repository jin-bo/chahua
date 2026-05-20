"""P7.3.4 handoff_panel inbound handler 协议 + 副作用（docs/P7.3 §3.2 / §3.3）。

覆盖：
- 未知字段 → NOTICE error 不入队。
- targets < 2 / 含重复 / 含不在场茶客 → NOTICE error 不入队。
- summarizer 在 targets 里 / 不在场 → NOTICE error 不入队。
- cap 数学：len(targets) > min(MAX_PANEL_TARGETS, max-has_summ) → NOTICE error。
- 合法 panel → HANDOFF_ENQUEUED（队列含一条 panel item，带 targets + summarizer）
  + 启动 wrapper。
- handoff drain 跑期间再 panel → append 队尾、不抢占。
- user-turn 跑期间 panel → cancel user-turn + 启 handoff drain。
"""

from __future__ import annotations

import asyncio
import contextlib

import pytest

from chahua import admin
from chahua.events import ChahuaEventType
from chahua.handoff import MAX_PANEL_TARGETS, HandoffKind
from chahua.orchestrator import Orchestrator
from chahua.server import (
    ChahuaServer,
    INBOUND_HANDOFF_PANEL,
    INFLIGHT_KIND_HANDOFF,
    INFLIGHT_KIND_USER,
    _bind_inbound_handlers,
    _install_handler_slots,
)
from chahua.session import build_room_session

_PERSONAS = ["宝总", "范总", "玲子", "汪小姐", "爷叔"]


@pytest.fixture
def session_and_srv(env_paths):
    rc = admin.create_room(
        paths=env_paths, room_id="t1", name="t1",
        guests=[
            {"persona": f"chahua/personas/{n}.md", "name": n}
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


def _panel_frame(targets, summarizer=None) -> dict:
    f = {"type": INBOUND_HANDOFF_PANEL, "targets": list(targets)}
    if summarizer is not None:
        f["summarizer"] = summarizer
    return f


async def _send(srv, frame) -> list[dict]:
    captured: list[dict] = []
    await srv._handle_inbound(frame, lambda e: captured.append(e.to_dict()))
    return captured


def _assert_rejected(captured, queue) -> None:
    notices = _by_type(captured, ChahuaEventType.NOTICE.value)
    assert notices and notices[0]["data"]["level"] == "error"
    assert len(queue) == 0


# ── 协议层 ───────────────────────────────────────────────────────────────────


async def test_panel_unknown_field_notice_error(session_and_srv) -> None:
    session, srv = session_and_srv
    captured = await _send(
        srv, {**_panel_frame(["宝总", "范总"]), "extra": "noise"},
    )
    _assert_rejected(captured, session.orchestrator._handoff_queue)
    assert srv._inflight_turn_task is None


@pytest.mark.parametrize(
    "targets",
    [
        ["宝总"],            # < 2
        [],                  # 空
        "宝总范总",          # 非 list
        ["宝总", ""],        # 含空串
        ["宝总", 42],        # 含非 str
    ],
)
async def test_panel_targets_malformed_notice_error(
    session_and_srv, targets,
) -> None:
    session, srv = session_and_srv
    captured = await _send(srv, {"type": INBOUND_HANDOFF_PANEL, "targets": targets})
    _assert_rejected(captured, session.orchestrator._handoff_queue)


async def test_panel_duplicate_targets_notice_error(session_and_srv) -> None:
    session, srv = session_and_srv
    captured = await _send(srv, _panel_frame(["宝总", "宝总", "范总"]))
    _assert_rejected(captured, session.orchestrator._handoff_queue)


async def test_panel_target_not_in_room_notice_error(session_and_srv) -> None:
    session, srv = session_and_srv
    captured = await _send(srv, _panel_frame(["宝总", "查无此人"]))
    _assert_rejected(captured, session.orchestrator._handoff_queue)


async def test_panel_summarizer_in_targets_notice_error(session_and_srv) -> None:
    session, srv = session_and_srv
    captured = await _send(
        srv, _panel_frame(["宝总", "范总"], summarizer="宝总"),
    )
    _assert_rejected(captured, session.orchestrator._handoff_queue)


async def test_panel_summarizer_not_in_room_notice_error(session_and_srv) -> None:
    session, srv = session_and_srv
    captured = await _send(
        srv, _panel_frame(["宝总", "范总"], summarizer="查无此人"),
    )
    _assert_rejected(captured, session.orchestrator._handoff_queue)


async def test_panel_cap_math_rejects_oversized_panel(session_and_srv) -> None:
    """cap 数学：max_consecutive_ai_turns 调小到 3、带 summarizer 时
    cap = min(4, 3-1) = 2，3 人 panel 被拒（docs §3.3 第 ⑤ 道）。"""
    session, srv = session_and_srv
    session.orchestrator.config = session.orchestrator.config.__class__(
        max_consecutive_ai_turns=3,
        max_speakers_per_pick=session.orchestrator.config.max_speakers_per_pick,
        speaker_cooldown_turns=session.orchestrator.config.speaker_cooldown_turns,
        summary_block_size=session.orchestrator.config.summary_block_size,
    )
    captured = await _send(
        srv, _panel_frame(["宝总", "范总", "玲子"], summarizer="汪小姐"),
    )
    _assert_rejected(captured, session.orchestrator._handoff_queue)


async def test_panel_cap_math_allows_exactly_at_cap(session_and_srv) -> None:
    """cap 边界：max=4、无 summarizer → cap=min(4,4)=4，4 人 panel 恰好放行。"""
    session, srv = session_and_srv
    captured = await _send(srv, _panel_frame(["宝总", "范总", "玲子", "汪小姐"]))
    assert _by_type(captured, ChahuaEventType.HANDOFF_ENQUEUED.value)
    assert MAX_PANEL_TARGETS == 4
    # 清理启动的 wrapper
    if srv._inflight_turn_task is not None:
        srv._inflight_turn_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await srv._inflight_turn_task


# ── 合法 panel + 启动 wrapper ───────────────────────────────────────────────


async def test_panel_legal_emits_enqueued_and_starts_wrapper(
    session_and_srv,
) -> None:
    session, srv = session_and_srv
    captured = await _send(
        srv, _panel_frame(["宝总", "范总", "玲子"], summarizer="汪小姐"),
    )
    enq = _by_type(captured, ChahuaEventType.HANDOFF_ENQUEUED.value)
    assert len(enq) == 1
    item = enq[0]["data"]["queue"][0]
    assert item["kind"] == "panel"
    assert item["targets"] == ["宝总", "范总", "玲子"]
    assert item["summarizer"] == "汪小姐"
    assert item["target"] is None

    assert srv._inflight_kind == INFLIGHT_KIND_HANDOFF
    assert srv._inflight_turn_task is not None

    try:
        await asyncio.wait_for(srv._inflight_turn_task, timeout=5.0)
    except asyncio.TimeoutError:
        srv._inflight_turn_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await srv._inflight_turn_task


async def test_panel_without_summarizer_enqueues(session_and_srv) -> None:
    session, srv = session_and_srv
    captured = await _send(srv, _panel_frame(["宝总", "范总"]))
    enq = _by_type(captured, ChahuaEventType.HANDOFF_ENQUEUED.value)
    assert len(enq) == 1
    assert enq[0]["data"]["queue"][0]["summarizer"] is None

    if srv._inflight_turn_task is not None:
        srv._inflight_turn_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await srv._inflight_turn_task


# ── 抢占 / 启动 ──────────────────────────────────────────────────────────────


async def test_panel_during_handoff_drain_appends_only(session_and_srv) -> None:
    session, srv = session_and_srv
    fake_drain = asyncio.create_task(asyncio.sleep(3600))
    srv._set_inflight(fake_drain, INFLIGHT_KIND_HANDOFF)
    try:
        captured = await _send(srv, _panel_frame(["宝总", "范总"]))
        assert len(session.orchestrator._handoff_queue) == 1
        assert session.orchestrator._handoff_queue[0].kind is HandoffKind.PANEL
        assert _by_type(captured, ChahuaEventType.HANDOFF_ENQUEUED.value)
        # in-flight 没被替换
        assert srv._inflight_turn_task is fake_drain
        assert srv._inflight_kind == INFLIGHT_KIND_HANDOFF
    finally:
        fake_drain.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await fake_drain


async def test_panel_during_user_turn_cancels_and_starts_drain(
    session_and_srv, monkeypatch,
) -> None:
    """user-turn 跑期间 panel → cancel user-turn + 启动 handoff drain。

    用真 ``_run_turn`` wrapper 当 in-flight user-turn（finally 清 slot 才让
    抢占后的 ``_inflight_turn_task is None`` 判定成立）；monkeypatch 把 AI 链 /
    drain 都换成阻塞器，避免真打 LLM。"""
    session, srv = session_and_srv

    user_turn_started = asyncio.Event()
    drain_started = asyncio.Event()

    async def _block_user(self, text, *, sink, task_id):
        user_turn_started.set()
        await asyncio.Event().wait()

    async def _block_drain(self, sink, *, task_id):
        drain_started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(Orchestrator, "submit_user_message", _block_user)
    monkeypatch.setattr(Orchestrator, "run_pending_handoff", _block_drain)

    user_turn = asyncio.create_task(
        srv._run_turn("你好", lambda _e: None, task_id=None),
    )
    srv._set_inflight(user_turn, INFLIGHT_KIND_USER)
    await asyncio.wait_for(user_turn_started.wait(), timeout=2.0)

    captured = await _send(srv, _panel_frame(["宝总", "范总"]))

    # user-turn 被 cancel、drain wrapper 启动
    assert user_turn.done()
    assert srv._inflight_kind == INFLIGHT_KIND_HANDOFF
    assert srv._inflight_turn_task is not None
    assert srv._inflight_turn_task is not user_turn
    assert _by_type(captured, ChahuaEventType.HANDOFF_ENQUEUED.value)
    assert len(session.orchestrator._handoff_queue) == 1
    await asyncio.wait_for(drain_started.wait(), timeout=2.0)

    srv._inflight_turn_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await srv._inflight_turn_task
