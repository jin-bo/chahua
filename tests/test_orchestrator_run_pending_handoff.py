"""``Orchestrator.run_pending_handoff`` drain loop（P7.1.5 承重墙，docs/P7 §3.4）。

覆盖：
- 空队列 / 单条 delegate happy path / cap 撞顶不 pop / speak cancel fixup /
  队列空后**不**回落 scoring / turn_start.data.scores 含 winner /
  ``_kick_detect_new_artifacts`` 被调 / 每 turn 只一帧 ``turn_end`` /
  ``turn_end`` 在 ``task_artifact_added`` 之前 emit。
"""

from __future__ import annotations

import asyncio
from typing import Optional
from unittest.mock import patch

import pytest

from chahua.cursor import GuestCursor
from chahua.debug_recorder import SCORING_PATH_HANDOFF_DELEGATE
from chahua.events import (
    STATUS_CANCELLED,
    STATUS_OK,
    ChahuaEnvelope,
    ChahuaEventType,
)
from chahua.handoff import HandoffItem, HandoffKind
from chahua.orchestrator import Orchestrator, OrchestratorConfig
from chahua.room import Room
from chahua.user_md import USER_SPEAKER_ID, UserConfig

from conftest import NoopScorer, NoopSummarizer, SpeakingStubGuest


def _build(*names: str, max_ai: int = 4) -> tuple[Orchestrator, Room]:
    room = Room(name="t")
    room.add_participant(USER_SPEAKER_ID)
    orch = Orchestrator(
        room=room,
        user_config=UserConfig(display_name="老金", full_md=None, source=None),
        scorer=NoopScorer(),
        summarizer=NoopSummarizer(),
        cursor=GuestCursor(),
        config=OrchestratorConfig(
            max_consecutive_ai_turns=max_ai,
            max_speakers_per_pick=1,
            speaker_cooldown_turns=0,
            summary_block_size=999,
        ),
    )
    for n in names:
        orch.register(SpeakingStubGuest(n, room), persona_md=f"{n} persona")
    return orch, room


def _delegate(target: str) -> HandoffItem:
    return HandoffItem(kind=HandoffKind.DELEGATE, target=target)


async def test_empty_queue_returns_immediately() -> None:
    orch, _ = _build("A")
    captured: list[ChahuaEnvelope] = []
    await orch.run_pending_handoff(captured.append, task_id=None)
    assert captured == []


async def test_single_delegate_happy_path() -> None:
    orch, room = _build("A", "B")
    orch.enqueue_handoff(_delegate("A"))
    captured: list[ChahuaEnvelope] = []
    await orch.run_pending_handoff(captured.append, task_id=None)

    turn_starts = [e for e in captured if e.type is ChahuaEventType.TURN_START]
    turn_ends = [e for e in captured if e.type is ChahuaEventType.TURN_END]
    consumed = [e for e in captured if e.type is ChahuaEventType.HANDOFF_CONSUMED]
    message_ends = [
        e for e in captured if e.type is ChahuaEventType.MESSAGE_END
    ]

    assert len(turn_starts) == 1
    assert len(turn_ends) == 1
    assert len(consumed) == 1
    assert len(message_ends) == 1

    # 同一 turn_id 串起整轮（取证关联用）
    tid = turn_starts[0].turn_id
    assert tid is not None
    assert turn_ends[0].turn_id == tid
    assert consumed[0].turn_id == tid
    assert message_ends[0].turn_id == tid

    # turn_start.data.scores 含 winner（A）
    scores = turn_starts[0].data["scores"]
    assert len(scores) == 1
    assert scores[0]["guest_name"] == "A"
    assert turn_starts[0].data["scoring_path"] == SCORING_PATH_HANDOFF_DELEGATE

    # HANDOFF_CONSUMED 的 data.item 是 HandoffItem.to_dict()
    assert consumed[0].data["item"]["target"] == "A"
    assert consumed[0].data["item"]["kind"] == "delegate"

    # 队列空 → next='user'
    assert turn_ends[0].data["next"] == "user"
    assert turn_ends[0].status == STATUS_OK

    # speak 实际发生了——transcript 有 A 一条
    assert any(m.speaker_id == "A" for m in room.messages_since(0))


