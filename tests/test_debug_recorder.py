"""``TurnRecorder`` 落盘行为（P6.1，docs/P6-调试与回放.md §1 + §不变量）。

覆盖：

- ``enabled=False`` 全方法 no-op；``debug/`` 目录不创建。
- ``enabled=True`` + ``capture_prompts=True`` 完整 happy path：``start_turn → record_scoring
  → record_message_start → record_tool(start/complete) → record_artifact_path →
  record_message_end → flush_turn`` 产出符合 schema 的 jsonl 行 + prompt 文件。
- ``capture_prompts=False``：``prompts/`` 不创建；jsonl 行内 ``prompt_file`` /
  ``speak_prompt_file`` 字段缺省。
- 同 ``call_id`` start/complete 帧合并到一条 ``tool_calls`` entry。
- ``flush_turn`` / ``discard_turn`` 后 ``_current`` 清空，下一帧 ``start_turn`` 重开。
- ``classify_tool_source`` 启发式（builtin / mcp / unknown）。
- ``read_jsonl_skip_bad`` 兼容 ``turns.jsonl``（中间夹一行坏的不阻断整文件）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from chahua._persist import read_jsonl_skip_bad
from chahua.debug_recorder import (
    NOOP_RECORDER,
    SCORING_PATH_BROADCAST,
    SCORING_PATH_HANDOFF_DELEGATE,
    SCORING_PATH_MENTION,
    SCORING_PATH_SCORING,
    TOOL_SOURCE_BUILTIN,
    TOOL_SOURCE_MCP,
    TOOL_SOURCE_UNKNOWN,
    TURNS_SCHEMA_VERSION,
    VALID_SCORING_PATHS,
    TurnRecorder,
    classify_tool_source,
)
from chahua.events import STATUS_CANCELLED, STATUS_OK
from chahua.scoring import ScoreKind, ScoreResult


# ── classify_tool_source 启发式 ────────────────────────────────────────────


def test_classify_tool_source_builtin():
    assert classify_tool_source("fs.read") == (TOOL_SOURCE_BUILTIN, None)
    assert classify_tool_source("task_write_artifact") == (
        TOOL_SOURCE_BUILTIN,
        None,
    )


def test_classify_tool_source_mcp():
    assert classify_tool_source("mcp__github__list_issues") == (
        TOOL_SOURCE_MCP,
        "github",
    )
    # 单段 server 名（少见但合法）
    assert classify_tool_source("mcp__local__tool") == (
        TOOL_SOURCE_MCP,
        "local",
    )


def test_classify_tool_source_mcp_no_server():
    """``mcp__`` 前缀但 server 段为空 —— 命名约定违规，仍归 mcp 但 server=None。"""
    assert classify_tool_source("mcp__") == (TOOL_SOURCE_MCP, None)


def test_classify_tool_source_unknown():
    assert classify_tool_source("") == (TOOL_SOURCE_UNKNOWN, None)


# ── enabled=False ────────────────────────────────────────────────────────────


def test_noop_recorder_no_disk_touch(tmp_path):
    """NOOP_RECORDER 应是真单例 + 所有方法 no-op + 构造不动盘。"""
    assert NOOP_RECORDER.enabled is False
    assert NOOP_RECORDER.capture_prompts is False
    # 所有 record_* 调用安全（不抛、不写盘）
    NOOP_RECORDER.start_turn(turn_id="turn_x", task_id=None, trigger={"kind": "user_msg"})
    NOOP_RECORDER.record_scoring(
        threshold=0.45, scorables=[], cooled=[], results=[], winners=[],
    )
    NOOP_RECORDER.record_message_start(
        message_id="msg_x", guest="A", speak_prompt="hi",
    )
    NOOP_RECORDER.record_tool_start(
        message_id="msg_x", call_id="c1", tool="fs.read", args={},
        source=TOOL_SOURCE_BUILTIN, mcp_server=None,
    )
    NOOP_RECORDER.record_tool_complete(
        message_id="msg_x", call_id="c1",
        status="ok", duration_ms=None, error=None,
    )
    NOOP_RECORDER.record_artifact_path(message_id="msg_x", path="tasks/t/artifacts/x")
    NOOP_RECORDER.record_message_end(message_id="msg_x", status=STATUS_OK, seq=1)
    NOOP_RECORDER.flush_turn()


def test_enabled_false_no_debug_dir(tmp_path):
    """``enabled=False`` 时 ``<room_dir>/debug/`` 不应被创建。"""
    rec = TurnRecorder(tmp_path, enabled=False, capture_prompts=True)
    assert rec.enabled is False
    assert rec.capture_prompts is False  # 不变量：enabled=False ⇒ capture_prompts=False
    assert not (tmp_path / "debug").exists()


def test_enabled_true_requires_room_dir():
    """enabled=True 但 room_dir=None → 构造期 ValueError（NOOP 实例除外）。"""
    with pytest.raises(ValueError):
        TurnRecorder(None, enabled=True, capture_prompts=False)


# ── happy path ──────────────────────────────────────────────────────────────


def _make_score(guest: str, score: float, kind: ScoreKind = ScoreKind.SCORED,
                raw: str = "") -> ScoreResult:
    return ScoreResult(guest_name=guest, score=score, kind=kind, raw=raw)


def test_full_happy_path(tmp_path):
    rec = TurnRecorder(tmp_path, enabled=True, capture_prompts=True)
    debug_dir = tmp_path / "debug"
    assert debug_dir.is_dir()

    rec.start_turn(
        turn_id="turn_abc",
        task_id="task_xyz",
        trigger={"kind": "user_msg", "ref_seq": 7},
    )
    # prompts/<turn_id>/ 由 write_text_atomic 在首次 prompt 落盘时按需创建 —— 末尾断言
    # ``scoring_Alice.txt`` 可读即间接验目录存在；start_turn 后空 turn 不建目录是 feature
    # （cooldown-only / winners=[] 的 turn 不该留空目录）。

    rec.record_scoring(
        threshold=0.45,
        scorables=["Alice", "Bob"],
        cooled=["Charlie"],
        results=[
            (_make_score("Alice", 0.72, raw='{"score":0.72}'), "ALICE PROMPT"),
            (_make_score("Bob", 0.30, raw='{"score":0.30}'), "BOB PROMPT"),
            (_make_score("Charlie", 0.0, kind=ScoreKind.COOLDOWN), None),
        ],
        winners=["Alice"],
        scoring_path=SCORING_PATH_SCORING,
    )

    rec.record_message_start(
        message_id="msg_alice",
        guest="Alice",
        speak_prompt="ALICE SPEAK PROMPT",
    )
    # 工具调用：start + complete 两帧合并
    rec.record_tool_start(
        message_id="msg_alice", call_id="c1",
        tool="task_write_artifact", args={"name": "draft.md", "content": "..."},
        source=TOOL_SOURCE_BUILTIN, mcp_server=None,
    )
    rec.record_artifact_path(
        message_id="msg_alice", path="tasks/task_xyz/artifacts/draft.md",
    )
    rec.record_tool_complete(
        message_id="msg_alice", call_id="c1",
        status="ok", duration_ms=123, error=None,
    )
    # MCP 工具一条（同 call_id 内部走启发式分类前外部传入）
    rec.record_tool_start(
        message_id="msg_alice", call_id="c2",
        tool="mcp__github__list_issues", args={"repo": "x"},
        source=TOOL_SOURCE_MCP, mcp_server="github",
    )
    rec.record_message_end(message_id="msg_alice", status=STATUS_OK, seq=43)

    rec.flush_turn()

    # ── 验落盘 ────────────────────────────────────────────────────────────
    rows = list(read_jsonl_skip_bad(debug_dir / "turns.jsonl"))
    assert len(rows) == 1
    row = rows[0]
    assert row["schema_version"] == TURNS_SCHEMA_VERSION
    assert row["turn_id"] == "turn_abc"
    assert row["task_id"] == "task_xyz"
    assert row["trigger"] == {"kind": "user_msg", "ref_seq": 7}
    assert row["scoring_path"] == SCORING_PATH_SCORING

    scoring = row["scoring"]
    assert scoring["threshold"] == 0.45
    assert scoring["scorables"] == ["Alice", "Bob"]
    assert scoring["cooled"] == ["Charlie"]
    assert scoring["winners"] == ["Alice"]
    assert len(scoring["results"]) == 3
    # capture_prompts=True：scored 那两位都有 prompt_file，cooldown 那位没有（prompt=None）
    by_guest = {r["guest"]: r for r in scoring["results"]}
    assert by_guest["Alice"]["prompt_file"] == "prompts/turn_abc/scoring_Alice.txt"
    assert by_guest["Bob"]["prompt_file"] == "prompts/turn_abc/scoring_Bob.txt"
    assert "prompt_file" not in by_guest["Charlie"]
    assert by_guest["Charlie"]["kind"] == "cooldown"
    # prompt 文件确实落了
    assert (debug_dir / "prompts/turn_abc/scoring_Alice.txt").read_text(
        encoding="utf-8"
    ) == "ALICE PROMPT"

    messages = row["messages"]
    assert len(messages) == 1
    msg = messages[0]
    assert msg["message_id"] == "msg_alice"
    assert msg["guest"] == "Alice"
    assert msg["status"] == STATUS_OK
    assert msg["seq"] == 43
    assert msg["speak_prompt_file"] == "prompts/turn_abc/speak_msg_alice.txt"
    assert (debug_dir / msg["speak_prompt_file"]).read_text(encoding="utf-8") == (
        "ALICE SPEAK PROMPT"
    )
    # 同 call_id 合并：tool_calls 应 2 条（c1 builtin + c2 mcp），c1 status="ok" duration=123
    assert len(msg["tool_calls"]) == 2
    c1 = next(t for t in msg["tool_calls"] if t["call_id"] == "c1")
    assert c1["tool"] == "task_write_artifact"
    assert c1["source"] == TOOL_SOURCE_BUILTIN
    assert c1["status"] == "ok"
    assert c1["duration_ms"] == 123
    assert c1["args"] == {"name": "draft.md", "content": "..."}  # complete 帧不覆盖 args
    c2 = next(t for t in msg["tool_calls"] if t["call_id"] == "c2")
    assert c2["source"] == TOOL_SOURCE_MCP
    assert c2["mcp_server"] == "github"
    assert c2["status"] == "started"  # 只有 start 帧 → status 仍是初始 "started"
    # 产物路径派生
    assert msg["artifact_paths"] == ["tasks/task_xyz/artifacts/draft.md"]


# ── capture_prompts=False ───────────────────────────────────────────────────


def test_capture_prompts_false_no_prompts_dir(tmp_path):
    rec = TurnRecorder(tmp_path, enabled=True, capture_prompts=False)
    assert rec.capture_prompts is False
    rec.start_turn(
        turn_id="turn_p", task_id=None, trigger={"kind": "ai_chain", "ref_seq": 1},
    )
    rec.record_scoring(
        threshold=0.45, scorables=["A"], cooled=[],
        results=[(_make_score("A", 0.8), "PROMPT A")],
        winners=["A"],
    )
    rec.record_message_start(message_id="msg_a", guest="A", speak_prompt="ANY")
    rec.record_message_end(message_id="msg_a", status=STATUS_OK, seq=1)
    rec.flush_turn()

    debug_dir = tmp_path / "debug"
    assert debug_dir.is_dir()
    # capture_prompts=False 时不创建 prompts/ 目录（不变量"capture_prompts=False 时不创建
    # prompts/ 目录"）
    assert not (debug_dir / "prompts").exists()

    rows = list(read_jsonl_skip_bad(debug_dir / "turns.jsonl"))
    assert len(rows) == 1
    row = rows[0]
    # 字段整缺省，不写空串（不变量"piggyback / 落盘 prompt_file 严格 enabled &&
    # capture_prompts 双开才出现"）
    for r in row["scoring"]["results"]:
        assert "prompt_file" not in r
    assert "speak_prompt_file" not in row["messages"][0]


# ── cancelled / partial 路径仍 flush ─────────────────────────────────────────


def test_cancel_path_still_flushes(tmp_path):
    """不变量"cancel / 异常路径仍 flush 一行"——message status=cancelled 时
    仍写 jsonl，半截取证可查。"""
    rec = TurnRecorder(tmp_path, enabled=True, capture_prompts=True)
    rec.start_turn(
        turn_id="turn_c", task_id=None,
        trigger={"kind": "user_msg", "ref_seq": 0},
    )
    rec.record_scoring(
        threshold=0.45, scorables=["A"], cooled=[],
        results=[(_make_score("A", 0.9, raw="raw_a"), "PA")],
        winners=["A"],
    )
    rec.record_message_start(message_id="msg_c", guest="A", speak_prompt="SP")
    # 模拟 cancel 路径：message_end 走 STATUS_CANCELLED，seq=None
    rec.record_message_end(message_id="msg_c", status=STATUS_CANCELLED, seq=None)
    rec.flush_turn()

    rows = list(read_jsonl_skip_bad(tmp_path / "debug" / "turns.jsonl"))
    assert len(rows) == 1
    assert rows[0]["messages"][0]["status"] == STATUS_CANCELLED
    assert rows[0]["messages"][0]["seq"] is None


# ── winners=[] 仍记一行（"为什么没人说话"） ───────────────────────────────


def test_no_winners_still_records(tmp_path):
    rec = TurnRecorder(tmp_path, enabled=True, capture_prompts=False)
    rec.start_turn(
        turn_id="turn_e", task_id=None,
        trigger={"kind": "ai_chain", "ref_seq": 5},
    )
    rec.record_scoring(
        threshold=0.45,
        scorables=["A", "B"], cooled=[],
        results=[
            (_make_score("A", 0.1, raw="raw_a"), None),
            (_make_score("B", 0.2, raw="raw_b"), None),
        ],
        winners=[],
    )
    # 注意：没有 record_message_start —— winners=[] 等于无人说话
    rec.flush_turn()

    rows = list(read_jsonl_skip_bad(tmp_path / "debug" / "turns.jsonl"))
    assert len(rows) == 1
    assert rows[0]["scoring"]["winners"] == []
    assert rows[0]["messages"] == []


# ── 全员冷却空转：threshold=None，所有 guest cooldown ─────────────────────


def test_all_cooldown_path(tmp_path):
    rec = TurnRecorder(tmp_path, enabled=True, capture_prompts=False)
    rec.start_turn(
        turn_id="turn_cd", task_id=None,
        trigger={"kind": "ai_chain", "ref_seq": 9},
    )
    rec.record_scoring(
        threshold=None,
        scorables=[],
        cooled=["A", "B"],
        results=[
            (_make_score("A", 0.0, kind=ScoreKind.COOLDOWN), None),
            (_make_score("B", 0.0, kind=ScoreKind.COOLDOWN), None),
        ],
        winners=[],
    )
    rec.flush_turn()

    rows = list(read_jsonl_skip_bad(tmp_path / "debug" / "turns.jsonl"))
    assert len(rows) == 1
    assert rows[0]["scoring"]["threshold"] is None
    assert {r["kind"] for r in rows[0]["scoring"]["results"]} == {"cooldown"}


# ── broadcast / mention 路径 ────────────────────────────────────────────────


@pytest.mark.parametrize(
    "path,winners",
    [
        (SCORING_PATH_BROADCAST, ["A", "B"]),
        (SCORING_PATH_MENTION, ["A"]),
        # handoff_delegate 与 @ 路由同走 threshold=None / ScoreKind.MENTION 口径。
        (SCORING_PATH_HANDOFF_DELEGATE, ["A"]),
    ],
)
def test_mention_and_broadcast_paths(tmp_path, path, winners):
    """三条确定性路由路径：threshold=None、results kind 全是 ``mention``、
    scoring_path 标签区分单 @ / @all / handoff_delegate。"""
    rec = TurnRecorder(tmp_path, enabled=True, capture_prompts=False)
    rec.start_turn(
        turn_id="turn_x", task_id=None,
        trigger={"kind": "user_msg", "ref_seq": 2},
    )
    rec.record_scoring(
        threshold=None,
        scorables=[],
        cooled=[],
        results=[(_make_score(g, 1.0, kind=ScoreKind.MENTION), None)
                 for g in winners],
        winners=winners,
        scoring_path=path,
    )
    rec.flush_turn()

    rows = list(read_jsonl_skip_bad(tmp_path / "debug" / "turns.jsonl"))
    assert rows[0]["scoring_path"] == path
    assert rows[0]["scoring"]["winners"] == winners


def test_unknown_scoring_path_falls_back_to_scoring(tmp_path):
    """非白名单 ``scoring_path`` → WARN + 降级为 ``SCORING_PATH_SCORING``。
    用合成 sentinel 字符串做 probe，不挪用任何"将来真实路径"——否则 P7.2 起
    `handoff_review` 进白名单会让这条测试为无关原因炸。"""
    rec = TurnRecorder(tmp_path, enabled=True, capture_prompts=False)
    rec.start_turn(
        turn_id="turn_x", task_id=None,
        trigger={"kind": "user_msg", "ref_seq": 0},
    )
    rec.record_scoring(
        threshold=None, scorables=[], cooled=[],
        results=[(_make_score("A", 1.0, kind=ScoreKind.MENTION), None)],
        winners=["A"],
        scoring_path="__not_a_real_path__",
    )
    rec.flush_turn()

    rows = list(read_jsonl_skip_bad(tmp_path / "debug" / "turns.jsonl"))
    assert rows[0]["scoring_path"] == SCORING_PATH_SCORING


def test_handoff_delegate_is_whitelisted():
    """P7.1.2 白名单回归点：``SCORING_PATH_HANDOFF_DELEGATE`` 必须在
    :data:`VALID_SCORING_PATHS` 中（否则 :meth:`record_scoring` 会把它默默降级）。"""
    assert SCORING_PATH_HANDOFF_DELEGATE in VALID_SCORING_PATHS


# ── 状态生命周期 ────────────────────────────────────────────────────────────


def test_flush_clears_in_flight(tmp_path):
    rec = TurnRecorder(tmp_path, enabled=True, capture_prompts=False)
    rec.start_turn(
        turn_id="t1", task_id=None, trigger={"kind": "user_msg", "ref_seq": 0},
    )
    rec.flush_turn()
    assert rec._current is None
    # 第二轮 start_turn 重开
    rec.start_turn(
        turn_id="t2", task_id=None, trigger={"kind": "ai_chain", "ref_seq": 1},
    )
    rec.flush_turn()
    rows = list(read_jsonl_skip_bad(tmp_path / "debug" / "turns.jsonl"))
    assert [r["turn_id"] for r in rows] == ["t1", "t2"]


def test_discard_turn_does_not_flush(tmp_path):
    """discard_turn 仅供 recorder 自身状态损坏时调 —— 不写 jsonl。"""
    rec = TurnRecorder(tmp_path, enabled=True, capture_prompts=False)
    rec.start_turn(
        turn_id="t1", task_id=None, trigger={"kind": "user_msg", "ref_seq": 0},
    )
    rec.discard_turn()
    assert rec._current is None
    # 没 flush 过 → jsonl 文件甚至不存在
    assert not (tmp_path / "debug" / "turns.jsonl").exists()


def test_record_without_start_is_noop(tmp_path):
    """``record_*`` 在没 start_turn 时调 —— 静默 no-op，不抛、不写盘。"""
    rec = TurnRecorder(tmp_path, enabled=True, capture_prompts=False)
    rec.record_scoring(
        threshold=0.45, scorables=[], cooled=[], results=[], winners=[],
    )
    rec.record_message_end(message_id="msg_x", status=STATUS_OK, seq=1)
    rec.flush_turn()  # 没 in-flight → 不写
    assert not (tmp_path / "debug" / "turns.jsonl").exists()


# ── jsonl skip bad（同 transcript.jsonl 口径） ──────────────────────────────


def test_read_jsonl_skip_bad_on_turns(tmp_path):
    """中间夹一行坏的 jsonl 不阻断整文件读出来 —— 与 transcript.jsonl 同口径
    （不变量"turns.jsonl 加载跳坏行"）。"""
    rec = TurnRecorder(tmp_path, enabled=True, capture_prompts=False)
    rec.start_turn(
        turn_id="ok1", task_id=None, trigger={"kind": "user_msg", "ref_seq": 0},
    )
    rec.flush_turn()
    # 手插一行坏的
    turns_path = tmp_path / "debug" / "turns.jsonl"
    with turns_path.open("a", encoding="utf-8") as f:
        f.write("this is not json\n")
    rec.start_turn(
        turn_id="ok2", task_id=None, trigger={"kind": "ai_chain", "ref_seq": 1},
    )
    rec.flush_turn()

    rows = list(read_jsonl_skip_bad(turns_path))
    assert [r["turn_id"] for r in rows] == ["ok1", "ok2"]


# ── 防护 ────────────────────────────────────────────────────────────────────


def test_record_artifact_path_dedup(tmp_path):
    rec = TurnRecorder(tmp_path, enabled=True, capture_prompts=False)
    rec.start_turn(
        turn_id="t", task_id="tsk", trigger={"kind": "user_msg", "ref_seq": 0},
    )
    rec.record_message_start(message_id="m", guest="A", speak_prompt="")
    rec.record_artifact_path(message_id="m", path="tasks/tsk/artifacts/x.md")
    rec.record_artifact_path(message_id="m", path="tasks/tsk/artifacts/x.md")  # 重复
    rec.record_artifact_path(message_id="m", path="tasks/tsk/artifacts/y.md")
    rec.record_message_end(message_id="m", status=STATUS_OK, seq=1)
    rec.flush_turn()
    rows = list(read_jsonl_skip_bad(tmp_path / "debug" / "turns.jsonl"))
    assert rows[0]["messages"][0]["artifact_paths"] == [
        "tasks/tsk/artifacts/x.md",
        "tasks/tsk/artifacts/y.md",
    ]


def test_record_tool_unknown_call_id_starts_new_entry(tmp_path):
    """call_id=None 永远不匹配——每帧自起 entry，避免错位合并。"""
    rec = TurnRecorder(tmp_path, enabled=True, capture_prompts=False)
    rec.start_turn(
        turn_id="t", task_id=None, trigger={"kind": "user_msg", "ref_seq": 0},
    )
    rec.record_message_start(message_id="m", guest="A", speak_prompt="")
    rec.record_tool_start(
        message_id="m", call_id=None, tool="x", args=None,
        source=TOOL_SOURCE_BUILTIN, mcp_server=None,
    )
    rec.record_tool_complete(
        message_id="m", call_id=None, status="ok", duration_ms=10, error=None,
    )
    rec.record_message_end(message_id="m", status=STATUS_OK, seq=1)
    rec.flush_turn()
    rows = list(read_jsonl_skip_bad(tmp_path / "debug" / "turns.jsonl"))
    # call_id=None 不参与合并 —— 两帧各起一条 entry
    assert len(rows[0]["messages"][0]["tool_calls"]) == 2


def test_jsonl_row_is_valid_json(tmp_path):
    """落盘 jsonl 行必须能 json.loads（包括 None 字段：trigger.ref_seq / task_id 等）。"""
    rec = TurnRecorder(tmp_path, enabled=True, capture_prompts=False)
    rec.start_turn(
        turn_id="t", task_id=None, trigger={"kind": "user_msg", "ref_seq": None},
    )
    rec.record_scoring(
        threshold=None, scorables=[], cooled=[], results=[], winners=[],
    )
    rec.flush_turn()
    raw = (tmp_path / "debug" / "turns.jsonl").read_text(encoding="utf-8")
    json.loads(raw.strip())  # 不抛即过
