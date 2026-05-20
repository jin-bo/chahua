"""P7.3.3 承重墙：``run_pending_handoff`` panel 项 drain（docs/P7.3 §4）。

覆盖：
- N 人 panel（带 summarizer）drain 跑通：一个 turn_id、N+1 个 message、
  turn_start.data.scores 含 N+1 个 winner。
- panelist prompt 含 ``<panel_context>`` 列出全体 panelist（不含 summarizer）。
- summarizer 是 ``winners[-1]``、prompt 含 ``<panel_summary_request>``。
- ``scoring_path`` 全程 ``handoff_panel``。
- panel ``cost=N(+1)``；档 ① 死项 drop、档 ② cap 撞顶不 pop。
- runtime 欠员过滤：panelist < 2 → 整项丢弃（不留孤儿 summarizer）；
  summarizer 失效但 panelist ≥ 2 → panel 跑无汇总。
- panel turn 中段 cancel → turn_end(cancelled) + 已说完 message 保留。
- ``_resolve_handoff_render`` 返回 winners / winner_blocks 等长。
"""

from __future__ import annotations

import asyncio

import pytest

from chahua._persist import read_jsonl_skip_bad
from chahua.cursor import GuestCursor
from chahua.debug_recorder import SCORING_PATH_HANDOFF_PANEL, TurnRecorder
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


def _build(
    *names: str, max_ai: int = 4, recorder=None,
) -> tuple[Orchestrator, Room]:
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
            max_consecutive_ai_turns=max_ai,
            max_speakers_per_pick=1,
            speaker_cooldown_turns=0,
            summary_block_size=999,
        ),
        **kwargs,
    )
    for n in names:
        orch.register(_CapturingGuest(n, room), persona_md=f"{n} persona")
    return orch, room


def _panel(targets, summarizer=None) -> HandoffItem:
    return HandoffItem(
        kind=HandoffKind.PANEL, targets=tuple(targets), summarizer=summarizer,
    )


async def test_panel_with_summarizer_drains_in_one_turn() -> None:
    """① N 人 panel + summarizer：一个 turn_id 下 N+1 个 message、
    turn_start.data.scores 含 N+1 个 winner。"""
    orch, room = _build("C", "D", "E", "F")
    orch.enqueue_handoff(_panel(["C", "D", "E"], summarizer="F"))

    captured: list[ChahuaEnvelope] = []
    await orch.run_pending_handoff(captured.append, task_id=None)

    turn_starts = [e for e in captured if e.type is ChahuaEventType.TURN_START]
    turn_ends = [e for e in captured if e.type is ChahuaEventType.TURN_END]
    message_ends = [e for e in captured if e.type is ChahuaEventType.MESSAGE_END]
    assert len(turn_starts) == 1
    assert len(turn_ends) == 1
    assert len(message_ends) == 4  # C/D/E + summarizer F

    tid = turn_starts[0].turn_id
    assert tid is not None
    assert all(e.turn_id == tid for e in message_ends)
    assert turn_ends[0].turn_id == tid

    scores = turn_starts[0].data["scores"]
    assert [s["guest_name"] for s in scores] == ["C", "D", "E", "F"]
    assert turn_starts[0].data["scoring_path"] == SCORING_PATH_HANDOFF_PANEL


async def test_panelist_prompt_has_panel_context_listing_all_panelists() -> None:
    """② panelist prompt 含 <panel_context> 且列出全体 panelist，不含 summarizer。"""
    orch, _ = _build("C", "D", "E", "F")
    orch.enqueue_handoff(_panel(["C", "D", "E"], summarizer="F"))
    await orch.run_pending_handoff(lambda _e: None, task_id=None)

    for name in ("C", "D", "E"):
        ctx = orch._guests[name].guest.last_ctx or ""
        assert "<panel_context>" in ctx and "</panel_context>" in ctx
        assert "C、D、E" in ctx
        assert "<panel_summary_request>" not in ctx


async def test_summarizer_is_last_and_gets_summary_block() -> None:
    """③ summarizer 是 winners[-1]、prompt 含 <panel_summary_request>、
    不含 <panel_context>（它不是 panelist）。"""
    orch, _ = _build("C", "D", "F")
    orch.enqueue_handoff(_panel(["C", "D"], summarizer="F"))
    await orch.run_pending_handoff(lambda _e: None, task_id=None)

    ctx = orch._guests["F"].guest.last_ctx or ""
    assert "<panel_summary_request>" in ctx
    assert "<panel_context>" not in ctx