async def test_cap_reached_does_not_pop_queue_head() -> None:
    """队首 cap 撞顶时**不 pop**——留到下次用户触发；本 drain 不 emit turn_start。"""
    orch, _ = _build("A", max_ai=1)
    orch.enqueue_handoff(_delegate("A"))
    # 入口清零后再设 max_ai=0，让 cost(1) 立刻撞顶。
    orch.config = OrchestratorConfig(
        max_consecutive_ai_turns=0,
        max_speakers_per_pick=1,
        speaker_cooldown_turns=0,
        summary_block_size=999,
    )
    captured: list[ChahuaEnvelope] = []
    await orch.run_pending_handoff(captured.append, task_id=None)

    # 队首仍在
    assert len(orch._handoff_queue) == 1
    # 也没 emit 任何 turn 级 envelope
    assert not any(
        e.type in (ChahuaEventType.TURN_START, ChahuaEventType.TURN_END)
        for e in captured
    )


async def test_queue_empty_after_drain_does_not_fall_back_to_scoring() -> None:
    """drain 完队列空——严格分流，**不**调 ``_run_ai_chain``（docs §3.4 评审反馈 #3）。"""
    orch, _ = _build("A")
    orch.enqueue_handoff(_delegate("A"))

    with patch.object(
        Orchestrator, "_run_ai_chain", autospec=True,
    ) as run_ai:
        await orch.run_pending_handoff(lambda _e: None, task_id=None)
        assert run_ai.call_count == 0


