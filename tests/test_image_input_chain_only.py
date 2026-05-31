"""P13：``images_rel`` 只流入 ``_run_ai_chain`` 一条路径。

钉死「附图范围 = 本轮触发用户消息且只进 chain」不变量：
- chain 路径（scoring 选中茶客）的 speak 收到 images_rel；
- handoff drain 路径的 speak 收到空 images_rel；
- pre-drain（有残留队列）不接像素。
"""

from __future__ import annotations

from chahua.cursor import GuestCursor
from chahua.events import (
    STATUS_OK,
    ChahuaEnvelope,
    ChahuaEventType,
    new_message_id,
)
from chahua.handoff import HandoffItem, HandoffKind
from chahua.orchestrator import Orchestrator, OrchestratorConfig
from chahua.room import Room
from chahua.user_md import USER_SPEAKER_ID, UserConfig

from conftest import FixedScorer, NoopSummarizer


class _CapturingGuest:
    """记录每次 speak 收到的 images_rel + 写 transcript（duck-typed TeaGuest）。"""

    def __init__(self, name: str, room: Room) -> None:
        self.name = name
        self._room = room
        self.images_seen: list[tuple[str, ...]] = []

    async def speak(
        self, context_message, *, turn_id, sink, cancellation_token=None,
        task_id=None, images_rel=(),
    ):
        self.images_seen.append(tuple(images_rel))
        mid = new_message_id()
        sink(ChahuaEnvelope(
            room_id=self._room.name, turn_id=turn_id, guest_name=self.name,
            message_id=mid, type=ChahuaEventType.MESSAGE_START,
        ))
        msg = self._room.append(self.name, "(reply)", message_id=mid, task_id=task_id)
        sink(ChahuaEnvelope(
            room_id=self._room.name, turn_id=turn_id, guest_name=self.name,
            message_id=mid, type=ChahuaEventType.MESSAGE_END, status=STATUS_OK,
            seq=msg.seq, data={"text": "(reply)"},
        ))
        return msg


def _build(
    scores: dict[str, float], *, max_ai: int = 1,
) -> tuple[Orchestrator, Room, dict[str, _CapturingGuest]]:
    room = Room(name="t")
    room.add_participant(USER_SPEAKER_ID)
    orch = Orchestrator(
        room=room,
        user_config=UserConfig(display_name="老金", full_md=None, source=None),
        scorer=FixedScorer(scores),
        summarizer=NoopSummarizer(),
        cursor=GuestCursor(),
        config=OrchestratorConfig(
            max_consecutive_ai_turns=max_ai,
            max_speakers_per_pick=1,
            speaker_cooldown_turns=0,
            summary_block_size=999,
        ),
    )
    guests: dict[str, _CapturingGuest] = {}
    for n in scores:
        g = _CapturingGuest(n, room)
        guests[n] = g
        orch.register(g, persona_md=f"{n} persona")  # type: ignore[arg-type]
    return orch, room, guests


async def test_chain_speak_receives_images() -> None:
    orch, _, guests = _build({"A": 1.0})
    await orch.submit_user_message("看图", images_rel=("share/x.png",))
    assert guests["A"].images_seen == [("share/x.png",)]


async def test_drain_speak_gets_empty_images() -> None:
    """pre-drain（残留 handoff 队列）的 speak 不接像素，chain 的 speak 才接。"""
    orch, _, guests = _build({"A": 0.0, "B": 1.0}, max_ai=4)
    # A 有一个残留 delegate（pre-drain 会先消费它），B 走 chain scoring 胜出。
    orch.enqueue_handoff(HandoffItem(kind=HandoffKind.DELEGATE, target="A"))
    await orch.submit_user_message("看图", images_rel=("share/x.png",))
    # drain 路径的 A 收到空；chain 路径的 B 第一周期（回应用户）看见图。
    assert guests["A"].images_seen == [()]
    assert guests["B"].images_seen  # B 至少发言一次
    assert guests["B"].images_seen[0] == ("share/x.png",)
    # B 在后续 AI 接力周期不再重发像素（first-cycle-only 不变量）。
    assert all(seen == () for seen in guests["B"].images_seen[1:])


async def test_images_only_first_cycle_not_ai_continuation() -> None:
    """多轮 AI 接力：只有回应用户的第一个 pick 周期带像素，后续接力周期退回文本。"""
    orch, _, guests = _build({"A": 1.0}, max_ai=4)
    await orch.submit_user_message("看图", images_rel=("share/x.png",))
    # A 反复被选中（cooldown=0），但只有第一次发言看见图。
    assert len(guests["A"].images_seen) >= 2
    assert guests["A"].images_seen[0] == ("share/x.png",)
    assert all(seen == () for seen in guests["A"].images_seen[1:])


async def test_no_images_chain_gets_empty_tuple() -> None:
    orch, _, guests = _build({"A": 1.0})
    await orch.submit_user_message("hi")
    assert guests["A"].images_seen == [()]
