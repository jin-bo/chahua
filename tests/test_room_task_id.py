"""P5.1.2: Message.task_id 字段 + Room.append 接入。

回归覆盖：
- 旧 transcript.jsonl（无 task_id 字段）加载不变
- 新 append(task_id=...) 落盘 round-trip
- task_id 非 str 时降级为 None + WARN
- to_jsonl_dict 在 task_id=None 时不写键（旧行兼容）
"""

from __future__ import annotations

import json
from pathlib import Path

from chahua.room import Message, Room


def _read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def test_message_default_task_id_is_none():
    m = Message(
        seq=1, speaker_id="user", text="hi", ts_ms=1, message_id="msg_1",
    )
    assert m.task_id is None


def test_message_to_jsonl_omits_task_id_when_none():
    m = Message(
        seq=1, speaker_id="user", text="hi", ts_ms=1, message_id="msg_1",
    )
    d = m.to_jsonl_dict()
    assert "task_id" not in d


def test_message_to_jsonl_includes_task_id_when_set():
    m = Message(
        seq=1, speaker_id="user", text="hi", ts_ms=1,
        message_id="msg_1", task_id="task_abc",
    )
    assert m.to_jsonl_dict()["task_id"] == "task_abc"


def test_message_from_jsonl_old_rows_load_with_none_task_id():
    old = {
        "seq": 1, "speaker_id": "user", "message_id": "msg_1",
        "ts_ms": 1, "text": "old",
    }
    m = Message.from_jsonl(old)
    assert m is not None
    assert m.task_id is None


def test_message_from_jsonl_with_task_id():
    obj = {
        "seq": 1, "speaker_id": "user", "message_id": "msg_1",
        "ts_ms": 1, "text": "new", "task_id": "task_x",
    }
    m = Message.from_jsonl(obj)
    assert m is not None
    assert m.task_id == "task_x"


def test_message_from_jsonl_invalid_task_id_falls_back_to_none(caplog):
    obj = {
        "seq": 1, "speaker_id": "user", "message_id": "msg_1",
        "ts_ms": 1, "text": "x", "task_id": 42,
    }
    m = Message.from_jsonl(obj)
    assert m is not None
    assert m.task_id is None
    assert any("task_id" in r.message for r in caplog.records)


def test_room_append_with_task_id_round_trip(tmp_path: Path):
    transcript = tmp_path / "transcript.jsonl"
    r = Room(name="t", transcript_path=transcript)
    r.add_participant("user")
    msg = r.append("user", "hello", task_id="task_abc")
    assert msg.task_id == "task_abc"

    rows = _read_jsonl(transcript)
    assert len(rows) == 1
    assert rows[0]["task_id"] == "task_abc"

    # 重 load 房间 —— task_id 仍在
    r2 = Room(name="t", transcript_path=transcript)
    last = r2.last_message()
    assert last is not None
    assert last.task_id == "task_abc"


def test_room_append_without_task_id_writes_no_task_id_field(tmp_path: Path):
    transcript = tmp_path / "transcript.jsonl"
    r = Room(name="t", transcript_path=transcript)
    r.add_participant("user")
    r.append("user", "hello")
    rows = _read_jsonl(transcript)
    assert "task_id" not in rows[0]


def test_room_loads_mixed_old_and_new_rows(tmp_path: Path):
    transcript = tmp_path / "transcript.jsonl"
    transcript.parent.mkdir(parents=True, exist_ok=True)
    transcript.write_text(
        "\n".join([
            json.dumps({
                "seq": 1, "speaker_id": "user", "message_id": "m1",
                "ts_ms": 1, "text": "old",
            }),
            json.dumps({
                "seq": 2, "speaker_id": "user", "message_id": "m2",
                "ts_ms": 2, "text": "new", "task_id": "task_x",
            }),
            "",
        ]),
        encoding="utf-8",
    )
    r = Room(name="t", transcript_path=transcript)
    msgs = r.messages_since(0)
    assert len(msgs) == 2
    assert msgs[0].task_id is None
    assert msgs[1].task_id == "task_x"
