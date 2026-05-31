"""P13：视觉附图纯瞬态 —— 不进 transcript / 不改 Message dataclass / 不入 envelope。

钉死「无持久化」不变量：发带图 user_message 后 transcript.jsonl 该行无 images 字段、
``Message`` 无 images 属性（防回归引入持久字段）。base64 只懒读现传，不落盘。
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

from chahua.cursor import GuestCursor
from chahua.events import (
    STATUS_OK,
    ChahuaEnvelope,
    ChahuaEventType,
    new_message_id,
)
from chahua.orchestrator import Orchestrator, OrchestratorConfig
from chahua.room import Message, Room
from chahua.user_md import USER_SPEAKER_ID, UserConfig

from conftest import FixedScorer, NoopSummarizer


class _StubGuest:
    def __init__(self, name: str, room: Room) -> None:
        self.name = name
        self._room = room

    async def speak(
        self, context_message, *, turn_id, sink, cancellation_token=None,
        task_id=None, images_rel=(),
    ):
        mid = new_message_id()
        return self._room.append(self.name, "(reply)", message_id=mid, task_id=task_id)


def test_message_dataclass_has_no_images_field() -> None:
    fields = {f.name for f in dataclasses.fields(Message)}
    assert "images" not in fields
    assert "images_rel" not in fields


async def test_transcript_row_has_no_images_field(tmp_path: Path) -> None:
    transcript = tmp_path / "transcript.jsonl"
    room = Room(name="t", transcript_path=transcript)
    room.add_participant(USER_SPEAKER_ID)
    orch = Orchestrator(
        room=room,
        user_config=UserConfig(display_name="老金", full_md=None, source=None),
        scorer=FixedScorer({"A": 1.0}),
        summarizer=NoopSummarizer(),
        cursor=GuestCursor(),
        config=OrchestratorConfig(
            max_consecutive_ai_turns=1, max_speakers_per_pick=1,
            speaker_cooldown_turns=0, summary_block_size=999,
        ),
    )
    orch.register(_StubGuest("A", room), persona_md="A persona")  # type: ignore[arg-type]

    await orch.submit_user_message("看图", images_rel=("share/x.png",))

    rows = [json.loads(line) for line in transcript.read_text().splitlines() if line]
    assert rows, "transcript 应有用户消息行"
    user_row = next(r for r in rows if r["speaker_id"] == USER_SPEAKER_ID)
    assert "images" not in user_row
    assert "images_rel" not in user_row
    # 用户文本里也不应混进 base64（懒读不入库）。
    assert "base64" not in user_row["text"]
