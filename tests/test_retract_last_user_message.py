"""P18「撤回未回复的最后一条用户消息」——server 权威、免 message_id。

复现优先，钉死承重语义：
- server 三段硬校验（idle ∧ 末条 ∧ user）任一不满足即拒。
- 主状态只截 transcript 末条；**cursor 不动**（尚无回复前提已保证没碰过被撤 seq）。
- 旁路一致收尾：summary（房间级 + 所有已实例化 task summarizer）+ debug turn。
"""

from __future__ import annotations

import json

from chahua._persist import read_jsonl_skip_bad
from chahua.debug_recorder import TurnRecorder
from chahua.events import NOTICE_LEVEL_ERROR, ChahuaEventType
from chahua.summarizer import Summarizer, TaskSummaries, TaskSummarizer
from chahua.user_md import USER_SPEAKER_ID


def _collect():
    out: list = []
    return out, out.append


def _notices(envs):
    return [e for e in envs if e.type == ChahuaEventType.NOTICE]


# ── server 行为 ─────────────────────────────────────────────────────────────


async def test_retract_happy_path(task_inbound_srv, monkeypatch):
    session, srv = task_inbound_srv
    # 隔离 snapshot 重发的重机械：只断言它被调用一次。
    snap_calls: list = []
    monkeypatch.setattr(srv, "_emit_room_snapshot", lambda sink: snap_calls.append(True))

    session.room.append(USER_SPEAKER_ID, "发错了这句")
    before_cursor = session.orchestrator.cursor.snapshot()
    envs, sink = _collect()

    await srv._inbound_retract_last_user_message({}, sink)

    # transcript 末条被截。
    assert len(session.room.messages_since(0)) == 0
    # cursor 一律不动（承重断言）。
    assert session.orchestrator.cursor.snapshot() == before_cursor
    # 无 NOTICE error、重发了一次 snapshot。
    assert _notices(envs) == []
    assert snap_calls == [True]


async def test_retract_full_wire_frame_with_type(task_inbound_srv, monkeypatch):
    """真实线帧只含 ``type`` —— 走 _handle_inbound 完整分发。空 allowlist + 恒排除
    ``type`` → 帧不被误拒、真的截断。回归守卫，防 ws 撤回被静默拒。"""
    session, srv = task_inbound_srv
    monkeypatch.setattr(srv, "_emit_room_snapshot", lambda sink: None)
    session.room.append(USER_SPEAKER_ID, "发错了这句")
    envs, sink = _collect()

    await srv._handle_inbound({"type": "retract_last_user_message"}, sink)

    assert len(session.room.messages_since(0)) == 0
    assert _notices(envs) == []


async def test_retract_rejects_when_last_is_guest(task_inbound_srv, monkeypatch):
    session, srv = task_inbound_srv
    monkeypatch.setattr(srv, "_emit_room_snapshot", lambda sink: None)
    session.room.append(USER_SPEAKER_ID, "问题")
    session.room.append("宝总", "回答")  # 末条是茶客 → 已有人接话。
    envs, sink = _collect()

    await srv._inbound_retract_last_user_message({}, sink)

    # 拒：transcript 不变 + NOTICE error。
    assert len(session.room.messages_since(0)) == 2
    notices = _notices(envs)
    assert len(notices) == 1
    assert notices[0].data["level"] == NOTICE_LEVEL_ERROR


async def test_retract_rejects_unknown_payload_key(task_inbound_srv, monkeypatch):
    session, srv = task_inbound_srv
    monkeypatch.setattr(srv, "_emit_room_snapshot", lambda sink: None)
    session.room.append(USER_SPEAKER_ID, "发错了这句")
    envs, sink = _collect()

    # 入站严格：无 payload 帧带任何键 → NOTICE error + 丢帧、transcript 不动。
    await srv._inbound_retract_last_user_message({"message_id": "x"}, sink)

    assert len(session.room.messages_since(0)) == 1
    assert len(_notices(envs)) == 1
    assert _notices(envs)[0].data["level"] == NOTICE_LEVEL_ERROR


async def test_retract_rejects_when_empty_room(task_inbound_srv):
    session, srv = task_inbound_srv
    envs, sink = _collect()

    await srv._inbound_retract_last_user_message({}, sink)

    assert len(_notices(envs)) == 1
    assert _notices(envs)[0].data["level"] == NOTICE_LEVEL_ERROR


async def test_retract_rejects_when_busy(task_inbound_srv, monkeypatch):
    session, srv = task_inbound_srv
    session.room.append(USER_SPEAKER_ID, "发错了这句")
    # 模拟房间忙 —— 用 busy_alive（含 bg run / MTS），不是 _inflight_alive。
    monkeypatch.setattr(srv._foreground_runtime, "busy_alive", lambda: True)
    envs, sink = _collect()

    await srv._inbound_retract_last_user_message({}, sink)

    # 拒：transcript 不变 + NOTICE error。
    assert len(session.room.messages_since(0)) == 1
    assert len(_notices(envs)) == 1
    assert _notices(envs)[0].data["level"] == NOTICE_LEVEL_ERROR


# ── 旁路收尾：房间级 Summarizer.truncate_after ──────────────────────────────


def test_summarizer_truncate_after_drops_covering_spans(tmp_path):
    p = tmp_path / "summary.jsonl"
    p.write_text(
        '{"start_seq": 1, "end_seq": 3, "text": "a"}\n'
        '{"start_seq": 4, "end_seq": 6, "text": "b"}\n',
        encoding="utf-8",
    )
    s = Summarizer(object(), summary_path=p)
    assert s.covered_until == 6

    s.truncate_after(5)  # 丢 end_seq >= 5 的 span（[4,6]），保 [1,3]。

    assert [sp.end_seq for sp in s.summaries] == [3]
    # 已原子重写盘：重载只剩前缀。
    assert [sp.end_seq for sp in Summarizer(object(), summary_path=p).summaries] == [3]


