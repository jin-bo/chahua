"""P7.2.5 承重墙：``run_pending_handoff`` review 项 drain（docs/P7.2 §4）。

覆盖：
- review 项 drain 跑通，winner prompt 含 ``<review_target>`` 且内嵌被审消息原文
  （``format_messages`` 的 ``<message>`` 包装）。
- delegate 项行为不回归（``extra_blocks=None``，无 ``<review_target>``）。
- ``scoring_path=="handoff_review"`` 落 turns.jsonl。
- review 项 turn 末尾 5 步与 delegate 同口径（单帧 turn_end / next=user）。
"""

from __future__ import annotations

from chahua._persist import read_jsonl_skip_bad
from chahua.cursor import GuestCursor
from chahua.debug_recorder import SCORING_PATH_HANDOFF_REVIEW, TurnRecorder
from chahua.events import ChahuaEnvelope, ChahuaEventType
from chahua.handoff import HandoffItem, HandoffKind
from chahua.orchestrator import Orchestrator, OrchestratorConfig
from chahua.room import Room
from chahua.user_md import USER_SPEAKER_ID, UserConfig

from conftest import NoopScorer, NoopSummarizer, SpeakingStubGuest


class _CapturingGuest(SpeakingStubGuest):
    """记录最近一次 speak 收到的 context_message，给 prompt 断言用。"""

    def __init__(self, name: str, room: Room) -> None:
        super().__init__(name, room)
        self.last_ctx: str | None = None

    async def speak(self, context_message, *, turn_id, sink, **kw):
        self.last_ctx = context_message
        return await super().speak(
            context_message, turn_id=turn_id, sink=sink, **kw,
        )


def _build(*names: str, recorder=None) -> tuple[Orchestrator, Room]:
    room = Room(name="t")
    room.add_participant(USER_SPEAKER_ID)
    kwargs = {}
    if recorder is not None:
        kwargs["recorder"] = recorder
    orch = Orchestrator(
        room=room,
        user_config=UserConfig(display_name="老金", full_md=None, source=None),
        scorer=NoopScorer(),
        summarizer=NoopSummarizer(),
        cursor=GuestCursor(),
        config=OrchestratorConfig(
            max_consecutive_ai_turns=4,
            max_speakers_per_pick=1,
            speaker_cooldown_turns=0,
            summary_block_size=999,
        ),
        **kwargs,
    )
    for n in names:
        orch.register(_CapturingGuest(n, room), persona_md=f"{n} persona")
    return orch, room


async def test_review_item_drains_and_injects_review_target() -> None:
    orch, room = _build("汪小姐", "魏理论")
    # 汪小姐先发一条会被审的消息。
    reviewed = room.append("汪小姐", "我觉得方案 A 更稳。")
    orch.enqueue_handoff(HandoffItem(
        kind=HandoffKind.REVIEW, target="魏理论",
        review_message_id=reviewed.message_id,
    ))

    captured: list[ChahuaEnvelope] = []
    await orch.run_pending_handoff(captured.append, task_id=None)

    reviewer = orch._guests["魏理论"].guest
    ctx = reviewer.last_ctx
    assert ctx is not None
    assert "<review_target>" in ctx and "</review_target>" in ctx
    # 被审消息原文走 format_messages 的 <message> 包装。
    assert "<message>\n汪小姐 说：我觉得方案 A 更稳。\n</message>" in ctx

    turn_starts = [e for e in captured if e.type is ChahuaEventType.TURN_START]
    assert turn_starts[0].data["scoring_path"] == SCORING_PATH_HANDOFF_REVIEW


async def test_delegate_item_has_no_review_target() -> None:
    """delegate 项不回归——extra_blocks=None，prompt 无 <review_target>。"""
    orch, room = _build("汪小姐")
    orch.enqueue_handoff(HandoffItem(kind=HandoffKind.DELEGATE, target="汪小姐"))
    await orch.run_pending_handoff(lambda _e: None, task_id=None)
    assert "<review_target>" not in (orch._guests["汪小姐"].guest.last_ctx or "")


async def test_review_turn_end_single_frame_next_user() -> None:
    """review 项 turn 末尾 5 步与 delegate 同口径：单帧 turn_end、队空 next=user。"""
    orch, room = _build("汪小姐", "魏理论")
    reviewed = room.append("汪小姐", "草稿在此。")
    orch.enqueue_handoff(HandoffItem(
        kind=HandoffKind.REVIEW, target="魏理论",
        review_message_id=reviewed.message_id,
    ))
    captured: list[ChahuaEnvelope] = []
    await orch.run_pending_handoff(captured.append, task_id=None)

    turn_ends = [e for e in captured if e.type is ChahuaEventType.TURN_END]
    assert len(turn_ends) == 1
    assert turn_ends[0].data["next"] == "user"


async def test_review_scoring_path_lands_in_turns_jsonl(tmp_path) -> None:
    recorder = TurnRecorder(tmp_path, enabled=True, capture_prompts=False)
    orch, room = _build("汪小姐", "魏理论", recorder=recorder)
    reviewed = room.append("汪小姐", "结论先写这里。")
    orch.enqueue_handoff(HandoffItem(
        kind=HandoffKind.REVIEW, target="魏理论",
        review_message_id=reviewed.message_id,
    ))
    await orch.run_pending_handoff(lambda _e: None, task_id=None)

    rows = list(read_jsonl_skip_bad(tmp_path / "debug" / "turns.jsonl"))
    assert len(rows) == 1
    assert rows[0]["scoring_path"] == SCORING_PATH_HANDOFF_REVIEW
    # trigger.handoff_item 自带 review_message_id（to_dict 全字段）。
    assert rows[0]["trigger"]["handoff_item"]["kind"] == "review"
    assert (
        rows[0]["trigger"]["handoff_item"]["review_message_id"]
        == reviewed.message_id
    )
