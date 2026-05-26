"""P12.1 backward-compat：旧 room.toml 用 flat-form built-in 路径仍能加载（P12.1）。

承重契约：pre-P12.1 用户的 user_data 下 room.toml 还会写
``chahua/personas/宝总.md`` 这种 flat-form 路径；P12.1 起 ship 自带 personas 改 dir-form
``chahua/personas/宝总/宝总.md`` —— `find_in_data_then_app` 直查 flat-form 路径会
miss，必须有 backward-compat 让用户升级后房间不至于打不开。

`_try_p12_1_dir_form_rewrite` + `load_room_config` 自动尝试 dir-form 升级 + WARN 一次。
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from chahua._paths import ENV_APP_ROOT, ENV_USER_DATA_ROOT, Paths
from chahua.config import (
    RoomConfigError,
    _try_p12_1_dir_form_rewrite,
    load_room_config,
)


REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def paths(tmp_path, monkeypatch):
    user_data = tmp_path / "userdata"
    user_data.mkdir()
    monkeypatch.setenv(ENV_APP_ROOT, str(REPO_ROOT))
    monkeypatch.setenv(ENV_USER_DATA_ROOT, str(user_data))
    return Paths.from_env()


# ── _try_p12_1_dir_form_rewrite 纯函数 ─────────────────────────────


def test_rewrite_flat_form_to_dir_form():
    assert (
        _try_p12_1_dir_form_rewrite("chahua/personas/宝总.md")
        == "chahua/personas/宝总/宝总.md"
    )
    assert (
        _try_p12_1_dir_form_rewrite("chahua/personas/Yvonne.md")
        == "chahua/personas/Yvonne/Yvonne.md"
    )


def test_rewrite_returns_none_for_dir_form():
    # 已是 dir-form 不应被重写
    assert _try_p12_1_dir_form_rewrite("chahua/personas/宝总/宝总.md") is None


def test_rewrite_returns_none_for_non_persona_path():
    assert _try_p12_1_dir_form_rewrite("rooms/foo/bar.md") is None
    assert _try_p12_1_dir_form_rewrite("README.md") is None


def test_rewrite_returns_none_for_non_md_suffix():
    assert _try_p12_1_dir_form_rewrite("chahua/personas/宝总.txt") is None
    assert _try_p12_1_dir_form_rewrite("chahua/personas/宝总") is None


def test_rewrite_returns_none_for_empty_stem():
    assert _try_p12_1_dir_form_rewrite("chahua/personas/.md") is None


# ── 集成：load_room_config 走自动升级路径 ──────────────────────────


def test_load_room_with_flat_form_path_auto_upgrades(paths, caplog):
    """种一个老 room.toml（flat-form persona ref），验证 load_room_config 仍能加载 +
    WARN 一次。"""
    room_dir = paths.user_data_root / "rooms" / "legacy"
    room_dir.mkdir(parents=True)
    (room_dir / "room.toml").write_text(
        """\
[room]
name = "legacy"

[[guest]]
name = "宝总"
persona = "chahua/personas/宝总.md"
permission = "read-only"
""",
        encoding="utf-8",
    )
    with caplog.at_level(logging.WARNING, logger="chahua.config"):
        rc = load_room_config(room_dir, paths=paths)
    # persona path 仍解析成功 + 指向 dir-form 真路径
    g = rc.guests[0]
    assert g.persona_path.name == "宝总.md"
    assert g.persona_path.parent.name == "宝总"  # dir-form
    # WARN 含路径升级提示
    assert any(
        "自动升级" in rec.message and "宝总" in rec.message for rec in caplog.records
    ), f"未见自动升级 WARN，caplog: {[r.message for r in caplog.records]}"


def test_load_room_with_unknown_persona_still_raises(paths):
    """rewrite 命中但 dir-form 也不存在 → 仍走原错误路径。"""
    room_dir = paths.user_data_root / "rooms" / "ghost"
    room_dir.mkdir(parents=True)
    (room_dir / "room.toml").write_text(
        """\
[room]
name = "ghost"

[[guest]]
name = "Ghost"
persona = "chahua/personas/Ghost.md"
permission = "read-only"
""",
        encoding="utf-8",
    )
    with pytest.raises(RoomConfigError, match="persona 文件不存在"):
        load_room_config(room_dir, paths=paths)
