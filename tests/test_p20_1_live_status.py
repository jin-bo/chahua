"""P20.1 茶客实时状态提示（docs/P20.1-茶客实时状态提示.md §7）。

transport 把 ``LLM_RETRY`` / ``COMPACTION_STARTED`` 转成 ``guest_status`` hint，
对应的结束事件（``LLM_CALL_COMPLETED`` / ``COMPACTION_SETTLED``）补一帧 ``idle``。
真 agentao 的一条在 ``tests/test_agentao_contract.py``。
"""

from __future__ import annotations

from typing import Any

from agentao.transport import AgentEvent, EventType

from chahua.agent_run_sink import BatchMessageSink
from chahua.events import ChahuaEnvelope, ChahuaEventType
from chahua.room_runtime import ROUTER_MODE_BACKGROUND, RoomEventRouter
from chahua.transport_bridge import ChahuaTransport


def _retry(n: int = 1, **over: Any) -> AgentEvent:
    data = {"retry": n, "max_retries": 4, "delay_s": 1.62, "reason": "status=503"}
    data.update(over)
    return AgentEvent(EventType.LLM_RETRY, data)


def _completed(status: str = "ok") -> AgentEvent:
    return AgentEvent(EventType.LLM_CALL_COMPLETED, {"status": status, "duration_ms": 10})


def _comp_started() -> AgentEvent:
    return AgentEvent(EventType.COMPACTION_STARTED, {"trigger": "auto", "kind": "full", "reason": "x"})


def _comp_settled() -> AgentEvent:
    return AgentEvent(EventType.COMPACTION_SETTLED, {"kind": "full", "status": "success"})


def _run(events, *, transport: ChahuaTransport | None = None, message_id: str = "msg_a"):
    t = transport or ChahuaTransport(room_id="r", guest_name="A")
    out: list[ChahuaEnvelope] = []
    with t.bind(sink=out.append, turn_id="turn_a", message_id=message_id):
        for ev in events:
            t._handle(ev)  # noqa: SLF001
    return [e for e in out if e.type is ChahuaEventType.GUEST_STATUS]


def test_retry_emits_retrying_with_cleaned_fields():
    [env] = _run([_retry(extra_key="x", delay_s=float("nan"))])
    assert env.message_id == "msg_a" and env.turn_id == "turn_a" and env.guest_name == "A"
    assert env.data == {"state": "retrying", "retry": 1, "max_retries": 4, "reason": "status=503"}


def test_each_retry_refreshes_then_completed_clears():
    envs = _run([_retry(1), _retry(2), _completed()])
    assert [e.data["state"] for e in envs] == ["retrying", "retrying", "idle"]
    assert envs[1].data["retry"] == 2


def test_error_completed_also_clears():
    envs = _run([_retry(1), _completed("error")])
    assert [e.data["state"] for e in envs] == ["retrying", "idle"]


def test_completed_without_retry_emits_nothing():
    assert _run([_completed(), _completed()]) == []


def test_compaction_started_then_settled():
    envs = _run([_comp_started(), _comp_settled()])
    assert [e.data for e in envs] == [{"state": "compacting"}, {"state": "idle"}]


def test_settled_without_started_emits_nothing():
    # light / microcompact 不发 STARTED，只有 SETTLED —— 不应冒出多余 idle
    assert _run([_comp_settled()]) == []


def test_completed_does_not_clear_compacting():
    envs = _run([_comp_started(), _completed()])
    assert [e.data["state"] for e in envs] == ["compacting"]


def test_p20_records_still_fed():
    t = ChahuaTransport(room_id="r", guest_name="A")
    with t.bind(sink=lambda _e: None, turn_id="turn_a", message_id="msg_a"):
        for ev in (_retry(1), _completed(), _comp_started(), _comp_settled()):
            t._handle(ev)  # noqa: SLF001
        snap = t.finalize_llm_records()
    assert len(snap["llm_calls"]) == 1 and len(snap["llm_calls"][0]["retries"]) == 1
    assert len(snap["compactions"]) == 1


def test_outside_bind_nothing_emitted_and_new_bind_resets():
    t = ChahuaTransport(room_id="r", guest_name="A")
    _run([_retry(1)], transport=t, message_id="msg_1")  # 没收到 COMPLETED 就结束
    t._handle(_retry(9))  # noqa: SLF001  bind 外：不发、不改状态
    # 新消息里一个无重试的 COMPLETED 不应补发上一条消息遗留的 idle
    assert _run([_completed()], transport=t, message_id="msg_2") == []


def test_inflight_snapshot_carries_live_status():
    t = ChahuaTransport(room_id="r", guest_name="A")
    with t.bind(sink=lambda _e: None, turn_id="turn_a", message_id="msg_a"):
        assert t.inflight_snapshot()["live_status"] is None
        t._handle(_retry(2))  # noqa: SLF001
        assert t.inflight_snapshot()["live_status"]["state"] == "retrying"
        t._handle(_completed())  # noqa: SLF001
        assert t.inflight_snapshot()["live_status"] is None


def test_sink_failure_never_breaks_handle():
    t = ChahuaTransport(room_id="r", guest_name="A")

    def boom(_env):
        raise RuntimeError("sink down")

    with t.bind(sink=boom, turn_id="turn_a", message_id="msg_a"):
        t._handle(_retry(1))  # noqa: SLF001
        t._handle(_completed())  # noqa: SLF001
        snap = t.finalize_llm_records()
    assert len(snap["llm_calls"]) == 1


# ── 后台不发 ─────────────────────────────────────────────────────────────────


def _status_env() -> ChahuaEnvelope:
    return ChahuaEnvelope(
        room_id="r", turn_id="t", guest_name="A", message_id="m",
        type=ChahuaEventType.GUEST_STATUS, data={"state": "compacting"},
    )


def test_bg_run_sink_drops_guest_status():
    seen: list = []
    BatchMessageSink(run_id="run_x", downstream=seen.append)(_status_env())
    assert seen == []


def test_background_room_router_drops_guest_status():
    seen: list = []
    router = RoomEventRouter(seen.append)
    router.mode = ROUTER_MODE_BACKGROUND
    router(_status_env())
    assert seen == []
