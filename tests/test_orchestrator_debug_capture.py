"""P6.1 step 2：orchestrator / guest 注入 recorder 的集成测试（docs/P6 §验证）。

覆盖：

- 正常 happy path：``submit_user_message`` 跑完后 ``turns.jsonl`` 增一行；含
  candidates / scoring_path / winners / messages 子节。
- ``winners=[]`` 路径：全员低分 / 全员冷却也写一行（"为什么没人说话"）；
  ``turn_start`` envelope **不 emit**（前端协议不变）。
- 打分阶段 cancel：上一轮已 ``next='ai'`` 时补 fixup；**不写新 turn 行**
  （turn_id 在 pick 后才 mint）。
- 发言阶段 cancel：turn 行仍 flush，``messages[].status="cancelled"`` 取证保留。
- ``capture_prompts=False``：jsonl 行内 scoring_results 不含 ``prompt_file``，
  message 不含 ``speak_prompt_file``；``prompts/`` 目录不创建。
- broadcast / mention 路径：``scoring_path`` 字段对应 ``broadcast`` / ``mention``，
  ``threshold=null``，所有 winners kind=mention。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from chahua._persist import read_jsonl_skip_bad
from chahua.cursor import GuestCursor
from chahua.debug_recorder import (
    SCORING_PATH_BROADCAST,
    SCORING_PATH_MENTION,
    SCORING_PATH_SCORING,
    TurnRecorder,
)
from chahua.events import (
    STATUS_CANCELLED,
    STATUS_OK,
    ChahuaEnvelope,
    ChahuaEventType,
    new_message_id,
)
from chahua.orchestrator import Orchestrator, OrchestratorConfig
from chahua.room import Room
from chahua.scoring import IntentScorer, ScoreKind, ScoreResult

from chahua.user_md import USER_SPEAKER_ID, UserConfig

from conftest import FixedScorer, NoopSummarizer, SpeakingStubGuest


# ── stubs ───────────────────────────────────────────────────────────────────


class _CancellableStubGuest(SpeakingStubGuest):
    """speak 内部等一个 gate；被 cancel 时走 finally 路径
    （record_message_end status=cancelled）—— 给 cancel-during-speak 测用。"""

    def __init__(self, name, room, recorder, gate: asyncio.Event,
                 started: asyncio.Event) -> None:
        super().__init__(name, room, recorder)
        self._gate = gate
        self._started = started

    async def speak(
        self, context_message, *, turn_id, sink, cancellation_token=None, task_id=None,
    ):
        message_id = new_message_id()
        if self._recorder is not None:
            self._recorder.record_message_start(
                message_id=message_id, guest=self.name,
                speak_prompt=context_message,
            )
        sink(ChahuaEnvelope(
            room_id=self._room.name, turn_id=turn_id, guest_name=self.name,
            message_id=message_id, type=ChahuaEventType.MESSAGE_START,
        ))
        self._started.set()
        try:
            await self._gate.wait()
            return None  # 不会到这（cancel 必走）
        finally:
            if self._recorder is not None:
                self._recorder.record_message_end(
                    message_id=message_id, status=STATUS_CANCELLED, seq=None,
                )


# ── helpers ─────────────────────────────────────────────────────────────────


def _build_orch(
    room: Room, scorer: IntentScorer, recorder: TurnRecorder,
    *, max_ai_turns=4, speaker_cooldown=1,
) -> Orchestrator:
    return Orchestrator(
        room=room,
        user_config=UserConfig(display_name="测试者", full_md=None, source=None),
        scorer=scorer,
        summarizer=NoopSummarizer(),
        cursor=GuestCursor(),
        config=OrchestratorConfig(
            max_consecutive_ai_turns=max_ai_turns,
            max_speakers_per_pick=1,
            speaker_cooldown_turns=speaker_cooldown,
            summary_block_size=999,
        ),
        recorder=recorder,
    )


# ── 正常 happy path ──────────────────────────────────────────────────────────


async def test_happy_path_writes_turn_row(tmp_path: Path):
    room = Room(name="r1")
    room.add_participant(USER_SPEAKER_ID)
    rec = TurnRecorder(tmp_path, enabled=True, capture_prompts=True)
    scorer = FixedScorer({"A": 0.9, "B": 0.1})
    orch = _build_orch(room, scorer, rec, max_ai_turns=1)
    orch.register(SpeakingStubGuest("A", room, rec), persona_md="A")
    orch.register(SpeakingStubGuest("B", room, rec), persona_md="B")

    captured: list[ChahuaEnvelope] = []
    await orch.submit_user_message("你好", sink=captured.append)

    rows = list(read_jsonl_skip_bad(tmp_path / "debug" / "turns.jsonl"))
    assert len(rows) == 1
    row = rows[0]
    # scoring 路径，A 是 winner（B 没过阈值）
    assert row["scoring_path"] == SCORING_PATH_SCORING
    assert row["scoring"]["winners"] == ["A"]
    assert {r["guest"] for r in row["scoring"]["results"]} == {"A", "B"}
    # capture_prompts=True：A / B 都有 prompt_file
    by_guest = {r["guest"]: r for r in row["scoring"]["results"]}
    assert "prompt_file" in by_guest["A"]
    assert "prompt_file" in by_guest["B"]
    # message 落了 1 条（A 发言 stub），status=ok
    assert len(row["messages"]) == 1
    assert row["messages"][0]["guest"] == "A"
    assert row["messages"][0]["status"] == STATUS_OK
    # trigger 是 user_msg 路径
    assert row["trigger"]["kind"] == "user_msg"


# ── winners=[] 路径 ──────────────────────────────────────────────────────────


async def test_no_winners_writes_row_but_no_turn_start(tmp_path: Path):
    """全员低分 → winners=[]；jsonl 仍写一行（"为什么没人说话"），turn_start envelope
    **不 emit**（前端协议不变）。"""
    room = Room(name="r2")
    room.add_participant(USER_SPEAKER_ID)
    rec = TurnRecorder(tmp_path, enabled=True, capture_prompts=False)
    scorer = FixedScorer({"A": 0.1, "B": 0.1})  # 都低于默认 want_threshold=0.45
    orch = _build_orch(room, scorer, rec)
    orch.register(SpeakingStubGuest("A", room, rec), persona_md="A")
    orch.register(SpeakingStubGuest("B", room, rec), persona_md="B")

    captured: list[ChahuaEnvelope] = []
    await orch.submit_user_message("hi", sink=captured.append)

    rows = list(read_jsonl_skip_bad(tmp_path / "debug" / "turns.jsonl"))
    assert len(rows) == 1
    assert rows[0]["scoring"]["winners"] == []
    assert rows[0]["messages"] == []
    # turn_start envelope 未 emit（前端协议不变）
    types = [e.type for e in captured]
    assert ChahuaEventType.TURN_START not in types


# ── cancel during scoring ───────────────────────────────────────────────────


async def test_cancel_during_scoring_does_not_record_new_turn(tmp_path: Path):
    """打分阶段被 cancel：turn_id 还没 mint，**不写新 turn 行**；上一轮已 next='ai'
    的 fixup 走 last_open_turn_id 路径，与既有 envelope 行为一致。"""
    room = Room(name="r3")
    room.add_participant(USER_SPEAKER_ID)
    rec = TurnRecorder(tmp_path, enabled=True, capture_prompts=False)

    gate = asyncio.Event()
    second_round_started = asyncio.Event()

    class _Hanging(IntentScorer):
        def __init__(self) -> None:
            self._calls = 0

        async def score(
            self, *, guest_name, persona, transcript_text, user_config,
            subject_mention_count=0, task_block="",
        ):
            self._calls += 1
            if self._calls <= 2:
                # 第 1 轮：A 高分（max_speakers_per_pick=1 → A 发言）。
                return ScoreResult(
                    guest_name=guest_name,
                    score=0.9 if guest_name == "A" else 0.1,
                    kind=ScoreKind.SCORED,
                )
            # 第 2 轮：所有 guest hang 在 gate 上
            second_round_started.set()
            await gate.wait()
            return ScoreResult(guest_name=guest_name, score=0.0, kind=ScoreKind.SCORED)

    scorer = _Hanging()
    orch = _build_orch(room, scorer, rec, max_ai_turns=4, speaker_cooldown=1)
    orch.register(SpeakingStubGuest("A", room, rec), persona_md="A")
    orch.register(SpeakingStubGuest("B", room, rec), persona_md="B")

    task = asyncio.create_task(orch.submit_user_message("hi", sink=lambda _e: None))
    await asyncio.wait_for(second_round_started.wait(), timeout=2.0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # 第 1 轮 turn 已正常 flush（A 发言）；第 2 轮 scoring 还没到 start_turn 就被
    # cancel —— 只 1 行 jsonl。
    rows = list(read_jsonl_skip_bad(tmp_path / "debug" / "turns.jsonl"))
    assert len(rows) == 1
    assert rows[0]["scoring"]["winners"] == ["A"]


# ── cancel during speak —— 半截 turn 也 flush ─────────────────────────────


async def test_cancel_during_speak_still_flushes(tmp_path: Path):
    """发言阶段被 cancel：turn 行仍 flush 一行，``messages[].status="cancelled"``
    取证保留（不变量"cancel / 异常路径仍 flush 一行"）。"""
    room = Room(name="r4")
    room.add_participant(USER_SPEAKER_ID)
    rec = TurnRecorder(tmp_path, enabled=True, capture_prompts=False)

    gate = asyncio.Event()
    speak_started = asyncio.Event()
    scorer = FixedScorer({"A": 0.9})

    orch = _build_orch(room, scorer, rec, max_ai_turns=1)
    orch.register(
        _CancellableStubGuest("A", room, rec, gate, speak_started),
        persona_md="A",
    )

    task = asyncio.create_task(orch.submit_user_message("hi", sink=lambda _e: None))
    await asyncio.wait_for(speak_started.wait(), timeout=2.0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    rows = list(read_jsonl_skip_bad(tmp_path / "debug" / "turns.jsonl"))
    assert len(rows) == 1
    assert rows[0]["scoring"]["winners"] == ["A"]
    # 半截 message 也记下来了，status=cancelled
    assert len(rows[0]["messages"]) == 1
    assert rows[0]["messages"][0]["status"] == STATUS_CANCELLED


# ── capture_prompts=False ───────────────────────────────────────────────────


async def test_capture_prompts_false_omits_fields(tmp_path: Path):
    room = Room(name="r5")
    room.add_participant(USER_SPEAKER_ID)
    rec = TurnRecorder(tmp_path, enabled=True, capture_prompts=False)
    scorer = FixedScorer({"A": 0.9, "B": 0.1})
    orch = _build_orch(room, scorer, rec, max_ai_turns=1)
    orch.register(SpeakingStubGuest("A", room, rec), persona_md="A")
    orch.register(SpeakingStubGuest("B", room, rec), persona_md="B")

    await orch.submit_user_message("hi", sink=lambda _e: None)

    rows = list(read_jsonl_skip_bad(tmp_path / "debug" / "turns.jsonl"))
    for r in rows[0]["scoring"]["results"]:
        assert "prompt_file" not in r
    for m in rows[0]["messages"]:
        assert "speak_prompt_file" not in m
    assert not (tmp_path / "debug" / "prompts").exists()


# ── broadcast / mention 路径 ────────────────────────────────────────────────


async def test_broadcast_path_records(tmp_path: Path):
    room = Room(name="r6")
    room.add_participant(USER_SPEAKER_ID)
    rec = TurnRecorder(tmp_path, enabled=True, capture_prompts=False)
    scorer = FixedScorer({"A": 0.1, "B": 0.1})  # 任意 —— @all 不走打分
    orch = _build_orch(room, scorer, rec, max_ai_turns=2)
    orch.register(SpeakingStubGuest("A", room, rec), persona_md="A")
    orch.register(SpeakingStubGuest("B", room, rec), persona_md="B")

    await orch.submit_user_message("@all 大家好", sink=lambda _e: None)

    rows = list(read_jsonl_skip_bad(tmp_path / "debug" / "turns.jsonl"))
    # @all 全员发言一次（绕过 max_ai_turns 内层 cap）+ 可能的下一轮 scoring(全冷却→空)。
    # 至少首行是 broadcast 路径。
    first = rows[0]
    assert first["scoring_path"] == SCORING_PATH_BROADCAST
    assert set(first["scoring"]["winners"]) == {"A", "B"}
    assert first["scoring"]["threshold"] is None
    for r in first["scoring"]["results"]:
        assert r["kind"] == "mention"


async def test_mention_path_records(tmp_path: Path):
    room = Room(name="r7")
    room.add_participant(USER_SPEAKER_ID)
    rec = TurnRecorder(tmp_path, enabled=True, capture_prompts=False)
    scorer = FixedScorer({"A": 0.0})
    orch = _build_orch(room, scorer, rec, max_ai_turns=1)
    orch.register(SpeakingStubGuest("A", room, rec), persona_md="A")

    await orch.submit_user_message("@A 在吗", sink=lambda _e: None)

    rows = list(read_jsonl_skip_bad(tmp_path / "debug" / "turns.jsonl"))
    assert rows[0]["scoring_path"] == SCORING_PATH_MENTION
    assert rows[0]["scoring"]["winners"] == ["A"]
    assert rows[0]["scoring"]["threshold"] is None


# ── NOOP 路径：enabled=false 时 orchestrator 不动盘 ─────────────────────────


async def test_disabled_recorder_no_disk(tmp_path: Path):
    room = Room(name="r8")
    room.add_participant(USER_SPEAKER_ID)
    rec = TurnRecorder(tmp_path, enabled=False, capture_prompts=False)
    scorer = FixedScorer({"A": 0.9})
    orch = _build_orch(room, scorer, rec, max_ai_turns=1)
    orch.register(SpeakingStubGuest("A", room, rec), persona_md="A")

    await orch.submit_user_message("hi", sink=lambda _e: None)

    assert not (tmp_path / "debug").exists()
