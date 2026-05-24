"""P11 C3：4 个 AGENT_RUN_* envelope 类型 + ``_project_agent_runs`` 投影 helper +
后台白名单加入 4 个里程碑。
"""

from __future__ import annotations

from chahua.agent_run import (
    AGENT_RUN_ISSUED_BY_AGENT,
    AGENT_RUN_ISSUED_BY_USER,
    create as create_agent_run,
)
from chahua.events import (
    ChahuaEnvelope,
    ChahuaEventType,
    NOOP_SINK,
)
from chahua.room_runtime import (
    _BACKGROUND_WHITELIST,
    ROUTER_MODE_BACKGROUND,
    RoomEventRouter,
    RoomRuntime,
)
from chahua.server_room_snapshot import _project_agent_runs


def test_4_agent_run_event_types_exist() -> None:
    """4 个新 enum 字符串值与 envelope round-trip。"""
    assert ChahuaEventType.AGENT_RUN_STARTED.value == "agent_run_started"
    assert ChahuaEventType.AGENT_RUN_FINISHED.value == "agent_run_finished"
    assert ChahuaEventType.AGENT_RUN_CANCELLED.value == "agent_run_cancelled"
    assert ChahuaEventType.AGENT_RUN_ERROR.value == "agent_run_error"


def test_envelope_round_trip_with_agent_run_started() -> None:
    env = ChahuaEnvelope(
        room_id="r",
        turn_id=None,
        guest_name=None,
        message_id=None,
        type=ChahuaEventType.AGENT_RUN_STARTED,
        data={"run_id": "run_abc", "guest_name": "Bob", "issued_by": "user"},
    )
    out = env.to_dict()
    assert out["type"] == "agent_run_started"
    assert out["data"]["run_id"] == "run_abc"


def test_background_whitelist_contains_all_agent_run_events() -> None:
    """4 个 AGENT_RUN_* 全部加入后台里程碑白名单。"""
    assert ChahuaEventType.AGENT_RUN_STARTED in _BACKGROUND_WHITELIST
    assert ChahuaEventType.AGENT_RUN_FINISHED in _BACKGROUND_WHITELIST
    assert ChahuaEventType.AGENT_RUN_CANCELLED in _BACKGROUND_WHITELIST
    assert ChahuaEventType.AGENT_RUN_ERROR in _BACKGROUND_WHITELIST


def test_background_router_passes_agent_run_events() -> None:
    """后台路由模式实测放行 AGENT_RUN_*。"""
    seen: list[ChahuaEnvelope] = []
    router = RoomEventRouter(seen.append, mode=ROUTER_MODE_BACKGROUND)

    for kind in (
        ChahuaEventType.AGENT_RUN_STARTED,
        ChahuaEventType.AGENT_RUN_FINISHED,
        ChahuaEventType.AGENT_RUN_CANCELLED,
        ChahuaEventType.AGENT_RUN_ERROR,
    ):
        env = ChahuaEnvelope(
            room_id="r", turn_id=None, guest_name=None,
            message_id=None, type=kind, data={},
        )
        router(env)
    assert len(seen) == 4


# ── _project_agent_runs 投影 helper ─────────────────────────────────────


def _runtime_with_runs(*runs) -> RoomRuntime:
    rt = RoomRuntime(
        room_id="r",
        session=object(),  # type: ignore[arg-type]
        router=RoomEventRouter(NOOP_SINK),
    )
    for run in runs:
        rt.agent_runs[run.run_id] = run
    return rt


def test_project_agent_runs_empty() -> None:
    rt = _runtime_with_runs()
    assert _project_agent_runs(rt) == []


def test_project_agent_runs_minimal_fields_only() -> None:
    """``task_id`` / ``source_guest`` 缺省不写键（与 envelope shape 同口径）。"""
    run = create_agent_run(
        room_id="r", guest_name="Bob",
        instruction="ping", issued_by=AGENT_RUN_ISSUED_BY_USER,
    )
    rt = _runtime_with_runs(run)
    out = _project_agent_runs(rt)
    assert len(out) == 1
    item = out[0]
    assert item["run_id"] == run.run_id
    assert item["guest_name"] == "Bob"
    assert item["issued_by"] == "user"
    assert item["instruction_preview"] == "ping"
    assert "task_id" not in item
    assert "source_guest" not in item
    assert item["created_at_ms"] > 0


def test_project_agent_runs_with_task_and_source() -> None:
    """task_id + source_guest 都给：均写键。"""
    run = create_agent_run(
        room_id="r", guest_name="Bob", instruction="审稿方案 v1",
        task_id="task_xyz",
        issued_by=AGENT_RUN_ISSUED_BY_AGENT, source_guest="Alice",
    )
    rt = _runtime_with_runs(run)
    out = _project_agent_runs(rt)
    item = out[0]
    assert item["task_id"] == "task_xyz"
    assert item["source_guest"] == "Alice"
    assert item["issued_by"] == "agent"


def test_project_agent_runs_truncates_long_instruction_preview() -> None:
    """instruction_preview 截 ≤30 字符（与 UI 运行列表条同口径）。"""
    long_text = "a" * 100
    run = create_agent_run(room_id="r", guest_name="Bob", instruction=long_text)
    rt = _runtime_with_runs(run)
    item = _project_agent_runs(rt)[0]
    assert item["instruction_preview"] == "a" * 30
    assert len(item["instruction_preview"]) == 30


def test_project_agent_runs_multiple_runs_preserves_each() -> None:
    """两条不同 target 的 bg run 全部投影出来。"""
    r1 = create_agent_run(room_id="r", guest_name="Bob", instruction="p1")
    r2 = create_agent_run(room_id="r", guest_name="Alice", instruction="p2")
    rt = _runtime_with_runs(r1, r2)
    out = _project_agent_runs(rt)
    assert len(out) == 2
    by_guest = {item["guest_name"] for item in out}
    assert by_guest == {"Bob", "Alice"}
