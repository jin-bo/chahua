"""``admin.create_room`` inflate manifest defaults + 事务性（P12 C3）。

覆盖：
- 多 guest 各持 manifest → 各自字段独立 inflate
- 入参 guest dict 缺 permission key → 走 manifest defaults
- 入参 guest dict permission=None → 同上
- 入参 guest dict 显式 permission → 覆盖 manifest
- 坏 manifest → create_room 抛 + 房间目录不被创建（事务性）
- 多 guest 第二位 manifest 坏 → 第一位的 inflate 也不留半成品（mkdir 之前先全解析）
"""

from __future__ import annotations

from pathlib import Path

import pytest

from chahua import admin
from chahua._paths import ENV_APP_ROOT, ENV_USER_DATA_ROOT, Paths
from chahua.config import RoomConfigError
from chahua.permissions import DEFAULT_MODE
from chahua.persona_manifest import PersonaManifestError


REPO_ROOT = Path(__file__).resolve().parent.parent
PERSONAS_DIR_REL = Path("chahua/personas")


@pytest.fixture
def paths(tmp_path, monkeypatch):
    user_data = tmp_path / "userdata"
    user_data.mkdir()
    monkeypatch.setenv(ENV_APP_ROOT, str(REPO_ROOT))
    monkeypatch.setenv(ENV_USER_DATA_ROOT, str(user_data))
    return Paths.from_env()


def _make_persona(paths: Paths, name: str, manifest_body: str = "") -> str:
    persona_dir = paths.user_data_root / PERSONAS_DIR_REL / name
    persona_dir.mkdir(parents=True)
    (persona_dir / f"{name}.md").write_text(f"I am {name}", encoding="utf-8")
    if manifest_body:
        (persona_dir / "persona.toml").write_text(manifest_body, encoding="utf-8")
    return f"chahua/personas/{name}/{name}.md"


def _find_guest(rc, gname: str):
    for g in rc.guests:
        if g.name == gname:
            return g
    raise AssertionError(f"guest {gname!r} 不在房间")


# ── 多 guest 独立 inflate ───────────────────────────────────────────


def test_multiple_guests_each_inflate_independently(paths):
    p1 = _make_persona(
        paths, "Yvonne",
        manifest_body='schema_version = 1\n[defaults.guest]\npermission = "workspace-write"\n',
    )
    p2 = _make_persona(
        paths, "Maya",
        manifest_body='schema_version = 1\nsummary = "设计师"\n',
    )
    rc = admin.create_room(
        paths=paths, room_id="r1", name="r1",
        guests=[
            {"persona": p1, "name": "Yvonne"},
            {"persona": p2, "name": "Maya"},
        ],
    )
    assert _find_guest(rc, "Yvonne").permission == "workspace-write"
    assert _find_guest(rc, "Yvonne").summary is None  # manifest 没 summary
    assert _find_guest(rc, "Maya").permission == DEFAULT_MODE
    assert _find_guest(rc, "Maya").summary == "设计师"


def test_missing_permission_key_uses_manifest(paths):
    """前端不送 permission 字段（C3 modals.js 改动）→ guest dict 无 permission key。"""
    persona_rel = _make_persona(
        paths, "Yvonne",
        manifest_body='schema_version = 1\n[defaults.guest]\npermission = "workspace-write"\n',
    )
    rc = admin.create_room(
        paths=paths, room_id="r1", name="r1",
        guests=[{"persona": persona_rel, "name": "Yvonne"}],  # 无 permission key
    )
    assert _find_guest(rc, "Yvonne").permission == "workspace-write"


def test_explicit_none_permission_uses_manifest(paths):
    persona_rel = _make_persona(
        paths, "Yvonne",
        manifest_body='schema_version = 1\n[defaults.guest]\npermission = "workspace-write"\n',
    )
    rc = admin.create_room(
        paths=paths, room_id="r1", name="r1",
        guests=[{"persona": persona_rel, "name": "Yvonne", "permission": None}],
    )
    assert _find_guest(rc, "Yvonne").permission == "workspace-write"


def test_explicit_permission_overrides_manifest(paths):
    persona_rel = _make_persona(
        paths, "Yvonne",
        manifest_body='schema_version = 1\n[defaults.guest]\npermission = "workspace-write"\n',
    )
    rc = admin.create_room(
        paths=paths, room_id="r1", name="r1",
        guests=[
            {"persona": persona_rel, "name": "Yvonne", "permission": "read-only"},
        ],
    )
    assert _find_guest(rc, "Yvonne").permission == "read-only"


# ── 事务性 ──────────────────────────────────────────────────────────


def test_bad_manifest_aborts_create_no_room_dir(paths):
    persona_rel = _make_persona(
        paths, "Yvonne", manifest_body="schema_version = 2\n"  # 错
    )
    with pytest.raises(PersonaManifestError):
        admin.create_room(
            paths=paths, room_id="bad", name="bad",
            guests=[{"persona": persona_rel, "name": "Yvonne"}],
        )
    # 关键断言：房间目录不应被创建。
    assert not (paths.user_data_root / "rooms" / "bad").exists()


def test_second_guest_bad_manifest_no_partial_room(paths):
    """事务性：第二位 manifest 坏 → 第一位也不进 toml，房间整盘回滚。"""
    p1 = _make_persona(
        paths, "Yvonne",
        manifest_body='schema_version = 1\nsummary = "ok"\n',
    )
    p2 = _make_persona(paths, "Maya", manifest_body="schema_version = 2\n")
    with pytest.raises(PersonaManifestError):
        admin.create_room(
            paths=paths, room_id="bad2", name="bad2",
            guests=[
                {"persona": p1, "name": "Yvonne"},
                {"persona": p2, "name": "Maya"},
            ],
        )
    assert not (paths.user_data_root / "rooms" / "bad2").exists()


def test_persona_md_not_found_does_not_query_manifest(paths):
    """persona md 路径不存在 → 不读 manifest（md_abs is None 分支），交给 load_room_config
    报 RoomConfigError。

    收窄 pytest.raises 类型 —— 防止未来重构 `if md_abs is not None` 守卫被误删时
    AttributeError(None.parent) 让本测试静默放过（reviewer #1）。
    """
    # 不预先 _make_persona —— persona 路径根本不存在
    with pytest.raises(RoomConfigError):
        admin.create_room(
            paths=paths, room_id="ghost", name="ghost",
            guests=[{"persona": "chahua/personas/Ghost/Ghost.md"}],
        )
    # 半成品房间应被 rmtree 回滚
    assert not (paths.user_data_root / "rooms" / "ghost").exists()


# ── flat-form built-in 零回归 ───────────────────────────────────────


def test_flat_form_builtin_no_manifest_default_permission(paths):
    rc = admin.create_room(
        paths=paths, room_id="r1", name="r1",
        guests=[
            {"persona": "chahua/personas/宝总.md", "name": "宝总"},
            {"persona": "chahua/personas/汪小姐.md", "name": "汪小姐"},
        ],
    )
    assert _find_guest(rc, "宝总").permission == DEFAULT_MODE
    assert _find_guest(rc, "汪小姐").permission == DEFAULT_MODE
