"""``TurnRecorder.load_index`` / ``load_turn``（P6.3.A，docs/P6.3 §5.1）。

覆盖：

- ``load_index()`` 跳坏行 + 倒序投影 + ``enabled=False`` 早返。
- ``load_index(limit=N)`` 取最新 N 条；``limit=None`` 全扫。
- ``load_turn`` happy path：返完整行 + 读 prompt 文件。
- ``load_turn`` missing turn_id → ``(None, {})``。
- ``load_turn`` prompt 文件被删 → key 整体缺（不空串）。
- ``load_turn`` 路径穿越（``prompt_file: "../../etc/passwd"``）→ 跳过 + WARN。
- 重复 ``turn_id`` 取第一条 + WARN。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from chahua.debug_recorder import TurnRecorder


def _write_turn_row(path: Path, row: dict) -> None:
    """直接追加一行 jsonl（bypass TurnRecorder 写 helper，让我们造任意 shape）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False))
        f.write("\n")


def _make_recorder(tmp_path, *, enabled=True, capture=True, max_turns=0):
    return TurnRecorder(
        tmp_path, enabled=enabled, capture_prompts=capture, max_turns=max_turns,
    )


def test_load_index_skips_bad_line(tmp_path):
    rec = _make_recorder(tmp_path)
    turns_path = rec._turns_path
    _write_turn_row(turns_path, {
        "schema_version": 1, "turn_id": "turn_0001", "ts_ms": 100,
        "task_id": None, "trigger": {"kind": "user_msg"},
        "scoring_path": "scoring",
        "scoring": {"winners": ["A"], "results": []},
        "messages": [],
    })
    # 故意写一行坏行 —— 应被 read_jsonl_skip_bad 跳过。
    with turns_path.open("a", encoding="utf-8") as f:
        f.write("not a json line\n")
    _write_turn_row(turns_path, {
        "schema_version": 1, "turn_id": "turn_0002", "ts_ms": 200,
        "task_id": None, "trigger": {"kind": "user_msg"},
        "scoring_path": "mention",
        "scoring": {"winners": ["B"], "results": []},
        "messages": [{"message_id": "msg_1"}],
    })
    idx = rec.load_index()
    # 倒序，最新在前。
    assert len(idx) == 2
    assert idx[0]["turn_id"] == "turn_0002"
    assert idx[0]["winners"] == ["B"]
    assert idx[0]["scoring_path"] == "mention"
    assert idx[0]["n_messages"] == 1
    assert idx[1]["turn_id"] == "turn_0001"
    assert idx[1]["winners"] == ["A"]
    assert idx[1]["trigger_kind"] == "user_msg"


def test_load_index_limit(tmp_path):
    rec = _make_recorder(tmp_path)
    for i in range(5):
        _write_turn_row(rec._turns_path, {
            "turn_id": f"turn_{i:020x}", "ts_ms": 1000 + i,
            "trigger": {"kind": "user_msg"},
            "scoring_path": "scoring",
            "scoring": {"winners": [], "results": []},
            "messages": [],
        })
    out = rec.load_index(limit=2)
    assert len(out) == 2
    # 倒序：最新两条 = turn_4, turn_3
    assert out[0]["turn_id"] == f"turn_{4:020x}"
    assert out[1]["turn_id"] == f"turn_{3:020x}"


def test_load_index_enabled_false(tmp_path):
    rec = _make_recorder(tmp_path, enabled=False)
    assert rec.load_index() == []


def test_load_index_missing_file(tmp_path):
    """``debug/turns.jsonl`` 不存在 → 静默返 ``[]``（首次启动是预期场景）。"""
    rec = _make_recorder(tmp_path)
    # 没人写过任何 turn。
    assert rec.load_index() == []
    assert rec.load_index(limit=10) == []


def test_load_turn_happy_path(tmp_path):
    rec = _make_recorder(tmp_path)
    prompts_dir = (tmp_path / "debug" / "prompts" / "turn_aaaa")
    prompts_dir.mkdir(parents=True)
    (prompts_dir / "scoring_宝总.txt").write_text("scoring prompt text", encoding="utf-8")
    (prompts_dir / "speak_msg_x.txt").write_text("speak prompt body", encoding="utf-8")
    _write_turn_row(rec._turns_path, {
        "turn_id": "turn_aaaa", "ts_ms": 5000, "task_id": None,
        "trigger": {"kind": "user_msg"},
        "scoring_path": "scoring",
        "scoring": {
            "winners": ["宝总"],
            "results": [{
                "guest": "宝总", "score": 0.8, "kind": "scored",
                "prompt_file": "prompts/turn_aaaa/scoring_宝总.txt",
            }],
        },
        "messages": [{
            "message_id": "msg_x", "guest": "宝总",
            "speak_prompt_file": "prompts/turn_aaaa/speak_msg_x.txt",
            "status": "ok", "seq": 1, "tool_calls": [], "artifact_paths": [],
        }],
    })
    turn, prompts = rec.load_turn("turn_aaaa")
    assert turn is not None
    assert turn["turn_id"] == "turn_aaaa"
    assert prompts["prompts/turn_aaaa/scoring_宝总.txt"] == "scoring prompt text"
    assert prompts["prompts/turn_aaaa/speak_msg_x.txt"] == "speak prompt body"


