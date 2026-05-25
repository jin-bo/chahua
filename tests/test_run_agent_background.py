"""P11 C6：``ChahuaServer._run_agent_background`` wrapper 回归。

覆盖三种终态：
- speak() 返 ``Message`` → ``AGENT_RUN_FINISHED`` + cursor 推进；
- ``CancelledError`` → ``AGENT_RUN_CANCELLED`` + 重抛 + 注册表清；
- speak() 抛普通异常被吞返 None → ``AGENT_RUN_ERROR``。

承重检查：
- finally 顺序：detect → flush propose → pop registry → emit terminal → discard active_guest_names；
- ``message_end`` envelope 经 BatchMessageSink 补 ``data.run_id`` 后到 router；
- TASK_PROPOSAL 缓冲在 run 完成后 flush；
- 任一步异常都不阻挡外层 discard。
"""

from __future__ import annotations

import asyncio
from typing import Any, Optional
from unittest.mock import AsyncMock

import pytest

from chahua.agent_run import create as create_agent_run
from chahua.events import (
    ChahuaEnvelope,
    ChahuaEventType,
    NOOP_SINK,
    STATUS_OK,
)
from chahua.room_runtime import RoomEventRouter, RoomRuntime
from chahua.server import ChahuaServer, _install_handler_slots


class _StubCursor:
    def __init__(self) -> None:
        self.set_calls: list[tuple[str, int]] = []

    def set(self, name: str, seq: int) -> None:
        self.set_calls.append((name, seq))


class _StubRoom:
    name = "test-room"


class _StubArtifactDetector:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, Optional[str]]] = []

    def detect(self, sink: Any, task_id: Optional[str]) -> None:
        self.calls.append((sink, task_id))


class _StubMessage:
    def __init__(self, seq: int = 7, message_id: str = "msg_speak") -> None:
        self.seq = seq
        self.message_id = message_id


class _StubGuest:
    """speak() 行为可配置。"""

    def __init__(
        self,
        name: str = "Bob",
        *,
        msg: Optional[_StubMessage] = None,
        raise_cancel: bool = False,
        raise_exception: Optional[Exception] = None,
        on_speak_emit_propose: Optional[ChahuaEnvelope] = None,
    ) -> None:
        self.name = name
        self._msg = msg
        self._raise_cancel = raise_cancel
        self._raise_exception = raise_exception
        self._on_speak_emit_propose = on_speak_emit_propose
        self.speak_calls: list[dict] = []

    async def speak(self, *args: Any, **kwargs: Any) -> Optional[_StubMessage]:
        self.speak_calls.append(dict(kwargs))
        sink = kwargs.get("sink")
        # 模拟 bg 茶客在 bind 期内 emit 一条 TASK_PROPOSAL（验证 BatchMessageSink 缓冲）
        if self._on_speak_emit_propose is not None and sink is not None:
            sink(self._on_speak_emit_propose)
        if self._raise_cancel:
            raise asyncio.CancelledError()
        if self._raise_exception is not None:
            # 模拟 speak 内部把异常吞掉返 None（与真实 speak 行为一致）
            return None
        if self._msg is not None:
            # 同样要 emit message_end(ok) 给 sink，验证 run_id 补字段
            if sink is not None:
                sink(ChahuaEnvelope(
                    room_id="test-room", turn_id=None,
                    guest_name=self.name, message_id=self._msg.message_id,
                    type=ChahuaEventType.MESSAGE_END, status=STATUS_OK,
                    seq=self._msg.seq, data={"text": "hi"},
                ))
            return self._msg
        return None


class _StubOrchestrator:
    def __init__(self, guest: _StubGuest) -> None:
        self._guest = guest
        self.cursor = _StubCursor()
        self._artifact_detector = _StubArtifactDetector()
        # 给 wrapper context 装配读这些 stub。
        self.last_build_ctx_kwargs: Optional[dict] = None

    def get_guest(self, name: str):
        return self._guest if self._guest.name == name else None

    def _build_context_for(
        self, guest_name: str, *, task_id: Optional[str] = None,
        extra_blocks: Optional[list[str]] = None,
    ) -> str:
        self.last_build_ctx_kwargs = {
            "guest_name": guest_name, "task_id": task_id,
            "extra_blocks": list(extra_blocks or []),
        }
        return "ctx"

    def _kick_detect_new_artifacts(
        self, sink: Any, task_id: Optional[str]
    ) -> None:
        self._artifact_detector.detect(sink, task_id)


class _StubSession:
    def __init__(self, orchestrator: _StubOrchestrator) -> None:
        self.orchestrator = orchestrator
        self.room = _StubRoom()


