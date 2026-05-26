"""``admin.add_guest`` inflate manifest defaults（P12 C3）。

覆盖：
- manifest 持 permission → inflate 进 [[guest]].permission；入参 None
- 入参显式 permission 覆盖 manifest
- 无 manifest 入参 None → DEFAULT_MODE 回归
- manifest 持 isolation → inflate
- manifest 持 summary → inflate（落进 resolve_guest_summary 「手写」档）
- 坏 manifest → add_guest 抛 PersonaManifestError、不静默 fallback
- 入参 permission="" 不被 or 吞掉 —— 现有 is_valid_mode 校验抛
- persona md 走双根 resolve（user_data 优先于 app_root）
"""

from __future__ import annotations

from pathlib import Path

import pytest

from chahua import admin
from chahua._paths import ENV_APP_ROOT, ENV_USER_DATA_ROOT, Paths
from chahua.config import RoomConfig, load_room_config
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


def _make_yvonne(paths: Paths, *, manifest_body: str = "") -> str:
    """造一个 user_data 下的 dir-form persona Yvonne，返回 persona rel-path。"""
    persona_dir = paths.user_data_root / PERSONAS_DIR_REL / "Yvonne"
    persona_dir.mkdir(parents=True)
    (persona_dir / "Yvonne.md").write_text("I am Yvonne", encoding="utf-8")
    if manifest_body:
        (persona_dir / "persona.toml").write_text(manifest_body, encoding="utf-8")
    return "chahua/personas/Yvonne/Yvonne.md"


def _seed_room_with_one_guest(paths: Paths) -> RoomConfig:
    """种一个已有一位茶客（宝总）的房间，回出 RoomConfig。"""
    return admin.create_room(
        paths=paths, room_id="r1", name="r1",
        guests=[{"persona": "chahua/personas/宝总.md", "name": "宝总"}],
    )


def _find_guest(rc: RoomConfig, name: str):
    for g in rc.guests:
        if g.name == name:
            return g
    raise AssertionError(f"guest {name!r} 不在房间")


# ── manifest permission inflate ─────────────────────────────────────────


def test_manifest_permission_inflate_when_no_explicit(paths):
    persona_rel = _make_yvonne(
        paths,
        manifest_body='schema_version = 1\n[defaults.guest]\npermission = "workspace-write"\n',
    )
    rc = _seed_room_with_one_guest(paths)
    rc2 = admin.add_guest(
        paths=paths, room_dir=rc.room_dir, persona=persona_rel, name="Yvonne",
        permission=None,
    )
    assert _find_guest(rc2, "Yvonne").permission == "workspace-write"


def test_explicit_permission_overrides_manifest(paths):
    persona_rel = _make_yvonne(
        paths,
        manifest_body='schema_version = 1\n[defaults.guest]\npermission = "workspace-write"\n',
    )
    rc = _seed_room_with_one_guest(paths)
    rc2 = admin.add_guest(
        paths=paths, room_dir=rc.room_dir, persona=persona_rel, name="Yvonne",
        permission="read-only",
    )
    assert _find_guest(rc2, "Yvonne").permission == "read-only"


def test_no_manifest_falls_back_to_default_mode(paths):
    persona_rel = _make_yvonne(paths)  # 无 manifest
    rc = _seed_room_with_one_guest(paths)
    rc2 = admin.add_guest(
        paths=paths, room_dir=rc.room_dir, persona=persona_rel, name="Yvonne",
        permission=None,
    )
    assert _find_guest(rc2, "Yvonne").permission == DEFAULT_MODE


def test_default_mode_when_no_manifest_or_explicit(paths):
    """flat-form 内置 persona —— 无 manifest 入口，等同 P12 之前行为。"""
    rc = _seed_room_with_one_guest(paths)
    rc2 = admin.add_guest(
        paths=paths, room_dir=rc.room_dir,
        persona="chahua/personas/汪小姐.md", name="汪小姐",
        permission=None,
    )
    assert _find_guest(rc2, "汪小姐").permission == DEFAULT_MODE


# ── manifest isolation / summary inflate ─────────────────────────────


def test_manifest_isolation_inflate(paths):
    persona_rel = _make_yvonne(
        paths,
        manifest_body='schema_version = 1\n[defaults.guest]\nisolation = "global"\n',
    )
    rc = _seed_room_with_one_guest(paths)
    rc2 = admin.add_guest(
        paths=paths, room_dir=rc.room_dir, persona=persona_rel, name="Yvonne",
    )
    assert _find_guest(rc2, "Yvonne").isolation == "global"


def test_manifest_summary_inflate(paths):
    persona_rel = _make_yvonne(
        paths,
        manifest_body='schema_version = 1\nsummary = "市场调研顾问"\n',
    )
    rc = _seed_room_with_one_guest(paths)
    rc2 = admin.add_guest(
        paths=paths, room_dir=rc.room_dir, persona=persona_rel, name="Yvonne",
    )
    assert _find_guest(rc2, "Yvonne").summary == "市场调研顾问"


# ── 坏 manifest fail-fast ───────────────────────────────────────────


def test_bad_manifest_schema_version_raises(paths):
    persona_rel = _make_yvonne(paths, manifest_body="schema_version = 2\n")
    rc = _seed_room_with_one_guest(paths)
    with pytest.raises(PersonaManifestError, match="schema_version"):
        admin.add_guest(
            paths=paths, room_dir=rc.room_dir, persona=persona_rel, name="Yvonne",
        )


