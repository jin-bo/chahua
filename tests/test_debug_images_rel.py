"""P13 C3：debug 取证记 ``images_rel``（rel-only），bytes 绝不入盘。"""

from __future__ import annotations

from pathlib import Path

from chahua._persist import read_jsonl_skip_bad
from chahua.debug_recorder import TurnRecorder


def _one_turn_with_images(tmp_path: Path, images_rel) -> dict:
    rec = TurnRecorder(tmp_path, enabled=True, capture_prompts=False)
    rec.start_turn(turn_id="turn_abc", task_id=None, trigger={"kind": "user_msg", "ref_seq": 0})
    rec.record_message_start(
        message_id="m1", guest="A", speak_prompt="ctx", images_rel=images_rel,
    )
    rec.record_message_end(message_id="m1", status="ok", seq=1)
    rec.flush_turn()
    rows = list(read_jsonl_skip_bad(tmp_path / "debug" / "turns.jsonl"))
    assert len(rows) == 1
    return rows[0]


def test_images_rel_recorded_rel_only(tmp_path: Path) -> None:
    row = _one_turn_with_images(tmp_path, ("share/x.png", "share/y.jpg"))
    msg = row["messages"][0]
    assert msg["images_rel"] == ["share/x.png", "share/y.jpg"]
    # 绝不落 bytes / base64。
    blob = (tmp_path / "debug" / "turns.jsonl").read_text()
    assert "base64" not in blob
    assert "data" not in msg  # 没有 base64 data 字段


def test_no_images_omits_field(tmp_path: Path) -> None:
    row = _one_turn_with_images(tmp_path, ())
    assert "images_rel" not in row["messages"][0]
