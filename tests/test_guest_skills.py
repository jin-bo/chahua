"""PersonaAssets.materialize_skills 回归。

核心契约：persona sibling 的 ``skills/`` 经 ``<wd>/.agentao/skills/`` 暴露给 Agentao
默认 SkillManager，让 **全局** ``~/.agentao/skills/`` **+** **persona 自带** skill
同时加载（不互斥）。explicit-dir 模式会屏蔽全局，所以这条契约要锁住。
"""

from __future__ import annotations

from pathlib import Path

from chahua.persona_assets import PersonaAssets


def _seed_skills(root: Path, names: tuple[str, ...]) -> Path:
    skills = root / "src" / "skills"
    skills.mkdir(parents=True)
    for n in names:
        (skills / n).mkdir()
        (skills / n / "SKILL.md").write_text(f"# {n}\n", encoding="utf-8")
    return skills


def _assets_for(skills: Path) -> PersonaAssets:
    # persona_path 字段在 materialize_skills 中未参与；任一存在路径即可。
    return PersonaAssets(persona_path=skills.parent, skills_dir=skills)


def test_materialize_creates_symlink_or_copy(tmp_path):
    persona_skills = _seed_skills(tmp_path / "persona", ("review",))
    wd = tmp_path / "guest-wd"
    wd.mkdir()
    _assets_for(persona_skills).materialize_skills(wd)
    target = wd / ".agentao" / "skills"
    assert target.exists()
    assert (target / "review" / "SKILL.md").read_text(encoding="utf-8") == "# review\n"


def test_materialize_replaces_stale_link(tmp_path):
    """切换 persona / 切权限 → 重 build session，旧 skills 链要被新 persona 的覆盖。"""
    wd = tmp_path / "guest-wd"
    wd.mkdir()
    old = _seed_skills(tmp_path / "old", ("old-skill",))
    _assets_for(old).materialize_skills(wd)
    new = _seed_skills(tmp_path / "new", ("new-skill",))
    _assets_for(new).materialize_skills(wd)
    target = wd / ".agentao" / "skills"
    assert (target / "new-skill" / "SKILL.md").exists()
    assert not (target / "old-skill").exists()


def test_materialize_is_idempotent_for_correct_symlink(tmp_path):
    """同一 source 再 materialize → 不该重 unlink/relink（POSIX 上 readlink 一致 → 直接返）。"""
    persona_skills = _seed_skills(tmp_path / "persona", ("a",))
    wd = tmp_path / "guest-wd"
    wd.mkdir()
    a = _assets_for(persona_skills)
    a.materialize_skills(wd)
    target = wd / ".agentao" / "skills"
    if not target.is_symlink():
        return  # Windows copy fallback —— 跳过 idempotency 断言（copytree 会幂等清+建）。
    before_inode = target.lstat().st_ino
    a.materialize_skills(wd)
    assert target.lstat().st_ino == before_inode


def test_materialize_skips_when_no_skills_dir(tmp_path):
    """skills_dir=None → no-op，不创建 .agentao/skills。"""
    wd = tmp_path / "guest-wd"
    wd.mkdir()
    PersonaAssets(persona_path=tmp_path).materialize_skills(wd)
    assert not (wd / ".agentao" / "skills").exists()


def test_materialize_clears_stale_when_new_persona_has_no_skills(tmp_path):
    """remove_guest 保留 guests/<name>/ 工作区；重名后加进一个无 skills 的 persona
    必须清除上一份 persona 的 skills 残留，否则 Agentao 还在加载旧 skill。"""
    wd = tmp_path / "guest-wd"
    wd.mkdir()
    old = _seed_skills(tmp_path / "old", ("ghost-skill",))
    _assets_for(old).materialize_skills(wd)
    target = wd / ".agentao" / "skills"
    assert (target / "ghost-skill").exists()

    PersonaAssets(persona_path=tmp_path).materialize_skills(wd)
    assert not target.exists()


def test_default_skillmanager_sees_persona_skills_via_target(tmp_path, monkeypatch):
    """SkillManager 默认模式（working_directory 指向 guest wd）应扫到 .agentao/skills 下的 persona skill。

    锁住"持 SkillManager(working_directory=wd) 即可同时看到 global + persona"的契约。
    global skills 是用户级（``~/.agentao/skills/``），我们这测里 monkeypatch HOME 让它指
    向一个空 tmp，所以这里看到的是 persona skill 单独贡献的那条。
    """
    persona_skills = _seed_skills(tmp_path / "persona", ("alpha", "beta"))
    wd = tmp_path / "guest-wd"
    wd.mkdir()
    _assets_for(persona_skills).materialize_skills(wd)

    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))

    from agentao.skills.manager import SkillManager
    mgr = SkillManager(working_directory=wd)
    names = set(mgr.available_skills.keys())
    assert {"alpha", "beta"}.issubset(names)
