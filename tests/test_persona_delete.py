"""persona 删除（destructive，路径强校验）单元测（P12.6 Step 3）。"""

from __future__ import annotations

import pytest

from chahua import persona_import
from chahua.persona_import import PersonaImportError, delete_persona

PERSONAS = "chahua/personas"


def _make_folder(tmp_path, name="Yvonne"):
    src = tmp_path / "src" / name
    src.mkdir(parents=True)
    (src / f"{name}.md").write_text("# SOUL", encoding="utf-8")
    return src


def test_delete_removes_dir(env_paths, tmp_path) -> None:
    persona_import.import_from_folder(env_paths, _make_folder(tmp_path))
    target = env_paths.user_data_root / PERSONAS / "Yvonne"
    assert target.is_dir()
    delete_persona(env_paths, "Yvonne")
    assert not target.exists()


@pytest.mark.parametrize("bad", ["../foo", "/etc", "a/b", "..", "."])
def test_delete_traversal_rejected(env_paths, tmp_path, bad) -> None:
    # 先装一个真 persona，确认穿越删除不误伤它。
    persona_import.import_from_folder(env_paths, _make_folder(tmp_path))
    survivor = env_paths.user_data_root / PERSONAS / "Yvonne"
    with pytest.raises(PersonaImportError):
        delete_persona(env_paths, bad)
    assert survivor.is_dir()  # 啥也没删


def test_delete_normalized_name_does_not_hit_other_dir(env_paths, tmp_path) -> None:
    """畸形名 ``"a/b"`` sanitize 后 = ``"a-b"`` —— 若真有个装好的 ``a-b`` persona，
    精确匹配守卫必须拒绝该请求、不误删 ``a-b``（操作键须与目录名精确相等）。"""
    persona_import.import_from_folder(env_paths, _make_folder(tmp_path, name="a-b"))
    victim = env_paths.user_data_root / PERSONAS / "a-b"
    assert victim.is_dir()
    with pytest.raises(PersonaImportError, match="精确匹配"):
        delete_persona(env_paths, "a/b")
    assert victim.is_dir()  # 没被误删
    # 用精确目录名仍可正常删。
    delete_persona(env_paths, "a-b")
    assert not victim.exists()


def test_delete_symlink_alias_rejected(env_paths, tmp_path) -> None:
    """persona 目录别名是 symlink → delete 拒绝 —— 否则 resolve() 跟进链接，删别名等于删掉
    被指向的真 persona。"""
    persona_import.import_from_folder(env_paths, _make_folder(tmp_path, name="Real"))
    base = env_paths.user_data_root / PERSONAS
    real = base / "Real"
    alias = base / "Alias"
    alias.symlink_to(real, target_is_directory=True)
    with pytest.raises(PersonaImportError, match="符号链接"):
        delete_persona(env_paths, "Alias")
    assert real.is_dir()        # 真 persona 没被误删
    assert alias.is_symlink()   # 别名也未动（拒绝即停）


def test_delete_builtin_not_in_user_data_rejected(env_paths) -> None:
    # 宝总 是内置 dir-form（只在 app_root），user_data 没有 → 解析后非目录 → 拒。
    with pytest.raises(PersonaImportError):
        delete_persona(env_paths, "宝总")


def test_delete_nonexistent_rejected(env_paths) -> None:
    with pytest.raises(PersonaImportError):
        delete_persona(env_paths, "DoesNotExist")


def test_delete_override_reveals_builtin(env_paths) -> None:
    # user_data 放一个与内置同名的 dir-form override。
    override = env_paths.user_data_root / PERSONAS / "宝总"
    override.mkdir(parents=True)
    (override / "宝总.md").write_text("# 我的覆盖版", encoding="utf-8")
    delete_persona(env_paths, "宝总")
    assert not override.exists()
    # 内置经 find_in_data_then_app 重新浮现。
    found = env_paths.find_in_data_then_app("chahua/personas/宝总/宝总.md")
    assert found is not None
    assert str(env_paths.app_root) in str(found)
