"""``ChahuaServer._run_handoff_turn`` wrapper（P7.1.7，docs/P7 §3.4 反向评审 v3-#3）。

P7.1.6 已经把 wrapper 落进 server.py（inbound handler 引用它）；P7.1.7 单独
回归三条 wrapper 生命周期合约：

- 正常跑完：两槽 ``_inflight_turn_task`` / ``_inflight_kind`` 都被 finally 清。
- 中途 cancel：``swallow``——不 reraise（与 ``_run_turn`` 同口径，避免 ws task
  以 cancelled 状态冒出去触发 "Task exception was never retrieved" warning）+
  两槽都清。
- 内部异常：``swallow + log.exception`` + 两槽都清（同上口径）。

wrapper 内部不补 cancel fixup（``turn_end(cancelled)``）——那是 orchestrator
``run_pending_handoff`` 的 speak 段 try/except 负责（已在 P7.1.5 测过）。
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from chahua import admin
from chahua.orchestrator import Orchestrator
from chahua.server import (
    ChahuaServer,
    INFLIGHT_KIND_HANDOFF,
    _bind_inbound_handlers,
    _install_handler_slots,
)
from chahua.session import build_room_session


@pytest.fixture
def srv(env_paths):
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
    yield srv
    session.close()


async def test_run_handoff_turn_normal_completion_clears_both_slots(
    srv, monkeypatch,
) -> None:
    async def _noop_drain(_self, sink, *, task_id):
        return None

    monkeypatch.setattr(Orchestrator, "run_pending_handoff", _noop_drain)
    task = asyncio.create_task(
        srv._run_handoff_turn(lambda _e: None, task_id=None),
    )
    srv._set_inflight(task, INFLIGHT_KIND_HANDOFF)
    await task
    assert srv._inflight_turn_task is None
    assert srv._inflight_kind is None


async def test_run_handoff_turn_cancel_swallow_clears_both_slots(
    srv, monkeypatch,
) -> None:
    """cancel 中途——wrapper 应 swallow CancelledError 让 task 正常完成 +
    finally 清两槽。"""
    gate = asyncio.Event()

    async def _block_drain(_self, sink, *, task_id):
        gate.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(Orchestrator, "run_pending_handoff", _block_drain)

    task = asyncio.create_task(
        srv._run_handoff_turn(lambda _e: None, task_id=None),
    )
    srv._set_inflight(task, INFLIGHT_KIND_HANDOFF)

    await asyncio.wait_for(gate.wait(), timeout=2.0)
    task.cancel()
    # **关键**：cancel 不应 reraise——wrapper 走 except CancelledError swallow
    # ＋ finally 清两槽。task 正常 done（cancelled=False）。
    await task
    assert task.done()
    assert not task.cancelled()
    assert srv._inflight_turn_task is None
    assert srv._inflight_kind is None


async def test_run_handoff_turn_exception_swallow_clears_both_slots(
    srv, monkeypatch, caplog,
) -> None:
    """drain 内部抛异常——wrapper ``log.exception`` + swallow + 清两槽。
    避免 "Task exception was never retrieved" warning（与 ``_run_turn`` 同口径）。"""

    async def _boom_drain(_self, sink, *, task_id):
        raise RuntimeError("boom-from-drain")

    monkeypatch.setattr(Orchestrator, "run_pending_handoff", _boom_drain)

    caplog.set_level(logging.ERROR)
    task = asyncio.create_task(
        srv._run_handoff_turn(lambda _e: None, task_id=None),
    )
    srv._set_inflight(task, INFLIGHT_KIND_HANDOFF)

    await task
    assert task.done()
    assert task.exception() is None
    assert any("handoff drain crashed" in r.message for r in caplog.records)
    assert srv._inflight_turn_task is None
    assert srv._inflight_kind is None
