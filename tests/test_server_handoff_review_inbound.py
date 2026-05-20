"""P7.2.6 handoff_review inbound handler 协议 + 副作用（docs/P7.2 §3.2 / §3.4）。

覆盖：
- 未知字段 → NOTICE error 不入队。
- target 不在场 → NOTICE error 不入队。
- message_id 不存在 → NOTICE error 不入队。
- 合法 review → HANDOFF_ENQUEUED envelope（队列项 kind=review + review_message_id）
  + _inflight_kind="handoff" + 启动 wrapper。
- handoff drain 跑期间再 review → append 队尾、不抢占（与 delegate 同口径）。
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
    INBOUND_HANDOFF_REVIEW,
    INFLIGHT_KIND_HANDOFF,
    _bind_inbound_handlers,
    _install_handler_slots,
)
from chahua.session import build_room_session


@pytest.fixture
def session_and_srv(env_paths):
    rc = admin.create_room(
        paths=env_paths, room_id="t1", name="t1",
        guests=[{"persona": "chahua/personas/宝总.md", "name": "宝总"}],
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


def _seed_message(session) -> str:
    """落一条茶客发言到 transcript，返回其 message_id（被审对象）。"""
    msg = session.room.append("宝总", "我先把方案写出来。")
    return msg.message_id


def _review_frame(target: str, message_id: str) -> dict:
    return {
        "type": INBOUND_HANDOFF_REVIEW,
        "target": target,
        "message_id": message_id,
    }


# ── 协议层 ───────────────────────────────────────────────────────────────────


async def test_review_unknown_field_notice_error(session_and_srv) -> None:
    session, srv = session_and_srv
    mid = _seed_message(session)
    captured: list[dict] = []
    await srv._handle_inbound(
        {**_review_frame("宝总", mid), "extra": "noise"},
        lambda e: captured.append(e.to_dict()),
    )
    notices = _by_type(captured, ChahuaEventType.NOTICE.value)
    assert notices and notices[0]["data"]["level"] == "error"
    assert len(session.orchestrator._handoff_queue) == 0
    assert srv._inflight_turn_task is None


@pytest.mark.parametrize("bad", [None, "", 42])
async def test_review_target_malformed_notice_error(session_and_srv, bad) -> None:
    """target 缺省 / 空串 / 非 str → NOTICE error（docs/P7.2 §3.2 三道校验）。"""
    session, srv = session_and_srv
    mid = _seed_message(session)
    frame = {"type": INBOUND_HANDOFF_REVIEW, "message_id": mid}
    if bad is not None:
        frame["target"] = bad
    captured: list[dict] = []
    await srv._handle_inbound(frame, lambda e: captured.append(e.to_dict()))
    notices = _by_type(captured, ChahuaEventType.NOTICE.value)
    assert notices and notices[0]["data"]["level"] == "error"
    assert len(session.orchestrator._handoff_queue) == 0


async def test_review_target_not_in_room_notice_error(session_and_srv) -> None:
    session, srv = session_and_srv
    mid = _seed_message(session)
    captured: list[dict] = []
    await srv._handle_inbound(
        _review_frame("不存在", mid),
        lambda e: captured.append(e.to_dict()),
    )
    notices = _by_type(captured, ChahuaEventType.NOTICE.value)
    assert notices and notices[0]["data"]["level"] == "error"
    assert len(session.orchestrator._handoff_queue) == 0


async def test_review_message_id_not_found_notice_error(session_and_srv) -> None:
    session, srv = session_and_srv
    captured: list[dict] = []
    await srv._handle_inbound(
        _review_frame("宝总", "msg_does_not_exist"),
        lambda e: captured.append(e.to_dict()),
    )
    notices = _by_type(captured, ChahuaEventType.NOTICE.value)
    assert notices and notices[0]["data"]["level"] == "error"
    assert len(session.orchestrator._handoff_queue) == 0


@pytest.mark.parametrize("bad", [None, "", 42])
async def test_review_message_id_malformed_notice_error(
    session_and_srv, bad,
) -> None:
    """message_id 缺省 / 空串 / 非 str → NOTICE error（docs/P7.2 §3.2 三道校验）。"""
    session, srv = session_and_srv
    frame = {"type": INBOUND_HANDOFF_REVIEW, "target": "宝总"}
    if bad is not None:
        frame["message_id"] = bad
    captured: list[dict] = []
    await srv._handle_inbound(frame, lambda e: captured.append(e.to_dict()))
    notices = _by_type(captured, ChahuaEventType.NOTICE.value)
    assert notices and notices[0]["data"]["level"] == "error"
    assert len(session.orchestrator._handoff_queue) == 0


# ── 合法 review + 启动 wrapper ──────────────────────────────────────────────


async def test_review_legal_emits_enqueued_and_starts_wrapper(
    session_and_srv,
) -> None:
    session, srv = session_and_srv
    mid = _seed_message(session)
    captured: list[dict] = []
    await srv._handle_inbound(
        _review_frame("宝总", mid),
        lambda e: captured.append(e.to_dict()),
    )
    enq = _by_type(captured, ChahuaEventType.HANDOFF_ENQUEUED.value)
    assert len(enq) == 1
    item = enq[0]["data"]["queue"][0]
    assert item["kind"] == "review"
    assert item["target"] == "宝总"
    assert item["review_message_id"] == mid
    assert item["reason"] is None

    assert srv._inflight_kind == INFLIGHT_KIND_HANDOFF
    assert srv._inflight_turn_task is not None

    try:
        await asyncio.wait_for(srv._inflight_turn_task, timeout=5.0)
    except asyncio.TimeoutError:
        srv._inflight_turn_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await srv._inflight_turn_task


# ── handoff drain 中再 review → append 队尾不抢占 ───────────────────────────


async def test_review_during_handoff_drain_appends_only(session_and_srv) -> None:
    session, srv = session_and_srv
    mid = _seed_message(session)
    fake_drain = asyncio.create_task(asyncio.sleep(3600))
    srv._set_inflight(fake_drain, INFLIGHT_KIND_HANDOFF)

    captured: list[dict] = []
    try:
        await srv._handle_inbound(
            _review_frame("宝总", mid),
            lambda e: captured.append(e.to_dict()),
        )
        assert len(session.orchestrator._handoff_queue) == 1
        assert session.orchestrator._handoff_queue[0].kind is HandoffKind.REVIEW
        assert _by_type(captured, ChahuaEventType.HANDOFF_ENQUEUED.value)
        # in-flight 没被替换
        assert srv._inflight_turn_task is fake_drain
        assert srv._inflight_kind == INFLIGHT_KIND_HANDOFF
    finally:
        fake_drain.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await fake_drain


async def test_review_clears_with_handoff_clear(session_and_srv) -> None:
    """review 项进队后 handoff_clear 能把它列进 items_dropped（envelope 复用）。"""
    session, srv = session_and_srv
    session.orchestrator.enqueue_handoff(HandoffItem(
        kind=HandoffKind.REVIEW, target="宝总", review_message_id="msg_x",
    ))
    captured: list[dict] = []
    await srv._handle_inbound(
        {"type": "handoff_clear"},
        lambda e: captured.append(e.to_dict()),
    )
    cleared = _by_type(captured, ChahuaEventType.HANDOFF_CLEARED.value)
    assert len(cleared) == 1
    assert cleared[0]["data"]["items_dropped"][0]["kind"] == "review"
