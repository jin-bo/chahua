"""``admin.list_installed_personas`` 单元测（P12.6 Step 4）。

列 user_data 下全部 dir-form（含无 provenance），内置 / flat-form 不进列表。
"""

from __future__ import annotations

import os

from chahua import admin, persona_import

PERSONAS = "chahua/personas"


def _import_folder(env_paths, tmp_path, name="Yvonne", summary="伊冯顾问") -> None:
    src = tmp_path / "src" / name
    src.mkdir(parents=True)
    (src / f"{name}.md").write_text("# SOUL", encoding="utf-8")
    (src / "persona.toml").write_text(
        f'schema_version = 1\nversion = "1.0.0"\nsummary = "{summary}"\n',
        encoding="utf-8",
    )
    persona_import.import_from_folder(env_paths, src)


def _make_dirform_no_provenance(env_paths, name="Manual") -> None:
    d = env_paths.user_data_root / PERSONAS / name
    d.mkdir(parents=True)
    (d / f"{name}.md").write_text("# manual soul", encoding="utf-8")


def _make_flatform(env_paths, name="Flat") -> None:
    d = env_paths.user_data_root / PERSONAS
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.md").write_text("# flat soul", encoding="utf-8")


def _by_name(rows):
    return {r["name"]: r for r in rows}


def test_empty_when_no_user_personas(env_paths) -> None:
    assert admin.list_installed_personas(env_paths) == []


def test_lists_all_dirform_with_and_without_provenance(env_paths, tmp_path) -> None:
    _import_folder(env_paths, tmp_path, "Yvonne")
    _make_dirform_no_provenance(env_paths, "Manual")
    _make_flatform(env_paths, "Flat")

    rows = _by_name(admin.list_installed_personas(env_paths))

    # 两个 dir-form 都在。
    assert set(rows) == {"Yvonne", "Manual"}
    # flat-form 不进列表。
    assert "Flat" not in rows

    # 有 provenance 行。
    yv = rows["Yvonne"]
    assert yv["source_type"] == "folder"
    assert yv["status"] == persona_import.STATUS_UNKNOWN
    assert yv["installed_version"] == "1.0.0"
    assert yv["local_modified"] is False
    assert yv["latest_version"] is None
    assert yv["summary"] == "伊冯顾问"  # 与 manifest 同口径
    assert yv["persona"] == "chahua/personas/Yvonne/Yvonne.md"

    # 无 provenance 行。
    mn = rows["Manual"]
    assert mn["source_type"] == "unknown"
    assert mn["status"] == persona_import.STATUS_SOURCE_UNAVAILABLE
    assert mn["installed_version"] is None
    assert "来源未知" in mn["detail"]


def test_builtin_appform_not_listed(env_paths) -> None:
    # app_root（仓库）有内置 dir-form 宝总；list 只扫 user_data → 不出现。
    rows = _by_name(admin.list_installed_personas(env_paths))
    assert "宝总" not in rows


def test_skips_dot_dirs(env_paths, tmp_path) -> None:
    _import_folder(env_paths, tmp_path, "Yvonne")
    # 模拟原子替换瞬态残骸。
    residue = env_paths.user_data_root / PERSONAS / ".Yvonne.update-123-abc"
    residue.mkdir(parents=True)
    (residue / "Yvonne.md").write_text("# tmp", encoding="utf-8")

    rows = _by_name(admin.list_installed_personas(env_paths))
    assert set(rows) == {"Yvonne"}
    assert ".Yvonne.update-123-abc" not in rows


def test_recovers_interrupted_update_orphan_bak(env_paths, tmp_path) -> None:
    """崩溃恢复：原子 swap 在 rename(target→bak) 后被 kill，旧版只剩 .<name>.bak-<pid>-<tok>/
    —— list 前把它还原回 <name>/，persona 不再「消失」。"""
    _import_folder(env_paths, tmp_path, "Yvonne")
    base = env_paths.user_data_root / PERSONAS
    target = base / "Yvonne"
    bak = base / f".Yvonne.bak-{os.getpid() + 1}-abcdef"  # 异进程 pid + 合法 6-hex token
    target.rename(bak)  # 模拟中断后的盘面状态
    assert not target.exists()

    rows = _by_name(admin.list_installed_personas(env_paths))
    assert "Yvonne" in rows
    assert target.is_dir()
    assert (target / "Yvonne.md").read_text(encoding="utf-8") == "# SOUL"
    assert not bak.exists()  # bak 残骸已清


def test_does_not_recover_current_pid_workdir(env_paths, tmp_path) -> None:
    """当前进程 pid 的 .bak- 可能是本进程在途的并发更新 —— 恢复绝不抢它（race 安全）。"""
    _import_folder(env_paths, tmp_path, "Yvonne")
    base = env_paths.user_data_root / PERSONAS
    target = base / "Yvonne"
    bak = base / f".Yvonne.bak-{os.getpid()}-abcdef"
    target.rename(bak)

    rows = _by_name(admin.list_installed_personas(env_paths))
    assert "Yvonne" not in rows  # 未恢复
    assert bak.is_dir()          # bak 原样保留
    assert not target.exists()


def test_discover_personas_recovers_interrupted_update(env_paths, tmp_path) -> None:
    """consumption 路径（picker discovery / session 重建经同一 public wrapper）也跑崩溃恢复
    —— 不必等用户打开「已安装」页，引用该 persona 的房间重建即可正常解析。"""
    _import_folder(env_paths, tmp_path, "Yvonne")
    base = env_paths.user_data_root / PERSONAS
    target = base / "Yvonne"
    bak = base / f".Yvonne.bak-{os.getpid() + 1}-abcdef"
    target.rename(bak)
    assert not target.exists()

    names = {row["name"] for row in admin.discover_personas(env_paths)}
    assert "Yvonne" in names      # 恢复后 picker 看得到
    assert target.is_dir()
    assert not bak.exists()


def test_skips_symlink_dirs(env_paths, tmp_path) -> None:
    """symlink 目录别名不进列表 —— is_dir() 会跟进链接，须显式 is_symlink 跳过。"""
    _import_folder(env_paths, tmp_path, "Yvonne")
    base = env_paths.user_data_root / PERSONAS
    (base / "Alias").symlink_to(base / "Yvonne", target_is_directory=True)

    rows = _by_name(admin.list_installed_personas(env_paths))
    assert "Yvonne" in rows
    assert "Alias" not in rows
