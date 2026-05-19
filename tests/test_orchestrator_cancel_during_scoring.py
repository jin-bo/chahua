"""回归：scoring 阶段被 cancel 时 orchestrator 应补 turn_end 让 UI 收回「停止」按钮
（_run_ai_chain 外层 while 的 except CancelledError 兜底，对照内 for 的 fixup）。"""

from __future__ import annotations

import asyncio

import pytest

from chahua.cursor import GuestCursor
from chahua.events import (
    STATUS_CANCELLED,
    STATUS_OK,
    ChahuaEnvelope,
    ChahuaEventType,
)
from chahua.orchestrator import Orchestrator, OrchestratorConfig
from chahua.room import Room
from chahua.scoring import IntentScorer, ScoreKind, ScoreResult
from chahua.user_md import USER_SPEAKER_ID, UserConfig

from conftest import NoopSummarizer, SpeakingStubGuest


class _ScriptedScorer(IntentScorer):
    """第 1 轮秒返高分；第 2 轮所有 guest 都阻塞在 gate 上 —— 模拟 LLM 在飞。"""

    def __init__(self, *, second_round_started: asyncio.Event) -> None:
        self._second_round_started = second_round_started
        self._gate = asyncio.Event()  # 永不 set —— 测试只 cancel
        self._calls = 0

    async def score(
        self,
        *,
        guest_name,
        persona,
        transcript_text,
        user_config,
        subject_mention_count=0,
        task_block="",
    ):
        self._calls += 1
        if self._calls <= 2:
            return ScoreResult(
                guest_name=guest_name,
                score=0.9 if guest_name == "A" else 0.1,
                kind=ScoreKind.SCORED,
            )
        self._second_round_started.set()
        await self._gate.wait()
        return ScoreResult(guest_name=guest_name, score=0.0, kind=ScoreKind.SCORED)


async def test_cancel_during_mid_chain_scoring_emits_fixup_turn_end():
    room = Room(name="test-room")
    room.add_participant(USER_SPEAKER_ID)
    user_config = UserConfig(display_name="测试者", full_md=None, source=None)
    cursor = GuestCursor()
    second_round_started = asyncio.Event()
    scorer = _ScriptedScorer(second_round_started=second_round_started)
    summarizer = NoopSummarizer()

    orch = Orchestrator(
        room=room,
        user_config=user_config,
        scorer=scorer,
        summarizer=summarizer,
        cursor=cursor,
        # max_speakers_per_pick=1：第 1 轮只 A 发言；第 2 轮 A 冷却挡掉，scorables=[B]
        # 进 scoring，恰好对应触发条件。summary_block_size=999 防 _kick_summarize 触发。
        config=OrchestratorConfig(
            max_consecutive_ai_turns=4,
            max_speakers_per_pick=1,
            speaker_cooldown_turns=1,
            summary_block_size=999,
        ),
    )
    orch.register(SpeakingStubGuest("A", room), persona_md="A persona")
    orch.register(SpeakingStubGuest("B", room), persona_md="B persona")

    captured: list[ChahuaEnvelope] = []
    sink: list = captured.append  # type: ignore[assignment]

    task = asyncio.create_task(orch.submit_user_message("你好", sink=sink))
    await asyncio.wait_for(second_round_started.wait(), timeout=2.0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    turn_ends = [e for e in captured if e.type == ChahuaEventType.TURN_END]
    assert len(turn_ends) >= 2, [e.to_dict() for e in captured]

    open_end, fixup_end = turn_ends[-2], turn_ends[-1]
    assert open_end.data.get("next") == "ai"
    assert open_end.status == STATUS_OK
    assert fixup_end.turn_id == open_end.turn_id
    assert fixup_end.data.get("next") == "user"
    assert fixup_end.status == STATUS_CANCELLED


async def test_cancel_during_first_round_scoring_no_fixup():
    """对照：第 1 轮 scoring 时被 cancel 不应补 turn_end（前端按钮还是「发送」态）。

    避免修复过头反向 emit 一条 UI 没在等的 turn_end。
    """
    room = Room(name="test-room")
    room.add_participant(USER_SPEAKER_ID)
    user_config = UserConfig(display_name="测试者", full_md=None, source=None)
    cursor = GuestCursor()
    first_round_started = asyncio.Event()

    class _HangingScorer(IntentScorer):
        def __init__(self) -> None:
            self._gate = asyncio.Event()

        async def score(
            self,
            *,
            guest_name,
            persona,
            transcript_text,
            user_config,
            subject_mention_count=0,
            task_block="",
        ):
            first_round_started.set()
            await self._gate.wait()
            return ScoreResult(guest_name=guest_name, score=0.0, kind=ScoreKind.SCORED)

    scorer = _HangingScorer()
    summarizer = NoopSummarizer()

    orch = Orchestrator(
        room=room,
        user_config=user_config,
        scorer=scorer,
        summarizer=summarizer,
        cursor=cursor,
        config=OrchestratorConfig(summary_block_size=999),
    )
    orch.register(SpeakingStubGuest("A", room), persona_md="A persona")

    captured: list[ChahuaEnvelope] = []
    sink: list = captured.append  # type: ignore[assignment]

    task = asyncio.create_task(orch.submit_user_message("你好", sink=sink))
    await asyncio.wait_for(first_round_started.wait(), timeout=2.0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # 没有过 turn_start，更不应该有 turn_end —— 一条轮级 envelope 都不该 emit。
    turn_events = [
        e
        for e in captured
        if e.type in (ChahuaEventType.TURN_START, ChahuaEventType.TURN_END)
    ]
    assert turn_events == [], [e.to_dict() for e in turn_events]
