"""persona provenance（``.chahua-source.json``）单元测（P12.6 Step 2）。

覆盖：本地导入写 provenance / provenance 不被采集 / read_source 容错 /
content_hash 稳定性 / github 导入写 provenance（monkeypatch 网络）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from chahua import persona_import
from chahua.persona_import import (
    SOURCE_FILENAME,
    GithubProvenance,
    PersonaSource,
    _content_hash,
    read_source,
)


# ── 本地导入写 provenance ─────────────────────────────────────────────────


def _make_folder(tmp_path: Path, *, with_version: bool = True, extra: dict | None = None) -> Path:
    src = tmp_path / "src" / "Yvonne"
    src.mkdir(parents=True)
    (src / "Yvonne.md").write_text("# SOUL\n你是伊冯。", encoding="utf-8")
    if with_version:
        (src / "persona.toml").write_text(
            'schema_version = 1\nversion = "1.2.0"\n', encoding="utf-8"
        )
    for name, body in (extra or {}).items():
        (src / name).write_text(body, encoding="utf-8")
    return src


def test_folder_import_writes_provenance(env_paths, tmp_path) -> None:
    src = _make_folder(tmp_path)
    persona_import.import_from_folder(env_paths, src)

    target = env_paths.user_data_root / "chahua/personas/Yvonne"
    sidecar = target / SOURCE_FILENAME
    assert sidecar.is_file()
    data = json.loads(sidecar.read_text(encoding="utf-8"))
    assert data["schema_version"] == 1
    assert data["name"] == "Yvonne"
    assert data["source_type"] == "folder"
    assert data["source_path"] == str(src)
    assert data["content_hash"].startswith("sha256:")
    assert data["version"] == "1.2.0"
    assert data["imported_at"] and data["updated_at"]
    # folder 来源没有 github 子结构。
    assert "github" not in data


def test_folder_import_version_null_when_no_manifest(env_paths, tmp_path) -> None:
    src = _make_folder(tmp_path, with_version=False)
    persona_import.import_from_folder(env_paths, src)
    src_obj = read_source(env_paths.user_data_root / "chahua/personas/Yvonne")
    assert src_obj is not None
    assert src_obj.version is None


# ── provenance 不被当 persona 内容采集 ────────────────────────────────────


def test_provenance_not_collected(env_paths, tmp_path) -> None:
    # 源目录里放一份 .chahua-source.json（模拟从已纳管的 persona 复制出来再导入）。
    src = _make_folder(
        tmp_path, extra={SOURCE_FILENAME: '{"schema_version": 1, "junk": true}'}
    )
    result = persona_import.import_from_folder(env_paths, src)

    # 不出现在 extras。
    assert SOURCE_FILENAME not in result.extras
    # 目标目录里的 .chahua-source.json 是 importer 新写的，不是源里那份 junk。
    target = env_paths.user_data_root / "chahua/personas/Yvonne"
    data = json.loads((target / SOURCE_FILENAME).read_text(encoding="utf-8"))
    assert data["source_type"] == "folder"
    assert "junk" not in data


def test_provenance_does_not_affect_content_hash(env_paths, tmp_path) -> None:
    # 两个内容相同的源，一个含 .chahua-source.json 一个不含 —— content_hash 应相等。
    src_a = _make_folder(tmp_path / "a")
    src_b = _make_folder(
        tmp_path / "b", extra={SOURCE_FILENAME: '{"schema_version": 1}'}
    )
    files_a = persona_import._collect_local_files(src_a)
    files_b = persona_import._collect_local_files(src_b)
    assert _content_hash(files_a) == _content_hash(files_b)


# ── read_source 容错 ──────────────────────────────────────────────────────


def test_read_source_missing_file(tmp_path) -> None:
    d = tmp_path / "p"
    d.mkdir()
    assert read_source(d) is None


def test_read_source_bad_json(tmp_path, caplog) -> None:
    d = tmp_path / "p"
    d.mkdir()
    (d / SOURCE_FILENAME).write_text("{not json", encoding="utf-8")
    assert read_source(d) is None


def test_read_source_wrong_schema_version(tmp_path) -> None:
    d = tmp_path / "p"
    d.mkdir()
    (d / SOURCE_FILENAME).write_text(
        json.dumps({"schema_version": 2, "name": "X", "source_type": "folder"}),
        encoding="utf-8",
    )
    assert read_source(d) is None


def test_read_source_missing_required_field(tmp_path) -> None:
    d = tmp_path / "p"
    d.mkdir()
    # 缺 content_hash → 降级 None。
    (d / SOURCE_FILENAME).write_text(
        json.dumps({
            "schema_version": 1, "name": "X", "source_type": "folder",
            "source_url": "/x", "source_path": "/x",
            "imported_at": "t", "updated_at": "t",
        }),
        encoding="utf-8",
    )
    assert read_source(d) is None


def test_read_source_roundtrip_folder(tmp_path) -> None:
    d = tmp_path / "p"
    d.mkdir()
    src = PersonaSource(
        name="X", source_type="folder", source_url="/x",
        content_hash="sha256:abc", version="1.0.0",
        imported_at="t", updated_at="t", source_path="/x",
    )
    persona_import.write_source(d, src)
    got = read_source(d)
    assert got == src


def test_read_source_roundtrip_github(tmp_path) -> None:
    d = tmp_path / "p"
    d.mkdir()
    src = PersonaSource(
        name="X", source_type="github", source_url="https://github.com/o/r",
        content_hash="sha256:abc", version=None,
        imported_at="t", updated_at="t",
        github=GithubProvenance(
            owner="o", repo="r", ref="main", path="personas/X",
            commit_sha="deadbeef",
        ),
    )
    persona_import.write_source(d, src)
    got = read_source(d)
    assert got == src


def test_read_source_github_missing_subobject(tmp_path) -> None:
    d = tmp_path / "p"
    d.mkdir()
    (d / SOURCE_FILENAME).write_text(
        json.dumps({
            "schema_version": 1, "name": "X", "source_type": "github",
            "source_url": "u", "content_hash": "sha256:a", "version": None,
            "imported_at": "t", "updated_at": "t",
            # 缺 github 子结构 → 降级 None。
        }),
        encoding="utf-8",
    )
    assert read_source(d) is None


# ── content_hash 稳定性 ──────────────────────────────────────────────────


def test_content_hash_stable_and_sensitive() -> None:
    files1 = [("a.md", b"hello"), ("b/c.txt", b"world")]
    files2 = [("b/c.txt", b"world"), ("a.md", b"hello")]  # 顺序不同
    assert _content_hash(files1) == _content_hash(files2)
    # 改一个字节 → 变。
    files3 = [("a.md", b"hellp"), ("b/c.txt", b"world")]
    assert _content_hash(files1) != _content_hash(files3)
    # 路径分隔符归一：Windows \\ 与 POSIX / 同 hash。
    files4 = [("a.md", b"hello"), ("b\\c.txt", b"world")]
    assert _content_hash(files1) == _content_hash(files4)


# ── github 导入写 provenance（monkeypatch 网络）───────────────────────────


def test_github_import_writes_provenance(env_paths, monkeypatch) -> None:
    files = [
        ("Yvonne.md", b"# SOUL"),
        ("persona.toml", b'schema_version = 1\nversion = "2.0.0"\n'),
    ]
    monkeypatch.setattr(
        persona_import, "_fetch_github_dir", lambda *a, **k: files
    )
    monkeypatch.setattr(
        persona_import, "_gh_latest_commit_sha", lambda *a, **k: "cafe1234"
    )
    persona_import.import_from_github(
        env_paths, "https://github.com/owner/repo/tree/main/personas/Yvonne"
    )
    src = read_source(env_paths.user_data_root / "chahua/personas/Yvonne")
    assert src is not None
    assert src.source_type == "github"
    assert src.github is not None
    assert src.github.owner == "owner"
    assert src.github.repo == "repo"
    assert src.github.ref == "main"
    assert src.github.path == "personas/Yvonne"
    assert src.github.commit_sha == "cafe1234"
    assert src.version == "2.0.0"


def test_github_import_commit_sha_best_effort(env_paths, monkeypatch) -> None:
    # commit sha 取数失败 → 导入仍成功，commit_sha=None（best-effort）。
    files = [("Yvonne.md", b"# SOUL")]
    monkeypatch.setattr(
        persona_import, "_fetch_github_dir", lambda *a, **k: files
    )

    def _boom(*a, **k):
        raise persona_import._GitHubError("rate limit", code=403)

    monkeypatch.setattr(persona_import, "_gh_latest_commit_sha", _boom)
    persona_import.import_from_github(
        env_paths, "https://github.com/owner/repo/tree/main/personas/Yvonne"
    )
    src = read_source(env_paths.user_data_root / "chahua/personas/Yvonne")
    assert src is not None
    assert src.github is not None
    assert src.github.commit_sha is None
