"""``persona_import`` 在 ``_write_files`` 之前对 ``persona.toml`` 字节做 dry-run 校验（P12 C4）。

承重契约：plan §"持久化、事件契约"第 7 条「坏 manifest 永远不创建 target_dir」——
否则用户改完源仓库重导会撞 ``mkdir(exist_ok=False)`` 抛 "已存在"。

覆盖：
- 合法 persona.toml → 导入成功，文件落 user_data
- 坏 persona.toml（schema_version 错）→ PersonaImportError；target_dir **不存在**
- 坏 persona.toml + 用户改完重导 → 第二次能成功（不撞 "已存在"）
- 无 persona.toml → 导入成功，行为零变化（manifest 可选）
- 非 UTF-8 persona.toml → PersonaImportError（parse_persona_manifest_bytes 已包装）
"""

from __future__ import annotations

from pathlib import Path

import pytest

from chahua._paths import ENV_APP_ROOT, ENV_USER_DATA_ROOT, Paths
from chahua.persona_import import (
    PersonaImportError,
    import_from_folder,
)


REPO_ROOT = Path(__file__).resolve().parent.parent
TARGET_REL = Path("chahua/personas")


@pytest.fixture
def paths(tmp_path, monkeypatch):
    user_data = tmp_path / "userdata"
    user_data.mkdir()
    monkeypatch.setenv(ENV_APP_ROOT, str(REPO_ROOT))
    monkeypatch.setenv(ENV_USER_DATA_ROOT, str(user_data))
    return Paths.from_env()


def _make_source(tmp_path: Path, name: str, manifest_body: str = "") -> Path:
    """造一个源 persona 目录（用于 import_from_folder）。"""
    src = tmp_path / "src" / name
    src.mkdir(parents=True)
    (src / f"{name}.md").write_text(f"# {name}\n\nI am {name}", encoding="utf-8")
    if manifest_body:
        (src / "persona.toml").write_text(manifest_body, encoding="utf-8")
    return src


def _make_source_bytes(tmp_path: Path, name: str, manifest_bytes: bytes) -> Path:
    """造源目录、persona.toml 是显式字节（用来塞非 UTF-8 等怪输入）。"""
    src = tmp_path / "src" / name
    src.mkdir(parents=True)
    (src / f"{name}.md").write_text(f"# {name}", encoding="utf-8")
    (src / "persona.toml").write_bytes(manifest_bytes)
    return src


# ── 合法 manifest 路径 ──────────────────────────────────────────────


def test_valid_manifest_imports(paths, tmp_path):
    src = _make_source(
        tmp_path, "Yvonne",
        manifest_body='schema_version = 1\nsummary = "测试"\n',
    )
    result = import_from_folder(paths, src)
    assert result.name == "Yvonne"
    target_dir = paths.user_data_root / TARGET_REL / "Yvonne"
    assert (target_dir / "Yvonne.md").is_file()
    assert (target_dir / "persona.toml").is_file()


def test_no_manifest_imports_unchanged_behavior(paths, tmp_path):
    src = _make_source(tmp_path, "Yvonne")  # 无 persona.toml
    result = import_from_folder(paths, src)
    assert result.name == "Yvonne"
    target_dir = paths.user_data_root / TARGET_REL / "Yvonne"
    assert (target_dir / "Yvonne.md").is_file()
    assert not (target_dir / "persona.toml").exists()


# ── 坏 manifest dry-run 拦截 ────────────────────────────────────────


def test_bad_manifest_no_target_dir(paths, tmp_path):
    """坏 manifest → PersonaImportError + target_dir **不存在**。"""
    src = _make_source(tmp_path, "Bad", manifest_body="schema_version = 2\n")
    with pytest.raises(PersonaImportError, match="schema_version"):
        import_from_folder(paths, src)
    target_dir = paths.user_data_root / TARGET_REL / "Bad"
    assert not target_dir.exists(), "坏 manifest 不应留下半成品 target_dir"


def test_bad_manifest_then_fix_can_reimport(paths, tmp_path):
    """关键回归：用户改完源仓库后第二次能成功导入 —— 不撞 _write_files 的
    `mkdir(exist_ok=False)` "已存在"。"""
    src = _make_source(tmp_path, "FixMe", manifest_body="schema_version = 2\n")
    with pytest.raises(PersonaImportError):
        import_from_folder(paths, src)
    # 用户改完 manifest 重导
    (src / "persona.toml").write_text("schema_version = 1\n", encoding="utf-8")
    result = import_from_folder(paths, src)
    assert result.name == "FixMe"
    assert (paths.user_data_root / TARGET_REL / "FixMe" / "persona.toml").is_file()


