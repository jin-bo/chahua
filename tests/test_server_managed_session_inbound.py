"""P8.3 ``managed_session_start`` / ``managed_session_stop`` inbound 回归
（docs/P8.3-原生自动推进.md §3.2 / §3.3）。

覆盖 ``start`` 四道校验各一条 + happy path；``stop`` 清队列 + 无 MTS 空操作。
"""

from __future__ import annotations

import pytest

from chahua import admin
from chahua.events import ChahuaEventType
from chahua.handoff import HandoffItem, HandoffKind, ManagedSession
from chahua.orchestrator import Orchestrator
from chahua.server import (
    ChahuaServer,
    INBOUND_MANAGED_SESSION_START,
    INBOUND_MANAGED_SESSION_STOP,
    _bind_inbound_handlers,
    _install_handler_slots,
)
from chahua.session import build_room_session


@pytest.fixture
def session_and_srv(env_paths, monkeypatch):
    # MTS 校验通过会经 _enqueue_handoff_and_maybe_start 启 drain —— monkeypatch
    # run_pending_handoff 成 noop，避免测试真跑 LLM。
    async def _noop_drain(self, sink, *, task_id):
        return None

    monkeypatch.setattr(Orchestrator, "run_pending_handoff", _noop_drain)

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


def _open_task(session) -> str:
    task = session.tasks_store.open_task(title="重构", goal="把模块拆开")
    return task.id


async def _send(srv, frame):
    captured: list[dict] = []
    await srv._handle_inbound(frame, lambda e: captured.append(e.to_dict()))
    return captured


# ── managed_session_start：校验 ───────────────────────────────────────────


async def test_start_unknown_field_notice_error(session_and_srv) -> None:
    session, srv = session_and_srv
    tid = _open_task(session)
    cap = await _send(srv, {
        "type": INBOUND_MANAGED_SESSION_START, "task_id": tid,
        "manager_guest": "宝总", "budget": 6, "bogus": 1,
    })
    assert _by_type(cap, ChahuaEventType.NOTICE.value)
    assert srv._session.orchestrator.managed_session is None


async def test_start_task_id_not_active(session_and_srv) -> None:
    session, srv = session_and_srv
    _open_task(session)
    cap = await _send(srv, {
        "type": INBOUND_MANAGED_SESSION_START, "task_id": "task_bogus",
        "manager_guest": "宝总", "budget": 6,
    })
    notices = _by_type(cap, ChahuaEventType.NOTICE.value)
    assert notices and notices[0]["data"]["level"] == "error"
    assert srv._session.orchestrator.managed_session is None


async def test_start_manager_not_present(session_and_srv) -> None:
    session, srv = session_and_srv
    tid = _open_task(session)
    cap = await _send(srv, {
        "type": INBOUND_MANAGED_SESSION_START, "task_id": tid,
        "manager_guest": "查无此人", "budget": 6,
    })
    notices = _by_type(cap, ChahuaEventType.NOTICE.value)
    assert notices and notices[0]["data"]["level"] == "error"
    assert srv._session.orchestrator.managed_session is None


@pytest.mark.parametrize("budget", [0, 21, "6", 1.5, True])
async def test_start_budget_out_of_range(session_and_srv, budget) -> None:
    session, srv = session_and_srv
    tid = _open_task(session)
    cap = await _send(srv, {
        "type": INBOUND_MANAGED_SESSION_START, "task_id": tid,
        "manager_guest": "宝总", "budget": budget,
    })
    notices = _by_type(cap, ChahuaEventType.NOTICE.value)
    assert notices and notices[0]["data"]["level"] == "error"
    assert srv._session.orchestrator.managed_session is None


async def test_start_already_running_rejected(session_and_srv) -> None:
    session, srv = session_and_srv
    tid = _open_task(session)
    orch = srv._session.orchestrator
    orch._managed_session = ManagedSession(
        task_id=tid, manager_guest="宝总", budget=3,
    )
    cap = await _send(srv, {
        "type": INBOUND_MANAGED_SESSION_START, "task_id": tid,
        "manager_guest": "宝总", "budget": 6,
    })
    notices = _by_type(cap, ChahuaEventType.NOTICE.value)
    assert notices and notices[0]["data"]["level"] == "error"
    # 旧 MTS 不被替换、budget 不变。
    assert orch.managed_session.budget == 3


async def test_start_rejected_when_handoff_pending(session_and_srv) -> None:
    """调度起点不干净（队列里有手动 delegate）→ 拒绝开启 MTS（Codex review P2）。
    否则既有 handoff 跑完会被 _advance 误当 MTS worker 计入预算。"""
    session, srv = session_and_srv
    tid = _open_task(session)
    orch = srv._session.orchestrator
    orch.enqueue_handoff(
        HandoffItem(kind=HandoffKind.DELEGATE, target="宝总", reason="手动指派")
    )
    cap = await _send(srv, {
        "type": INBOUND_MANAGED_SESSION_START, "task_id": tid,
        "manager_guest": "宝总", "budget": 6,
    })
    notices = _by_type(cap, ChahuaEventType.NOTICE.value)
    assert notices and notices[0]["data"]["level"] == "error"
    assert orch.managed_session is None


async def test_start_happy_path(session_and_srv) -> None:
    session, srv = session_and_srv
    tid = _open_task(session)
    cap = await _send(srv, {
        "type": INBOUND_MANAGED_SESSION_START, "task_id": tid,
        "manager_guest": "宝总", "budget": 5,
    })
    orch = srv._session.orchestrator
    assert orch.managed_session is not None
    assert orch.managed_session.manager_guest == "宝总"
    assert orch.managed_session.budget == 5
    # emit managed_session_started + handoff_enqueued（kickoff delegate）。
    started = _by_type(cap, ChahuaEventType.MANAGED_SESSION_STARTED.value)
    assert len(started) == 1
    assert started[0]["data"] == {
        "task_id": tid, "manager_guest": "宝总", "budget": 5,
    }
    enq = _by_type(cap, ChahuaEventType.HANDOFF_ENQUEUED.value)
    assert enq and enq[-1]["data"]["queue"][0]["target"] == "宝总"


