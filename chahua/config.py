"""``room.toml`` 配置加载（设计文档 §4 / §6 P1.5）。

**P1.5 严格只支持三件事**：

- ``[room]``：``name`` (必), ``topic``, ``rules``, ``user_md``（USER.md 路径覆盖，§3.8.1）
- ``[[guest]]``：``name`` (必), ``persona`` (必), ``permission``
- 未知字段（含被显式后压到 P4 的 ``provider`` / ``model`` / ``isolation`` 等）一律报错，
  错误信息里讲清楚是 P4 的事还是真打错了

**为什么严格而不是忽略未知键**：toml 写错 / 写了 P4 字段以为生效，是这个阶段最容易发生
的事。静默忽略会让用户配置和实际行为之间出现幻觉。错得响一点更安全。

**路径解析**：``persona`` / ``user_md`` 相对路径相对 **repo_root**，绝对路径原样
（走 :func:`chahua._paths.resolve_under`）。同一 repo 多房间共享人格 → 相对 repo_root
是合适默认。room-self-contained 需要时 P4 再扩展。
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from ._paths import resolve_under
from .permissions import VALID_MODES, is_valid_mode

# [room] 段允许的键。P1.5 故意保持小。
_ALLOWED_ROOM_KEYS: frozenset[str] = frozenset(
    {"name", "topic", "rules", "user_md"}
)
# [[guest]] 段允许的键。
_ALLOWED_GUEST_KEYS: frozenset[str] = frozenset(
    {"name", "persona", "permission"}
)
# 显式后压到 P4 的字段 —— 用户写了的话报错时点出来。
_DEFERRED_GUEST_KEYS: frozenset[str] = frozenset(
    {"provider", "base_url", "model", "isolation", "extra_mcp_servers", "transport"}
)
_DEFERRED_ROOM_KEYS: frozenset[str] = frozenset(
    {
        "max_consecutive_ai_turns",
        "want_threshold",
        "speaker_cooldown_turns",
        "onboarding_threshold",
    }
)
# 顶层白名单。[scoring] / [summary] 不在这里，走 _DEFERRED_TOPLEVEL_SECTIONS 单独
# 报"P4 的事"，比"未知键"更有信息量。
_ALLOWED_TOPLEVEL_KEYS: frozenset[str] = frozenset({"room", "guest"})
_DEFERRED_TOPLEVEL_SECTIONS: frozenset[str] = frozenset({"scoring", "summary"})


class RoomConfigError(ValueError):
    """``room.toml`` 解析错误。消息里要说清楚是哪个 toml、哪段出的问题。"""


@dataclass(frozen=True)
class GuestConfig:
    name: str
    persona_path: Path
    """已解析过的绝对路径，调用方直接 ``read_text`` 即可。"""

    permission: str
    """已校验在 :data:`chahua.permissions.VALID_MODES` 内。"""

    def read_persona(self) -> str:
        return self.persona_path.read_text(encoding="utf-8")

    def workspace_in(self, room_dir: Path) -> Path:
        """茶客的 ``working_directory`` 约定 = ``<room_dir>/guests/<name>/``。

        单点定义这个路径 —— 之前 cli.py 的 _build_guests、.gitignore 注释、
        RoomConfig 文档都各自拼一遍，三处口径要同步。
        """
        return room_dir / "guests" / self.name


@dataclass(frozen=True)
class RoomConfig:
    room_dir: Path
    """已 ``.resolve()`` 过的绝对路径。茶客 workspace 由 :meth:`GuestConfig.workspace_in`
    决定（约定 ``<room_dir>/guests/<name>/``，与 ``.gitignore`` 的裸 ``guests/`` 行对应）。"""

    name: str
    topic: str = ""
    rules: str = ""
    guests: tuple[GuestConfig, ...] = ()
    user_md_override: str | None = None
    """``[room].user_md`` 字段值；传给 ``load_user_md(..., explicit=...)``。
    已在加载期校验存在 —— 如果用户在 toml 里显式给了 ``user_md``，那个路径必须真存在
    （typo 不会静默回退到默认 USER.md）。"""


# ── 入口 ─────────────────────────────────────────────────────────────────────


def load_room_config(room_dir: Path, *, repo_root: Path) -> RoomConfig:
    """读 ``<room_dir>/room.toml`` 并校验。

    所有错误都包成 :class:`RoomConfigError` 抛出，CLI 层一致捕获并打用户友好提示。
    """
    room_dir = room_dir.resolve()
    toml_path = room_dir / "room.toml"
    if not toml_path.is_file():
        raise RoomConfigError(
            f"找不到 {toml_path}。\n"
            f"先在房间目录下写一份 room.toml，参考 rooms/p1-test/room.toml。"
        )

    try:
        with toml_path.open("rb") as f:
            data: dict[str, Any] = tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise RoomConfigError(f"{toml_path} TOML 解析失败：{e}") from e

    return _build(data, room_dir=room_dir, repo_root=repo_root)


# ── 内部 ─────────────────────────────────────────────────────────────────────


def _build(
    data: dict[str, Any],
    *,
    room_dir: Path,
    repo_root: Path,
) -> RoomConfig:
    toml_path = room_dir / "room.toml"
    _check_unknown_toplevel(data, toml_path)

    room_raw = data.get("room", {})
    if not isinstance(room_raw, dict):
        raise RoomConfigError(f"{toml_path}: [room] 必须是表（table）")

    _check_unknown_keys(
        section="[room]",
        keys=room_raw,
        allowed=_ALLOWED_ROOM_KEYS,
        deferred=_DEFERRED_ROOM_KEYS,
        toml_path=toml_path,
    )

    name = _as_str(room_raw.get("name"), label="[room].name", toml_path=toml_path)
    if not name:
        raise RoomConfigError(f"{toml_path}: [room].name 必填")

    topic = _as_str(room_raw.get("topic"), label="[room].topic", toml_path=toml_path)
    rules = _as_str(room_raw.get("rules"), label="[room].rules", toml_path=toml_path)

    user_md_override = _resolve_user_md_override(
        room_raw.get("user_md"),
        repo_root=repo_root,
        toml_path=toml_path,
    )

    guests = _build_guests(
        data.get("guest", []), repo_root=repo_root, toml_path=toml_path
    )

    return RoomConfig(
        room_dir=room_dir,
        name=name,
        topic=topic,
        rules=rules,
        guests=guests,
        user_md_override=user_md_override,
    )


def _build_guests(
    guests_raw: Any, *, repo_root: Path, toml_path: Path
) -> tuple[GuestConfig, ...]:
    if not isinstance(guests_raw, list) or not guests_raw:
        raise RoomConfigError(
            f"{toml_path}: 至少要有一个 [[guest]] 段"
        )

    seen_names: set[str] = set()
    out: list[GuestConfig] = []
    for i, g in enumerate(guests_raw):
        if not isinstance(g, dict):
            raise RoomConfigError(f"{toml_path}: [[guest]] #{i} 不是表（table）")

        name = _as_str(
            g.get("name"), label=f"[[guest]] #{i} name", toml_path=toml_path
        )
        if not name:
            raise RoomConfigError(f"{toml_path}: [[guest]] #{i} 缺 name")
        if name in seen_names:
            raise RoomConfigError(f"{toml_path}: 茶客名 {name!r} 重复")
        seen_names.add(name)

        _check_unknown_keys(
            section=f"[[guest]] {name!r}",
            keys=g,
            allowed=_ALLOWED_GUEST_KEYS,
            deferred=_DEFERRED_GUEST_KEYS,
            toml_path=toml_path,
        )

        persona_raw = _as_str(
            g.get("persona"),
            label=f"[[guest]] {name!r} persona",
            toml_path=toml_path,
        )
        if not persona_raw:
            raise RoomConfigError(
                f"{toml_path}: [[guest]] {name!r} 缺 persona"
            )
        persona_path = resolve_under(repo_root, persona_raw).resolve()
        if not persona_path.is_file():
            raise RoomConfigError(
                f"{toml_path}: [[guest]] {name!r} 的 persona 文件不存在：{persona_path}"
            )

        permission_raw = _as_str(
            g.get("permission") or "read-only",
            label=f"[[guest]] {name!r} permission",
            toml_path=toml_path,
        )
        if not is_valid_mode(permission_raw):
            raise RoomConfigError(
                f"{toml_path}: [[guest]] {name!r} permission={permission_raw!r} "
                f"不在 {VALID_MODES} 内"
            )

        out.append(
            GuestConfig(
                name=name, persona_path=persona_path, permission=permission_raw
            )
        )

    return tuple(out)


def _resolve_user_md_override(
    raw: Any, *, repo_root: Path, toml_path: Path
) -> str | None:
    """处理 ``[room].user_md`` 字段。给了的话必须存在 —— typo 不能静默回退。

    返回原字符串（不是 resolved Path）—— ``load_user_md`` 自己会再次拿 ``repo_root``
    拼一遍。这里只是早一步校验。
    """
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise RoomConfigError(
            f"{toml_path}: [room].user_md 必须是字符串，得到 {type(raw).__name__}"
        )
    s = raw.strip()
    if not s:
        return None
    candidate = resolve_under(repo_root, s)
    if not candidate.is_file():
        raise RoomConfigError(
            f"{toml_path}: [room].user_md={s!r} 指定的文件不存在：{candidate}"
        )
    return s


def _as_str(value: Any, *, label: str, toml_path: Path) -> str:
    """toml 字段必须是字符串。``None`` → ``""``；其他非 str 类型 → 报错。

    避免之前 ``str(value).strip()`` 静默把 ``user_md = ["x"]`` 变成 ``"['x']"``、
    把 ``topic = true`` 变成 ``"True"`` 这种幻觉行为。
    """
    if value is None:
        return ""
    if not isinstance(value, str):
        raise RoomConfigError(
            f"{toml_path}: {label} 必须是字符串，得到 {type(value).__name__}"
        )
    return value.strip()


def _check_unknown_toplevel(data: dict[str, Any], toml_path: Path) -> None:
    """顶层只允许 ``room`` / ``guest``。``scoring`` / ``summary`` 单独指向 P4。"""
    for key in data:
        if key in _ALLOWED_TOPLEVEL_KEYS:
            continue
        if key in _DEFERRED_TOPLEVEL_SECTIONS:
            raise RoomConfigError(
                f"{toml_path}: [{key}] 段是 P4 的事（模型分流），"
                f"P1.5 全茶客共用一个 LLMClient；先把这段注释掉。"
            )
        raise RoomConfigError(
            f"{toml_path}: 顶层未知键 [{key}]。"
            f"P1.5 只支持 [room] 和 [[guest]]。"
        )


def _check_unknown_keys(
    *,
    section: str,
    keys: Iterable[str],
    allowed: frozenset[str],
    deferred: frozenset[str],
    toml_path: Path,
) -> None:
    """对一个 section 的键做白名单校验，未知键报错并区分 P4 / 真打错。"""
    unknown = set(keys) - allowed
    if not unknown:
        return
    in_deferred = sorted(unknown & deferred)
    truly_unknown = sorted(unknown - deferred)
    msgs: list[str] = []
    if in_deferred:
        msgs.append(f"P4 字段（P1.5 不支持，先注释）：{in_deferred}")
    if truly_unknown:
        msgs.append(
            f"未知字段（拼写错误？）：{truly_unknown}。"
            f"该 section 允许的键：{sorted(allowed)}"
        )
    raise RoomConfigError(
        f"{toml_path}: {section} 字段问题 —— " + "；".join(msgs)
    )
