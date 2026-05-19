"""P6.2 envelope piggyback：``TURN_START.data.scoring_prompts`` /
``MESSAGE_START.data.speak_prompt``（docs/P6 §7.5）。

**不变量**：``enabled && capture_prompts`` 双开才出现；任一关 → 整字段**不写 key**
（区分"prompt 捕获关了" vs "prompt 是空"两种语义）。``schema_version`` 不 bump。

覆盖：

- orchestrator emit ``TURN_START``：``capture_prompts=True`` 时 ``data.scoring_prompts``
  含所有 scorable guest；``False`` 时整字段缺。
- :class:`TeaGuest` emit ``MESSAGE_START``：``capture_prompts=True`` 时
  ``data.speak_prompt`` 是完整 context_message；``False`` 时整字段缺。
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from chahua.cursor import GuestCursor
from chahua.debug_recorder import NOOP_RECORDER, TurnRecorder
from chahua.events import (
    STATUS_OK,
    ChahuaEnvelope,
    ChahuaEventType,
)
from chahua.guest import TeaGuest
from chahua.orchestrator import Orchestrator, OrchestratorConfig
from chahua.room import Room
from chahua.user_md import USER_SPEAKER_ID, UserConfig

from conftest import FixedScorer, NoopSummarizer, SpeakingStubGuest


# ── orchestrator TURN_START piggyback ──────────────────────────────────────


def _orch(room: Room, recorder: TurnRecorder) -> Orchestrator:
    return Orchestrator(
        room=room,
        user_config=UserConfig(display_name="测试者", full_md=None, source=None),
        scorer=FixedScorer({"A": 0.9, "B": 0.1}, include_raw=False),
        summarizer=NoopSummarizer(),
        cursor=GuestCursor(),
        config=OrchestratorConfig(
            max_consecutive_ai_turns=1,
            max_speakers_per_pick=1,
            speaker_cooldown_turns=1,
            summary_block_size=999,
        ),
        recorder=recorder,
    )


async def test_turn_start_carries_scoring_prompts_when_capture_on(tmp_path: Path):
    room = Room(name="r1")
    room.add_participant(USER_SPEAKER_ID)
    rec = TurnRecorder(tmp_path, enabled=True, capture_prompts=True)
    orch = _orch(room, rec)
    orch.register(SpeakingStubGuest("A", room, rec), persona_md="A")
    orch.register(SpeakingStubGuest("B", room, rec), persona_md="B")

    captured: list[ChahuaEnvelope] = []
    await orch.submit_user_message("hi", sink=captured.append)

    turn_start = next(e for e in captured if e.type is ChahuaEventType.TURN_START)
    assert "scoring_prompts" in turn_start.data
    sp = turn_start.data["scoring_prompts"]
    assert sp == {"A": "PROMPT for A", "B": "PROMPT for B"}


async def test_turn_start_carries_empty_scoring_prompts_on_mention_path(tmp_path: Path):
    """``@<guest>`` 路径绕过 LLM 打分但 capture 仍开 ⇒ key 必须存在（值是 ``{}``），
    前端据此区分"capture 已关"（缺 key）与"capture 开但本 turn 无 LLM 打分"（空）。
    """
    room = Room(name="r1m")
    room.add_participant(USER_SPEAKER_ID)
    rec = TurnRecorder(tmp_path, enabled=True, capture_prompts=True)
    orch = _orch(room, rec)
    orch.register(SpeakingStubGuest("A", room, rec), persona_md="A")
    orch.register(SpeakingStubGuest("B", room, rec), persona_md="B")

    captured: list[ChahuaEnvelope] = []
    await orch.submit_user_message("@A hi", sink=captured.append)

    turn_start = next(e for e in captured if e.type is ChahuaEventType.TURN_START)
    assert "scoring_prompts" in turn_start.data
    assert turn_start.data["scoring_prompts"] == {}


async def test_turn_start_omits_scoring_prompts_when_capture_off(tmp_path: Path):
    """capture_prompts=False ⇒ 整字段不写 key（区分关 vs 空两种语义）。"""
    room = Room(name="r2")
    room.add_participant(USER_SPEAKER_ID)
    rec = TurnRecorder(tmp_path, enabled=True, capture_prompts=False)
    orch = _orch(room, rec)
    orch.register(SpeakingStubGuest("A", room, rec), persona_md="A")
    orch.register(SpeakingStubGuest("B", room, rec), persona_md="B")

    captured: list[ChahuaEnvelope] = []
    await orch.submit_user_message("hi", sink=captured.append)

    turn_start = next(e for e in captured if e.type is ChahuaEventType.TURN_START)
    assert "scoring_prompts" not in turn_start.data


async def test_turn_start_omits_when_recorder_disabled(tmp_path: Path):
    """enabled=False（NOOP 实例两个 attr 都 False）→ 也不写 key。"""
    room = Room(name="r3")
    room.add_participant(USER_SPEAKER_ID)
    rec = TurnRecorder(tmp_path, enabled=False, capture_prompts=False)
    orch = _orch(room, rec)
    orch.register(SpeakingStubGuest("A", room, rec), persona_md="A")
    orch.register(SpeakingStubGuest("B", room, rec), persona_md="B")

    captured: list[ChahuaEnvelope] = []
    await orch.submit_user_message("hi", sink=captured.append)

    turn_start = next(e for e in captured if e.type is ChahuaEventType.TURN_START)
    assert "scoring_prompts" not in turn_start.data


# ── TeaGuest MESSAGE_START piggyback ───────────────────────────────────────


class _FakeTransport:
    """:class:`ChahuaTransport` 的最小替身 —— 只够 :meth:`TeaGuest.speak` 跑通。

    收 ``emit_chahua`` 调用进 ``events`` 供断言；``bind`` 不动状态、退出即返。
    """

    def __init__(self) -> None:
        self.events: list[tuple[ChahuaEventType, dict]] = []

    @contextmanager
    def bind(self, **_kwargs: Any):
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


def _make_teaguest(
    room: Room, recorder: TurnRecorder, *, reply: str = "ok"
) -> tuple[TeaGuest, _FakeTransport]:
    """绕 :meth:`TeaGuest.__init__`（Agentao / MCP / Permission 全套依赖）—— 直接装
    属性，只用得到的 4 个字段。调用方负责把 "A" 加进 ``room.participants``。
    """
    g = TeaGuest.__new__(TeaGuest)
    g.name = "A"
    g.room = room
    g._recorder = recorder
    fake = _FakeTransport()
    g._transport = fake  # type: ignore[assignment]
    agent = type("FakeAgent", (), {})()
    agent.arun = AsyncMock(return_value=reply)
    g.agent = agent  # type: ignore[attr-defined]
    room.add_participant("A")
    return g, fake


async def test_message_start_carries_speak_prompt_when_capture_on(tmp_path: Path):
    room = Room(name="r4")
    room.add_participant(USER_SPEAKER_ID)
    rec = TurnRecorder(tmp_path, enabled=True, capture_prompts=True)
    guest, fake = _make_teaguest(room, rec)

    await guest.speak(
        "完整的上下文消息", turn_id="turn_x", sink=lambda _e: None,
    )

    start_evs = [d for t, d in fake.events if t is ChahuaEventType.MESSAGE_START]
    assert len(start_evs) == 1
    assert start_evs[0]["speak_prompt"] == "完整的上下文消息"


async def test_message_start_omits_speak_prompt_when_capture_off(tmp_path: Path):
    room = Room(name="r5")
    room.add_participant(USER_SPEAKER_ID)
    rec = TurnRecorder(tmp_path, enabled=True, capture_prompts=False)
    guest, fake = _make_teaguest(room, rec)

    await guest.speak(
        "另一段上下文", turn_id="turn_y", sink=lambda _e: None,
    )

    start_evs = [d for t, d in fake.events if t is ChahuaEventType.MESSAGE_START]
    assert len(start_evs) == 1
    assert "speak_prompt" not in start_evs[0]


async def test_message_start_omits_when_recorder_disabled(tmp_path: Path):
    """完全关 debug（NOOP_RECORDER 同款）—— capture_prompts 必为 False，整字段缺。"""
    room = Room(name="r6")
    room.add_participant(USER_SPEAKER_ID)
    guest, fake = _make_teaguest(room, NOOP_RECORDER)

    await guest.speak(
        "noop 路径", turn_id="turn_z", sink=lambda _e: None,
    )

    start_evs = [d for t, d in fake.events if t is ChahuaEventType.MESSAGE_START]
    assert len(start_evs) == 1
    assert "speak_prompt" not in start_evs[0]
