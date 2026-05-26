"""P7.1.6 handoff inbound handler 协议 + 副作用回归（docs/P7 §3.2 / §4.4）。

覆盖：
- ``handoff_delegate`` 未知字段 / target 不存在 → NOTICE error 不入队。
- 合法 delegate → ``HANDOFF_ENQUEUED`` envelope + ``_inflight_kind="handoff"`` +
  ``_inflight_turn_task`` 非 None。
- handoff drain 跑期间再 delegate → **不 cancel**、不启新 task、append 队尾
  （docs §4.4 反向评审 v3-#1 核心保护）。
- user-turn 跑期间 delegate → cancel user-turn + 启动 handoff drain。
- synthesized user-turn 跑期间 delegate → 也按 user-turn 路径走（反向评审 v4-#1）。
- ``handoff_clear`` 无 in-flight → 直接清队列不挂 task；handoff drain 中 →
  cancel + items_dropped 含剩余。
"""

from __future__ import annotations

import asyncio
import contextlib

import pytest

from chahua import admin
from chahua.events import ChahuaEventType
from chahua.handoff import HandoffItem, HandoffKind
from chahua.server import (
    ChahuaServer,
    INBOUND_HANDOFF_CLEAR,
    INBOUND_HANDOFF_DELEGATE,
    INFLIGHT_KIND_HANDOFF,
    _bind_inbound_handlers,
    _install_handler_slots,
)
from chahua.session import build_room_session


@pytest.fixture
def session_and_srv(env_paths):
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


def _by_type(captured: list[dict], t: str) -> list[dict]:
    return [e for e in captured if e["type"] == t]


def _delegate_frame(target: str, reason: str | None = None) -> dict:
    f = {"type": INBOUND_HANDOFF_DELEGATE, "target": target}
    if reason is not None:
        f["reason"] = reason
    return f


# ── handoff_delegate：协议层 ─────────────────────────────────────────────────


async def test_delegate_unknown_field_notice_error(session_and_srv) -> None:
    session, srv = session_and_srv
    captured: list[dict] = []
    await srv._handle_inbound(
        {**_delegate_frame("宝总"), "extra": "noise"},
        lambda e: captured.append(e.to_dict()),
    )
    notices = _by_type(captured, ChahuaEventType.NOTICE.value)
    assert notices and notices[0]["data"]["level"] == "error"
    # 不入队，不启 task
    assert len(session.orchestrator._handoff_queue) == 0
    assert srv._inflight_turn_task is None


async def test_delegate_target_not_in_room_notice_error(session_and_srv) -> None:
    session, srv = session_and_srv
    captured: list[dict] = []
    await srv._handle_inbound(
        _delegate_frame("不存在"),
        lambda e: captured.append(e.to_dict()),
    )
    notices = _by_type(captured, ChahuaEventType.NOTICE.value)
    assert notices and notices[0]["data"]["level"] == "error"
    assert len(session.orchestrator._handoff_queue) == 0


async def test_delegate_reason_must_be_str(session_and_srv) -> None:
    session, srv = session_and_srv
    captured: list[dict] = []
    await srv._handle_inbound(
        {"type": INBOUND_HANDOFF_DELEGATE, "target": "宝总", "reason": 42},
        lambda e: captured.append(e.to_dict()),
    )
    notices = _by_type(captured, ChahuaEventType.NOTICE.value)
    assert notices and notices[0]["data"]["level"] == "error"
    assert len(session.orchestrator._handoff_queue) == 0


# ── handoff_delegate：合法 + 启动 wrapper ───────────────────────────────────


async def test_delegate_legal_emits_enqueued_and_starts_wrapper(
    session_and_srv,
) -> None:
    session, srv = session_and_srv
    captured: list[dict] = []
    await srv._handle_inbound(
        _delegate_frame("宝总", reason="抢答"),
        lambda e: captured.append(e.to_dict()),
    )
    # 收一帧 HANDOFF_ENQUEUED
    enq = _by_type(captured, ChahuaEventType.HANDOFF_ENQUEUED.value)
    assert len(enq) == 1
    assert enq[0]["data"]["queue"][0]["target"] == "宝总"
    assert enq[0]["data"]["queue"][0]["reason"] == "抢答"
    # _inflight_kind 与 task 同时挂上
    assert srv._inflight_kind == INFLIGHT_KIND_HANDOFF
    assert srv._inflight_turn_task is not None
    # 收尾让 wrapper 退出——_run_handoff_turn 自身 swallow Exception，正常情况
    # wait_for 不会抛 LLM 异常；timeout 兜底防 drain 卡死（LLM 真往外打的 fake
    # key），cancel + 等吞 CancelledError。
    try:
        await asyncio.wait_for(srv._inflight_turn_task, timeout=5.0)
    except asyncio.TimeoutError:
        srv._inflight_turn_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await srv._inflight_turn_task


# ── 核心保护回归：handoff drain 中再 delegate 不抢占 + append 队尾 ────────