async def test_speak_cancel_emits_turn_end_cancelled_and_keeps_remaining() -> None:
    """speak 段被 cancel：turn_end(cancelled) 必须 emit 让 UI 收回按钮；
    队列剩余项保留（cancel 不丢未消费的项）。"""

    class _BlockingGuest(SpeakingStubGuest):
        def __init__(self, name: str, room: Room, gate: asyncio.Event) -> None:
            super().__init__(name, room)
            self._gate = gate

        async def speak(self, ctx, *, turn_id, sink, task_id=None, **kw):
            self._gate.set()
            await asyncio.Event().wait()  # 永不返回，等 cancel

    gate = asyncio.Event()
    room = Room(name="t")
    room.add_participant(USER_SPEAKER_ID)
    orch = Orchestrator(
        room=room,
        user_config=UserConfig(display_name="老金", full_md=None, source=None),
        scorer=NoopScorer(),
        summarizer=NoopSummarizer(),
        cursor=GuestCursor(),
        config=OrchestratorConfig(
            max_consecutive_ai_turns=4, max_speakers_per_pick=1,
            speaker_cooldown_turns=0, summary_block_size=999,
        ),
    )
    orch.register(_BlockingGuest("A", room, gate), persona_md="A persona")
    orch.register(SpeakingStubGuest("B", room), persona_md="B persona")

    orch.enqueue_handoff(_delegate("A"))
    orch.enqueue_handoff(_delegate("B"))

    captured: list[ChahuaEnvelope] = []
    task = asyncio.create_task(
        orch.run_pending_handoff(captured.append, task_id=None),
    )
    await asyncio.wait_for(gate.wait(), timeout=2.0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    turn_ends = [e for e in captured if e.type is ChahuaEventType.TURN_END]
    assert any(
        e.status == STATUS_CANCELLED and e.data.get("next") == "user"
        for e in turn_ends
    ), [e.to_dict() for e in turn_ends]

    # B 还在队列等下次触发（cancel 不弃未消费项）
    assert len(orch._handoff_queue) == 1
    assert orch._handoff_queue[0].target == "B"


async def test_each_turn_emits_single_turn_end_frame() -> None:
    """drain 每个 turn 末尾只一帧 ``turn_end``——禁止 speculative ``next='ai'`` +
    补 ``next='user'`` 两帧（docs §8 反向评审 #3）。"""
    orch, _ = _build("A", "B")
    orch.enqueue_handoff(_delegate("A"))
    orch.enqueue_handoff(_delegate("B"))

    captured: list[ChahuaEnvelope] = []
    await orch.run_pending_handoff(captured.append, task_id=None)

    # 两个 turn 各一帧 turn_end，按 turn_id 分组检查
    turn_ends = [e for e in captured if e.type is ChahuaEventType.TURN_END]
    assert len(turn_ends) == 2
    by_turn = {e.turn_id: [] for e in turn_ends}
    for e in turn_ends:
        by_turn[e.turn_id].append(e)
    for tid, group in by_turn.items():
        assert len(group) == 1, (tid, [e.to_dict() for e in group])

    # 第 1 个 turn next='ai'（队列还有 B）；第 2 个 next='user'（drain 完）
    assert turn_ends[0].data["next"] == "ai"
    assert turn_ends[1].data["next"] == "user"


async def test_kick_detect_new_artifacts_called_per_turn() -> None:
    """每 turn 末尾必跑 ``_kick_detect_new_artifacts``——缺它 handoff turn 写
    ``./task/<x>`` 后 task_info 不刷、P5.4 通道 3 自动归集失效（docs §8 不变量）。"""
    orch, _ = _build("A")
    orch.enqueue_handoff(_delegate("A"))

    with patch.object(
        orch, "_kick_detect_new_artifacts",
    ) as detect:
        await orch.run_pending_handoff(lambda _e: None, task_id="t1")
        assert detect.call_count == 1
        # 传入的 task_id 必须与 drain 入参一致（透传不依赖 store snapshot）
        args, kwargs = detect.call_args
        # _kick_detect_new_artifacts(sink, task_id) 位置参数
        assert args[1] == "t1"


async def test_drain_drops_queued_item_whose_target_was_removed() -> None:
    """Codex review 回归：cap 撞顶留下的队首项，target 可能在后续 ``remove_guest``
    后失效。drain 必须 WARN + 跳过这种 stale 项，不能让 ``_let_speak`` 抛 KeyError
    把承运 user-turn 的 drain 整个 abort（用户消息得不到回应）。"""
    orch, _ = _build("A", "B", max_ai=2)
    # 队列：[A-stale, B] —— A 会被"删除"模拟 remove_guest
    orch.enqueue_handoff(_delegate("A"))
    orch.enqueue_handoff(_delegate("B"))
    # 模拟 remove_guest：直接从 _guests dict 删掉 A（绕过 admin 路径，单元测够用）
    del orch._guests["A"]

    captured: list[ChahuaEnvelope] = []
    await orch.run_pending_handoff(captured.append, task_id=None)

    # A 项被丢弃；B 项被消费
    assert len(orch._handoff_queue) == 0
    consumed = [
        e for e in captured if e.type is ChahuaEventType.HANDOFF_CONSUMED
    ]
    assert len(consumed) == 1
    assert consumed[0].data["item"]["target"] == "B"


async def test_user_message_resumes_stranded_handoff_queue() -> None:
    """Codex review 回归：drain 因 cap 撞顶留下队首时，下次 ``submit_user_message``
    必须续 drain；否则队列项被无限跳过（docs §3.4 "下次用户触发"含发普通消息）。"""
    # cap=1：第一轮 drain 跑 A 后撞顶，B 留在队列。
    orch, room = _build("A", "B", max_ai=1)
    orch.enqueue_handoff(_delegate("A"))
    orch.enqueue_handoff(_delegate("B"))
    captured: list[ChahuaEnvelope] = []
    await orch.run_pending_handoff(captured.append, task_id=None)
    assert len(orch._handoff_queue) == 1
    assert orch._handoff_queue[0].target == "B"

    # 用户发普通消息——应当先 drain B，再走 _run_ai_chain（cap 已用尽则 ai chain 空跑）。
    captured.clear()
    await orch.submit_user_message("继续", sink=captured.append)

    assert len(orch._handoff_queue) == 0
    # B 已发言（HANDOFF_CONSUMED 包含 target=B）
    consumed = [
        e for e in captured if e.type is ChahuaEventType.HANDOFF_CONSUMED
    ]
    assert len(consumed) == 1
    assert consumed[0].data["item"]["target"] == "B"


async def test_turn_end_emitted_before_artifact_detection_hook() -> None:
    """``turn_end`` envelope 顺序必须在 ``_kick_detect_new_artifacts`` 触发的
    ``task_artifact_added`` 之前——否则前端收到任务面板更新时旧 turn 还没结束，
    状态错位（docs §8 与 ``_run_ai_chain`` 同口径不变量）。"""
    orch, _ = _build("A")
    orch.enqueue_handoff(_delegate("A"))

    emit_order: list[str] = []

    def sink(env: ChahuaEnvelope) -> None:
        emit_order.append(env.type.value)

    # Hook 内部 emit 一条 fake task_artifact_added，断言它出现在 turn_end 之后
    real_detect = orch._kick_detect_new_artifacts

    def fake_detect(s, tid: Optional[str]) -> None:
        real_detect(s, tid)
        s(ChahuaEnvelope(
            room_id="t", turn_id=None, guest_name=None, message_id=None,
            type=ChahuaEventType.TASK_ARTIFACT_ADDED, data={"task_id": tid or "x"},
        ))

    with patch.object(
        orch, "_kick_detect_new_artifacts", side_effect=fake_detect,
    ):
        await orch.run_pending_handoff(sink, task_id="t1")

    turn_end_idx = emit_order.index("turn_end")
    art_added_idx = emit_order.index("task_artifact_added")
    assert turn_end_idx < art_added_idx, emit_order
