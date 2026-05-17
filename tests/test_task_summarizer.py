"""P5.2.12 任务级 summarizer + cursor 落盘行为。

走 ``TaskSummarizer._collect_block``（同步纯逻辑 → 不调 LLM）+
``maybe_summarize`` 真路径（``chat_oneshot`` monkeypatch 返罐头文本，验真正
``summary.jsonl`` append + cursor 推进）。``TaskSummaries`` 池行为另行测。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from chahua._persist import read_jsonl_skip_bad
from chahua.room import Room
from chahua.summarizer import TaskSummaries, TaskSummarizer
from chahua.user_md import USER_SPEAKER_ID


class _StubClient:
    """LLMClient 占位 —— ``_collect_block`` 是同步纯逻辑不碰 LLM；
    ``maybe_summarize`` 路径里 ``chat_oneshot`` 走 monkeypatch 拦截，client 本身
    不被取属性，任意对象都成。"""

    pass


@pytest.fixture
def room():
    r = Room(name="t")
    r.add_participant(USER_SPEAKER_ID)
    return r


def _make_summarizer(tmp_path: Path, task_id: str = "t-1") -> TaskSummarizer:
    task_dir = tmp_path / "tasks" / task_id
    task_dir.mkdir(parents=True, exist_ok=True)
    return TaskSummarizer(
        _StubClient(),  # type: ignore[arg-type]
        summary_path=task_dir / "summary.jsonl",
        cursor_path=task_dir / "summary_cursor.json",
        task_id=task_id,
    )


# ── _collect_block ─────────────────────────────────────────────────────────


def test_collect_block_filters_by_task_id(tmp_path, room):
    s = _make_summarizer(tmp_path)
    # 5 条 t-1 + 5 条 t-2 + 5 条 房间级 task_id=None
    for i in range(5):
        room.append(USER_SPEAKER_ID, f"a{i}", task_id="t-1")
        room.append(USER_SPEAKER_ID, f"b{i}", task_id="t-2")
        room.append(USER_SPEAKER_ID, f"c{i}", task_id=None)
    block = s._collect_block(room, block_size=3)
    assert block is not None and len(block) == 5
    assert {m.task_id for m in block} == {"t-1"}
    assert [m.text for m in block] == [f"a{i}" for i in range(5)]


def test_collect_block_below_threshold_returns_none(tmp_path, room):
    s = _make_summarizer(tmp_path)
    # 只 2 条匹配，block_size=5 不够，返 None。
    for i in range(2):
        room.append(USER_SPEAKER_ID, f"a{i}", task_id="t-1")
    for i in range(10):
        room.append(USER_SPEAKER_ID, f"b{i}", task_id="t-other")
    assert s._collect_block(room, block_size=5) is None


def test_collect_block_advances_cursor_when_below_threshold(tmp_path, room):
    """不够一块时也推 cursor —— 下次 latest 不增就不重扫已知不匹配的那段。"""
    s = _make_summarizer(tmp_path)
    for i in range(20):
        room.append(USER_SPEAKER_ID, f"x{i}", task_id="t-other")
    assert s.considered_seq == 0
    assert s._collect_block(room, block_size=3) is None
    assert s.considered_seq == 20


def test_collect_block_caps_at_double_block_size(tmp_path, room):
    """与 room-level 同口径：单次 prompt 不超过 2×block_size 条 task 消息。"""
    s = _make_summarizer(tmp_path)
    for i in range(50):
        room.append(USER_SPEAKER_ID, f"a{i}", task_id="t-1")
    block = s._collect_block(room, block_size=5)
    assert block is not None and len(block) == 10  # 2 × 5


def test_collect_block_no_new_messages(tmp_path, room):
    """latest_seq <= considered_seq 直接返 None。"""
    s = _make_summarizer(tmp_path)
    s._considered_seq = 100  # 假装已扫到 100
    for i in range(5):
        room.append(USER_SPEAKER_ID, f"a{i}", task_id="t-1")
    # room.latest_seq = 5 < 100，return None
    assert s._collect_block(room, block_size=3) is None


# ── cursor 持久化 ──────────────────────────────────────────────────────────


def test_cursor_flush_roundtrip(tmp_path):
    """flush_cursor 写文件，新实例从同一路径加载得到同值。"""
    s = _make_summarizer(tmp_path)
    s._considered_seq = 42
    s.flush_cursor()
    s2 = _make_summarizer(tmp_path)
    assert s2.considered_seq == 42


def test_cursor_missing_means_zero(tmp_path):
    """没 cursor 文件 → 从 0 开始（与 room-level Summarizer 同口径）。"""
    s = _make_summarizer(tmp_path)
    assert s.considered_seq == 0


def test_cursor_corrupt_treated_as_zero(tmp_path):
    """损坏 cursor 不阻塞房间加载（落盘宽容口径）。"""
    s = _make_summarizer(tmp_path)
    s._cursor_path.write_text("not json", encoding="utf-8")
    s2 = _make_summarizer(tmp_path)
    assert s2.considered_seq == 0


# ── maybe_summarize 端到端（monkeypatch chat_oneshot）────────────────────


async def _fake_oneshot_ok(*_args, **_kwargs) -> str:
    return "- 任务要点一\n- 任务要点二"


async def _fake_oneshot_empty(*_args, **_kwargs) -> str:
    return ""


async def test_maybe_summarize_writes_summary_jsonl(tmp_path, room, monkeypatch):
    monkeypatch.setattr("chahua.summarizer.chat_oneshot", _fake_oneshot_ok)
    s = _make_summarizer(tmp_path)
    for i in range(15):
        room.append(USER_SPEAKER_ID, f"m{i}", task_id="t-1")

    span = await s.maybe_summarize(room, {USER_SPEAKER_ID: "U"}, block_size=10)
    assert span is not None
    assert span.text == "- 任务要点一\n- 任务要点二"
    assert s._summary_path.exists()
    rows = list(read_jsonl_skip_bad(s._summary_path))
    assert len(rows) == 1 and rows[0]["text"] == span.text
    assert s.considered_seq >= room.latest_seq  # 成功后推到 latest 上


async def test_maybe_summarize_failure_backoff(tmp_path, room, monkeypatch):
    """LLM 空返回 → 不写 summary，但拉 next_eligible_seq 退避。"""
    monkeypatch.setattr("chahua.summarizer.chat_oneshot", _fake_oneshot_empty)
    s = _make_summarizer(tmp_path)
    for i in range(15):
        room.append(USER_SPEAKER_ID, f"m{i}", task_id="t-1")
    span = await s.maybe_summarize(room, {USER_SPEAKER_ID: "U"}, block_size=10)
    assert span is None
    assert not s._summary_path.exists() or list(read_jsonl_skip_bad(s._summary_path)) == []
    assert s._next_eligible_seq > room.latest_seq


async def test_maybe_summarize_ignores_other_tasks(tmp_path, room, monkeypatch):
    captured: list[list[str]] = []

    async def _capture(_llm, msgs, **_kw):
        # msgs[1].content 是 format_messages(block) 的文本
        captured.append([m["content"] for m in msgs])
        return "- ok"

    monkeypatch.setattr("chahua.summarizer.chat_oneshot", _capture)
    s = _make_summarizer(tmp_path, task_id="t-1")
    # 把别的任务 / 房间级穿插 —— LLM 入参里不该看到 b/c 系列。
    for i in range(15):
        room.append(USER_SPEAKER_ID, f"A{i}", task_id="t-1")
        room.append(USER_SPEAKER_ID, f"B{i}", task_id="t-2")
        room.append(USER_SPEAKER_ID, f"C{i}", task_id=None)
    await s.maybe_summarize(room, {USER_SPEAKER_ID: "U"}, block_size=10)
    assert captured, "chat_oneshot 没被调"
    body = captured[0][1]
    assert "A0" in body and "A14" in body
    assert "B0" not in body and "C0" not in body


# ── TaskSummaries 池 ───────────────────────────────────────────────────────


def test_task_summaries_close_flushes_all(tmp_path):
    """``close()`` flush 所有 lazy-装配过的 task summarizer 的 cursor。"""
    from chahua.tasks_store import TasksStore

    store = TasksStore(room_dir=tmp_path)
    t1 = store.open_task(title="一", goal="...")
    t2 = store.open_task(title="二", goal="...")

    pool = TaskSummaries(_StubClient(), tasks_store=store)  # type: ignore[arg-type]
    pool._ensure(t1.id)._considered_seq = 5
    pool._ensure(t2.id)._considered_seq = 9
    pool.close()

    # 重建池 → ensure 时从盘读 cursor，应当还原。
    pool2 = TaskSummaries(_StubClient(), tasks_store=store)  # type: ignore[arg-type]
    assert pool2._ensure(t1.id).considered_seq == 5
    assert pool2._ensure(t2.id).considered_seq == 9


async def test_task_summaries_kick_iterates_all_tasks(tmp_path, room, monkeypatch):
    """``kick`` 对 store 当前每个任务都走一次 maybe_summarize。"""
    from chahua.tasks_store import TasksStore

    monkeypatch.setattr("chahua.summarizer.chat_oneshot", _fake_oneshot_ok)
    store = TasksStore(room_dir=tmp_path)
    t1 = store.open_task(title="一", goal="...")
    t2 = store.open_task(title="二", goal="...")
    # 每任务 10 条
    for i in range(10):
        room.append(USER_SPEAKER_ID, f"a{i}", task_id=t1.id)
        room.append(USER_SPEAKER_ID, f"b{i}", task_id=t2.id)

    pool = TaskSummaries(_StubClient(), tasks_store=store)  # type: ignore[arg-type]
    await pool.kick(room, {USER_SPEAKER_ID: "U"}, block_size=5)

    s1 = pool.get(t1.id)
    s2 = pool.get(t2.id)
    assert s1 is not None and len(s1.summaries) == 1
    assert s2 is not None and len(s2.summaries) == 1


async def test_task_summaries_kick_continues_on_individual_failure(
    tmp_path, room, monkeypatch,
):
    """单任务 maybe_summarize 抛错不阻断其他任务的 kick。"""
    from chahua.tasks_store import TasksStore

    store = TasksStore(room_dir=tmp_path)
    t1 = store.open_task(title="一", goal="...")
    t2 = store.open_task(title="二", goal="...")
    for i in range(15):
        room.append(USER_SPEAKER_ID, f"a{i}", task_id=t1.id)
        room.append(USER_SPEAKER_ID, f"b{i}", task_id=t2.id)

    # chat_oneshot 在第一次调用时炸；第二次正常。kick 应跑完两个任务。
    calls = {"n": 0}

    async def _flaky(*_args, **_kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        return "- ok"

    monkeypatch.setattr("chahua.summarizer.chat_oneshot", _flaky)
    pool = TaskSummaries(_StubClient(), tasks_store=store)  # type: ignore[arg-type]
    await pool.kick(room, {USER_SPEAKER_ID: "U"}, block_size=10)
    # 第二个任务的 summarize 成功了
    s2 = pool.get(t2.id)
    assert s2 is not None and len(s2.summaries) == 1
