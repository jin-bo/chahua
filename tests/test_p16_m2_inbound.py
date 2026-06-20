"""P16 M2：room_info 快照字段 + swap_room_config 轻热替（schedule_mode / talkativeness）。

承重点：

- room_info 出 ``schedule_mode``（room 顶层）+ ``talkativeness``（guest dict，默认 1.0
  也带，前端滑杆要回显）。
- ``swap_room_config`` 轻热替：schedule_mode → ``orchestrator.config``；talkativeness →
  ``_GuestEntry``，**不重建茶客**（orchestrator / TeaGuest 实例不变、不 _replace_session）。
- mutator 写盘 + 范围 / 白名单校验 + 默认值不写盘。

wire 层 dispatch / 类型校验在 ``test_server_inbound.py``（spy 路由测）。
"""

from __future__ import annotations

import pytest

from chahua import admin
from chahua.config import RoomConfigError, load_room_config
from chahua.orchestrator_config import SCHEDULE_MODE_MANUAL, SCHEDULE_MODE_SCORING
from chahua.server import ChahuaServer
from chahua.session import build_room_session


def _seed(env_paths, *, room_id="m2"):
    return admin.create_room(
        paths=env_paths, room_id=room_id, name=room_id,
        guests=[{"persona": "chahua/personas/宝总/宝总.md", "name": "宝总"}],
    )


def _room_info_data(env_paths, session) -> dict:
    srv = object.__new__(ChahuaServer)
    srv._session = session  # type: ignore[attr-defined]
    srv._paths = env_paths  # type: ignore[attr-defined]
    captured: list[dict] = []
    srv._emit_room_info(lambda env: captured.append(env.to_dict()))
    assert len(captured) == 1
    return captured[0]["data"]


# ── room_info 快照字段 ────────────────────────────────────────────────────────


def test_room_info_schedule_mode_and_talkativeness_defaults(env_paths):
    rc = _seed(env_paths)
    session = build_room_session(rc.room_dir, env_paths)
    try:
        data = _room_info_data(env_paths, session)
        assert data["schedule_mode"] == SCHEDULE_MODE_SCORING
        assert data["guests"][0]["talkativeness"] == 1.0
    finally:
        session.close()


def test_room_info_reflects_non_default(env_paths):
    rc = _seed(env_paths)
    admin.update_room_schedule_mode(
        paths=env_paths, room_dir=rc.room_dir, mode=SCHEDULE_MODE_MANUAL
    )
    admin.update_guest_talkativeness(
        paths=env_paths, room_dir=rc.room_dir, name="宝总", talkativeness=1.8
    )
    session = build_room_session(rc.room_dir, env_paths)
    try:
        data = _room_info_data(env_paths, session)
        assert data["schedule_mode"] == SCHEDULE_MODE_MANUAL
        assert data["guests"][0]["talkativeness"] == 1.8
    finally:
        session.close()


# ── swap_room_config 轻热替（不重建）─────────────────────────────────────────


def test_swap_schedule_mode_light_no_rebuild(env_paths):
    rc = _seed(env_paths)
    session = build_room_session(rc.room_dir, env_paths)
    try:
        orch_before = session.orchestrator
        new_rc = admin.update_room_schedule_mode(
            paths=env_paths, room_dir=rc.room_dir, mode=SCHEDULE_MODE_MANUAL
        )
        session.swap_room_config(new_rc)
        # 同 orchestrator 实例 —— 没 _replace_session 重建。
        assert session.orchestrator is orch_before
        assert session.orchestrator.config.schedule_mode == SCHEDULE_MODE_MANUAL
        # 写盘保真。
        assert load_room_config(
            rc.room_dir, paths=env_paths
        ).schedule_mode == SCHEDULE_MODE_MANUAL
    finally:
        session.close()


def test_swap_talkativeness_light_no_rebuild(env_paths):
    rc = _seed(env_paths)
    session = build_room_session(rc.room_dir, env_paths)
    try:
        guest_before = session.orchestrator.get_guest("宝总")
        new_rc = admin.update_guest_talkativeness(
            paths=env_paths, room_dir=rc.room_dir, name="宝总", talkativeness=1.8
        )
        session.swap_room_config(new_rc)
        # 同 TeaGuest 实例 —— agentao / 进程内会话 / 记忆全不动。
        assert session.orchestrator.get_guest("宝总") is guest_before
        assert session.orchestrator._guests["宝总"].talkativeness == 1.8
        assert load_room_config(
            rc.room_dir, paths=env_paths
        ).guests[0].talkativeness == 1.8
    finally:
        session.close()


def test_swap_talkativeness_explicit_zero(env_paths):
    """显式 0.0（哑茶客）经 swap 落到 _GuestEntry，不被吞成 1.0。"""
    rc = _seed(env_paths)
    session = build_room_session(rc.room_dir, env_paths)
    try:
        new_rc = admin.update_guest_talkativeness(
            paths=env_paths, room_dir=rc.room_dir, name="宝总", talkativeness=0.0
        )
        session.swap_room_config(new_rc)
        assert session.orchestrator._guests["宝总"].talkativeness == 0.0
    finally:
        session.close()


# ── mutator 校验 + 默认不写盘 ─────────────────────────────────────────────────


def test_update_room_schedule_mode_rejects_deferred(env_paths):
    rc = _seed(env_paths)
    with pytest.raises(RoomConfigError):
        admin.update_room_schedule_mode(
            paths=env_paths, room_dir=rc.room_dir, mode="round_robin"
        )


def test_update_room_schedule_mode_default_not_written(env_paths):
    rc = _seed(env_paths)
    admin.update_room_schedule_mode(
        paths=env_paths, room_dir=rc.room_dir, mode=SCHEDULE_MODE_MANUAL
    )
    admin.update_room_schedule_mode(
        paths=env_paths, room_dir=rc.room_dir, mode=SCHEDULE_MODE_SCORING
    )
    toml = (rc.room_dir / "room.toml").read_text(encoding="utf-8")
    assert "schedule_mode" not in toml


@pytest.mark.parametrize("bad", [5.0, -0.5])
def test_update_guest_talkativeness_out_of_range(env_paths, bad):
    rc = _seed(env_paths)
    with pytest.raises(RoomConfigError):
        admin.update_guest_talkativeness(
            paths=env_paths, room_dir=rc.room_dir, name="宝总", talkativeness=bad
        )


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_update_guest_talkativeness_non_finite(env_paths, bad):
    """mutator 写盘前拒非有限值（NaN 漏过区间判会落 'nan' 到 toml + 永久静默茶客）。"""
    rc = _seed(env_paths)
    with pytest.raises(RoomConfigError):
        admin.update_guest_talkativeness(
            paths=env_paths, room_dir=rc.room_dir, name="宝总", talkativeness=bad
        )


def test_update_guest_talkativeness_unknown_name(env_paths):
    rc = _seed(env_paths)
    with pytest.raises(ValueError):
        admin.update_guest_talkativeness(
            paths=env_paths, room_dir=rc.room_dir, name="查无此人", talkativeness=1.5
        )


def test_update_guest_talkativeness_default_not_written(env_paths):
    rc = _seed(env_paths)
    admin.update_guest_talkativeness(
        paths=env_paths, room_dir=rc.room_dir, name="宝总", talkativeness=1.8
    )
    admin.update_guest_talkativeness(
        paths=env_paths, room_dir=rc.room_dir, name="宝总", talkativeness=1.0
    )
    toml = (rc.room_dir / "room.toml").read_text(encoding="utf-8")
    assert "talkativeness" not in toml
