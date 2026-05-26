"""Persona sidecar 扫描 —— ``mcp.json`` + ``skills/``。

persona 包有两种磁盘形态（见 :func:`chahua.admin.discover_personas`）：

- **flat**：``chahua/personas/<Name>.md`` —— 历史 ship 形态，``parent`` 是公共
  ``personas/`` 目录，不会有 per-persona sidecar。P12.1 起内置 5 位（宝总 / 汪小姐 /
  范总 / 玲子 / 爷叔）都迁到 dir form；flat 仅作为格式仍受支持（社区包可继续用）。
- **dir form**：``chahua/personas/<Name>/<Name>.md`` —— 由 ``persona_import`` 写出
  + P12.1 起内置 persona 也是这形态，``parent`` 是私有子目录，``mcp.json`` /
  ``skills/`` / ``persona.toml`` 才有意义。

本模块只在 dir form 下扫 sidecar；flat 形态 ``mcp_servers=None`` / ``skills_dir=None``。
判断口径：persona md 的父目录名 != ``_PERSONAS_DIR_NAME``。

**只读 + materialize**。trust / mutator 走别处（:mod:`chahua.trust` / :mod:`chahua.admin`）。
"""

from __future__ import annotations

import json
import logging
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from ._fs import link_dir_idempotent
from ._paths import Paths, _package_install_root

_log = logging.getLogger(__name__)


_MCP_JSON_NAME = "mcp.json"
_SKILLS_DIR_NAME = "skills"
# 公共 persona 目录名，dir-form 判定 + admin.PERSONAS_REL_DIR 同口径。
_PERSONAS_DIR_NAME = "personas"


def search_roots(paths: Paths) -> tuple[Path, Path, Path]:
    """三档 persona / asset 搜索根，按"user_data 优先 → app_root → package install"顺序。

    单点定义 —— :func:`persona_relative` / :func:`chahua.admin.discover_personas` 都从这里读，
    避免多处硬编排序漂移。
    """
    return (paths.user_data_root, paths.app_root, _package_install_root())


@dataclass(frozen=True)
class PersonaAssets:
    """一份 persona 的 sidecar 视图。

    所有字段都可能为空 —— 调用方按字段是否 None / 空列表决定是否传给 Agentao。
    """

    persona_path: Path
    """已 resolve 的 persona md 绝对路径（与 :class:`chahua.config.GuestConfig.persona_path` 同形）。"""

    mcp_servers: Optional[dict[str, dict[str, Any]]] = None
    """``mcp.json`` 里 ``mcpServers`` 字段；缺文件 / 解析失败 / 字段不是 dict → ``None``。
    格式与 :class:`agentao.Agentao` 的 ``extra_mcp_servers`` 直接兼容。"""

    skills_dir: Optional[Path] = None
    """persona sibling ``skills/`` 的绝对路径（仅当目录存在时填）。"""

    skills_available: tuple[str, ...] = ()
    """``skills/`` 下每个含 ``SKILL.md`` 的子目录名 —— 给前端 popover 列出。"""

    @property
    def has_mcp(self) -> bool:
        return bool(self.mcp_servers)

    @property
    def has_skills(self) -> bool:
        return bool(self.skills_available)

    def materialize_skills(self, into: Path) -> None:
        """把 persona 的 skills 暴露到 ``<into>/.agentao/skills``，无 sidecar 则清除残留。

        Agentao 默认 SkillManager 的 project-scope 扫描配合 ``~/.agentao/skills/`` 全局
        加载 —— 用户系统级 skill 与 persona 自带 skill 并存而非互斥。优先 symlink
        （persona 改 SKILL.md 立刻反映）；symlink 失败（Windows 普通用户）退到 copytree。

        idempotent：target 已正确指向 source 时直接跳过 —— Windows copy fallback 路径
        避免重复 copytree 整棵子树。

        ``skills_dir=None`` 时仍要清除 ``<into>/.agentao/skills`` —— ``remove_guest`` 故意保留
        ``guests/<name>/`` 工作区，重名重加后老 persona 的 skill 残留会被新 persona 继承。
        """
        source = self.skills_dir if (self.skills_dir and self.skills_dir.is_dir()) else None
        _materialize(into, source)


