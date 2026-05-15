"""chahua.persona_assets —— sidecar 扫 + persona 相对路径反推 回归。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from chahua._paths import ENV_APP_ROOT, ENV_USER_DATA_ROOT, Paths
from chahua.persona_assets import discover_assets, persona_relative


REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def paths(tmp_path, monkeypatch):
    user_data = tmp_path / "userdata"
    user_data.mkdir()
    monkeypatch.setenv(ENV_APP_ROOT, str(REPO_ROOT))
    monkeypatch.setenv(ENV_USER_DATA_ROOT, str(user_data))
    return Paths.from_env()


def _make_dir_persona(
    root: Path,
    name: str,
    *,
    with_mcp: bool = False,
    with_skills: tuple[str, ...] = (),
) -> Path:
    """造一个 dir-form persona：``<root>/chahua/personas/<name>/<name>.md`` + 可选 sidecar。"""
    pdir = root / "chahua" / "personas" / name
    pdir.mkdir(parents=True)
    md = pdir / f"{name}.md"
    md.write_text(f"# {name}\n", encoding="utf-8")
    if with_mcp:
        mcp = pdir / "mcp.json"
        mcp.write_text(json.dumps({
            "mcpServers": {
                "demo": {"command": "npx", "args": ["-y", "demo-mcp@latest"]},
            }
        }), encoding="utf-8")
    if with_skills:
        sk = pdir / "skills"
        sk.mkdir()
        for sname in with_skills:
            s = sk / sname
            s.mkdir()
            (s / "SKILL.md").write_text(f"# {sname}\n", encoding="utf-8")
    return md


# ── dir form ──────────────────────────────────────────────────────────────


def test_discover_assets_dir_form_with_mcp_and_skills(paths):
    md = _make_dir_persona(
        paths.user_data_root, "Yvonne",
        with_mcp=True, with_skills=("review-a", "review-b"),
    )
    assets = discover_assets(md)
    assert assets.has_mcp
    assert assets.has_skills
    assert "demo" in assets.mcp_servers
    assert assets.mcp_servers["demo"]["command"] == "npx"
    assert assets.skills_dir == md.parent / "skills"
    assert assets.skills_available == ("review-a", "review-b")


def test_discover_assets_dir_form_no_sidecars(paths):
    md = _make_dir_persona(paths.user_data_root, "Solo")
    assets = discover_assets(md)
    assert not assets.has_mcp
    assert not assets.has_skills
    assert assets.skills_dir is None


def test_discover_assets_skips_flat_form(paths):
    """flat persona（直接在 personas/ 下的 md）即使旁边偶然有 mcp.json 也不当 sidecar。"""
    flat_dir = paths.user_data_root / "chahua" / "personas"
    flat_dir.mkdir(parents=True)
    md = flat_dir / "宝总.md"
    md.write_text("# 宝总\n", encoding="utf-8")
    # 这个 mcp.json 是"公共目录里的"，不该被认成宝总的 sidecar。
    (flat_dir / "mcp.json").write_text("{}", encoding="utf-8")
    assets = discover_assets(md)
    assert not assets.has_mcp
    assert not assets.has_skills


# ── mcp.json 异常容忍 ─────────────────────────────────────────────────────


def test_discover_assets_broken_mcp_json_yields_none(paths):
    md = _make_dir_persona(paths.user_data_root, "Broken")
    (md.parent / "mcp.json").write_text("{ not json", encoding="utf-8")
    assets = discover_assets(md)
    assert assets.mcp_servers is None
    assert not assets.has_mcp


def test_discover_assets_mcp_json_without_mcpservers_field(paths):
    md = _make_dir_persona(paths.user_data_root, "Empty")
    (md.parent / "mcp.json").write_text(json.dumps({"other": "stuff"}), encoding="utf-8")
    assert discover_assets(md).mcp_servers is None


def test_discover_assets_filters_malformed_server_entries(paths):
    md = _make_dir_persona(paths.user_data_root, "Mixed")
    (md.parent / "mcp.json").write_text(json.dumps({
        "mcpServers": {
            "good": {"command": "x"},
            "bad-not-dict": "string instead of dict",
        }
    }), encoding="utf-8")
    assets = discover_assets(md)
    assert assets.mcp_servers == {"good": {"command": "x"}}


# ── skills 扫描 ────────────────────────────────────────────────────────────


def test_discover_assets_skills_dir_requires_skill_md(paths):
    """skills/ 下没 SKILL.md 的子目录不算可用 skill —— 与 SkillManager 同口径。"""
    md = _make_dir_persona(paths.user_data_root, "Half")
    (md.parent / "skills").mkdir()
    (md.parent / "skills" / "empty").mkdir()  # 没 SKILL.md
    (md.parent / "skills" / "real").mkdir()
    (md.parent / "skills" / "real" / "SKILL.md").write_text("x", encoding="utf-8")
    assets = discover_assets(md)
    assert assets.skills_available == ("real",)


# ── persona_relative ──────────────────────────────────────────────────────


def test_persona_relative_user_data_takes_precedence(paths):
    """user_data 命中优先 —— 信任 key 用这个相对串就能跨房稳定。"""
    md = _make_dir_persona(paths.user_data_root, "X")
    assert persona_relative(md, paths) == "chahua/personas/X/X.md"


def test_persona_relative_falls_back_to_absolute(paths, tmp_path):
    """三个搜索根都不命中 → 退到绝对路径字符串（外部硬编绝对路径的 corner case）。"""
    outside = tmp_path / "elsewhere" / "X.md"
    outside.parent.mkdir(parents=True)
    outside.write_text("x", encoding="utf-8")
    rel = persona_relative(outside, paths)
    assert rel == str(outside.resolve())
