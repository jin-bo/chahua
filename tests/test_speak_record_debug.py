"""P11 C4：``TeaGuest.speak(record_debug=False)`` 临时把 ``self._recorder`` 替换为
``NOOP_RECORDER``，避免 bg run 与并发前台 turn 串台。
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from chahua.debug_recorder import NOOP_RECORDER, TurnRecorder
from chahua.events import ChahuaEventType, STATUS_OK
from chahua.guest import TeaGuest
from chahua.room import Room
from chahua.user_md import USER_SPEAKER_ID


class _FakeTransport:
    def __init__(self) -> None:
        self.events: list[tuple[ChahuaEventType, dict]] = []
        # speak 进 with bind() 时收一次 recorder 实参——用它断言 NOOP 是否真传入。
        self.bound_recorders: list[Any] = []

    @contextmanager
    def bind(self, **kwargs: Any):
        self.bound_recorders.append(kwargs.get("recorder"))
        yield self

    def emit_chahua(
        self,
        type: ChahuaEventType,
        data: dict | None = None,
        *,
        status: str = STATUS_OK,
        seq: int | None = None,
    ) -> None:
        self.events.append((type, dict(data or {})))

    @property
    def partial_text(self) -> str:
        return ""


def _make_guest(room: Room, recorder: TurnRecorder, reply: str = "ok") -> tuple[TeaGuest, _FakeTransport]:
    g = TeaGuest.__new__(TeaGuest)
    g.name = "A"
    g.room = room
    g._recorder = recorder
    g._share_dir = None  # P13：speak() 调 resolve_images(self._share_dir, ...)
    fake = _FakeTransport()
    g._transport = fake  # type: ignore[assignment]
    agent = type("FakeAgent", (), {})()
    agent.arun = AsyncMock(return_value=reply)
    g.agent = agent  # type: ignore[attr-defined]
    room.add_participant("A")
    return g, fake


async def test_record_debug_default_uses_original_recorder(tmp_path: Path) -> None:
    """默认 record_debug=True：bind 期间走 self._recorder（真实 TurnRecorder）。"""
    room = Room(name="r-default")
    room.add_participant(USER_SPEAKER_ID)
    rec = TurnRecorder(tmp_path, enabled=True, capture_prompts=False)
    guest, fake = _make_guest(room, rec)

    await guest.speak("ctx", turn_id="turn_x", sink=lambda _e: None)

    assert fake.bound_recorders == [rec]  # 真 recorder 被绑


async def test_record_debug_false_substitutes_noop(tmp_path: Path) -> None:
    """record_debug=False：bind 期间 recorder 是 NOOP_RECORDER，原 recorder 保持不变。"""
    room = Room(name="r-noop")
    room.add_participant(USER_SPEAKER_ID)
    rec = TurnRecorder(tmp_path, enabled=True, capture_prompts=False)
    guest, fake = _make_guest(room, rec)
    original = guest._recorder

    await guest.speak(
        "ctx", turn_id="turn_x", sink=lambda _e: None, record_debug=False,
    )

    # 进 bind 时见 NOOP，不是原 recorder。
    assert fake.bound_recorders == [NOOP_RECORDER]
    # speak 结束后 self._recorder 复原为原 recorder（顺序复用）。
    assert guest._recorder is original


async def test_record_debug_false_restores_recorder_after_exception(tmp_path: Path) -> None:
    """speak() 中 arun 抛异常（被吞返 None）后，self._recorder 仍复原。"""
    room = Room(name="r-err")
    room.add_participant(USER_SPEAKER_ID)
    rec = TurnRecorder(tmp_path, enabled=True, capture_prompts=False)
    guest, _ = _make_guest(room, rec)
    guest.agent.arun = AsyncMock(side_effect=RuntimeError("boom"))  # type: ignore[attr-defined]
    original = guest._recorder

    result = await guest.speak(
        "ctx", turn_id="turn_x", sink=lambda _e: None, record_debug=False,
    )

    assert result is None  # error 路径返 None
    assert guest._recorder is original  # 复原


async def test_record_debug_alternates_between_calls(tmp_path: Path) -> None:
    """连续两次 speak（先 True 再 False，再 True）—— 每次 bind 看到的 recorder 与本次参数一致。"""
    room = Room(name="r-alt")
    room.add_participant(USER_SPEAKER_ID)
    rec = TurnRecorder(tmp_path, enabled=True, capture_prompts=False)
    guest, fake = _make_guest(room, rec)

    await guest.speak("c1", turn_id="t1", sink=lambda _e: None, record_debug=True)
    await guest.speak("c2", turn_id="t2", sink=lambda _e: None, record_debug=False)
    await guest.speak("c3", turn_id="t3", sink=lambda _e: None, record_debug=True)

    assert fake.bound_recorders == [rec, NOOP_RECORDER, rec]