# ── managed_session_stop ─────────────────────────────────────────────────


async def test_close_task_ends_managed_session(session_and_srv) -> None:
    """关掉 MTS 驱动的任务 → 显式结束 MTS（task_closed）。close 取消了 MTS drain，
    `_advance` 不会再跑到 task_closed 停止判定（Codex review P2）。"""
    session, srv = session_and_srv
    tid = _open_task(session)
    orch = srv._session.orchestrator
    orch._managed_session = ManagedSession(
        task_id=tid, manager_guest="宝总", budget=3,
    )
    cap = await _send(srv, {
        "type": "close_task", "task_id": tid, "status": "done",
    })
    assert orch.managed_session is None
    ended = _by_type(cap, ChahuaEventType.MANAGED_SESSION_ENDED.value)
    assert len(ended) == 1
    assert ended[0]["data"]["reason"] == "task_closed"


async def test_set_active_task_ends_managed_session(session_and_srv) -> None:
    """切 active 取消了 MTS drain → 显式结束 MTS（user_cancel）。"""
    session, srv = session_and_srv
    tid = _open_task(session)
    orch = srv._session.orchestrator
    orch._managed_session = ManagedSession(
        task_id=tid, manager_guest="宝总", budget=3,
    )
    cap = await _send(srv, {"type": "set_active_task", "task_id": None})
    assert orch.managed_session is None
    ended = _by_type(cap, ChahuaEventType.MANAGED_SESSION_ENDED.value)
    assert len(ended) == 1
    assert ended[0]["data"]["reason"] == "user_cancel"


async def test_open_task_ends_managed_session(session_and_srv) -> None:
    """开新任务也自动 set_active + 取消 MTS drain → 显式结束 MTS（user_cancel）。"""
    session, srv = session_and_srv
    tid = _open_task(session)
    orch = srv._session.orchestrator
    orch._managed_session = ManagedSession(
        task_id=tid, manager_guest="宝总", budget=3,
    )
    cap = await _send(srv, {
        "type": "open_task", "title": "新活儿", "goal": "干点别的",
    })
    assert orch.managed_session is None
    ended = _by_type(cap, ChahuaEventType.MANAGED_SESSION_ENDED.value)
    assert len(ended) == 1
    assert ended[0]["data"]["reason"] == "user_cancel"


async def test_replace_session_ends_managed_session(session_and_srv) -> None:
    """同房 session 重建（加/删茶客 / 改权限等）丢掉旧 orchestrator 的 MTS ——
    _replace_session 必须 emit managed_session_ended 让前端复位（Codex review P2）。"""
    session, srv = session_and_srv
    tid = _open_task(session)
    srv._session.orchestrator._managed_session = ManagedSession(
        task_id=tid, manager_guest="宝总", budget=3,
    )
    room_dir = srv._session.room_config.room_dir
    captured: list[dict] = []
    ok = srv._replace_session(
        room_dir, lambda e: captured.append(e.to_dict()), label="test",
    )
    assert ok
    ended = _by_type(captured, ChahuaEventType.MANAGED_SESSION_ENDED.value)
    assert len(ended) == 1
    # 新 session 的 orchestrator 没有 MTS。
    assert srv._session.orchestrator.managed_session is None


# ── 断线即结束 MTS（Codex review P2）───────────────────────────────────────


class _FakeWS:
    """空 ws：连上即断（async for 立即 StopAsyncIteration），跑 _serve_one 的
    连接生命周期 + finally。"""

    def __init__(self) -> None:
        self.sent: list[str] = []

    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration

    async def send(self, data: str) -> None:
        self.sent.append(data)

    async def close(self, *args, **kwargs) -> None:
        pass


async def test_disconnect_ends_managed_session(session_and_srv) -> None:
    """断线必经 _serve_one finally —— drain 被 cancel，MTS 须一并结束（user_cancel），
    否则重连后「托管中」按钮对着死调度复活、停不掉（Codex review P2）。"""
    session, srv = session_and_srv
    tid = _open_task(session)
    orch = srv._session.orchestrator
    orch._managed_session = ManagedSession(
        task_id=tid, manager_guest="宝总", budget=3,
    )
    await srv._serve_one(_FakeWS())
    assert orch.managed_session is None


async def test_stop_no_session_is_noop_with_info(session_and_srv) -> None:
    session, srv = session_and_srv
    cap = await _send(srv, {"type": INBOUND_MANAGED_SESSION_STOP})
    notices = _by_type(cap, ChahuaEventType.NOTICE.value)
    assert notices and notices[0]["data"]["level"] == "info"


async def test_stop_ends_session_and_clears_queue(session_and_srv) -> None:
    session, srv = session_and_srv
    tid = _open_task(session)
    orch = srv._session.orchestrator
    orch._managed_session = ManagedSession(
        task_id=tid, manager_guest="宝总", budget=3,
    )
    orch.enqueue_handoff(
        HandoffItem(kind=HandoffKind.DELEGATE, target="宝总", reason="x")
    )
    cap = await _send(srv, {"type": INBOUND_MANAGED_SESSION_STOP})

    assert orch.managed_session is None
    assert len(orch._handoff_queue) == 0
    ended = _by_type(cap, ChahuaEventType.MANAGED_SESSION_ENDED.value)
    assert len(ended) == 1
    assert ended[0]["data"]["reason"] == "user_stopped"
