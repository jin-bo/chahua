"""Persona MCP 信任清单（用户级，跨房共享）。

MCP server 配置里的 ``command`` + ``args`` 是任意可执行 —— 一份 GitHub 导入的 persona
能把 ``npx some-random-pkg`` 塞进去。茶话室策略：**默认不信任**，必须用户在 UI 里显式
勾"信任此 persona 的 MCP"才装载。Skills 是纯 prompt，不进信任门控（read-only 茶客
读了 SKILL.md 也只能"建议"，跑不出真破坏）。

**存储**：``<user_data_root>/.chahua-trusted-mcp.json``，原子写。key = persona 相对
路径字符串（与 ``room.toml`` 里 ``[[guest]].persona`` 字面值同口径），由
:func:`chahua.persona_assets.persona_relative` 反推得到 —— 同一 persona 在不同房间
共享同一条信任记录。

**故意不放 room.toml**：trust 是用户对**这台机器上这位 persona** 的判断，与房间无关。
"""

from __future__ import annotations

import logging

from ._paths import Paths
from ._persist import read_json_or_none, write_json_atomic

_log = logging.getLogger(__name__)


_TRUST_FILE_NAME = ".chahua-trusted-mcp.json"
_SCHEMA_VERSION = 1


def _trust_file(paths: Paths):
    return paths.user_data_root / _TRUST_FILE_NAME


def list_trusted(paths: Paths) -> frozenset[str]:
    """返回当前信任清单。文件缺 / 坏 → 空集合（默认不信任）。"""
    data = read_json_or_none(_trust_file(paths))
    if not isinstance(data, dict):
        return frozenset()
    trusted = data.get("trusted")
    if not isinstance(trusted, list):
        return frozenset()
    return frozenset(s for s in trusted if isinstance(s, str) and s)


def is_mcp_trusted(paths: Paths, persona_rel: str) -> bool:
    if not persona_rel:
        return False
    return persona_rel in list_trusted(paths)


def set_mcp_trust(paths: Paths, persona_rel: str, trusted: bool) -> None:
    """把 persona 加入 / 移出信任清单。原子写整文件。"""
    if not isinstance(persona_rel, str) or not persona_rel:
        raise ValueError(f"persona_rel 必须是非空字符串，得到 {persona_rel!r}")
    current = set(list_trusted(paths))
    if trusted:
        current.add(persona_rel)
    else:
        current.discard(persona_rel)
    p = _trust_file(paths)
    p.parent.mkdir(parents=True, exist_ok=True)
    # sorted 保稳定 —— 用户 git 备份 user_data 时多次写出 diff 友好。
    write_json_atomic(p, {"version": _SCHEMA_VERSION, "trusted": sorted(current)})
