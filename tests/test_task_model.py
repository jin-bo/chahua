"""P5.1.1: chahua/task.py 数据模型单测。

覆盖 §8.1 "落盘宽容" 口径：
- 合法 round-trip
- 未知字段 warn 后忽略
- 必需字段缺失 / 类型错 → None + WARN（不抛）
- status 非法 → 降级到 "blocked"
"""

from __future__ import annotations

from chahua.task import Decision, Task, new_decision_id, new_task_id


# ── Task ────────────────────────────────────────────────────────────────────


def test_task_new_defaults_to_open():
    t = Task.new(title="写 README", goal="把项目装配段拉到 README")
    assert t.status == "open"
    assert t.owner is None
    assert t.closed_at_ms is None
    assert t.id.startswith("task_")
    assert t.created_at_ms == t.updated_at_ms


def test_task_round_trip():
    t = Task.new(title="t", goal="g", owner="user")
    obj = t.to_jsonl_dict()
    back = Task.from_jsonl(obj)
    assert back == t


def test_task_unknown_fields_ignored(caplog):
    t = Task.new(title="t", goal="g")
    obj = t.to_jsonl_dict()
    obj["future_field"] = {"nested": [1, 2]}
    obj["another"] = "irrelevant"
    back = Task.from_jsonl(obj)
    assert back == t  # 字段全保住、未知键被默默丢


def test_task_missing_required_returns_none(caplog):
    bad = {"id": "task_x", "title": "t"}  # 缺 goal / status / 时间戳
    assert Task.from_jsonl(bad) is None
    assert any("malformed" in r.message for r in caplog.records)


def test_task_invalid_status_falls_back_to_blocked(caplog):
    t = Task.new(title="t", goal="g")
    obj = t.to_jsonl_dict()
    obj["status"] = "wat"
    back = Task.from_jsonl(obj)
    assert back is not None
    assert back.status == "blocked"
    assert any("不在合法集" in r.message for r in caplog.records)


def test_task_owner_non_str_becomes_none(caplog):
    t = Task.new(title="t", goal="g", owner="user")
    obj = t.to_jsonl_dict()
    obj["owner"] = 42
    back = Task.from_jsonl(obj)
    assert back is not None
    assert back.owner is None


def test_task_closed_at_ms_non_int_ignored(caplog):
    t = Task.new(title="t", goal="g")
    obj = t.to_jsonl_dict()
    obj["closed_at_ms"] = "not-a-number"
    back = Task.from_jsonl(obj)
    assert back is not None
    assert back.closed_at_ms is None
    assert any("closed_at_ms" in r.message for r in caplog.records)


# ── Decision ────────────────────────────────────────────────────────────────


def test_decision_round_trip():
    d = Decision.new(
        task_id="task_abc",
        supporting_message_ids=["msg_1", "msg_2"],
        summary="用 Electron 打包",
    )
    back = Decision.from_jsonl(d.to_jsonl_dict())
    assert back == d


def test_decision_supporting_ids_dedup_and_filter():
    obj = {
        "decision_id": "dec_x",
        "task_id": "task_y",
        "supporting_message_ids": ["a", "a", 1, "b", None, "b"],
        "summary": "s",
        "marked_by": "user",
        "ts_ms": 1234,
    }
    d = Decision.from_jsonl(obj)
    assert d is not None
    assert d.supporting_message_ids == ("a", "b")


def test_decision_missing_required_returns_none(caplog):
    bad = {"decision_id": "dec_x", "summary": "s"}
    assert Decision.from_jsonl(bad) is None


def test_decision_unknown_fields_ignored():
    d = Decision.new(
        task_id="task_x",
        supporting_message_ids=["m"],
        summary="s",
    )
    obj = d.to_jsonl_dict()
    obj["future"] = 1
    back = Decision.from_jsonl(obj)
    assert back == d


def test_decision_supporting_non_list_falls_back_to_empty(caplog):
    obj = {
        "decision_id": "dec_x",
        "task_id": "task_y",
        "supporting_message_ids": "not a list",
        "summary": "s",
        "marked_by": "user",
        "ts_ms": 0,
    }
    d = Decision.from_jsonl(obj)
    assert d is not None
    assert d.supporting_message_ids == ()


# ── id helpers ──────────────────────────────────────────────────────────────


def test_new_ids_have_expected_prefix_and_length():
    tid = new_task_id()
    did = new_decision_id()
    assert tid.startswith("task_") and len(tid) == 5 + 20  # prefix + 10 hex bytes
    assert did.startswith("dec_") and len(did) == 4 + 20
