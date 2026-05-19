"""``TurnRecorder`` 按 ``max_turns`` 截断（P6.3.B，docs/P6.3 §9）。

覆盖：

- ``flush_turn`` 自增 ``_turn_count``，超 cap 时按事务删最老 turn_id（jsonl 行 +
  ``prompts/<turn_id>/`` 子目录）。
- ``__init__`` 期数 ``_turn_count`` + 兜底 rotate（启动期把上次累积的超额一次清掉）。
- ``max_turns = 0`` 关 rotation；负值由 config 层拒，本层 defensive 走"关"语义。
- rotation 失败不阻断（IO try/except 包到底）。
"""

from __future__ import annotations

import json

import pytest

from chahua._persist import read_jsonl_skip_bad
from chahua.debug_recorder import TurnRecorder


def _start_flush(recorder: TurnRecorder, turn_id: str) -> None:
    """简化：开一帧 → 立刻 flush。Test 不关心 in-flight 内容，只关心 jsonl 行数。"""
    recorder.start_turn(
        turn_id=turn_id, task_id=None,
        trigger={"kind": "user_msg", "ref_seq": None},
    )
    recorder.flush_turn()


def _read_turn_ids(path) -> list[str]:
    return [row["turn_id"] for row in read_jsonl_skip_bad(path)]


def test_max_turns_default_500(tmp_path):
    """默认形参 ``max_turns=500`` —— 既有 P6.1 调用方零改动。"""
    rec = TurnRecorder(tmp_path, enabled=True, capture_prompts=True)
    assert rec._max_turns == 500


def test_rotation_drops_oldest(tmp_path):
    rec = TurnRecorder(
        tmp_path, enabled=True, capture_prompts=False, max_turns=3,
    )
    for i in range(5):
        _start_flush(rec, f"turn_{i:020x}")
    ids = _read_turn_ids(rec._turns_path)
    # 5 写入 → cap 3：剪掉最老 2 条，保最新 3 条。
    assert len(ids) == 3
    assert ids == [f"turn_{i:020x}" for i in range(2, 5)]
    assert rec._turn_count == 3


def test_rotation_zero_disables(tmp_path):
    """``max_turns = 0`` 是"关 rotation"：写多少 jsonl 就长多少行。"""
    rec = TurnRecorder(
        tmp_path, enabled=True, capture_prompts=False, max_turns=0,
    )
    for i in range(20):
        _start_flush(rec, f"turn_{i:020x}")
    ids = _read_turn_ids(rec._turns_path)
    assert len(ids) == 20
    assert rec._turn_count == 20


def test_negative_max_turns_defensive(tmp_path):
    """config 层拒负值；本层 defensive 走"关 rotation"。"""
    rec = TurnRecorder(
        tmp_path, enabled=True, capture_prompts=False, max_turns=-5,
    )
    for i in range(10):
        _start_flush(rec, f"turn_{i:020x}")
    # negative → _max_turns 内部 normalize 成 0，rotate_if_needed 早 return。
    assert rec._max_turns == 0
    assert len(_read_turn_ids(rec._turns_path)) == 10


def test_init_rotates_existing_overflow(tmp_path):
    """长跑房间启动后立即把上次累积的超额一次性清掉（§9.1 主路径）。"""
    # 先 enabled=True 无 rotation 写 10 条。
    rec = TurnRecorder(
        tmp_path, enabled=True, capture_prompts=False, max_turns=0,
    )
    for i in range(10):
        _start_flush(rec, f"turn_{i:020x}")
    # 再启个新 recorder，cap=3 —— __init__ 期 seed + rotate 应剪到 3 条。
    rec2 = TurnRecorder(
        tmp_path, enabled=True, capture_prompts=False, max_turns=3,
    )
    ids = _read_turn_ids(rec2._turns_path)
    assert len(ids) == 3
    assert ids == [f"turn_{i:020x}" for i in range(7, 10)]
    assert rec2._turn_count == 3


def test_rotation_removes_prompt_subdir(tmp_path):
    """rotation 按事务删：jsonl 行 + ``prompts/<turn_id>/`` 子目录一起没。"""
    from chahua.events import STATUS_OK
    from chahua.scoring import ScoreKind, ScoreResult

    rec = TurnRecorder(
        tmp_path, enabled=True, capture_prompts=True, max_turns=2,
    )
    # 写 3 个 turn，每个带一个 speak prompt 让 prompts/<turn>/speak_*.txt 落盘。
    for i in range(3):
        tid = f"turn_{i:020x}"
        rec.start_turn(
            turn_id=tid, task_id=None,
            trigger={"kind": "user_msg", "ref_seq": None},
        )
        rec.record_message_start(
            message_id=f"msg_{i:020x}", guest="宝总",
            speak_prompt=f"speak content {i}",
        )
        rec.record_message_end(
            message_id=f"msg_{i:020x}", status=STATUS_OK, seq=i,
        )
        rec.flush_turn()
    # 3 个 turn，cap=2 → 最老一个被删（jsonl 行 + prompts 子目录）。
    debug_dir = tmp_path / "debug"
    remaining_subdirs = sorted(p.name for p in (debug_dir / "prompts").iterdir())
    assert remaining_subdirs == [f"turn_{i:020x}" for i in (1, 2)]


def test_no_rotation_when_under_cap(tmp_path):
    """常态：``_turn_count <= max_turns`` 时 rotate_if_needed 早 return，不读盘。"""
    rec = TurnRecorder(
        tmp_path, enabled=True, capture_prompts=False, max_turns=100,
    )
    for i in range(5):
        _start_flush(rec, f"turn_{i:020x}")
    assert rec._turn_count == 5
    assert len(_read_turn_ids(rec._turns_path)) == 5


def test_enabled_false_no_rotation(tmp_path):
    """``enabled=False`` 全 no-op；rotate_if_needed 早 return。"""
    rec = TurnRecorder(
        tmp_path, enabled=False, capture_prompts=False, max_turns=10,
    )
    # debug/ 目录甚至不该建。
    assert not (tmp_path / "debug").exists()
    rec.rotate_if_needed()  # safe
    # _turn_count 未走 seed 路径，保 0。
    assert rec._turn_count == 0


def test_rewrite_preserves_unicode(tmp_path):
    """jsonl tmp+rename 重写要保住 ensure_ascii=False 的中文，避免 \\uXXXX 翻倍体积。"""
    rec = TurnRecorder(
        tmp_path, enabled=True, capture_prompts=False, max_turns=2,
    )
    rec.start_turn(
        turn_id="turn_0", task_id=None,
        trigger={"kind": "user_msg", "ref_seq": None},
    )
    rec._current["scoring_path"] = "scoring"
    rec.flush_turn()
    rec.start_turn(
        turn_id="turn_1", task_id="task_中文", trigger={"kind": "ai_chain"},
    )
    rec.flush_turn()
    rec.start_turn(
        turn_id="turn_2", task_id="task_中文", trigger={"kind": "ai_chain"},
    )
    rec.flush_turn()
    # 3 写入 cap 2 → 删 turn_0；剩两条仍含中文 task_id。
    text = rec._turns_path.read_text(encoding="utf-8")
    assert "中文" in text
    assert "\\u" not in text  # 不应被 escape
