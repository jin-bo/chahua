"""P11 C2：``Orchestrator.active_guest_names`` 注入回归。

覆盖：

- 裸构 ``Orchestrator``（``active_guest_names is None``）时 ``let_speak`` 不抛、不修改 set；
- server 经 ``_attach_runtime_state(runtime)`` 注入后，orch / runtime 两端是同一 set 引用；
- 前台 ``let_speak()`` 执行中 set 含 ``guest_name``、finally 后 ``discard``。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Optional

import pytest

from chahua._orchestrator_scoring import ScoringOps
from chahua.events import NOOP_SINK
from chahua.room_runtime import RoomEventRouter, RoomRuntime
from chahua.server import ChahuaServer, _install_handler_slots


# ── 最小桩：scoring slot 只关心 ``_guests`` / ``_build_context_for`` / ``cursor`` / ``config`` ──


@dataclass
class _StubCursor:
    last_seen: dict[str, int]

    def set(self, name: str, seq: int) -> None:
        self.last_seen[name] = seq


@dataclass
class _StubMessage:
    seq: int = 1


@dataclass
class _StubConfig:
    speaker_cooldown_turns: int = 0


class _StubGuest:
    """speak() 返一个 _StubMessage 让 let_speak 走完正常路径（不进失败分支）。"""

    def __init__(self, name: str, *, observed: list[set[str]], names_ref: Optional[set[str]]) -> None:
        self.name = name
        self._observed = observed
        self._names_ref = names_ref

    async def speak(self, *args, **kwargs) -> _StubMessage:
        # 进 speak 的瞬间 snapshot active_guest_names —— let_speak 必须在 await 前 add。
        if self._names_ref is not None:
            self._observed.append(set(self._names_ref))
        return _StubMessage(seq=1)


class _StubEntry:
    def __init__(self, guest: _StubGuest) -> None:
        self.guest = guest


class _StubOrchestrator:
    """最小化 orch —— 只挂 ScoringOps.let_speak 用到的字段。"""

    def __init__(self, *, active_guest_names: Optional[set[str]] = None) -> None:
        self.active_guest_names = active_guest_names
        self._guests: dict[str, _StubEntry] = {}
        self._cooldown: dict[str, int] = {}
        self.cursor = _StubCursor(last_seen={})
        self.config = _StubConfig()

    def _build_context_for(self, *args, **kwargs) -> str:
        return ""


async def test_let_speak_no_set_when_active_guest_names_is_none() -> None:
    """裸构 Orchestrator（无 server 注入）—— let_speak 不抛、不读写 set。"""
    orch = _StubOrchestrator(active_guest_names=None)
    observed: list[set[str]] = []
    orch._guests["Bob"] = _StubEntry(_StubGuest("Bob", observed=observed, names_ref=None))

    slot = ScoringOps(orch)  # type: ignore[arg-type]
    await slot.let_speak("Bob", turn_id="turn_test", sink=NOOP_SINK)


async def test_let_speak_adds_and_discards_around_speak() -> None:
    """注入 set 后，speak() 期间含 guest_name；finally 后 discard。"""
    names: set[str] = set()
    orch = _StubOrchestrator(active_guest_names=names)
    observed: list[set[str]] = []
    orch._guests["Bob"] = _StubEntry(_StubGuest("Bob", observed=observed, names_ref=names))

    slot = ScoringOps(orch)  # type: ignore[arg-type]
    await slot.let_speak("Bob", turn_id="turn_test", sink=NOOP_SINK)

    assert observed == [{"Bob"}]  # speak 进入时见 Bob
    assert names == set()  # finally 后 discard 干净


async def test_let_speak_discards_on_speak_exception() -> None:
    """speak() 抛异常仍 discard —— finally 是承重墙。"""
    names: set[str] = set()
    orch = _StubOrchestrator(active_guest_names=names)

    class _AngryGuest:
        name = "Bob"

        async def speak(self, *args, **kwargs):
            raise RuntimeError("boom")

    orch._guests["Bob"] = _StubEntry(_AngryGuest())  # type: ignore[arg-type]

    slot = ScoringOps(orch)  # type: ignore[arg-type]
    with pytest.raises(RuntimeError):
        await slot.let_speak("Bob", turn_id="turn_test", sink=NOOP_SINK)
    assert names == set()


async def test_let_speak_discards_on_cancel() -> None:
    """CancelledError 透传同样走 finally。"""
    names: set[str] = set()
    orch = _StubOrchestrator(active_guest_names=names)

    class _SlowGuest:
        name = "Bob"

        async def speak(self, *args, **kwargs):
            await asyncio.sleep(100)

    orch._guests["Bob"] = _StubEntry(_SlowGuest())  # type: ignore[arg-type]

    slot = ScoringOps(orch)  # type: ignore[arg-type]
    task = asyncio.create_task(
        slot.let_speak("Bob", turn_id="turn_test", sink=NOOP_SINK),
    )
    await asyncio.sleep(0)  # 让 task 进 await。
    assert names == {"Bob"}  # speak 期间含 Bob
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert names == set()


# ── server 注入回归 ─────────────────────────────────────────────────────────


class _OrchHolder:
    def __init__(self) -> None:
        self.active_guest_names: Optional[set[str]] = None


class _Session:
    def __init__(self) -> None:
        self.orchestrator = _OrchHolder()


def test_attach_runtime_state_shares_set_with_orchestrator() -> None:
    """server._attach_runtime_state(runtime) 把同一 set 引用注入到 orch。"""
    srv = object.__new__(ChahuaServer)
    rt = RoomRuntime(
        room_id="r",
        session=_Session(),  # type: ignore[arg-type]
        router=RoomEventRouter(NOOP_SINK),
    )
    # P11 slot 拆分：``_attach_runtime_state`` 内部经 ``_make_start_agent_run``
    # 走 ``_agent_run_ops``，必装。
    _install_handler_slots(srv)

    srv._attach_runtime_state(rt)

    # 同一 set 实例，不是 copy ——后续 inbound add 立刻让 ScoringOps 看见。
    assert rt.session.orchestrator.active_guest_names is rt.active_guest_names
    rt.active_guest_names.add("Bob")
    assert "Bob" in rt.session.orchestrator.active_guest_names
