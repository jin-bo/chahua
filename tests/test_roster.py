"""P8.2-roster-a：手写 ``[[guest]].summary`` 能力花名册。

覆盖：① config 解析 summary 字段 ② ``<room>`` 在场行带摘要渲成 bullet 花名册
③ 全员无摘要 → 退回逗号单行 ④ 长摘要硬截断 ⑤ toml round-trip 不丢 summary。
"""

from __future__ import annotations

from pathlib import Path

from chahua import admin
from chahua.admin_room import _room_config_to_dict
from chahua.admin_toml import _render_room_toml
from chahua.config import load_room_config
from chahua.cursor import GuestCursor
from chahua.orchestrator import Orchestrator, OrchestratorConfig
from chahua.room import Room
from chahua.user_md import USER_SPEAKER_ID, UserConfig

from conftest import NoopScorer, NoopSummarizer


def _seed_room(env_paths):
    return admin.create_room(
        paths=env_paths, room_id="roster-cfg", name="roster test",
        guests=[{"persona": "chahua/personas/宝总/宝总.md", "name": "宝总"}],
    )


def _write_toml(rc, guest_extra: str) -> None:
    (rc.room_dir / "room.toml").write_text(
        '[room]\nname = "x"\n\n[[guest]]\nname = "宝总"\n'
        'persona = "chahua/personas/宝总/宝总.md"\npermission = "read-only"\n'
        + guest_extra,
        encoding="utf-8",
    )


# ─── ① config 解析 ──────────────────────────────────────────────────────────


def test_guest_summary_parsed(env_paths):
    rc = _seed_room(env_paths)
    _write_toml(rc, 'summary = "擅长成本测算与预算评审"\n')
    rc2 = load_room_config(rc.room_dir, paths=env_paths)
    assert rc2.guests[0].summary == "擅长成本测算与预算评审"


def test_guest_summary_absent_is_none(env_paths):
    rc = _seed_room(env_paths)
    _write_toml(rc, "")
    rc2 = load_room_config(rc.room_dir, paths=env_paths)
    assert rc2.guests[0].summary is None


# ─── ②③④ <room> 在场行渲染 ─────────────────────────────────────────────────


def _build_orch(*, roster=None, guests=("范总", "汪小姐")) -> Orchestrator:
    room = Room(name="黄河路")
    room.add_participant(USER_SPEAKER_ID)
    for g in guests:
        room.add_participant(g)
    return Orchestrator(
        room=room,
        user_config=UserConfig(display_name="老金", full_md=None, source=None),
        scorer=NoopScorer(),
        summarizer=NoopSummarizer(),
        cursor=GuestCursor(),
        config=OrchestratorConfig(onboarding_threshold=20),
        roster=roster,
    )


def test_room_block_renders_summary_roster():
    orch = _build_orch(roster={"范总": "擅长成本测算"})
    ctx = orch._build_context_for("范总", task_id=None)
    assert "Present:\n-" in ctx            # 有摘要 → bullet 花名册
    assert "- 范总 — 擅长成本测算" in ctx
    assert "- 汪小姐" in ctx            # 无摘要的茶客只列名
    assert "- 老金 (human user)" in ctx   # 用户无摘要、不带 —


def test_room_block_no_summary_falls_back_to_comma():
    orch = _build_orch(roster=None)
    ctx = orch._build_context_for("范总", task_id=None)
    assert "Present: 老金 (human user), 范总, 汪小姐" in ctx


def test_room_summary_truncated():
    orch = _build_orch(roster={"范总": "能" * 60})
    ctx = orch._build_context_for("范总", task_id=None)
    assert "能" * 40 + "…" in ctx
    assert "能" * 41 not in ctx


# ─── ⑤ toml round-trip ──────────────────────────────────────────────────────


def test_summary_round_trips_through_toml(env_paths):
    rc = _seed_room(env_paths)
    _write_toml(rc, 'summary = "擅长成本测算与预算评审"\n')
    rc1 = load_room_config(rc.room_dir, paths=env_paths)

    text = _render_room_toml(_room_config_to_dict(rc1, env_paths))
    assert "擅长成本测算与预算评审" in text

    # 重写回盘后再解析，summary 不丢
    (rc.room_dir / "room.toml").write_text(text, encoding="utf-8")
    rc2 = load_room_config(rc.room_dir, paths=env_paths)
    assert rc2.guests[0].summary == "擅长成本测算与预算评审"