def _make_runtime_with_guest(
    guest: _StubGuest, sink_log: list[ChahuaEnvelope]
) -> tuple[RoomRuntime, _StubOrchestrator]:
    orch = _StubOrchestrator(guest)
    session = _StubSession(orch)
    rt = RoomRuntime(
        room_id="rid",
        session=session,  # type: ignore[arg-type]
        router=RoomEventRouter(sink_log.append),
    )
    return rt, orch


def _server(rt: RoomRuntime) -> ChahuaServer:
    srv = object.__new__(ChahuaServer)
    srv._runtimes = {rt.room_id: rt}
    srv._foreground_id = rt.room_id
    # P11 slot 拆分：``_run_agent_background`` 是薄转发到 ``_agent_run_ops``，必装。
    _install_handler_slots(srv)
    return srv


async def test_finished_path_emits_message_end_and_terminal_with_run_id() -> None:
    """成功：message_end(ok) 经 BatchMessageSink 补 run_id 到 router；终态 AGENT_RUN_FINISHED。"""
    msg = _StubMessage(seq=42, message_id="msg_xyz")
    guest = _StubGuest(msg=msg)
    sink_log: list[ChahuaEnvelope] = []
    rt, orch = _make_runtime_with_guest(guest, sink_log)

    run = create_agent_run(room_id="rid", guest_name="Bob", instruction="hi")
    rt.agent_runs[run.run_id] = run
    rt.active_guest_names.add("Bob")

    srv = _server(rt)
    await srv._run_agent_background(rt, run)

    # 1) 注册表清空。
    assert rt.agent_runs == {}
    assert rt.agent_run_tasks == {}
    # 2) active_guest_names 已 discard。
    assert "Bob" not in rt.active_guest_names
    # 3) cursor 推进。
    assert orch.cursor.set_calls == [("Bob", 42)]
    # 4) detect 跑了一次（router 直发）。
    assert len(orch._artifact_detector.calls) == 1
    assert orch._artifact_detector.calls[0][1] is None  # task_id 为 None
    # 5) router 收到：message_end + agent_run_finished。
    types = [e.type for e in sink_log]
    assert ChahuaEventType.MESSAGE_END in types
    assert ChahuaEventType.AGENT_RUN_FINISHED in types
    # 6) message_end 补了 run_id。
    me = [e for e in sink_log if e.type is ChahuaEventType.MESSAGE_END][0]
    assert me.data["run_id"] == run.run_id
    # 7) AGENT_RUN_FINISHED 数据字段。
    term = [e for e in sink_log if e.type is ChahuaEventType.AGENT_RUN_FINISHED][0]
    assert term.data["run_id"] == run.run_id
    assert term.data["guest_name"] == "Bob"
    assert term.data["issued_by"] == "user"
    assert "error" not in term.data
    # 8) speak 收到 record_debug=False。
    assert guest.speak_calls[0]["record_debug"] is False


async def test_cancelled_path_emits_cancelled_terminal_and_reraises() -> None:
    """speak 抛 CancelledError → terminal=CANCELLED + 重抛（让 asyncio task 标 cancelled）。"""
    guest = _StubGuest(raise_cancel=True)
    sink_log: list[ChahuaEnvelope] = []
    rt, _ = _make_runtime_with_guest(guest, sink_log)
    run = create_agent_run(room_id="rid", guest_name="Bob", instruction="x")
    rt.agent_runs[run.run_id] = run
    rt.active_guest_names.add("Bob")

    srv = _server(rt)
    with pytest.raises(asyncio.CancelledError):
        await srv._run_agent_background(rt, run)

    # 终态是 CANCELLED。
    cancelled = [e for e in sink_log if e.type is ChahuaEventType.AGENT_RUN_CANCELLED]
    assert len(cancelled) == 1
    # 注册表清空 + discard。
    assert rt.agent_runs == {}
    assert "Bob" not in rt.active_guest_names


async def test_error_path_when_speak_returns_none() -> None:
    """speak 返 None（异常被吞）→ AGENT_RUN_ERROR + data.error = 'speak_failed'。"""
    guest = _StubGuest(raise_exception=RuntimeError("boom"))
    sink_log: list[ChahuaEnvelope] = []
    rt, _ = _make_runtime_with_guest(guest, sink_log)
    run = create_agent_run(room_id="rid", guest_name="Bob", instruction="x")
    rt.agent_runs[run.run_id] = run
    rt.active_guest_names.add("Bob")

    srv = _server(rt)
    await srv._run_agent_background(rt, run)

    err = [e for e in sink_log if e.type is ChahuaEventType.AGENT_RUN_ERROR]
    assert len(err) == 1
    assert err[0].data["error"] == "speak_failed"
    # 注册表清空 + discard。
    assert rt.agent_runs == {}
    assert "Bob" not in rt.active_guest_names