def test_load_turn_missing_id(tmp_path):
    rec = _make_recorder(tmp_path)
    _write_turn_row(rec._turns_path, {
        "turn_id": "turn_aaaa", "ts_ms": 1, "trigger": {"kind": "user_msg"},
        "scoring": {"winners": [], "results": []}, "messages": [],
    })
    turn, prompts = rec.load_turn("turn_notfound")
    assert turn is None
    assert prompts == {}


def test_load_turn_prompt_file_missing(tmp_path):
    """prompt 文件被删 → key 整体缺（不空串，docs §10 不变量）。"""
    rec = _make_recorder(tmp_path)
    _write_turn_row(rec._turns_path, {
        "turn_id": "turn_aaaa", "ts_ms": 1, "trigger": {"kind": "user_msg"},
        "scoring": {
            "winners": [],
            "results": [{
                "guest": "宝总", "score": 0.5, "kind": "scored",
                "prompt_file": "prompts/turn_aaaa/scoring_宝总.txt",
            }],
        },
        "messages": [],
    })
    # 故意不写 prompt 文件。
    turn, prompts = rec.load_turn("turn_aaaa")
    assert turn is not None
    assert prompts == {}  # key 整体缺


def test_load_turn_respects_capture_prompts_off(tmp_path):
    """``capture_prompts=False`` 必须不返老 prompt 体（codex 评审第 2 轮 P2）。

    场景：上一轮跑用 ``capture_prompts=True`` 落了 prompt 文件，本次启动改成 False；
    ``load_turn`` 仍能返 turn 行，但 ``prompts`` 必须 ``{}`` —— 否则前端 TURN_DETAIL
    把老 prompt 渲到 panel，违反不变量"单 key 严格 enabled && capture_prompts &&
    文件可读三重满足才出现"。
    """
    # 第一阶段：capture=True 写 turn + prompt 文件。
    rec_on = _make_recorder(tmp_path, capture=True)
    prompts_dir = tmp_path / "debug" / "prompts" / "turn_legacy"
    prompts_dir.mkdir(parents=True)
    (prompts_dir / "scoring_宝总.txt").write_text("legacy body", encoding="utf-8")
    _write_turn_row(rec_on._turns_path, {
        "turn_id": "turn_legacy", "ts_ms": 1, "trigger": {"kind": "user_msg"},
        "scoring": {
            "winners": [],
            "results": [{
                "guest": "宝总", "score": 0.5, "kind": "scored",
                "prompt_file": "prompts/turn_legacy/scoring_宝总.txt",
            }],
        },
        "messages": [],
    })

    # 第二阶段：同一房间 capture=False 启动；turns.jsonl + prompts/ 仍在盘上。
    rec_off = _make_recorder(tmp_path, capture=False)
    turn, prompts = rec_off.load_turn("turn_legacy")
    assert turn is not None
    assert turn["turn_id"] == "turn_legacy"
    # **核心断言**：prompts 必须空 —— 老体不能漏出来。
    assert prompts == {}


def test_load_turn_path_traversal_rejected(tmp_path, caplog):
    """jsonl 行内 ``prompt_file: "../../etc/passwd"`` —— 跳过 + WARN，不读越界。

    攻击模型：用户手工编辑 turns.jsonl 塞入越界路径。
    """
    rec = _make_recorder(tmp_path)
    # 落一个真存在但越界的"敏感"文件 —— 测试只看 load_turn 是否读它。
    (tmp_path / "secret.txt").write_text("don't read me", encoding="utf-8")
    _write_turn_row(rec._turns_path, {
        "turn_id": "turn_evil", "ts_ms": 1, "trigger": {"kind": "user_msg"},
        "scoring": {
            "winners": [],
            "results": [{
                "guest": "x", "score": 0, "kind": "scored",
                "prompt_file": "../secret.txt",
            }],
        },
        "messages": [],
    })
    turn, prompts = rec.load_turn("turn_evil")
    assert turn is not None
    assert prompts == {}  # 越界键不进字典
    # secret.txt 内容绝不出现在 prompts 任何值里
    assert all("don't read me" not in v for v in prompts.values())


def test_load_turn_duplicate_id_takes_first(tmp_path):
    """jsonl 异常状况（手工编辑 / fsck）下两条 turn_id 相同 —— 取第一条 + WARN。"""
    rec = _make_recorder(tmp_path)
    _write_turn_row(rec._turns_path, {
        "turn_id": "turn_dup", "ts_ms": 1, "trigger": {"kind": "user_msg"},
        "scoring": {"winners": ["A"], "results": []}, "messages": [],
    })
    _write_turn_row(rec._turns_path, {
        "turn_id": "turn_dup", "ts_ms": 2, "trigger": {"kind": "ai_chain"},
        "scoring": {"winners": ["B"], "results": []}, "messages": [],
    })
    turn, _ = rec.load_turn("turn_dup")
    assert turn is not None
    assert turn["scoring"]["winners"] == ["A"]
    assert turn["ts_ms"] == 1