def discover_assets(persona_path: Path) -> PersonaAssets:
    """从 persona md 绝对路径扫 sidecar。

    persona_path 通常来自 ``GuestConfig.persona_path``（已 resolve）。
    flat 形态（``parent.name == _PERSONAS_DIR_NAME``）直接返回空 assets，跳过磁盘 IO ——
    公共 ``personas/`` 下的 ``mcp.json`` / ``skills/`` 不该当个体 persona 的资产。
    """
    persona_path = persona_path.resolve()
    if persona_path.parent.name == _PERSONAS_DIR_NAME:
        return PersonaAssets(persona_path=persona_path)

    parent = persona_path.parent
    skills = parent / _SKILLS_DIR_NAME
    return PersonaAssets(
        persona_path=persona_path,
        mcp_servers=_load_mcp_servers(parent / _MCP_JSON_NAME),
        skills_dir=skills if skills.is_dir() else None,
        skills_available=_scan_skills(skills),
    )


def persona_relative(persona_path: Path, paths: Paths) -> str:
    """绝对 persona_path → 相对 ``chahua/personas/<x>.md``（如可能）。

    与 ``room.toml`` 里写法、 :func:`chahua.admin.discover_personas` 返回的 ``persona``
    字段、 :mod:`chahua.trust` 信任键三者口径统一 —— 信任清单按这个字符串索引。
    三档搜索根（:func:`search_roots`）任一命中即返；都不命中 → 返绝对路径 str。
    """
    persona_path = persona_path.resolve()
    for root in search_roots(paths):
        try:
            return str(persona_path.relative_to(root))
        except ValueError:
            continue
    return str(persona_path)


# ── internal ──────────────────────────────────────────────────────────────


def _load_mcp_servers(mcp_json_path: Path) -> Optional[dict[str, dict[str, Any]]]:
    if not mcp_json_path.is_file():
        return None
    try:
        with mcp_json_path.open("rb") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        _log.warning("mcp.json 解析失败，跳过 MCP 装载：%s（%s）", mcp_json_path, e)
        return None
    if not isinstance(data, dict):
        _log.warning("mcp.json 顶层不是 object，跳过：%s", mcp_json_path)
        return None
    servers = data.get("mcpServers")
    if not isinstance(servers, dict) or not servers:
        return None
    clean: dict[str, dict[str, Any]] = {}
    for name, cfg in servers.items():
        if isinstance(name, str) and isinstance(cfg, dict):
            clean[name] = dict(cfg)
        else:
            _log.warning("mcp.json 中 %r 项不是合法 server config，丢弃", name)
    return clean or None


def _scan_skills(skills_dir: Path) -> tuple[str, ...]:
    """扫 ``skills/<name>/SKILL.md`` 拿 skill 名列表。

    与 agentao SkillManager 同口径（子目录里有 ``SKILL.md`` 即认）；本函数只用于
    前端 popover 显示，不替代 SkillManager 的真加载。按名字升序稳定。
    """
    if not skills_dir.is_dir():
        return ()
    names: list[str] = []
    for sub in sorted(skills_dir.iterdir()):
        if sub.is_dir() and (sub / "SKILL.md").is_file():
            names.append(sub.name)
    return tuple(names)


def _materialize(working_directory: Path, persona_skills_dir: Optional[Path]) -> None:
    target = working_directory / ".agentao" / "skills"
    source = persona_skills_dir.resolve() if persona_skills_dir is not None else None

    if source is None:
        # 无 skills sibling：清掉 target 残留（``remove_guest`` 保留工作区，老 persona
        # 残留会被新 persona 继承）—— ``link_dir_idempotent`` 不覆盖"无 source"语义，
        # 单独走 unlink/rmtree。
        if target.is_symlink() or target.is_file():
            try:
                target.unlink()
            except OSError as e:
                _log.warning("清理旧 skills 链 / 文件失败 %s：%s", target, e)
        elif target.is_dir():
            try:
                shutil.rmtree(target)
            except OSError as e:
                _log.warning("清理旧 skills 目录失败 %s：%s", target, e)
        return

    if link_dir_idempotent(
        target, source, wipe_real_target=True, label="persona skills"
    ):
        return
    # 链失败（Windows 普通用户无权限）退到 copytree —— skills 是 prompt 资产，拷一份
    # 静态副本也能工作（agentao SkillManager 只读取 SKILL.md）。
    try:
        shutil.copytree(source, target)
    except OSError as e:
        _log.warning("拷 persona skills 到 %s 失败：%s（skills 将不加载）", target, e)
