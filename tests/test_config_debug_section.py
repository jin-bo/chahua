"""``[debug]`` section 解析（P6.1，docs/P6-调试与回放.md §2）。

口径与 ``[room.llm]`` 同：未知字段 → :class:`RoomConfigError`（"section all-or-nothing"
不变量）；缺段 → 全字段默认（``enabled=True``、``capture_prompts=True``）。
"""

from __future__ import annotations

import pytest

from chahua import admin
from chahua.config import DebugConfig, RoomConfigError, load_room_config


def _seed_room(env_paths):
    return admin.create_room(
        paths=env_paths, room_id="debug-cfg", name="debug cfg test",
        guests=[{"persona": "chahua/personas/宝总.md", "name": "宝总"}],
    )


def _write_toml(rc, extra: str) -> None:
    body = (
        '[room]\nname = "x"\n\n'
        + extra
        + '\n\n[[guest]]\nname = "宝总"\n'
        'persona = "chahua/personas/宝总.md"\npermission = "read-only"\n'
    )
    (rc.room_dir / "room.toml").write_text(body, encoding="utf-8")


def test_missing_section_defaults_on(env_paths):
    """缺 [debug] 段 → 默认 enabled=True / capture_prompts=True（同 onboarding
    场景：bug 发生前没人会去开，默认 off 等于"等 bug 复现后再开"但 bug 已经过去）。"""
    rc = _seed_room(env_paths)
    rc2 = load_room_config(rc.room_dir, paths=env_paths)
    assert rc2.debug == DebugConfig(enabled=True, capture_prompts=True)


def test_explicit_section_both_fields(env_paths):
    rc = _seed_room(env_paths)
    _write_toml(rc, "[debug]\nenabled = true\ncapture_prompts = false")
    rc2 = load_room_config(rc.room_dir, paths=env_paths)
    assert rc2.debug == DebugConfig(enabled=True, capture_prompts=False)


def test_disabled(env_paths):
    rc = _seed_room(env_paths)
    _write_toml(rc, "[debug]\nenabled = false")
    rc2 = load_room_config(rc.room_dir, paths=env_paths)
    # toml 字段层面 capture_prompts 仍是默认 True；TurnRecorder 装配期强制为 False。
    # config 层只忠实解析 toml 字段（不替 recorder 做语义约束）。
    assert rc2.debug.enabled is False
    assert rc2.debug.capture_prompts is True


def test_unknown_field_rejected(env_paths):
    rc = _seed_room(env_paths)
    _write_toml(rc, '[debug]\nenabled = true\nnovelfield = "x"')
    with pytest.raises(RoomConfigError, match=r"\[debug\].*未知字段"):
        load_room_config(rc.room_dir, paths=env_paths)


def test_bad_type_rejected(env_paths):
    """非 bool 字段 → RoomConfigError（与 ``[room]`` 编排参数同口径，宁可炸）。"""
    rc = _seed_room(env_paths)
    _write_toml(rc, '[debug]\nenabled = "yes"')
    with pytest.raises(RoomConfigError, match=r"\[debug\]\.enabled.*布尔值"):
        load_room_config(rc.room_dir, paths=env_paths)


def test_section_must_be_table(env_paths):
    """``debug = "x"`` 这种把 [debug] 写成标量 → RoomConfigError，给出类型提示。

    顶层 ``debug = "oops"`` 必须放在 ``[room]`` 之前，否则 TOML 解析把它当成
    ``[room].debug``（与 ``[room]`` 段未知字段规则冲突，错误信息会指向 ``[room]``）。
    """
    rc = _seed_room(env_paths)
    body = (
        'debug = "oops"\n\n'
        '[room]\nname = "x"\n\n'
        '[[guest]]\nname = "宝总"\n'
        'persona = "chahua/personas/宝总.md"\npermission = "read-only"\n'
    )
    (rc.room_dir / "room.toml").write_text(body, encoding="utf-8")
    with pytest.raises(RoomConfigError, match=r"\[debug\].*表"):
        load_room_config(rc.room_dir, paths=env_paths)
