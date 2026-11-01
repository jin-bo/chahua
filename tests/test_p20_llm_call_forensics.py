"""P20 茶客调用消耗取证（docs/P20-茶客调用消耗取证.md §8）。

transport 按当前 message_id 缓冲 ``LLM_RETRY`` / ``LLM_CALL_COMPLETED`` /
``COMPACTION_SETTLED``，经幂等 ``finalize_llm_records()`` 冻结成快照；
``message_end.data`` 与 ``turns.jsonl`` 用同一份。真依赖（真 Agentao + 假 SSE 端点）
的两条在 ``tests/test_agentao_contract.py``。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from agentao.transport import AgentEvent, EventType

from chahua._llm_call_records import (
    MAX_STR_LEN,
    MAX_COMPACTIONS,
    MAX_LLM_CALLS,
    MAX_RETRIES_PER_CALL,
)
from chahua._persist import read_jsonl_skip_bad
from chahua.debug_recorder import TurnRecorder
from chahua.events import STATUS_CANCELLED, STATUS_ERROR, STATUS_OK, ChahuaEventType
from chahua.guest import TeaGuest
from chahua.room import Room
from chahua.transport_bridge import ChahuaTransport


# ── 上游事件样本（字段照 agentao 0.5.18 runtime/llm_call.py） ─────────────────


def _completed(**over: Any) -> AgentEvent:
    data = {
        "attempt": 7,
        "status": "ok",
        "duration_ms": 1200,
        "model_latency_ms": 1200,
        "first_token_ms": 300,
        "first_reasoning_ms": None,
        "error_class": None,
        "error_message": None,
        "finish_reason": "stop",
        "finish_reason_reported": True,
        "prompt_tokens": 1000,
        "completion_tokens": 50,
        "cache_read_tokens": 800,
        "cache_creation_tokens": 0,
        "reasoning_tokens": 0,
    }
    data.update(over)
    return AgentEvent(EventType.LLM_CALL_COMPLETED, data)


def _failed(**over: Any) -> AgentEvent:
    data = {
        "attempt": 8,
        "status": "error",
        "duration_ms": 900,
        "model_latency_ms": 900,
        "first_token_ms": None,
        "first_reasoning_ms": None,
        "error_class": "RateLimitError",
        "error_message": "x" * 500,
        "streamed": False,
        "finish_reason": None,
        "prompt_tokens": None,
        "completion_tokens": None,
        "cache_read_tokens": None,
        "cache_creation_tokens": None,
        "reasoning_tokens": None,
    }
    data.update(over)
    return AgentEvent(EventType.LLM_CALL_COMPLETED, data)


def _retry(n: int = 1, reason: str = "status=429") -> AgentEvent:
    return AgentEvent(EventType.LLM_RETRY, {
        "retry": n, "max_retries": 3, "delay_s": 1.5, "reason": reason,
    })


def _compaction(**over: Any) -> AgentEvent:
    data = {
        "trigger": "auto", "kind": "full", "reason": "threshold",
        "status": "success", "pre_msgs": 120, "post_msgs": 14,
        "pre_tokens_history": 98000, "post_tokens_history": 6100,
        "duration_ms": 21000, "detail": None,
    }
    data.update(over)
    return AgentEvent(EventType.COMPACTION_SETTLED, data)


# ── transport 层 ─────────────────────────────────────────────────────────────


def _feed(transport: ChahuaTransport, events, *, message_id: str = "msg_m") -> dict:
    """bind → 喂事件 → finalize（bind 内，与成功路径的 message_end 时机一致）。"""
    with transport.bind(sink=lambda _e: None, turn_id="turn_x", message_id=message_id):
        for ev in events:
            transport._handle(ev)  # noqa: SLF001
        return transport.finalize_llm_records()


def test_two_calls_copied_verbatim():
    t = ChahuaTransport(room_id="r", guest_name="A")
    snap = _feed(t, [_completed(finish_reason="tool_calls"), _completed(prompt_tokens=1100)])
    calls = snap["llm_calls"]
    assert len(calls) == 2
    assert calls[0]["finish_reason"] == "tool_calls"
    assert calls[0]["prompt_tokens"] == 1000
    assert calls[0]["cache_read_tokens"] == 800
    assert calls[0]["first_token_ms"] == 300
    assert calls[1]["prompt_tokens"] == 1100
    # 成功调用不带 error 键、不带 retries 键
    assert "error_class" not in calls[0]
    assert "retries" not in calls[0]
    assert "compactions" not in snap
    assert "llm_records_truncated" not in snap


def test_unknown_upstream_keys_not_passed_through():
    t = ChahuaTransport(room_id="r", guest_name="A")
    snap = _feed(t, [_completed(some_future_key="leak", attempt=99)])
    call = snap["llm_calls"][0]
    assert "some_future_key" not in call
    assert "attempt" not in call  # agent 级序号，不是重试次数，不用
    assert "model_latency_ms" not in call


def test_retries_attach_to_following_completed():
    t = ChahuaTransport(room_id="r", guest_name="A")
    snap = _feed(t, [_retry(1), _retry(2, "APIConnectionError"), _completed()])
    calls = snap["llm_calls"]
    assert len(calls) == 1
    assert [r["retry"] for r in calls[0]["retries"]] == [1, 2]
    assert calls[0]["retries"][1]["reason"] == "APIConnectionError"
    assert calls[0]["retries"][0] == {
        "retry": 1, "max_retries": 3, "delay_s": 1.5, "reason": "status=429",
    }


def test_failed_call_keeps_error_and_truncates_message():
    t = ChahuaTransport(room_id="r", guest_name="A")
    snap = _feed(t, [_retry(1), _failed()])
    call = snap["llm_calls"][0]
    assert call["status"] == "error"
    assert call["error_class"] == "RateLimitError"
    assert len(call["error_message"]) == MAX_STR_LEN
    assert call["prompt_tokens"] is None
    assert len(call["retries"]) == 1


def test_pending_retries_become_cancelled_placeholder():
    t = ChahuaTransport(room_id="r", guest_name="A")
    snap = _feed(t, [_completed(), _retry(1), _retry(2)])
    calls = snap["llm_calls"]
    assert len(calls) == 2
    assert calls[1]["status"] == "cancelled"
    assert [r["retry"] for r in calls[1]["retries"]] == [1, 2]
    # 占位不伪造 token 字段（缺键 = 未知）
    assert "prompt_tokens" not in calls[1]


def test_finalize_idempotent_no_duplicate_placeholder():
    t = ChahuaTransport(room_id="r", guest_name="A")
    with t.bind(sink=lambda _e: None, turn_id="turn_x", message_id="msg_m"):
        t._handle(_retry(1))  # noqa: SLF001
        first = t.finalize_llm_records()
        second = t.finalize_llm_records()
    third = t.finalize_llm_records()  # bind 外（speak 外层 finally 的时机）
    assert first == second == third
    assert len(first["llm_calls"]) == 1


def test_events_after_finalize_are_dropped():
    t = ChahuaTransport(room_id="r", guest_name="A")
    with t.bind(sink=lambda _e: None, turn_id="turn_x", message_id="msg_m"):
        t._handle(_completed())  # noqa: SLF001
        snap = t.finalize_llm_records()
        t._handle(_completed(prompt_tokens=5))  # noqa: SLF001
        t._handle(_compaction())  # noqa: SLF001
    assert t.finalize_llm_records() == snap
    assert len(snap["llm_calls"]) == 1


def test_snapshot_is_a_copy():
    t = ChahuaTransport(room_id="r", guest_name="A")
    snap = _feed(t, [_completed()])
    snap["llm_calls"][0]["prompt_tokens"] = -1
    assert t.finalize_llm_records()["llm_calls"][0]["prompt_tokens"] == 1000


def test_compaction_recorded():
    t = ChahuaTransport(room_id="r", guest_name="A")
    snap = _feed(t, [_compaction(detail="d" * 900, extra="nope"), _completed()])
    comp = snap["compactions"]
    assert len(comp) == 1
    assert comp[0]["kind"] == "full"
    assert comp[0]["pre_tokens_history"] == 98000
    assert len(comp[0]["detail"]) == MAX_STR_LEN
    assert "extra" not in comp[0]


def test_no_cross_message_leak_and_events_outside_bind_dropped():
    t = ChahuaTransport(room_id="r", guest_name="A")
    _feed(t, [_completed(), _compaction()], message_id="msg_1")
    t._handle(_completed(prompt_tokens=7))  # noqa: SLF001  bind 外：丢
    snap2 = _feed(t, [_completed(prompt_tokens=2)], message_id="msg_2")
    assert [c["prompt_tokens"] for c in snap2["llm_calls"]] == [2]
    assert "compactions" not in snap2


def test_empty_message_has_empty_list():
    t = ChahuaTransport(room_id="r", guest_name="A")
    assert _feed(t, []) == {"llm_calls": []}


def test_call_cap_sets_truncated():
    t = ChahuaTransport(room_id="r", guest_name="A")
    snap = _feed(t, [_completed(prompt_tokens=i) for i in range(MAX_LLM_CALLS + 1)])
    assert len(snap["llm_calls"]) == MAX_LLM_CALLS
    # 丢的是最新的：保留前 64 条
    assert snap["llm_calls"][-1]["prompt_tokens"] == MAX_LLM_CALLS - 1
    assert snap["llm_records_truncated"] is True


def test_retry_cap_sets_truncated():
    t = ChahuaTransport(room_id="r", guest_name="A")
    snap = _feed(t, [*(_retry(i) for i in range(1, MAX_RETRIES_PER_CALL + 2)), _completed()])
    assert len(snap["llm_calls"][0]["retries"]) == MAX_RETRIES_PER_CALL
    assert snap["llm_records_truncated"] is True


def test_compaction_cap_sets_truncated():
    t = ChahuaTransport(room_id="r", guest_name="A")
    snap = _feed(t, [_compaction() for _ in range(MAX_COMPACTIONS + 1)])
    assert len(snap["compactions"]) == MAX_COMPACTIONS
    assert snap["llm_records_truncated"] is True


def test_bad_types_sanitized():
    t = ChahuaTransport(room_id="r", guest_name="A")
    snap = _feed(t, [_completed(prompt_tokens=True, completion_tokens="12",
                                finish_reason=3, duration_ms=1.5)])
    call = snap["llm_calls"][0]
    assert call["prompt_tokens"] is None      # bool 不是 int
    assert call["completion_tokens"] is None
    assert call["finish_reason"] is None
    assert call["duration_ms"] is None


# ── TeaGuest.speak 三条路径：message_end 与落盘同一快照 ─────────────────────


def _make_guest(tmp_path: Path, arun) -> tuple[TeaGuest, TurnRecorder, list]:
    room = Room(name="r")
    rec = TurnRecorder(tmp_path, enabled=True, capture_prompts=False)
    rec.start_turn(turn_id="turn_x", task_id=None, trigger={"kind": "user_msg", "ref_seq": 0})
    g = TeaGuest.__new__(TeaGuest)
    g.name = "A"
    g.room = room
    g._recorder = rec
    g._share_dir = None
    g._transport = ChahuaTransport(room_id="r", guest_name="A")
    agent = type("FakeAgent", (), {})()

    async def _arun(*_a, **_k):
        return await arun(g._transport)

    agent.arun = _arun
    g.agent = agent  # type: ignore[attr-defined]
    room.add_participant("A")
    return g, rec, []


def _rows(tmp_path: Path) -> list[dict]:
    return list(read_jsonl_skip_bad(tmp_path / "debug" / "turns.jsonl"))


def _end(envs) -> Any:
    ends = [e for e in envs if e.type == ChahuaEventType.MESSAGE_END]
    assert len(ends) == 1
    return ends[0]


async def test_speak_ok_snapshot_in_envelope_and_disk(tmp_path: Path):
    async def arun(transport):
        transport._handle(_retry(1))  # noqa: SLF001
        transport._handle(_completed(finish_reason="tool_calls"))  # noqa: SLF001
        transport._handle(_completed())  # noqa: SLF001
        return "hi"

    g, rec, envs = _make_guest(tmp_path, arun)
    msg = await g.speak("ctx", turn_id="turn_x", sink=envs.append)
    assert msg is not None
    rec.flush_turn()
    end = _end(envs)
    assert end.status == STATUS_OK
    assert end.data["text"] == "hi"
    disk = _rows(tmp_path)[0]["messages"][0]
    assert len(end.data["llm_calls"]) == 2
    assert disk["llm_calls"] == end.data["llm_calls"]
    assert disk["llm_calls"][0]["retries"][0]["retry"] == 1


async def test_bg_run_speak_message_end_has_no_llm_records(tmp_path: Path):
    """bg run（record_debug=False）不计：实时 message_end 不带 llm_calls（调试抽屉
    会对它兜底建桩，带了就把后台 run 计进「本轮茶客调用」，与盘上无记录不一致）。"""
    async def arun(transport):
        transport._handle(_completed())  # noqa: SLF001
        return "hi"

    g, _rec, envs = _make_guest(tmp_path, arun)
    msg = await g.speak("ctx", turn_id="turn_x", sink=envs.append, record_debug=False)
    assert msg is not None
    assert "llm_calls" not in _end(envs).data


async def test_bg_run_error_path_has_no_llm_records(tmp_path: Path):
    """失败 / 取消路径（``_emit_failure``）与成功路径同一判据：bg run 不带。"""
    async def arun(transport):
        transport._handle(_failed())  # noqa: SLF001
        raise RuntimeError("boom")

    g, _rec, envs = _make_guest(tmp_path, arun)
    assert await g.speak("ctx", turn_id="turn_x", sink=envs.append, record_debug=False) is None
    end = _end(envs)
    assert end.status == STATUS_ERROR
    assert "llm_calls" not in end.data


async def test_debug_disabled_room_message_end_has_no_llm_records(tmp_path: Path):
    """``[debug] enabled=false``：盘上不落，实时也不带（envelope == 磁盘）。"""
    async def arun(transport):
        transport._handle(_completed())  # noqa: SLF001
        return "hi"

    g, _rec, envs = _make_guest(tmp_path, arun)
    g._recorder = TurnRecorder(tmp_path / "off", enabled=False, capture_prompts=False)
    assert await g.speak("ctx", turn_id="turn_x", sink=envs.append) is not None
    assert "llm_calls" not in _end(envs).data


async def test_speak_error_path_carries_snapshot(tmp_path: Path):
    async def arun(transport):
        transport._handle(_failed())  # noqa: SLF001
        raise RuntimeError("boom")

    g, rec, envs = _make_guest(tmp_path, arun)
    assert await g.speak("ctx", turn_id="turn_x", sink=envs.append) is None
    rec.flush_turn()
    end = _end(envs)
    assert end.status == STATUS_ERROR
    assert end.data["error"] == "boom"
    disk = _rows(tmp_path)[0]["messages"][0]
    assert end.data["llm_calls"][0]["status"] == "error"
    assert disk["llm_calls"] == end.data["llm_calls"]


async def test_speak_cancel_path_placeholder_once(tmp_path: Path):
    import asyncio

    async def arun(transport):
        transport._handle(_retry(1))  # noqa: SLF001
        raise asyncio.CancelledError()

    g, rec, envs = _make_guest(tmp_path, arun)
    with pytest.raises(asyncio.CancelledError):
        await g.speak("ctx", turn_id="turn_x", sink=envs.append)
    rec.flush_turn()
    end = _end(envs)
    assert end.status == STATUS_CANCELLED
    disk = _rows(tmp_path)[0]["messages"][0]
    assert end.data["llm_calls"] == disk["llm_calls"]
    assert [c["status"] for c in disk["llm_calls"]] == ["cancelled"]


async def test_speak_truncated_flag_in_both(tmp_path: Path):
    async def arun(transport):
        for _ in range(MAX_LLM_CALLS + 1):
            transport._handle(_completed())  # noqa: SLF001
        return "hi"

    g, rec, envs = _make_guest(tmp_path, arun)
    await g.speak("ctx", turn_id="turn_x", sink=envs.append)
    rec.flush_turn()
    end = _end(envs)
    disk = _rows(tmp_path)[0]["messages"][0]
    assert end.data["llm_records_truncated"] is True
    assert disk["llm_records_truncated"] is True


async def test_recorder_message_entry_always_has_llm_calls(tmp_path: Path):
    async def arun(transport):
        return "hi"

    g, rec, envs = _make_guest(tmp_path, arun)
    await g.speak("ctx", turn_id="turn_x", sink=envs.append)
    rec.flush_turn()
    assert _rows(tmp_path)[0]["messages"][0]["llm_calls"] == []


async def test_finalize_failure_never_breaks_speak(tmp_path: Path, monkeypatch):
    async def arun(transport):
        transport._handle(_completed())  # noqa: SLF001
        return "hi"

    g, rec, envs = _make_guest(tmp_path, arun)

    def boom():
        raise RuntimeError("finalize broke")

    monkeypatch.setattr(g._transport, "finalize_llm_records", boom)
    msg = await g.speak("ctx", turn_id="turn_x", sink=envs.append)
    assert msg is not None and msg.text == "hi"
    end = _end(envs)
    assert end.status == STATUS_OK
    assert "llm_calls" not in end.data
    # 落盘与实时一致：无记录 = 缺键，不留 start 时的 [] 冒充「0 次调用」
    rec.flush_turn()
    assert "llm_calls" not in _rows(tmp_path)[0]["messages"][0]


def test_non_finite_retry_delay_dropped():
    import json
    import math

    from chahua._llm_call_records import LlmCallRecords

    r = LlmCallRecords()
    r.on_event(EventType.LLM_RETRY, {"retry": 1, "delay_s": math.inf})
    r.on_event(EventType.LLM_RETRY, {"retry": 2, "delay_s": math.nan})
    r.on_event(EventType.LLM_CALL_COMPLETED, {"status": "ok"})
    snap = r.finalize()
    assert [x["delay_s"] for x in snap["llm_calls"][0]["retries"]] == [None, None]
    json.dumps(snap, allow_nan=False)


def test_handle_never_raises_on_garbage():
    t = ChahuaTransport(room_id="r", guest_name="A")
    with t.bind(sink=lambda _e: None, turn_id="turn_x", message_id="msg_m"):
        t._handle(AgentEvent(EventType.LLM_CALL_COMPLETED, None))  # type: ignore[arg-type]  # noqa: SLF001
        t._handle(AgentEvent(EventType.LLM_RETRY, "nope"))  # type: ignore[arg-type]  # noqa: SLF001
        snap = t.finalize_llm_records()
    assert snap == {"llm_calls": []}


def test_record_message_end_without_snapshot_means_no_record(tmp_path: Path):
    """调用方不传快照 → 缺 ``llm_calls`` 键（无记录），不冒充「0 次调用」。"""
    rec = TurnRecorder(tmp_path, enabled=True, capture_prompts=False)
    rec.start_turn(turn_id="turn_x", task_id=None, trigger={"kind": "user_msg", "ref_seq": 0})
    rec.record_message_start(message_id="msg_m", guest="A", speak_prompt="")
    rec.record_message_end(message_id="msg_m", status=STATUS_OK, seq=1)
    rec.flush_turn()
    assert "llm_calls" not in _rows(tmp_path)[0]["messages"][0]
