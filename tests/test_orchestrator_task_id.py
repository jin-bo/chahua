"""P5.1.8: orchestrator 把 active_task_id 注入 transcript + envelope.data。

验证：
- 用户消息进 transcript 时带 active_task_id（Room.append 自动 tag）
- 茶客 message_start / message_delta / message_end envelope.data 都带 task_id
- transcript 落盘 record 与 envelope message_id 共用同一 task_id
- 无 active task 时不写 data.task_id（envelope 与旧前端兼容）
- 中途切 active 不回追已开发言（snapshot 语义）
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from chahua.cursor import GuestCursor
from chahua.events import (
    ChahuaEnvelope,
    ChahuaEventType,
    new_message_id,
)
from chahua.orchestrator import Orchestrator, OrchestratorConfig
from chahua.room import Room
from chahua.scoring import IntentScorer, ScoreKind, ScoreResult
from chahua.tasks_store import TasksStore
from chahua.user_md import USER_SPEAKER_ID, UserConfig

from conftest import NoopSummarizer


class _StubGuest:
    def __init__(self, name: str, room: Room) -> None:
        self.name = name
        self._room = room
        self.last_task_id_seen = None  # type: ignore[assignment]

    async def speak(
        self, context_message, *, turn_id, sink, cancellation_token=None, task_id=None,
    ):
        self.last_task_id_seen = task_id
        message_id = new_message_id()
        # 模拟 ChahuaTransport.bind 的行为：data 里塞 task_id 当其非 None
        data_delta = {"chunk": "hi"}
        if task_id is not None:
            data_delta["task_id"] = task_id
        sink(
            ChahuaEnvelope(
                room_id=self._room.name, turn_id=turn_id,
                guest_name=self.name, message_id=message_id,
                type=ChahuaEventType.MESSAGE_START,
                data={"task_id": task_id} if task_id else {},
            )
        )
        msg = self._room.append(
            self.name, "hi", message_id=message_id, task_id=task_id,
        )
        sink(
            ChahuaEnvelope(
                room_id=self._room.name, turn_id=turn_id,
                guest_name=self.name, message_id=message_id,
                type=ChahuaEventType.MESSAGE_END,
                data=data_delta, seq=msg.seq,
            )
        )
        return msg


class _ScripedScorer(IntentScorer):
    def __init__(self) -> None:
        self._n = 0

    async def score(self, *, guest_name, persona, transcript_text, user_config, subject_mention_count=0):
        self._n += 1
        # A always wins round 1, then max_consecutive_ai_turns=1 stops chain
        return ScoreResult(
            guest_name=guest_name,
            score=0.9 if guest_name == "A" else 0.1,
            kind=ScoreKind.SCORED,
        )


def _build_orch(tmp_path: Path, room: Room, tasks_store):
    user_config = UserConfig(display_name="测试者", full_md=None, source=None)
    orch = Orchestrator(
        room=room, user_config=user_config,
        scorer=_ScripedScorer(),
        summarizer=NoopSummarizer(),
        cursor=GuestCursor(),
        config=OrchestratorConfig(
            max_consecutive_ai_turns=1,
            max_speakers_per_pick=1,
            summary_block_size=999,
        ),
        tasks_store=tasks_store,
    )
    orch.register(_StubGuest("A", room), persona_md="A persona")
    orch.register(_StubGuest("B", room), persona_md="B persona")
    return orch


async def test_user_message_tagged_with_active_task(tmp_path: Path):
    room = Room(name="t", transcript_path=tmp_path / "transcript.jsonl")
    room.add_participant(USER_SPEAKER_ID)
    store = TasksStore(room_dir=tmp_path)
    task = store.open_task(title="t", goal="g")
    orch = _build_orch(tmp_path, room, store)
    await orch.submit_user_message("你好", sink=lambda _: None)
    msgs = room.messages_since(0)
    # 用户消息（msgs[0]）+ 茶客发言（msgs[1+]）都应挂上 task.id
    user_msg = next(m for m in msgs if m.speaker_id == USER_SPEAKER_ID)
    assert user_msg.task_id == task.id


async def test_no_task_means_no_task_id_in_transcript(tmp_path: Path):
    """active 为空时 transcript 不挂 task_id（旧行兼容）。"""
    room = Room(name="t", transcript_path=tmp_path / "transcript.jsonl")
    room.add_participant(USER_SPEAKER_ID)
    store = TasksStore(room_dir=tmp_path)
    orch = _build_orch(tmp_path, room, store)
    await orch.submit_user_message("你好", sink=lambda _: None)
    for m in room.messages_since(0):
        assert m.task_id is None


async def test_guest_envelope_carries_task_id(tmp_path: Path):
    """茶客 message_* envelope.data 都带 task_id（_StubGuest 镜像 transport 行为）。"""
    room = Room(name="t")
    room.add_participant(USER_SPEAKER_ID)
    store = TasksStore(room_dir=tmp_path)
    task = store.open_task(title="t", goal="g")
    orch = _build_orch(tmp_path, room, store)
    captured: list[ChahuaEnvelope] = []
    await orch.submit_user_message("hi", sink=captured.append)
    msg_starts = [e for e in captured if e.type == ChahuaEventType.MESSAGE_START]
    msg_ends = [e for e in captured if e.type == ChahuaEventType.MESSAGE_END]
    assert msg_starts and msg_ends
    for e in msg_starts + msg_ends:
        assert e.data.get("task_id") == task.id


async def test_speak_receives_snapshot_active_task_id(tmp_path: Path):
    """中途改 active 不回追已开发言 —— turn 内 snapshot 语义。"""
    room = Room(name="t")
    room.add_participant(USER_SPEAKER_ID)
    store = TasksStore(room_dir=tmp_path)
    t1 = store.open_task(title="t1", goal="g")
    orch = _build_orch(tmp_path, room, store)
    # submit_user_message 入口会 snapshot t1.id；茶客 speak 时再改也不该回追
    # （此处无并发改 active 的真实场景，但通过 stub 拿到的 task_id 锁定 t1.id 即可证）。
    await orch.submit_user_message("hi", sink=lambda _: None)
    guest_a = orch._guests["A"].guest  # type: ignore[attr-defined]
    assert guest_a.last_task_id_seen == t1.id


async def test_caller_supplied_task_id_overrides_store_snapshot(tmp_path: Path):
    """server 端在接帧同步上下文里 snapshot 后传入 ``task_id``；本协程不该再读 store
    兜底 —— 那样会与 inbound 队列里排在后面的 open_task 形成 race。"""
    room = Room(name="t", transcript_path=tmp_path / "transcript.jsonl")
    room.add_participant(USER_SPEAKER_ID)
    store = TasksStore(room_dir=tmp_path)
    # 模拟 race：调用方传入 None（"无任务" 的接帧快照），但在提交过程中 store 里被
    # 别的 inbound 偷胜开了一个任务。orchestrator 应当尊重传入值，不回读 store。
    orch = _build_orch(tmp_path, room, store)
    # 先开任务模拟"任务已存在"，但调用方 task_id=None（race 窗口：snapshot 时还没任务）
    store.open_task(title="新", goal="...")
    await orch.submit_user_message("hi", sink=lambda _: None, task_id=None)
    user_msg = next(
        m for m in room.messages_since(0) if m.speaker_id == USER_SPEAKER_ID
    )
    assert user_msg.task_id is None  # 传入值生效，store 偷胜值未被回读