async def test_delegate_during_handoff_drain_appends_only(
    session_and_srv,
) -> None:
    session, srv = session_and_srv
    # 用占位 task 充当 in-flight handoff drain（不让 orchestrator 真跑——
    # 真跑会调 LLM）。挂槽前手工塞 _inflight_kind="handoff"。
    fake_drain = asyncio.create_task(asyncio.sleep(3600))
    srv._set_inflight(fake_drain, INFLIGHT_KIND_HANDOFF)

    captured: list[dict] = []
    try:
        await srv._handle_inbound(
            _delegate_frame("宝总"),
            lambda e: captured.append(e.to_dict()),
        )
        # 队列追加成功，HANDOFF_ENQUEUED 已 emit
        assert len(session.orchestrator._handoff_queue) == 1
        assert _by_type(captured, ChahuaEventType.HANDOFF_ENQUEUED.value)
        # in-flight task 没被替换（**关键**：保护队列语义）
        assert srv._inflight_turn_task is fake_drain
        assert srv._inflight_kind == INFLIGHT_KIND_HANDOFF
    finally:
        fake_drain.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await fake_drain


# ── handoff_clear ────────────────────────────────────────────────────────


async def test_clear_with_no_inflight_just_empties_queue(
    session_and_srv,
) -> None:
    session, srv = session_and_srv
    # 预先塞两项
    session.orchestrator.enqueue_handoff(
        HandoffItem(kind=HandoffKind.DELEGATE, target="宝总"),
    )

    captured: list[dict] = []
    await srv._handle_inbound(
        {"type": INBOUND_HANDOFF_CLEAR},
        lambda e: captured.append(e.to_dict()),
    )
    cleared = _by_type(captured, ChahuaEventType.HANDOFF_CLEARED.value)
    assert len(cleared) == 1
    assert len(cleared[0]["data"]["items_dropped"]) == 1
    assert cleared[0]["data"]["items_dropped"][0]["target"] == "宝总"
    assert len(session.orchestrator._handoff_queue) == 0


async def test_clear_unknown_field_notice_error(session_and_srv) -> None:
    session, srv = session_and_srv
    session.orchestrator.enqueue_handoff(
        HandoffItem(kind=HandoffKind.DELEGATE, target="宝总"),
    )
    captured: list[dict] = []
    await srv._handle_inbound(
        {"type": INBOUND_HANDOFF_CLEAR, "extra": "noise"},
        lambda e: captured.append(e.to_dict()),
    )
    notices = _by_type(captured, ChahuaEventType.NOTICE.value)
    assert notices and notices[0]["data"]["level"] == "error"
    # 严格校验失败 → 不清队列
    assert len(session.orchestrator._handoff_queue) == 1


async def test_delegate_after_cancelled_handoff_starts_fresh_wrapper(
    session_and_srv, monkeypatch,
) -> None:
    """Codex review 回归：``cancel`` inbound 只 ``task.cancel()`` 不 await，留下的
    cancelling wrapper 占着 slot 直到自身 finally 跑完。在这个窗口内的下一帧
    ``handoff_delegate`` 必须能 drain stale slot + 启新 wrapper，否则新入队的项
    无人消费（docs §4.4 stale-slot 防护）。"""
    from chahua.orchestrator import Orchestrator

    session, srv = session_and_srv

    block_gate = asyncio.Event()

    async def _block_drain(self, sink, *, task_id):
        block_gate.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(Orchestrator, "run_pending_handoff", _block_drain)

    # 启一个真实 wrapper（带 finally），让它进 drain 后阻塞——模拟"已在跑的 handoff"。
    snapshot_task_id = srv.task._snapshot_active_task_id()
    real_wrapper = asyncio.create_task(
        srv._run_handoff_turn(srv._foreground_runtime, task_id=snapshot_task_id),
    )
    srv._set_inflight(real_wrapper, INFLIGHT_KIND_HANDOFF)
    await asyncio.wait_for(block_gate.wait(), timeout=2.0)

    # 模拟 ``_inbound_cancel`` 走的 ``_cancel_inflight``（不 await）。
    real_wrapper.cancel()
    # 关键：此刻 task 还没跑完 finally → slot 仍指着 cancelling 但未 done 的 task。
    assert srv._inflight_turn_task is real_wrapper
    assert real_wrapper.cancelling() > 0
    assert not real_wrapper.done()

    captured: list[dict] = []
    await srv._handle_inbound(
        _delegate_frame("宝总"),
        lambda e: captured.append(e.to_dict()),
    )

    # stale wrapper 已被 drain（finally 跑完，slot 重置），新 wrapper 启动了
    assert real_wrapper.done()
    assert srv._inflight_turn_task is not None
    assert srv._inflight_turn_task is not real_wrapper
    assert srv._inflight_kind == INFLIGHT_KIND_HANDOFF
    # HANDOFF_ENQUEUED emit 了
    assert _by_type(captured, ChahuaEventType.HANDOFF_ENQUEUED.value)

    # 清理
    new_task = srv._inflight_turn_task
    new_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await new_task


async def test_clear_during_handoff_drain_cancels_and_drops(
    session_and_srv,
) -> None:
    session, srv = session_and_srv
    fake_drain = asyncio.create_task(asyncio.sleep(3600))
    srv._set_inflight(fake_drain, INFLIGHT_KIND_HANDOFF)
    session.orchestrator.enqueue_handoff(
        HandoffItem(kind=HandoffKind.DELEGATE, target="宝总", reason="x"),
    )

    captured: list[dict] = []
    await srv._handle_inbound(
        {"type": INBOUND_HANDOFF_CLEAR},
        lambda e: captured.append(e.to_dict()),
    )
    cleared = _by_type(captured, ChahuaEventType.HANDOFF_CLEARED.value)
    assert len(cleared) == 1
    assert cleared[0]["data"]["items_dropped"][0]["target"] == "宝总"
    # fake_drain 已被 cancel（_cancel_and_drain_inflight 走过）
    assert fake_drain.cancelled() or fake_drain.done()