def test_summarizer_truncate_after_resets_backoff(tmp_path):
    p = tmp_path / "summary.jsonl"
    s = Summarizer(object(), summary_path=p)
    s._next_eligible_seq = 10
    s._failures = 3

    s.truncate_after(5)  # 退避游标越过被撤 seq → 复位。

    assert s._next_eligible_seq == 0
    assert s._failures == 0


# ── 旁路收尾：TaskSummarizer / TaskSummaries cursor clamp ────────────────────


def _write_task_cursor(tmp_path, task_id: str, considered: int) -> None:
    cursor_path = tmp_path / task_id / "summary_cursor.json"
    cursor_path.parent.mkdir(parents=True, exist_ok=True)
    cursor_path.write_text(
        json.dumps({"considered_until_seq": considered}), encoding="utf-8"
    )


def _make_task_summarizer(tmp_path, task_id: str, considered: int) -> TaskSummarizer:
    _write_task_cursor(tmp_path, task_id, considered)
    return TaskSummarizer(
        object(),
        summary_path=tmp_path / task_id / "summary.jsonl",
        cursor_path=tmp_path / task_id / "summary_cursor.json",
        task_id=task_id,
    )


class _StubTask:
    def __init__(self, task_id: str) -> None:
        self.id = task_id


class _StubTasksStore:
    """只实现 TaskSummaries.truncate_after 需要的两个方法 —— list_tasks + task_dir。"""

    def __init__(self, tmp_path, task_ids: list[str]) -> None:
        self._tmp = tmp_path
        self._ids = task_ids

    def list_tasks(self):
        return [_StubTask(i) for i in self._ids]

    def task_dir(self, task_id: str):
        return self._tmp / task_id


def test_task_summarizer_truncate_after_clamps_cursor(tmp_path):
    ts = _make_task_summarizer(tmp_path, "t1", considered=8)
    assert ts.considered_until_seq == 8

    ts.truncate_after(5)  # clamp 到 seq-1 = 4，并立即 flush 落盘。

    assert ts.considered_until_seq == 4
    on_disk = json.loads((tmp_path / "t1" / "summary_cursor.json").read_text())
    assert on_disk["considered_until_seq"] == 4


def test_task_summaries_truncate_after_clamps_all_tasks_from_disk(tmp_path):
    """承重（评审 #3）：不只 active、也不只本会话已实例化 —— 遍历 store 全量任务，
    连纯落盘的 cursor（本会话尚未 _ensure）也 clamp。这里 _by_id 起始为空，两个任务
    的 cursor 都只在盘上。"""
    _write_task_cursor(tmp_path, "a", 9)
    _write_task_cursor(tmp_path, "b", 7)
    store = _StubTasksStore(tmp_path, ["a", "b"])
    tsum = TaskSummaries(object(), tasks_store=store)
    assert tsum._by_id == {}  # 撤回前没实例化过任何 task summarizer。

    tsum.truncate_after(5)

    # 两个任务都被 _ensure（从盘 load cursor）+ clamp 到 seq-1，且落盘。
    assert tsum._ensure("a").considered_until_seq == 4
    assert tsum._ensure("b").considered_until_seq == 4
    assert json.loads((tmp_path / "a" / "summary_cursor.json").read_text())[
        "considered_until_seq"
    ] == 4


def test_task_summarizer_truncate_after_noop_when_cursor_already_before(tmp_path):
    ts = _make_task_summarizer(tmp_path, "t1", considered=2)
    ts.truncate_after(5)  # cursor 已在 seq-1 之前 → 不动。
    assert ts.considered_until_seq == 2


# ── 旁路收尾：TurnRecorder.drop_turns_from ──────────────────────────────────


def _flush_turn(rec: TurnRecorder, turn_id: str, *, ref_seq) -> None:
    rec.start_turn(
        turn_id=turn_id, task_id=None,
        trigger={"kind": "user_msg", "ref_seq": ref_seq},
    )
    rec.flush_turn()


def test_drop_turns_from_by_trigger_ref_seq(tmp_path):
    rec = TurnRecorder(tmp_path, enabled=True, capture_prompts=False)
    _flush_turn(rec, "turn_a", ref_seq=2)
    _flush_turn(rec, "turn_b", ref_seq=5)  # 被撤 seq=5 触发。
    assert rec._turn_count == 2

    rec.drop_turns_from(5)

    ids = [row["turn_id"] for row in read_jsonl_skip_bad(rec._turns_path)]
    assert ids == ["turn_a"]
    assert rec._turn_count == 1


def test_drop_turns_from_by_message_seq(tmp_path):
    """turn 覆盖判定也认 messages[].seq（不只 trigger.ref_seq）。"""
    rec = TurnRecorder(tmp_path, enabled=True, capture_prompts=False)
    rec.start_turn(
        turn_id="turn_c", task_id=None,
        trigger={"kind": "user_msg", "ref_seq": 1},
    )
    rec.record_message_start(message_id="m1", guest="宝总", speak_prompt="")
    rec.record_message_end(message_id="m1", status="ok", seq=6)
    rec.flush_turn()

    rec.drop_turns_from(6)  # 该 turn 产出了 seq=6 的发言 → 删。

    assert [row["turn_id"] for row in read_jsonl_skip_bad(rec._turns_path)] == []
    assert rec._turn_count == 0


def test_drop_turns_from_disabled_recorder_noop():
    rec = TurnRecorder(None, enabled=False, capture_prompts=False)
    rec.drop_turns_from(5)  # 不抛即可（NOOP 语义）。