async def test_scoring_path_lands_in_turns_jsonl(tmp_path) -> None:
    """④ panel turn 的 scoring_path 全程 handoff_panel。"""
    recorder = TurnRecorder(tmp_path, enabled=True, capture_prompts=False)
    orch, _ = _build("C", "D", "E", "F", recorder=recorder)
    orch.enqueue_handoff(_panel(["C", "D", "E"], summarizer="F"))
    await orch.run_pending_handoff(lambda _e: None, task_id=None)

    rows = list(read_jsonl_skip_bad(tmp_path / "debug" / "turns.jsonl"))
    assert len(rows) == 1
    assert rows[0]["scoring_path"] == SCORING_PATH_HANDOFF_PANEL
    assert rows[0]["scoring"]["winners"] == ["C", "D", "E", "F"]
    assert rows[0]["trigger"]["handoff_item"]["kind"] == "panel"
    assert rows[0]["trigger"]["handoff_item"]["targets"] == ["C", "D", "E"]
    assert rows[0]["trigger"]["handoff_item"]["summarizer"] == "F"


async def test_panel_without_summarizer_runs_n_panelists() -> None:
    """summarizer 缺省：panel 跑 N 位、无汇总那一发。"""
    orch, _ = _build("C", "D")
    orch.enqueue_handoff(_panel(["C", "D"]))
    captured: list[ChahuaEnvelope] = []
    await orch.run_pending_handoff(captured.append, task_id=None)

    message_ends = [e for e in captured if e.type is ChahuaEventType.MESSAGE_END]
    assert len(message_ends) == 2


async def test_panel_cost_blocks_second_item_at_cap() -> None:
    """⑥ panel cost=N(+1)：3 人 panel + summarizer cost=4，max_ai=4 时跑得动，
    其后队列项档 ② 撞 cap 不 pop、留队首。"""
    orch, _ = _build("C", "D", "E", "F", "G", max_ai=4)
    orch.enqueue_handoff(_panel(["C", "D", "E"], summarizer="F"))
    orch.enqueue_handoff(HandoffItem(kind=HandoffKind.DELEGATE, target="G"))
    captured: list[ChahuaEnvelope] = []
    await orch.run_pending_handoff(captured.append, task_id=None)

    # panel 跑了一个 turn（cost 4 == max），G 留在队首
    turn_starts = [e for e in captured if e.type is ChahuaEventType.TURN_START]
    assert len(turn_starts) == 1
    assert len(orch._handoff_queue) == 1
    assert orch._handoff_queue[0].target == "G"


async def test_dead_panel_item_cost_exceeds_max_is_dropped() -> None:
    """⑩ 档 ①：cost > max_consecutive_ai_turns 的 panel 死项 → pop + drop + WARN，
    **不 break**，后续队列项照常被消费。"""
    orch, _ = _build("C", "D", "E", "G", max_ai=2)
    # 3 人 panel cost=3 > max=2 → 死项。
    orch.enqueue_handoff(_panel(["C", "D", "E"]))
    orch.enqueue_handoff(HandoffItem(kind=HandoffKind.DELEGATE, target="G"))
    captured: list[ChahuaEnvelope] = []
    await orch.run_pending_handoff(captured.append, task_id=None)

    # 死 panel 被丢弃；后续 delegate G 没被挡死，照常跑。
    assert len(orch._handoff_queue) == 0
    assert any(
        e.type is ChahuaEventType.MESSAGE_END and e.guest_name == "G"
        for e in captured
    )


async def test_dead_panel_does_not_block_later_runnable_item() -> None:
    """codex review P2 回归：valid 项后面跟一个死 panel、死 panel 后面还有
    runnable 项时，turn 末尾 lookahead 必须先清死项——否则死项让 has_next
    误判提前 return，把 runnable 项挡到下次用户触发。

    队列 [delegate A, panel(C,D,E) cost=3, delegate B]，max_ai=2：A 跑完后
    死 panel（cost 3 > 2）应被清掉，B 照常跑。"""
    orch, _ = _build("A", "C", "D", "E", "B", max_ai=2)
    orch.enqueue_handoff(HandoffItem(kind=HandoffKind.DELEGATE, target="A"))
    orch.enqueue_handoff(_panel(["C", "D", "E"]))  # cost 3 > max 2 → 死项
    orch.enqueue_handoff(HandoffItem(kind=HandoffKind.DELEGATE, target="B"))
    captured: list[ChahuaEnvelope] = []
    await orch.run_pending_handoff(captured.append, task_id=None)

    # A 和 B 都跑了、死 panel 被清、队列空。
    spoken = [
        e.guest_name for e in captured
        if e.type is ChahuaEventType.MESSAGE_END
    ]
    assert spoken == ["A", "B"]
    assert len(orch._handoff_queue) == 0