def test_bad_manifest_unknown_key_raises(paths):
    persona_rel = _make_yvonne(
        paths,
        manifest_body='schema_version = 1\nversion = "0.1.0"\n',
    )
    rc = _seed_room_with_one_guest(paths)
    with pytest.raises(PersonaManifestError, match="version"):
        admin.add_guest(
            paths=paths, room_dir=rc.room_dir, persona=persona_rel, name="Yvonne",
        )


def test_bad_manifest_invalid_permission_raises(paths):
    persona_rel = _make_yvonne(
        paths,
        manifest_body='schema_version = 1\n[defaults.guest]\npermission = "wat"\n',
    )
    rc = _seed_room_with_one_guest(paths)
    with pytest.raises(PersonaManifestError, match="wat"):
        admin.add_guest(
            paths=paths, room_dir=rc.room_dir, persona=persona_rel, name="Yvonne",
        )


# ── 跨 phase 既有行为 breadcrumb ─────────────────────────────────────


def test_empty_permission_swallowed_by_renderer_breadcrumb(paths):
    """**Breadcrumb / known-issue**：plan §"承重不变量"第 6 条「禁止 or-trap」仅约束
    C3 新增代码路径（`_build_guest_with_manifest_defaults` 用 `is not None`）。下游
    renderer `_render_room_toml` (admin_toml.py:257) 与 loader (config.py:595) 都有
    P12 之前的 ``or DEFAULT_MODE`` 兜底 —— 显式 ``permission=""`` 会被静默吞成
    DEFAULT_MODE 写盘。

    本测试**钉住既有行为**：让未来读 inflate 链路的 reviewer 知道 §6 不变量在跨 phase
    范围上不完全成立。修是另一个 phase（要同时改 renderer + loader + 旧 toml 数据集），
    出 P12 C3 范围。
    """
    persona_rel = _make_yvonne(paths)
    rc = _seed_room_with_one_guest(paths)
    rc2 = admin.add_guest(
        paths=paths, room_dir=rc.room_dir, persona=persona_rel, name="Yvonne",
        permission="",
    )
    # 当前行为：renderer 吞 "" → DEFAULT_MODE
    assert _find_guest(rc2, "Yvonne").permission == DEFAULT_MODE
    # 如果这条 assert 哪天变成 raise，说明 renderer 已收紧 —— 把 breadcrumb 改成
    # `pytest.raises(...)` + 更新 plan 不变量。


# ── flat-form 守卫 ────────────────────────────────────────────────


def test_flat_form_does_not_consult_root_persona_toml(paths):
    """flat-form persona 的 md.parent 是 chahua/personas/ 本身 —— 若误置一份
    persona.toml 在根，不应让所有内置 persona 共享同份 [defaults.guest]。
    `_build_guest_with_manifest_defaults` 需有 dir-form 判定守卫。"""
    # 在 user_data 下 chahua/personas/persona.toml 投毒 —— 任何 dir-form 都不该读它
    poison = paths.user_data_root / PERSONAS_DIR_REL / "persona.toml"
    poison.parent.mkdir(parents=True, exist_ok=True)
    poison.write_text(
        'schema_version = 1\n[defaults.guest]\npermission = "workspace-write"\n',
        encoding="utf-8",
    )
    # 加 flat-form 内置 persona —— 不应继承毒丸
    rc = _seed_room_with_one_guest(paths)
    rc2 = admin.add_guest(
        paths=paths, room_dir=rc.room_dir,
        persona="chahua/personas/汪小姐.md", name="汪小姐",
    )
    # 必须是 DEFAULT_MODE 而非 workspace-write
    assert _find_guest(rc2, "汪小姐").permission == DEFAULT_MODE


# ── 双根 resolve ─────────────────────────────────────────────────────


def test_dual_root_resolve_user_data_wins(paths):
    """user_data 与 app_root 同名 dir-form persona：manifest 取 user_data 版本。"""
    # user_data 注入「范总」dir-form + manifest summary="user_data 版本"
    persona_dir = paths.user_data_root / PERSONAS_DIR_REL / "范总"
    persona_dir.mkdir(parents=True)
    (persona_dir / "范总.md").write_text("override", encoding="utf-8")
    (persona_dir / "persona.toml").write_text(
        'schema_version = 1\nsummary = "user_data 版本"\n', encoding="utf-8"
    )
    rc = _seed_room_with_one_guest(paths)
    rc2 = admin.add_guest(
        paths=paths, room_dir=rc.room_dir,
        persona="chahua/personas/范总/范总.md",
        name="范总2",
    )
    assert _find_guest(rc2, "范总2").summary == "user_data 版本"


# ── 持久化到 room.toml ───────────────────────────────────────────────


def test_inflate_persists_to_room_toml(paths):
    """重 load 房间能拿到 inflate 后的字段 —— 验真写盘。"""
    persona_rel = _make_yvonne(
        paths,
        manifest_body="""\
schema_version = 1
summary = "市场调研"

[defaults.guest]
permission = "workspace-write"
isolation = "global"
""",
    )
    rc = _seed_room_with_one_guest(paths)
    admin.add_guest(
        paths=paths, room_dir=rc.room_dir, persona=persona_rel, name="Yvonne",
    )
    rc_reloaded = load_room_config(rc.room_dir, paths=paths)
    g = _find_guest(rc_reloaded, "Yvonne")
    assert g.permission == "workspace-write"
    assert g.isolation == "global"
    assert g.summary == "市场调研"
