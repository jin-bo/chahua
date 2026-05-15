"""权限模式同步入口（设计文档 §3.4.1）。

agentao 的 read-only 拦截**不是单点**：

- `PermissionEngine.set_mode(...)` —— 决定一次工具调用是否被允许 / 是否要确认。
- `agent.tool_runner.set_readonly_mode(...)` —— 在 planning 阶段就拒绝非只读工具
  （见 ``agentao/runtime/tool_planning.py``：``if readonly_mode and not tool.is_read_only: 拒绝``）。

只设其一不够。茶话室对外只暴露 :func:`apply_permission_mode` 这一个入口；其他模块**禁止**
单独调 ``set_mode`` —— 这是评审里的高优问题，靠纪律 + 单入口防住。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Union

from agentao.permissions import PermissionMode

if TYPE_CHECKING:
    from agentao import Agentao

# PLAN 是 agentao 内部模式（read-only writes + safe shell），茶话室不暴露给用户。
# 其余从枚举派生，agentao 加新模式时这里自动跟上。
_HIDDEN: frozenset[PermissionMode] = frozenset({PermissionMode.PLAN})
_MODE_MAP: dict[str, PermissionMode] = {
    m.value: m for m in PermissionMode if m not in _HIDDEN
}

VALID_MODES: tuple[str, ...] = tuple(_MODE_MAP)

# 默认权限：read-only —— admin / config / server / 前端 picker 共用同一字面量。
DEFAULT_MODE: str = PermissionMode.READ_ONLY.value


def is_valid_mode(mode: str) -> bool:
    """字符串是否是茶话室暴露的合法权限模式。

    供 config 等模块做 toml-time 前置校验用；``apply_permission_mode`` 内部还会
    再校验一次，所以这里只是"早报错"而不是兜底。
    """
    return mode in _MODE_MAP


def apply_permission_mode(agent: "Agentao", mode: Union[str, PermissionMode]) -> None:
    """同步设置 PermissionEngine 和 ToolRunner 的两层只读拦截。

    Args:
        agent: 已构造的 Agentao 实例。
        mode: 字符串（必须在 :data:`VALID_MODES` 内）或 :class:`PermissionMode`。

    Raises:
        ValueError: 字符串模式不在白名单内（包含 PLAN 被显式拒绝）。
    """
    if isinstance(mode, str):
        try:
            pm = _MODE_MAP[mode]
        except KeyError:
            raise ValueError(f"permission={mode!r} 不在 {VALID_MODES} 内") from None
    else:
        if mode in _HIDDEN:
            raise ValueError(f"PermissionMode.{mode.name} 不对茶话室暴露")
        pm = mode

    # tool_runner 在 Agentao.__init__ 里无条件构造、permission_engine 由 TeaGuest 显式
    # 传入。若到这里二者还缺，说明上层装配出错，属于内部不变量违反。
    assert agent.permission_engine is not None, "agent.permission_engine missing"
    assert agent.tool_runner is not None, "agent.tool_runner missing"

    agent.permission_engine.set_mode(pm)
    # tool_runner 的 readonly 标志独立维护，必须显式同步。
    agent.tool_runner.set_readonly_mode(pm == PermissionMode.READ_ONLY)
