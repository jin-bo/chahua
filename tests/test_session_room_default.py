"""P4.9：``[room.llm]`` 与 env 推断的两层 fallback。

针对 :func:`chahua.session._resolve_room_default_spec` 单测 —— 不走完整
:func:`build_room_session`（那条路径还要装 LLMClient / 茶客实例，依赖凭据 env）。
四个场景：

1. ``[room.llm]`` only —— toml 段在，env 缺，spec 取 toml。
2. env only —— toml 段缺，env 在，spec 取 env。
3. 都在 —— toml 赢（env 完全忽略，避免 shell ``LLM_TEMPERATURE`` "偷胜"覆盖
   用户在 toml 里的 ``temperature``）。
4. 都缺 —— :class:`RoomConfigError`，错误信息要同时列两条 fix 路径。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from chahua import admin
from chahua._paths import ENV_APP_ROOT, ENV_USER_DATA_ROOT, Paths
from chahua.config import RoomConfig, RoomConfigError, load_room_config
from chahua.llm_spec import LLMSpec
from chahua.session import _resolve_room_default_spec


REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def paths(tmp_path, monkeypatch):
    """user_data_root 在 tmp 下；app_root 指向真仓库根（ship 自带 personas）——
    与 test_admin.py 同口径，自含一份避免跨文件 fixture 依赖。"""
    user_data = tmp_path / "userdata"
    user_data.mkdir()
    monkeypatch.setenv(ENV_APP_ROOT, str(REPO_ROOT))
    monkeypatch.setenv(ENV_USER_DATA_ROOT, str(user_data))
    return Paths.from_env()


def _clear_llm_env(monkeypatch):
    """剔掉所有可能让 ``try_from_env`` 拿到值的 env，避免 host 真实凭据污染测试。"""
    for k in list(os.environ):
        if (
            k == "LLM_PROVIDER"
            or k == "LLM_TEMPERATURE"
            or k.endswith("_MODEL")
            or k.endswith("_BASE_URL")
        ):
            monkeypatch.delenv(k, raising=False)


def _seed_room(paths) -> RoomConfig:
    return admin.create_room(
        paths=paths, room_id="default-spec", name="默认 spec",
        guests=[{"persona": "chahua/personas/宝总/宝总.md", "name": "宝总"}],
    )


def test_resolve_room_default_toml_only(paths, monkeypatch):
    _clear_llm_env(monkeypatch)
    rc = _seed_room(paths)
    rc2 = admin.update_room_llm(
        paths=paths, room_dir=rc.room_dir, section="room",
        spec_dict={"model": "openai/gpt-5.4", "temperature": 0.7},
    )
    spec = _resolve_room_default_spec(rc2)
    assert spec == LLMSpec(
        provider="openai", model="gpt-5.4", temperature=0.7,
    )


def test_resolve_room_default_env_only(paths, monkeypatch):
    _clear_llm_env(monkeypatch)
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_MODEL", "gpt-5.4-mini")
    rc = _seed_room(paths)
    spec = _resolve_room_default_spec(rc)
    assert spec == LLMSpec(provider="openai", model="gpt-5.4-mini")


def test_resolve_room_default_toml_wins_over_env(paths, monkeypatch):
    """toml 配了 [room.llm] 就完全忽略 env —— shell 里 export 的 OPENAI_MODEL 不该
    "偷胜"覆盖用户在 toml 里写明的 model（同理 temperature 等其它字段）。"""
    _clear_llm_env(monkeypatch)
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_MODEL", "ENV-WINS-IF-WRONG")
    monkeypatch.setenv("LLM_TEMPERATURE", "0.1")
    rc = _seed_room(paths)
    rc2 = admin.update_room_llm(
        paths=paths, room_dir=rc.room_dir, section="room",
        spec_dict={"model": "openai/gpt-5.4", "temperature": 0.9},
    )
    spec = _resolve_room_default_spec(rc2)
    assert spec.model == "gpt-5.4"
    assert spec.temperature == pytest.approx(0.9)


def test_resolve_room_default_both_missing_raises(paths, monkeypatch):
    """都缺 → RoomConfigError，错误信息列两条 fix 路径（[room.llm] 或 env）让用户
    一眼知道修哪里。"""
    _clear_llm_env(monkeypatch)
    rc = _seed_room(paths)
    with pytest.raises(RoomConfigError) as exc_info:
        _resolve_room_default_spec(rc)
    msg = str(exc_info.value)
    assert "[room.llm]" in msg
    assert "LLM_PROVIDER" in msg


# ── [room.llm] 解析 sanity ────────────────────────────────────────────────


def test_room_llm_unknown_field_rejected(tmp_path: Path, paths):
    """[room.llm] 段同走 LLMSpec.from_toml —— 未知字段被拒（与 [scoring] 等口径
    一致），定位标签是 "[room.llm]"。"""
    rc = _seed_room(paths)
    bad = (
        '[room]\nname = "x"\n\n'
        '[room.llm]\nmodel = "openai/gpt-4"\nnovelfield = 1\n\n'
        '[[guest]]\nname = "宝总"\npersona = "chahua/personas/宝总/宝总.md"\n'
        'permission = "read-only"\n'
    )
    (rc.room_dir / "room.toml").write_text(bad, encoding="utf-8")
    with pytest.raises(RoomConfigError, match=r"\[room\.llm\].*未知字段"):
        load_room_config(rc.room_dir, paths=paths)