async def test_propose_buffer_flushed_after_run() -> None:
    """speak 期间 emit 的 TASK_PROPOSAL 被 BatchMessageSink 缓冲；wrapper finally 经 router flush。"""
    propose_env = ChahuaEnvelope(
        room_id="test-room", turn_id=None, guest_name="Bob",
        message_id=None, type=ChahuaEventType.TASK_PROPOSAL,
        data={"kind": "decision"},
    )
    msg = _StubMessage(seq=1, message_id="msg_q")
    guest = _StubGuest(msg=msg, on_speak_emit_propose=propose_env)
    sink_log: list[ChahuaEnvelope] = []
    rt, _ = _make_runtime_with_guest(guest, sink_log)
    run = create_agent_run(room_id="rid", guest_name="Bob", instruction="x")
    rt.agent_runs[run.run_id] = run
    rt.active_guest_names.add("Bob")

    srv = _server(rt)
    await srv._run_agent_background(rt, run)

    # TASK_PROPOSAL 必然在 router 出现；message_end 在它之前到达。
    types_in_order = [e.type for e in sink_log]
    assert ChahuaEventType.TASK_PROPOSAL in types_in_order
    # 顺序：propose flush 在 emit_terminal 之前（设计 §「wrapper finally 顺序」）。
    p_idx = types_in_order.index(ChahuaEventType.TASK_PROPOSAL)
    t_idx = types_in_order.index(ChahuaEventType.AGENT_RUN_FINISHED)
    assert p_idx < t_idx


async def test_target_guest_missing_emits_error() -> None:
    """登记 run 与 wrapper 启动之间该茶客被移除 —— wrapper 仍走 ERROR 终态 + 注册表清。"""
    # 茶客 name="Other"——orchestrator.get_guest("Bob") 返 None。
    guest = _StubGuest(name="Other")
    sink_log: list[ChahuaEnvelope] = []
    rt, _ = _make_runtime_with_guest(guest, sink_log)
    run = create_agent_run(room_id="rid", guest_name="Bob", instruction="x")
    rt.agent_runs[run.run_id] = run
    rt.active_guest_names.add("Bob")

    srv = _server(rt)
    await srv._run_agent_background(rt, run)

    err = [e for e in sink_log if e.type is ChahuaEventType.AGENT_RUN_ERROR]
    assert len(err) == 1
    assert rt.agent_runs == {}
    assert "Bob" not in rt.active_guest_names


async def test_finally_pop_runs_before_emit_terminal() -> None:
    """先 pop 注册表，再 emit terminal —— 让 snapshot 在间隙重投不到死 run。"""
    # 通过让 detect/flush 提前观察 agent_runs：detect 阶段已经 pop 了吗？
    # 设计文档说「pop 在 emit terminal 前」，但 detect / flush 在 pop 之前。
    # 验证：在 emit terminal 时 (= router 接收 AGENT_RUN_FINISHED 的瞬间) agent_runs
    # 已为空（pop 已发生）。
    msg = _StubMessage(seq=1, message_id="msg_p")
    guest = _StubGuest(msg=msg)
    snapshots: list[dict] = []

    rt, _ = _make_runtime_with_guest(guest, [])
    # 包一层 router：在投递 AGENT_RUN_FINISHED 那一刻 snapshot agent_runs。
    original_router = rt.router

    def spy_sink(env: ChahuaEnvelope) -> None:
        if env.type is ChahuaEventType.AGENT_RUN_FINISHED:
            snapshots.append(dict(rt.agent_runs))
        original_router.ws_sink(env)

    rt.router = RoomEventRouter(spy_sink)
    run = create_agent_run(room_id="rid", guest_name="Bob", instruction="x")
    rt.agent_runs[run.run_id] = run
    rt.active_guest_names.add("Bob")

    srv = _server(rt)
    await srv._run_agent_background(rt, run)

    assert snapshots == [{}]  # emit terminal 时 agent_runs 已经清空


async def test_active_guest_names_discarded_even_if_emit_fails() -> None:
    """emit terminal 抛异常仍要走外层 discard active_guest_names。"""
    msg = _StubMessage(seq=1, message_id="msg_a")
    guest = _StubGuest(msg=msg)
    rt, _ = _make_runtime_with_guest(guest, [])

    # 把 router 替换成抛异常的：emit terminal 时炸。
    def angry_sink(env: ChahuaEnvelope) -> None:
        if env.type is ChahuaEventType.AGENT_RUN_FINISHED:
            raise RuntimeError("emit boom")

    rt.router = RoomEventRouter(angry_sink)
    run = create_agent_run(room_id="rid", guest_name="Bob", instruction="x")
    rt.agent_runs[run.run_id] = run
    rt.active_guest_names.add("Bob")

    srv = _server(rt)
    # wrapper 内 emit 异常被吞，不抛到外层；但 finally discard 必跑。
    await srv._run_agent_background(rt, run)

    assert "Bob" not in rt.active_guest_names
    assert rt.agent_runs == {}
