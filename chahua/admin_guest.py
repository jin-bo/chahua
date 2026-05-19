"""每位茶客的 mutator：增删改 permission / LLM / isolation / extra_mcp。

P6.x 重构：从 :mod:`chahua.admin` 抽出。本模块只动 ``[[guest]]`` 数组的某一项 ——
房间级（``[room]`` / ``[room.llm]`` / ``[scoring]`` 等）由 :mod:`chahua.admin_room`
负责；USER.md / 头像 / raw room.toml 编辑器在 :mod:`chahua.admin_user`。

共用 helper（:func:`_mutate_guest_in_snapshot`）—— "find + replace + 404 raise"
骨架；调用方负责字段级 transform。

外部 import 通过 :mod:`chahua.admin` reexport，老路径全部保留可用。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable, Literal, Optional, Sequence

from ._paths import Paths
from .admin_room import (
    _read_existing_for_mutate,
    _rewrite_and_validate,
    _validate_llm_spec_dict,
)
from .admin_toml import GuestSnapshot
from .config import (
    DEFAULT_ISOLATION,
    VALID_ISOLATION,
    _build_extra_mcp_servers,
    RoomConfig,
    RoomConfigError,
)
from .llm_spec import LLM_TOML_FIELDS
from .permissions import DEFAULT_MODE, VALID_MODES, is_valid_mode

_log = logging.getLogger(__name__)


def add_guest(
    *,
    paths: Paths,
    room_dir: Path,
    persona: str,
    name: Optional[str] = None,
    permission: str = DEFAULT_MODE,
) -> RoomConfig:
    """往现有房间加一位茶客。返回新的 `RoomConfig`。

    `name` 缺省 = persona 文件名（不带 .md）。重名拒绝。persona 文件不存在 / 权限非法
    等问题靠校验路径（重新 `load_room_config`）兜底。
    """
    snapshot = _read_existing_for_mutate(room_dir, paths)
    if name is None or not name.strip():
        name = Path(persona).stem
    if any(g["name"] == name for g in snapshot["guests"]):
        raise ValueError(f"已有同名茶客：{name!r}")
    snapshot["guests"] = list(snapshot["guests"]) + [
        {"name": name, "persona": persona, "permission": permission}
    ]
    return _rewrite_and_validate(room_dir, snapshot, paths)


def remove_guest(*, paths: Paths, room_dir: Path, name: str) -> RoomConfig:
    """从现有房间移除一位茶客。返回新的 `RoomConfig`。

    最后一位茶客不能移（房间必须至少 1 位 —— `_build_guests` 的硬约束）。
    名字不在册 → ValueError。

    `guests/<name>/` 工作区不删 —— 茶客被移除后用户也许想要保留聊天产物 / 它的工具
    输出。需要的话用户自己手动 rm 那个子目录。
    """
    snapshot = _read_existing_for_mutate(room_dir, paths)
    new_guests = [g for g in snapshot["guests"] if g["name"] != name]
    if len(new_guests) == len(snapshot["guests"]):
        raise ValueError(f"茶客 {name!r} 不在房间里")
    if not new_guests:
        raise ValueError("不能移除最后一位茶客（房间至少要有 1 人）")
    snapshot["guests"] = new_guests
    return _rewrite_and_validate(room_dir, snapshot, paths)


def _mutate_guest_in_snapshot(
    snapshot,
    *,
    name: str,
    transform: Callable[[GuestSnapshot], GuestSnapshot],
) -> None:
    """``snapshot["guests"]`` 里找名为 ``name`` 的茶客，用 ``transform`` 出的新 dict 替换。
    没找到 → :class:`ValueError`。``update_guest_*`` 三个 mutator 共用 —— 共享 "find +
    replace + 404 raise" 骨架，只让调用方负责字段级 transform。
    """
    new_guests: list[GuestSnapshot] = []
    found = False
    for g in snapshot["guests"]:
        if g["name"] == name:
            new_guests.append(transform(g))
            found = True
        else:
            new_guests.append(g)
    if not found:
        raise ValueError(f"茶客 {name!r} 不在房间里")
    snapshot["guests"] = new_guests


def update_guest_permission(
    *, paths: Paths, room_dir: Path, name: str, permission: str
) -> RoomConfig:
    """改一位茶客的 permission，返回新的 ``RoomConfig``。

    ``permission`` 必须在 :data:`chahua.permissions.VALID_MODES` 内（PLAN 等隐藏模式拒）；
    名字不在册 → ValueError。改完即 reload —— 单点在写盘到生效之间不存在"半个状态"。

    本函数只动 ``room.toml``。session 重装让新 permission 真正生效（茶客 close + 重建
    agentao instance）是调用方的事（server._update_guest_permission 走 _replace_session）。
    """
    if not is_valid_mode(permission):
        raise ValueError(f"permission={permission!r} 不在 {VALID_MODES} 内")
    snapshot = _read_existing_for_mutate(room_dir, paths)
    _mutate_guest_in_snapshot(
        snapshot, name=name,
        transform=lambda g: {**g, "permission": permission},
    )
    return _rewrite_and_validate(room_dir, snapshot, paths)


def update_guest_llm(
    *, paths: Paths, room_dir: Path, name: str, spec_dict: Optional[dict[str, Any]]
) -> RoomConfig:
    """覆盖一位茶客的 LLM 字段（``model`` / ``base_url`` / ``api_key_env``）；
    ``spec_dict=None`` → 清掉该茶客的 LLM 三件套，回到房间默认。

    名字不在册 → :class:`ValueError`；spec_dict 非空走 :meth:`LLMSpec.from_toml` 预校验。
    存在性校验先于 spec 校验 —— 用户既改错名字又写坏 spec 时，先告诉他名字找不到。
    """
    snapshot = _read_existing_for_mutate(room_dir, paths)
    if not any(g["name"] == name for g in snapshot["guests"]):
        raise ValueError(f"茶客 {name!r} 不在房间里")
    validated: Optional[dict[str, Any]] = (
        _validate_llm_spec_dict(spec_dict, label=f"[[guest]] {name!r}")
        if spec_dict is not None
        else None
    )

    def _patch(g: GuestSnapshot) -> GuestSnapshot:
        cleaned: GuestSnapshot = {k: v for k, v in g.items() if k not in LLM_TOML_FIELDS}  # type: ignore[misc]
        if validated is not None:
            cleaned.update(validated)  # type: ignore[typeddict-item]
        return cleaned

    _mutate_guest_in_snapshot(snapshot, name=name, transform=_patch)
    return _rewrite_and_validate(room_dir, snapshot, paths)


def update_guest_isolation(
    *, paths: Paths, room_dir: Path, name: str,
    isolation: Literal["room", "global"],
) -> RoomConfig:
    """改一位茶客的 ``isolation``（``"room"`` / ``"global"`` 二选一）。

    切换会改变工作目录路径：``room`` → ``<room_dir>/guests/<name>/``；``global`` →
    ``<user_data_root>/guests/<name>/``。旧路径下的 ``.agentao/memory.db`` /
    ``sessions/`` **不会自动迁移**（设计 §2.5：单机本地、用户最懂自己的 .agentao
    干净不干净；自动迁移要处理"两边都有"的 ambiguity 复杂度不值）。

    名字不在册 → :class:`ValueError`；isolation 非法 → :class:`RoomConfigError`。
    """
    if isolation not in VALID_ISOLATION:
        raise RoomConfigError(
            f"isolation={isolation!r} 不在 {sorted(VALID_ISOLATION)} 内"
        )

    def _patch(g: GuestSnapshot) -> GuestSnapshot:
        # 默认值不进 snapshot —— 让回写 toml 时省一行。
        cleaned: GuestSnapshot = {k: v for k, v in g.items() if k != "isolation"}  # type: ignore[misc]
        if isolation != DEFAULT_ISOLATION:
            cleaned["isolation"] = isolation
        return cleaned

    snapshot = _read_existing_for_mutate(room_dir, paths)
    _mutate_guest_in_snapshot(snapshot, name=name, transform=_patch)
    return _rewrite_and_validate(room_dir, snapshot, paths)


def update_guest_extra_mcp(
    *, paths: Paths, room_dir: Path, name: str,
    servers: Sequence[dict[str, Any]],
) -> RoomConfig:
    """覆盖一位茶客的 ``[[guest.extra_mcp_servers]]`` 数组段（整体替换语义）。

    ``servers=[]`` → 清掉该茶客的所有房间级 MCP entry。语义见设计 §2.4：

    - 用户在自己 room.toml 里手写 → 等价用户意图，**自动信任**，不进 trust 清单。
    - 与 persona sidecar mcp 同名时**房间级覆盖** persona（合并在
      :func:`chahua.guest._merged_mcp_configs`）。

    写盘前预校验走 :func:`chahua.config._build_extra_mcp_servers` —— 同 guest 下重名 /
    缺 name / 缺 command / 类型错全在这层抛 :class:`RoomConfigError`，磁盘不动。重名
    强约束的取舍：toml 数组表的顺序在 mutator → 写盘 → reload 之间不稳定（按 dict 序），
    让用户依赖顺序是雷；想覆盖 persona 写一条房间级条目就够。

    名字不在册 → :class:`ValueError`（存在性校验先于 servers 校验，避免"既改错名字又写
    坏 servers"时把 servers 错误先报出来）。
    """
    snapshot = _read_existing_for_mutate(room_dir, paths)
    if not any(g["name"] == name for g in snapshot["guests"]):
        raise ValueError(f"茶客 {name!r} 不在房间里")

    # 预校验：复用 config 的 parser，list → dict 校验（重名 / 字段类型）。
    # 用 sentinel 路径让 RoomConfigError 上下文清晰（mutator 本来不映射 toml 文件）。
    _build_extra_mcp_servers(
        list(servers) if servers else None,
        label=f"[[guest]] {name!r}",
        toml_path=room_dir / "room.toml",
    )

    # 规范化：去多余键 + 规范化字段类型，保证写盘前后字面量稳定。
    cleaned_entries: list[dict[str, Any]] = []
    for s in servers:
        entry: dict[str, Any] = {
            "name": str(s["name"]).strip(),
            "command": str(s["command"]).strip(),
        }
        if s.get("args") is not None:
            entry["args"] = [str(a) for a in s["args"]]
        if s.get("env") is not None:
            entry["env"] = {str(k): str(v) for k, v in s["env"].items()}
        cleaned_entries.append(entry)

    def _patch(g: GuestSnapshot) -> GuestSnapshot:
        cleaned: GuestSnapshot = {k: v for k, v in g.items() if k != "extra_mcp_servers"}  # type: ignore[misc]
        if cleaned_entries:
            cleaned["extra_mcp_servers"] = cleaned_entries
        return cleaned

    _mutate_guest_in_snapshot(snapshot, name=name, transform=_patch)
    return _rewrite_and_validate(room_dir, snapshot, paths)
