"""``admin_persona.discover_personas`` 接 ``persona.toml`` manifest 后的集成测（P12 C2）。

覆盖：
- manifest + 老 ``<Name>.toml`` 同存 → display_name 取 manifest，summary 出现。
- 仅 manifest → display_name / summary 来自 manifest。
- 仅老 toml → display_name 来自 legacy，summary 为 None。
- 无 manifest / 无 legacy → 走 stem 兜底，summary 为 None。
- flat-form built-in 行为零变化（不查 manifest，summary 永远 None）。
- 坏 manifest → persona 仍在 picker 可见（走 legacy 兜底）、summary None、WARN 一次。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from chahua import admin
from chahua._paths import ENV_APP_ROOT, ENV_USER_DATA_ROOT, Paths


REPO_ROOT = Path(__file__).resolve().parent.parent
PERSONAS_DIR_REL = Path("chahua/personas")


@pytest.fixture
def paths(tmp_path, monkeypatch):
    """user_data_root 在 tmp 下；app_root 指向真仓库根（ship 自带 personas 还在）。

    多数测试只装 user_data 下的 persona —— app_root 自带的 5 位仍会被扫到，按 name
    排序比 user_data 增量晚但不影响行为；测试只断言 user_data 下的字段值。
    """
    user_data = tmp_path / "userdata"
    user_data.mkdir()
    monkeypatch.setenv(ENV_APP_ROOT, str(REPO_ROOT))
    monkeypatch.setenv(ENV_USER_DATA_ROOT, str(user_data))
    return Paths.from_env()


def _make_dir_form(paths: Paths, dirname: str, *, md_body: str = "I am persona") -> Path:
    """造一个 dir-form persona：user_data/chahua/personas/<dirname>/<dirname>.md。"""
    persona_dir = paths.user_data_root / PERSONAS_DIR_REL / dirname
    persona_dir.mkdir(parents=True)
    (persona_dir / f"{dirname}.md").write_text(md_body, encoding="utf-8")
    return persona_dir


def _find(found: list[dict], name: str) -> dict:
    for p in found:
        if p["name"] == name:
            return p
    raise AssertionError(f"persona {name!r} 没在 discover 结果里")


# ── manifest 优先级 ─────────────────────────────────────────────────────


def test_manifest_present_overrides_legacy(paths):
    persona_dir = _make_dir_form(paths, "Yvonne")
    # 同存：老 sidecar 写 [guest].name = "旧名"
    (persona_dir / "Yvonne.toml").write_text(
        '[guest]\nname = "旧名"\n', encoding="utf-8"
    )
    # 新 manifest 写 display_name = "伊冯" + summary
    (persona_dir / "persona.toml").write_text(
        'schema_version = 1\ndisplay_name = "伊冯"\nsummary = "市场调研"\n',
        encoding="utf-8",
    )
    found = admin.discover_personas(paths)
    p = _find(found, "伊冯")
    assert p["summary"] == "市场调研"
    # 老名字 "旧名" 不应再出现（manifest 胜）。
    assert all(x["name"] != "旧名" for x in found)


def test_manifest_only_display_and_summary(paths):
    persona_dir = _make_dir_form(paths, "Maya")
    (persona_dir / "persona.toml").write_text(
        'schema_version = 1\ndisplay_name = "玛雅"\nsummary = "设计师"\n',
        encoding="utf-8",
    )
    found = admin.discover_personas(paths)
    p = _find(found, "玛雅")
    assert p["summary"] == "设计师"
    assert p["persona"] == "chahua/personas/Maya/Maya.md"


def test_manifest_without_summary(paths):
    persona_dir = _make_dir_form(paths, "Maya")
    (persona_dir / "persona.toml").write_text(
        'schema_version = 1\ndisplay_name = "玛雅"\n', encoding="utf-8"
    )
    found = admin.discover_personas(paths)
    p = _find(found, "玛雅")
    assert p["summary"] is None  # manifest 没写 → 字段在但值 None


def test_manifest_without_display_name_falls_back_to_dirname(paths):
    persona_dir = _make_dir_form(paths, "Yvonne")
    (persona_dir / "persona.toml").write_text(
        'schema_version = 1\nsummary = "市场调研"\n', encoding="utf-8"
    )
    found = admin.discover_personas(paths)
    p = _find(found, "Yvonne")  # display_name 缺 → 退到目录名
    assert p["summary"] == "市场调研"


def test_manifest_without_display_name_hops_to_legacy_first(paths):
    """三级 fallback 第 2 档不能跳过：manifest 在但 display_name 缺 → 先查 legacy
    <stem>.toml [guest].name，仍缺再退目录名（plan §"承重不变量"第 5 条）。"""
    persona_dir = _make_dir_form(paths, "Yvonne")
    # legacy 提供名字
    (persona_dir / "Yvonne.toml").write_text(
        '[guest]\nname = "伊冯"\n', encoding="utf-8"
    )
    # manifest 提供 summary 但 display_name 缺
    (persona_dir / "persona.toml").write_text(
        'schema_version = 1\nsummary = "市场调研"\n', encoding="utf-8"
    )
    found = admin.discover_personas(paths)
    p = _find(found, "伊冯")  # 必须落到 legacy 名 —— 不能直接跳到 "Yvonne"
    assert p["summary"] == "市场调研"
    # 同时验「Yvonne」目录名不出现 —— 否则三级 fallback 第 2 档被跳了。
    assert all(x["name"] != "Yvonne" for x in found)


def test_manifest_display_name_empty_treated_as_missing(paths):
    """display_name = "" / "  " 与缺字段同义：picker 走下一档 fallback（与 legacy
    _read_persona_toml_name 的 strip+空→None 口径一致）。"""
    persona_dir = _make_dir_form(paths, "Yvonne")
    (persona_dir / "Yvonne.toml").write_text(
        '[guest]\nname = "伊冯"\n', encoding="utf-8"
    )
    (persona_dir / "persona.toml").write_text(
        'schema_version = 1\ndisplay_name = "  "\nsummary = "市场调研"\n',
        encoding="utf-8",
    )
    found = admin.discover_personas(paths)
    # display_name 空白 → 退 legacy → "伊冯"
    p = _find(found, "伊冯")
    assert p["summary"] == "市场调研"


def test_manifest_summary_empty_treated_as_missing(paths):
    persona_dir = _make_dir_form(paths, "Yvonne")
    (persona_dir / "persona.toml").write_text(
        'schema_version = 1\nsummary = "   "\n', encoding="utf-8"
    )
    found = admin.discover_personas(paths)
    p = _find(found, "Yvonne")
    assert p["summary"] is None  # 全空白等同未写


# ── 无 manifest 路径（legacy / stem 兜底）─────────────────────────────


def test_legacy_only(paths):
    persona_dir = _make_dir_form(paths, "Yvonne")
    (persona_dir / "Yvonne.toml").write_text(
        '[guest]\nname = "伊冯"\n', encoding="utf-8"
    )
    found = admin.discover_personas(paths)
    p = _find(found, "伊冯")
    assert p["summary"] is None  # legacy 不提供 summary


def test_neither_manifest_nor_legacy(paths):
    _make_dir_form(paths, "Yvonne")
    # 既无 persona.toml 又无 Yvonne.toml → 退到 stem
    found = admin.discover_personas(paths)
    p = _find(found, "Yvonne")
    assert p["summary"] is None


def test_builtin_personas_have_summary_after_p12_1(paths):
    """P12.1 把仓库自带 5 位迁到 dir-form + manifest，每位都有 summary。
    （P12.1 前是 flat-form，summary 一律 None。）"""
    found = admin.discover_personas(paths)
    for builtin_name in ("宝总", "汪小姐", "范总", "玲子", "爷叔"):
        p = _find(found, builtin_name)
        assert isinstance(p["summary"], str) and p["summary"], (
            f"P12.1 后内置 {builtin_name} 应有 summary"
        )


def test_pure_flat_form_summary_always_none(paths):
    """合成一个真 flat-form persona（personas/<Name>.md 直接放根，无子目录），验证
    flat-form 路径不查 manifest、summary 始终 None。
    """
    md = paths.user_data_root / "chahua" / "personas" / "FlatOnly.md"
    md.parent.mkdir(parents=True, exist_ok=True)
    md.write_text("I am flat", encoding="utf-8")
    found = admin.discover_personas(paths)
    p = _find(found, "FlatOnly")
    assert p["summary"] is None


# ── 坏 manifest 容错 ────────────────────────────────────────────────


def test_broken_manifest_visible_with_legacy_fallback(paths, caplog):
    persona_dir = _make_dir_form(paths, "Yvonne")
    (persona_dir / "Yvonne.toml").write_text(
        '[guest]\nname = "伊冯"\n', encoding="utf-8"
    )
    # schema_version 错 → PersonaManifestError
    (persona_dir / "persona.toml").write_text(
        "schema_version = 2\n", encoding="utf-8"
    )
    import logging
    with caplog.at_level(logging.WARNING):
        found = admin.discover_personas(paths)
    # persona 仍可见、display 退到 legacy、summary None。
    p = _find(found, "伊冯")
    assert p["summary"] is None
    # WARN 含 persona.toml 路径
    assert any("persona.toml" in rec.message for rec in caplog.records)


def test_broken_manifest_no_legacy_falls_back_to_dirname(paths):
    persona_dir = _make_dir_form(paths, "Yvonne")
    (persona_dir / "persona.toml").write_text(
        "schema_version = 2\n", encoding="utf-8"
    )
    found = admin.discover_personas(paths)
    p = _find(found, "Yvonne")  # 没 legacy → 退到目录名
    assert p["summary"] is None


def test_broken_toml_syntax_visible(paths):
    persona_dir = _make_dir_form(paths, "Yvonne")
    (persona_dir / "persona.toml").write_text(
        "schema_version = 1\n[unclosed\n", encoding="utf-8"
    )
    found = admin.discover_personas(paths)
    # persona 仍在结果里（走 stem 兜底）；不抛。
    _find(found, "Yvonne")


# ── user_data 优先覆盖 app_root 内置 ──────────────────────────────────


def test_user_data_persona_overrides_app_root_builtin(paths):
    """user_data 注入的 dir-form 范总 应覆盖 app_root 自带的 dir-form 范总
    （P12.1 起内置 5 位都是 dir-form；P12.1 前测的是 dir-form-overrides-flat）。"""
    persona_dir = paths.user_data_root / PERSONAS_DIR_REL / "范总"
    persona_dir.mkdir(parents=True)
    (persona_dir / "范总.md").write_text("override soul", encoding="utf-8")
    (persona_dir / "persona.toml").write_text(
        'schema_version = 1\nsummary = "user_data 版本"\n', encoding="utf-8"
    )
    found = admin.discover_personas(paths)
    p = _find(found, "范总")
    assert p["summary"] == "user_data 版本"  # user_data 胜
    assert p["persona"] == "chahua/personas/范总/范总.md"  # dir-form 路径
