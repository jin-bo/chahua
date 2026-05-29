"""persona version 纯展示（**不参与判定**）单元测（P12.6 Step 3）。

承重不变量：「变没变」只由 commit sha / content_hash 定；version 仅渲到 UI。
降级（上游版本号更低）也照样 update_available；上游 version 取数失败不波及 status。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from chahua import persona_import
from chahua.persona_import import (
    GithubProvenance,
    PersonaSource,
    _content_hash,
    _version_from_files,
    check_persona_update,
    read_source,
)

PERSONAS = "chahua/personas"


def _make_folder(tmp_path: Path, *, version="1.2.0", body="# SOUL v1") -> Path:
    src = tmp_path / "src" / "Yvonne"
    src.mkdir(parents=True)
    (src / "Yvonne.md").write_text(body, encoding="utf-8")
    if version is not None:
        (src / "persona.toml").write_text(
            f'schema_version = 1\nversion = "{version}"\n', encoding="utf-8"
        )
    return src


def _installed(env_paths) -> Path:
    return env_paths.user_data_root / PERSONAS / "Yvonne"


def _bump_source(src: Path, *, version, body) -> None:
    (src / "Yvonne.md").write_text(body, encoding="utf-8")
    if version is None:
        (src / "persona.toml").unlink(missing_ok=True)
    else:
        (src / "persona.toml").write_text(
            f'schema_version = 1\nversion = "{version}"\n', encoding="utf-8"
        )


# ── version 落档 + 回填 ───────────────────────────────────────────────────


def test_version_recorded_on_import(env_paths, tmp_path) -> None:
    persona_import.import_from_folder(env_paths, _make_folder(tmp_path, version="1.2.0"))
    assert read_source(_installed(env_paths)).version == "1.2.0"


def test_check_fills_latest_version(env_paths, tmp_path) -> None:
    src = _make_folder(tmp_path, version="1.2.0")
    persona_import.import_from_folder(env_paths, src)
    _bump_source(src, version="1.3.0", body="# SOUL v2")
    st = check_persona_update(env_paths, "Yvonne")
    assert st.status == "update_available"
    assert st.installed_version == "1.2.0"
    assert st.latest_version == "1.3.0"


# ── 降级 / 未 bump 仍提示（version 不改写 status）─────────────────────────


def test_downgrade_still_update_available(env_paths, tmp_path) -> None:
    src = _make_folder(tmp_path, version="1.2.0")
    persona_import.import_from_folder(env_paths, src)
    # 上游版本号更低 + 内容变。
    _bump_source(src, version="1.1.0", body="# SOUL 降级版")
    st = check_persona_update(env_paths, "Yvonne")
    assert st.status == "update_available"  # 不是 up_to_date
    assert st.latest_version == "1.1.0"


def test_same_version_but_content_changed_still_update_available(env_paths, tmp_path) -> None:
    src = _make_folder(tmp_path, version="1.2.0")
    persona_import.import_from_folder(env_paths, src)
    # version 没 bump，只改内容。
    _bump_source(src, version="1.2.0", body="# 改了正文但忘 bump 版本")
    st = check_persona_update(env_paths, "Yvonne")
    assert st.status == "update_available"
    assert st.latest_version == "1.2.0"


# ── 无 version 退化 ───────────────────────────────────────────────────────


def test_no_version_degrades_to_null(env_paths, tmp_path) -> None:
    src = _make_folder(tmp_path, version=None)
    persona_import.import_from_folder(env_paths, src)
    assert read_source(_installed(env_paths)).version is None
    _bump_source(src, version=None, body="# 改了内容")
    st = check_persona_update(env_paths, "Yvonne")
    assert st.installed_version is None
    assert st.latest_version is None
    assert st.status == "update_available"  # 仍由内容信号定


# ── 上游 version 取数失败不波及 status（github）────────────────────────────


def test_github_latest_version_fetch_failure_keeps_status(env_paths, monkeypatch) -> None:
    target = env_paths.user_data_root / PERSONAS / "GH"
    target.mkdir(parents=True)
    files = [("GH.md", b"# gh"), ("persona.toml", b'schema_version = 1\nversion = "1.0.0"\n')]
    for rel, data in files:
        (target / rel).write_bytes(data)
    persona_import.write_source(
        target,
        PersonaSource(
            name="GH", source_type="github", source_url="u",
            content_hash=_content_hash(files), version=_version_from_files(files),
            imported_at="t", updated_at="t",
            github=GithubProvenance(owner="o", repo="r", ref="main", path="p", commit_sha="old"),
        ),
    )
    monkeypatch.setattr(persona_import, "_gh_latest_commit_sha", lambda *a, **k: "new")

    def _boom(*a, **k):
        raise persona_import._GitHubError("404", code=404)

    monkeypatch.setattr(persona_import, "_gh_fetch_one_file", _boom)
    st = check_persona_update(env_paths, "GH")
    assert st.status == "update_available"  # 绝不退 error / source_unavailable
    assert st.latest_version is None
    assert "上游版本信息不可用" in st.detail


# ── 断言无比较逻辑 ────────────────────────────────────────────────────────


def test_no_cmp_version_in_modules() -> None:
    import chahua.persona_import as pi
    import chahua.persona_manifest as pm

    assert not hasattr(pi, "_cmp_version")
    assert not hasattr(pm, "_cmp_version")