async def test_underfilled_panel_dropped_no_orphan_summarizer() -> None:
    """⑦ panelist 过滤到 1 人 → 整项丢弃（含 summarizer，队列里不留孤儿）。"""
    orch, _ = _build("C", "F")  # D / E 未注册 → 过滤后只剩 C
    orch.enqueue_handoff(_panel(["C", "D", "E"], summarizer="F"))
    captured: list[ChahuaEnvelope] = []
    await orch.run_pending_handoff(captured.append, task_id=None)

    assert len(orch._handoff_queue) == 0
    assert not any(
        e.type is ChahuaEventType.TURN_START for e in captured
    )
    # summarizer F 没被单独 speak（不留孤儿）。
    assert not any(
        e.type is ChahuaEventType.MESSAGE_END for e in captured
    )


async def test_summarizer_unavailable_panel_runs_without_summary() -> None:
    """⑧ summarizer 失效但 panelist ≥ 2 → panel 照跑 N 位、无汇总那一发。"""
    orch, _ = _build("C", "D")  # summarizer F 未注册
    orch.enqueue_handoff(_panel(["C", "D"], summarizer="F"))
    captured: list[ChahuaEnvelope] = []
    await orch.run_pending_handoff(captured.append, task_id=None)

    message_ends = [e for e in captured if e.type is ChahuaEventType.MESSAGE_END]
    assert [e.guest_name for e in message_ends] == ["C", "D"]


async def test_panel_mid_turn_cancel_keeps_finished_messages() -> None:
    """⑨ panel turn 中段 cancel：已说完的 panelist message 保留、
    turn_end(cancelled) emit。"""

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
    orch.register(SpeakingStubGuest("C", room), persona_md="C persona")
    orch.register(_BlockingGuest("D", room, gate), persona_md="D persona")
    orch.register(SpeakingStubGuest("E", room), persona_md="E persona")
    orch.enqueue_handoff(_panel(["C", "D", "E"]))

    captured: list[ChahuaEnvelope] = []
    task = asyncio.create_task(
        orch.run_pending_handoff(captured.append, task_id=None),
    )
    await asyncio.wait_for(gate.wait(), timeout=2.0)  # 等 C 说完、D 卡住
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # C 已说完一条 ok message。
    message_ends = [e for e in captured if e.type is ChahuaEventType.MESSAGE_END]
    assert any(
        e.guest_name == "C" and e.status == STATUS_OK for e in message_ends
    )
    turn_ends = [e for e in captured if e.type is ChahuaEventType.TURN_END]
    assert any(
        e.status == STATUS_CANCELLED and e.data.get("next") == "user"
        for e in turn_ends
    )


def test_resolve_handoff_render_returns_equal_length() -> None:
    """⑪ _resolve_handoff_render 返回的 winners / winner_blocks 等长。"""
    orch, _ = _build("C", "D", "E", "F")
    # panel + summarizer
    winners, path, blocks = orch._resolve_handoff_render(
        _panel(["C", "D", "E"], summarizer="F"),
    )
    assert len(winners) == len(blocks) == 4
    assert winners == ["C", "D", "E", "F"]
    assert path == SCORING_PATH_HANDOFF_PANEL
    # panel 无 summarizer
    winners, _, blocks = orch._resolve_handoff_render(_panel(["C", "D"]))
    assert len(winners) == len(blocks) == 2
    # delegate
    winners, _, blocks = orch._resolve_handoff_render(
        HandoffItem(kind=HandoffKind.DELEGATE, target="C"),
    )
    assert len(winners) == len(blocks) == 1
    assert blocks == [None]


def test_handoff_cost() -> None:
    """panel cost = panelist 数 + 有无 summarizer；delegate 恒 1。"""
    assert Orchestrator._handoff_cost(_panel(["C", "D", "E"], summarizer="F")) == 4
    assert Orchestrator._handoff_cost(_panel(["C", "D"])) == 2
    assert Orchestrator._handoff_cost(
        HandoffItem(kind=HandoffKind.DELEGATE, target="C"),
    ) == 1
