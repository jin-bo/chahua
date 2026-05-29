"""persona 更新 / 检查（folder + github）单元测（P12.6 Step 3）。

覆盖：folder check/update / 本地改动检测正交 / force 语义 / 原子替换回滚 /
github check（monkeypatch sha）/ github update。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from chahua import persona_import
from chahua.persona_import import (
    GithubProvenance,
    PersonaImportError,
    PersonaSource,
    _content_hash,
    _version_from_files,
    check_persona_update,
    delete_persona,  # noqa: F401  (确保符号存在)
    read_source,
    update_persona,
)

PERSONAS = "chahua/personas"


# ── 帮手 ──────────────────────────────────────────────────────────────────


def _make_folder(tmp_path: Path, *, name="Yvonne", body="# SOUL v1", version="1.2.0") -> Path:
    src = tmp_path / "src" / name
    src.mkdir(parents=True)
    (src / f"{name}.md").write_text(body, encoding="utf-8")
    if version is not None:
        (src / "persona.toml").write_text(
            f'schema_version = 1\nversion = "{version}"\n', encoding="utf-8"
        )
    return src


def _installed(env_paths, name="Yvonne") -> Path:
    return env_paths.user_data_root / PERSONAS / name


# ── folder 检查 ───────────────────────────────────────────────────────────


def test_folder_check_up_to_date(env_paths, tmp_path) -> None:
    src = _make_folder(tmp_path)
    persona_import.import_from_folder(env_paths, src)
    st = check_persona_update(env_paths, "Yvonne")
    assert st.status == "up_to_date"
    assert st.local_modified is False


def test_folder_check_update_available(env_paths, tmp_path) -> None:
    src = _make_folder(tmp_path)
    persona_import.import_from_folder(env_paths, src)
    # 改源目录某文件。
    (src / "Yvonne.md").write_text("# SOUL v2 改了", encoding="utf-8")
    st = check_persona_update(env_paths, "Yvonne")
    assert st.status == "update_available"
    assert st.local_modified is False  # 改的是源、不是已安装


def test_folder_check_source_gone(env_paths, tmp_path) -> None:
    src = _make_folder(tmp_path)
    persona_import.import_from_folder(env_paths, src)
    import shutil
    shutil.rmtree(src)
    st = check_persona_update(env_paths, "Yvonne")
    assert st.status == "source_unavailable"
    assert "源目录" in st.detail


# ── folder 更新 ───────────────────────────────────────────────────────────


def test_folder_update_replaces_content(env_paths, tmp_path) -> None:
    src = _make_folder(tmp_path)
    persona_import.import_from_folder(env_paths, src)
    (src / "Yvonne.md").write_text("# SOUL v2 新内容", encoding="utf-8")
    result = update_persona(env_paths, "Yvonne")
    assert result.name == "Yvonne"
    # 已安装内容 = 新源内容。
    installed_md = (_installed(env_paths) / "Yvonne.md").read_text(encoding="utf-8")
    assert installed_md == "# SOUL v2 新内容"
    # provenance content_hash 更新到新内容 → 再 check 应 up_to_date。
    assert check_persona_update(env_paths, "Yvonne").status == "up_to_date"


def test_folder_import_stores_absolute_source_path(env_paths, tmp_path, monkeypatch) -> None:
    """相对 src 导入也要存绝对 source_path —— 否则 sidecar 重启 / 切换启动方式后按彼时 cwd
    解析，源被误判缺失或落到另一个相对目录。"""
    src = tmp_path / "Yvonne"
    src.mkdir()
    (src / "Yvonne.md").write_text("# SOUL", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    persona_import.import_from_folder(env_paths, Path("Yvonne"))  # 故意传相对路径
    source = read_source(_installed(env_paths))
    assert Path(source.source_path).is_absolute()
    assert source.source_path == str(src.resolve())


def test_folder_update_provenance_preserves_imported_at(env_paths, tmp_path) -> None:
    src = _make_folder(tmp_path)
    persona_import.import_from_folder(env_paths, src)
    before = read_source(_installed(env_paths))
    (src / "Yvonne.md").write_text("# changed", encoding="utf-8")
    update_persona(env_paths, "Yvonne")
    after = read_source(_installed(env_paths))
    assert after.imported_at == before.imported_at  # 首次导入时间保留
    assert after.content_hash != before.content_hash


# ── 本地改动检测正交 ──────────────────────────────────────────────────────


def test_local_modified_orthogonal_upstream_clean(env_paths, tmp_path) -> None:
    src = _make_folder(tmp_path)
    persona_import.import_from_folder(env_paths, src)
    # 改已安装目录文件（不动源）。
    (_installed(env_paths) / "Yvonne.md").write_text("本地手改", encoding="utf-8")
    st = check_persona_update(env_paths, "Yvonne")
    assert st.local_modified is True
    assert st.status == "up_to_date"  # 上游没变


def test_local_modified_orthogonal_upstream_also_changed(env_paths, tmp_path) -> None:
    src = _make_folder(tmp_path)
    persona_import.import_from_folder(env_paths, src)
    (_installed(env_paths) / "Yvonne.md").write_text("本地手改", encoding="utf-8")
    (src / "Yvonne.md").write_text("# 上游也改了", encoding="utf-8")
    st = check_persona_update(env_paths, "Yvonne")
    assert st.local_modified is True
    assert st.status == "update_available"  # 两维独立


# ── force 语义 ────────────────────────────────────────────────────────────


def test_update_refused_when_local_modified_without_force(env_paths, tmp_path) -> None:
    src = _make_folder(tmp_path)
    persona_import.import_from_folder(env_paths, src)
    (_installed(env_paths) / "Yvonne.md").write_text("本地手改", encoding="utf-8")
    with pytest.raises(PersonaImportError, match="本地已修改"):
        update_persona(env_paths, "Yvonne", force=False)
    # 不动盘 —— 本地改动还在。
    assert (_installed(env_paths) / "Yvonne.md").read_text(encoding="utf-8") == "本地手改"


def test_detect_local_modified_unverifiable_treated_as_modified(
    env_paths, tmp_path, monkeypatch
) -> None:
    """已安装目录采集失败（超大文件 / 文件过多 / 过深 / 空目录）→ 无从判断时按已改动
    处理（返 True），update(force=False) 拒绝覆盖，绝不静默丢用户改动。"""
    src = _make_folder(tmp_path)
    persona_import.import_from_folder(env_paths, src)
    source = read_source(_installed(env_paths))

    def _boom(_dir):
        raise PersonaImportError("无法采集已安装目录（超大文件等）")

    monkeypatch.setattr(persona_import, "_collect_local_files", _boom)
    assert persona_import._detect_local_modified(_installed(env_paths), source) is True


def test_update_force_overwrites_local(env_paths, tmp_path) -> None:
    src = _make_folder(tmp_path)
    persona_import.import_from_folder(env_paths, src)
    (_installed(env_paths) / "Yvonne.md").write_text("本地手改", encoding="utf-8")
    update_persona(env_paths, "Yvonne", force=True)
    # 覆盖回源内容。
    assert (_installed(env_paths) / "Yvonne.md").read_text(encoding="utf-8") == "# SOUL v1"


def test_no_provenance_local_modified_false_and_update_refused(env_paths) -> None:
    # 手动放一个 dir-form persona，无 provenance。
    d = _installed(env_paths, "Manual")
    d.mkdir(parents=True)
    (d / "Manual.md").write_text("# manual", encoding="utf-8")
    st = check_persona_update(env_paths, "Manual")
    assert st.status == "source_unavailable"
    assert st.local_modified is False
    with pytest.raises(PersonaImportError, match="无来源记录"):
        update_persona(env_paths, "Manual")


# ── 原子替换 + 回滚 ──────────────────────────────────────────────────────


def test_update_rollback_on_bad_manifest(env_paths, tmp_path) -> None:
    src = _make_folder(tmp_path)
    persona_import.import_from_folder(env_paths, src)
    before = (_installed(env_paths) / "Yvonne.md").read_text(encoding="utf-8")
    # 源里塞坏 manifest（schema_version=2）+ 改内容。
    (src / "persona.toml").write_text("schema_version = 2\n", encoding="utf-8")
    (src / "Yvonne.md").write_text("# 半成品", encoding="utf-8")
    with pytest.raises(PersonaImportError):
        update_persona(env_paths, "Yvonne")
    # 旧版完好。
    assert (_installed(env_paths) / "Yvonne.md").read_text(encoding="utf-8") == before
    # 无残留 .bak / .update- tmp。
    personas_dir = env_paths.user_data_root / PERSONAS
    residue = [p.name for p in personas_dir.iterdir() if p.name.startswith(".Yvonne.")]
    assert residue == []


# ── GitHub 检查（monkeypatch 网络）────────────────────────────────────────


def _setup_github(env_paths, *, name="GH", commit_sha="oldsha", version="1.0.0") -> Path:
    target = _installed(env_paths, name)
    target.mkdir(parents=True)
    files = [
        (f"{name}.md", b"# gh soul"),
        ("persona.toml", f'schema_version = 1\nversion = "{version}"\n'.encode()),
    ]
    for rel, data in files:
        (target / rel).write_bytes(data)
    persona_import.write_source(
        target,
        PersonaSource(
            name=name, source_type="github",
            source_url="https://github.com/o/r/tree/main/p",
            content_hash=_content_hash(files), version=_version_from_files(files),
            imported_at="t", updated_at="t",
            github=GithubProvenance(
                owner="o", repo="r", ref="main", path="p", commit_sha=commit_sha,
            ),
        ),
    )
    return target


def test_update_rejected_when_source_drops_markdown(env_paths, tmp_path) -> None:
    """上游删 / 改名了 persona ``.md`` → 更新拒绝替换（manifest 仍合法但 discover/list 会
    跳过这种目录 → persona 消失）。旧版完好。"""
    src = _make_folder(tmp_path)
    persona_import.import_from_folder(env_paths, src)
    before = (_installed(env_paths) / "Yvonne.md").read_text(encoding="utf-8")
    # 源里删掉 <Name>.md，只留 manifest（仍合法 manifest）。
    (src / "Yvonne.md").unlink()
    with pytest.raises(PersonaImportError, match="Yvonne.md"):
        update_persona(env_paths, "Yvonne", force=True)
    # 旧版没被换走。
    assert (_installed(env_paths) / "Yvonne.md").read_text(encoding="utf-8") == before
    # 无残留工作目录。
    personas_dir = env_paths.user_data_root / PERSONAS
    assert [p.name for p in personas_dir.iterdir() if p.name.startswith(".Yvonne.")] == []


def test_update_rejected_when_source_renames_markdown(env_paths, tmp_path) -> None:
    """上游把 ``<name>.md`` 改名成别的单份 *.md → 更新拒绝（房间存的是 <name>/<name>.md
    确切路径，改名后重建即失败）。「恰好一份 *.md」兜底只在 import 成立、update 不接受。"""
    src = _make_folder(tmp_path)
    persona_import.import_from_folder(env_paths, src)
    before = (_installed(env_paths) / "Yvonne.md").read_text(encoding="utf-8")
    # 源里把 Yvonne.md 改名成 Soul.md（仍是唯一一份 .md）。
    (src / "Yvonne.md").rename(src / "Soul.md")
    with pytest.raises(PersonaImportError, match="Yvonne.md"):
        update_persona(env_paths, "Yvonne", force=True)
    # 旧版没被换走，<name>.md 仍在。
    assert (_installed(env_paths) / "Yvonne.md").read_text(encoding="utf-8") == before
    assert not (_installed(env_paths) / "Soul.md").exists()


def test_github_check_update_available(env_paths, monkeypatch) -> None:
    _setup_github(env_paths, commit_sha="oldsha")
    monkeypatch.setattr(persona_import, "_gh_latest_commit_sha", lambda *a, **k: "newsha")
    monkeypatch.setattr(
        persona_import, "_gh_fetch_one_file",
        lambda *a, **k: b'schema_version = 1\nversion = "2.0.0"\n',
    )
    st = check_persona_update(env_paths, "GH")
    assert st.status == "update_available"
    assert st.latest_version == "2.0.0"


def test_github_check_up_to_date_no_version_fetch(env_paths, monkeypatch) -> None:
    _setup_github(env_paths, commit_sha="samesha")
    monkeypatch.setattr(persona_import, "_gh_latest_commit_sha", lambda *a, **k: "samesha")

    called = {"fetch": False}

    def _spy(*a, **k):
        called["fetch"] = True
        raise AssertionError("不该在同 sha 时取上游 version")

    monkeypatch.setattr(persona_import, "_gh_fetch_one_file", _spy)
    st = check_persona_update(env_paths, "GH")
    assert st.status == "up_to_date"
    assert called["fetch"] is False  # 同 sha 不多发请求


def test_github_check_rate_limit_is_error(env_paths, monkeypatch) -> None:
    _setup_github(env_paths, commit_sha="oldsha")

    def _boom(*a, **k):
        raise persona_import._GitHubError(
            "GitHub 403 —— 可能撞到匿名 rate limit（60 req/h），稍后再试", code=403
        )

    monkeypatch.setattr(persona_import, "_gh_latest_commit_sha", _boom)
    st = check_persona_update(env_paths, "GH")
    assert st.status == "error"
    assert "rate limit" in st.detail


def test_github_check_404_is_source_unavailable(env_paths, monkeypatch) -> None:
    _setup_github(env_paths, commit_sha="oldsha")

    def _boom(*a, **k):
        raise persona_import._GitHubError("404", code=404)

    monkeypatch.setattr(persona_import, "_gh_latest_commit_sha", _boom)
    st = check_persona_update(env_paths, "GH")
    assert st.status == "source_unavailable"


# ── GitHub 更新（monkeypatch）─────────────────────────────────────────────


def test_github_update_replaces_and_bumps_provenance(env_paths, monkeypatch) -> None:
    _setup_github(env_paths, commit_sha="oldsha", version="1.0.0")
    new_files = [
        ("GH.md", b"# gh soul v2"),
        ("persona.toml", b'schema_version = 1\nversion = "2.0.0"\n'),
    ]
    monkeypatch.setattr(persona_import, "_collect_from_source", lambda src: new_files)
    monkeypatch.setattr(persona_import, "_gh_latest_commit_sha", lambda *a, **k: "newsha")
    result = update_persona(env_paths, "GH")
    assert result.version == "2.0.0"
    installed = (_installed(env_paths, "GH") / "GH.md").read_text(encoding="utf-8")
    assert installed == "# gh soul v2"
    src = read_source(_installed(env_paths, "GH"))
    assert src.github.commit_sha == "newsha"
    assert src.version == "2.0.0"


def test_github_update_keeps_old_sha_on_transient_refresh_failure(
    env_paths, monkeypatch
) -> None:
    """更新成功装新内容，但刷新 commit sha 撞临时失败（None）→ provenance 保留旧基线，
    绝不退化成 None（否则下次 check 看 commit_sha=None 永久误判 source_unavailable）。"""
    _setup_github(env_paths, commit_sha="oldsha", version="1.0.0")
    new_files = [
        ("GH.md", b"# gh soul v2"),
        ("persona.toml", b'schema_version = 1\nversion = "2.0.0"\n'),
    ]
    monkeypatch.setattr(persona_import, "_collect_from_source", lambda src: new_files)
    monkeypatch.setattr(persona_import, "_safe_latest_commit_sha", lambda *a, **k: None)
    result = update_persona(env_paths, "GH")
    assert result.version == "2.0.0"
    src = read_source(_installed(env_paths, "GH"))
    assert src.github.commit_sha == "oldsha"  # 保留旧基线，不退化 None
    # 内容仍更新到新版。
    assert (_installed(env_paths, "GH") / "GH.md").read_text(encoding="utf-8") == "# gh soul v2"
    # 再 check 不应是 source_unavailable（基线仍在）。
    monkeypatch.setattr(persona_import, "_gh_latest_commit_sha", lambda *a, **k: "oldsha")
    assert check_persona_update(env_paths, "GH").status == "up_to_date"