def test_unknown_top_key_rejected(paths, tmp_path):
    src = _make_source(
        tmp_path, "WithFuture",
        manifest_body='schema_version = 1\nversion = "0.1.0"\n',  # version 是 P12.4
    )
    with pytest.raises(PersonaImportError, match="version"):
        import_from_folder(paths, src)
    assert not (paths.user_data_root / TARGET_REL / "WithFuture").exists()


def test_invalid_permission_rejected(paths, tmp_path):
    src = _make_source(
        tmp_path, "BadPerm",
        manifest_body='schema_version = 1\n[defaults.guest]\npermission = "wat"\n',
    )
    with pytest.raises(PersonaImportError, match="wat"):
        import_from_folder(paths, src)
    assert not (paths.user_data_root / TARGET_REL / "BadPerm").exists()


# ── 非 UTF-8 ───────────────────────────────────────────────────────


def test_non_utf8_manifest_rejected(paths, tmp_path):
    """parse_persona_manifest_bytes 把 UnicodeDecodeError 包成 PersonaManifestError ——
    persona_import 单 catch 一类 → 转 PersonaImportError。"""
    src = _make_source_bytes(tmp_path, "BadBytes", b"\xff\xfe nope")
    with pytest.raises(PersonaImportError, match="UTF-8"):
        import_from_folder(paths, src)
    assert not (paths.user_data_root / TARGET_REL / "BadBytes").exists()


# ── 坏 toml 语法 ───────────────────────────────────────────────────


def test_bad_toml_syntax_rejected(paths, tmp_path):
    src = _make_source(
        tmp_path, "BadSyntax",
        manifest_body="schema_version = 1\n[unclosed\n",
    )
    with pytest.raises(PersonaImportError, match="TOML"):
        import_from_folder(paths, src)
    assert not (paths.user_data_root / TARGET_REL / "BadSyntax").exists()


# ── 大小写 / 嵌套位置守卫 ─────────────────────────────────────────


def test_miscased_manifest_filename_rejected(paths, tmp_path):
    """根级 Persona.toml / PERSONA.toml 等错名 → 拒。

    macOS APFS / Windows NTFS case-insensitive FS 上 load_persona_manifest 会找到错名
    文件、Linux ext4 case-sensitive 上找不到 —— 跨平台行为漂移，import 阶段严格拒
    最稳（reviewer #1）。
    """
    src = tmp_path / "src" / "BadCase"
    src.mkdir(parents=True)
    (src / "BadCase.md").write_text("# BadCase", encoding="utf-8")
    (src / "Persona.toml").write_text("schema_version = 1\n", encoding="utf-8")
    with pytest.raises(PersonaImportError, match="大小写"):
        import_from_folder(paths, src)
    assert not (paths.user_data_root / TARGET_REL / "BadCase").exists()


def test_nested_subdir_persona_toml_ignored(paths, tmp_path):
    """嵌套 skills/foo/persona.toml 不被识别为 manifest（runtime 只读根级）。

    本测试钉住「manifest 仅根级」契约 —— reviewer #4。
    """
    src = tmp_path / "src" / "NestedTomly"
    src.mkdir(parents=True)
    (src / "NestedTomly.md").write_text("# x", encoding="utf-8")
    nested_dir = src / "skills" / "foo"
    nested_dir.mkdir(parents=True)
    # 嵌套位置塞一个**坏** manifest —— 不应被发现 / 拒绝（spec：仅根级）
    (nested_dir / "persona.toml").write_text("schema_version = 99\n", encoding="utf-8")
    result = import_from_folder(paths, src)
    assert result.name == "NestedTomly"
    # 嵌套副本 dead weight 但确实被拷下来（与 spec 一致）
    target = paths.user_data_root / TARGET_REL / "NestedTomly"
    assert (target / "skills" / "foo" / "persona.toml").is_file()


# ── 既有 examples/personas/Yvonne 真包能装 ─────────────────────────


def test_examples_yvonne_imports_with_manifest(paths):
    """ship 在仓库的 examples/personas/Yvonne 持合法 persona.toml，应能直接装。"""
    src = REPO_ROOT / "examples" / "personas" / "Yvonne"
    assert (src / "persona.toml").is_file(), "C4 应已加 examples/personas/Yvonne/persona.toml"
    result = import_from_folder(paths, src)
    assert result.name == "Yvonne"
    assert (
        paths.user_data_root / TARGET_REL / "Yvonne" / "persona.toml"
    ).is_file()
