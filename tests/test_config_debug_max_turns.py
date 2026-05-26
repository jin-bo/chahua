"""``[debug].max_turns`` 解析（P6.3.B，docs/P6.3 §8）。

口径与 ``[debug].enabled`` / ``capture_prompts`` 同：``[debug]`` all-or-nothing；未知字段
拒（同 ``test_config_debug_section.py``）。本测试只覆盖 ``max_turns`` 字段：缺省走默认、
非整数拒、负值拒、``0`` 关 rotation、非默认值在 admin snapshot 中 round-trip 保住。
"""

from __future__ import annotations

import pytest

from chahua import admin
from chahua.admin import _debug_config_to_dict
from chahua.config import (
    DEBUG_DEFAULT_MAX_TURNS,
    DebugConfig,
    RoomConfigError,
    load_room_config,
)


def _seed_room(env_paths):
    return admin.create_room(
        paths=env_paths, room_id="debug-mt", name="debug max_turns test",
        guests=[{"persona": "chahua/personas/宝总/宝总.md", "name": "宝总"}],
    )


def _write_toml(rc, extra: str) -> None:
    body = (
        '[room]\nname = "x"\n\n'
        + extra
        + '\n\n[[guest]]\nname = "宝总"\n'
        'persona = "chahua/personas/宝总/宝总.md"\npermission = "read-only"\n'
    )
    (rc.room_dir / "room.toml").write_text(body, encoding="utf-8")


def test_missing_defaults_to_500(env_paths):
    """缺段 → DebugConfig 默认 max_turns = DEBUG_DEFAULT_MAX_TURNS (= 500)。"""
    rc = _seed_room(env_paths)
    rc2 = load_room_config(rc.room_dir, paths=env_paths)
    assert rc2.debug.max_turns == DEBUG_DEFAULT_MAX_TURNS
    assert rc2.debug == DebugConfig()


def test_explicit_max_turns(env_paths):
    rc = _seed_room(env_paths)
    _write_toml(rc, "[debug]\nmax_turns = 200")
    rc2 = load_room_config(rc.room_dir, paths=env_paths)
    assert rc2.debug.max_turns == 200


def test_zero_means_disable_rotation(env_paths):
    """``max_turns = 0`` 是"关 rotation"语义（不是关 debug）。config 层只解析；
    rotation 行为由 TurnRecorder 实现 —— 见 test_debug_recorder_rotation。"""
    rc = _seed_room(env_paths)
    _write_toml(rc, "[debug]\nmax_turns = 0")
    rc2 = load_room_config(rc.room_dir, paths=env_paths)
    assert rc2.debug.max_turns == 0


def test_negative_rejected(env_paths):
    rc = _seed_room(env_paths)
    _write_toml(rc, "[debug]\nmax_turns = -1")
    with pytest.raises(RoomConfigError, match=r"\[debug\]\.max_turns.*越界"):
        load_room_config(rc.room_dir, paths=env_paths)


def test_non_integer_rejected(env_paths):
    rc = _seed_room(env_paths)
    _write_toml(rc, '[debug]\nmax_turns = "300"')
    with pytest.raises(RoomConfigError, match=r"\[debug\]\.max_turns.*整数"):
        load_room_config(rc.room_dir, paths=env_paths)


def test_bool_rejected(env_paths):
    """bool 是 int 子类 —— 必须先剔（同 ORCH_FIELD_BOUNDS 口径）。"""
    rc = _seed_room(env_paths)
    _write_toml(rc, "[debug]\nmax_turns = true")
    with pytest.raises(RoomConfigError, match=r"\[debug\]\.max_turns.*整数"):
        load_room_config(rc.room_dir, paths=env_paths)


def test_admin_snapshot_only_emits_non_default():
    """``_debug_config_to_dict``：默认 max_turns=500 时整段返 None；非默认走 snapshot。

    防回归"结构化重写 room.toml 把默认 500 塞回来变噪声"以及"用户写过 = 200 经
    mutator round-trip 后丢失"。
    """
    # 全默认 → None
    assert _debug_config_to_dict(DebugConfig()) is None
    # 非默认 max_turns → snapshot 含 max_turns（不挂 enabled / capture_prompts 噪声）
    snap = _debug_config_to_dict(DebugConfig(max_turns=200))
    assert snap == {"max_turns": 200}
    # 全字段非默认 → 全 emit
    snap2 = _debug_config_to_dict(
        DebugConfig(enabled=False, capture_prompts=False, max_turns=0)
    )
    assert snap2 == {"enabled": False, "capture_prompts": False, "max_turns": 0}


def test_admin_snapshot_renders_to_toml(env_paths):
    """end-to-end：``DebugConfig(max_turns=200)`` 经 admin → admin_toml 渲染应在
    输出里出现 ``max_turns = 200``。"""
    from chahua.admin_toml import _render_room_toml
    snapshot = {
        "name": "x", "topic": "", "rules": "",
        "user_md": None, "orchestrator_overrides": {},
        "room_llm": None, "scoring": None, "summary": None,
        "debug": {"max_turns": 200},
        "guests": [
            {"name": "宝总", "persona": "chahua/personas/宝总/宝总.md",
             "permission": "read-only"},
        ],
    }
    text = _render_room_toml(snapshot)
    assert "[debug]" in text
    assert "max_turns = 200" in text
