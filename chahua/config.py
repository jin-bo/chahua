"""``room.toml`` 配置加载（设计文档 §4 / §6 P1.5）。

**P1.5 严格只支持三件事**：

- ``[room]``：``name`` (必), ``topic``, ``rules``, ``user_md``（USER.md 路径覆盖，§3.8.1）
- ``[[guest]]``：``name`` (必), ``persona`` (必), ``permission``
- 未知字段（含被显式后压到 P4 的 ``provider`` / ``model`` / ``isolation`` 等）一律报错，
  错误信息里讲清楚是 P4 的事还是真打错了

**为什么严格而不是忽略未知键**：toml 写错 / 写了 P4 字段以为生效，是这个阶段最容易发生
的事。静默忽略会让用户配置和实际行为之间出现幻觉。错得响一点更安全。

**路径解析**：
- ``persona`` 相对路径走 :meth:`Paths.find_in_data_then_app` 双根搜 ——
  ``user_data_root`` 优先（用户可 override 或加新茶客），fall through 到 ``app_root``
  （ship 自带）。绝对路径原样。
- ``user_md`` 相对路径只在 ``user_data_root`` 下找（USER.md 是用户私有，不该 fall
  through 到 app bundle）。绝对路径原样。
"""

from __future__ import annotations

import base64
import tomllib
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, Literal, Optional

from ._paths import Paths, resolve_under
from .llm_spec import LLM_TOML_FIELDS, LLMSpec
from .permissions import DEFAULT_MODE, VALID_MODES, is_valid_mode


@lru_cache(maxsize=128)
def read_avatar_data_uri(persona_path: Path) -> Optional[str]:
    """与 persona md sibling 同名的 ``.png`` → ``data:image/png;base64,...``；缺图返 ``None``。

    本函数被 sidebar 渲染（每次 ``room_info``）和 :func:`chahua.admin.discover_personas`
    （每次 ``room_info``）共调，每次都会读 PNG + base64 编码 5 次 30KB ≈ 150KB 的 CPU /
    内存浪费。``@lru_cache`` 按 absolute path key 一次性记住编码结果；persona 文件极少
    被替换，cache stale 的概率几乎为 0；如真要换图，重启 sidecar 即可。
    """
    try:
        data = persona_path.read_bytes()
    except FileNotFoundError:
        return None
    return f"data:image/png;base64,{base64.b64encode(data).decode('ascii')}"

# 编排参数的范围校验。值越界一律 RoomConfigError —— 用户配错宁可炸。
# 元组 (type, min_inclusive, max_inclusive_or_None)。bool 也是 int 子类故先剔除。
# 公开（无下划线）—— admin.py / server.py 都从这里取键名 / 顺序，单点声明避免漂移。
ORCH_FIELD_BOUNDS: dict[str, tuple[type, float, Optional[float]]] = {
    "want_threshold": (float, 0.0, 1.0),
    "max_consecutive_ai_turns": (int, 1, None),
    "speaker_cooldown_turns": (int, 0, None),
    "onboarding_threshold": (int, 1, None),
}

# [room] 段允许的键 = 固定四件套 + 编排参数（派生自 ORCH_FIELD_BOUNDS）+ ``llm``
# 子表（P4.9 起，房间默认 LLM 段，对应 toml 里的 ``[room.llm]`` dotted table）。
# ``llm`` 是 dict 而非标量，平面 key 校验不限制其内部字段；具体字段走
# :meth:`LLMSpec.from_toml`。
_ALLOWED_ROOM_KEYS: frozenset[str] = (
    frozenset({"name", "topic", "rules", "user_md", "llm"})
    | frozenset(ORCH_FIELD_BOUNDS)
)
# [[guest]] 段允许的键。LLM 四件套（model/base_url/api_key_env/temperature）的具体校验
# 在 LLMSpec.from_toml；这里只是顶层白名单。
_ALLOWED_GUEST_KEYS: frozenset[str] = frozenset(
    {
        "name", "persona", "permission", "isolation", "summary",
        "model", "base_url", "api_key_env", "temperature",
        "extra_mcp_servers",
    }
)
# 已移除的字段 —— 不会有未来 phase 接 `provider`（设计 §2.1：用 model = "<provider>/<model>"
# 合并写法）。出现 → 给定向 hint。
_REMOVED_GUEST_KEYS: frozenset[str] = frozenset({"provider"})
# 后续 phase 接入的字段 —— 现在写了报"未实现"。
_DEFERRED_GUEST_KEYS: frozenset[str] = frozenset({"transport"})

# `[[guest.extra_mcp_servers]]` 单条 entry 允许的键。其它键 → RoomConfigError（与 §2 一致：
# 不静默吞 typo）。公开 —— admin 端写盘 / mutator 都用同一 set。
EXTRA_MCP_ENTRY_KEYS: frozenset[str] = frozenset(
    {"name", "command", "args", "env"}
)

# isolation 合法值 + 默认值。公开 —— admin 端 mutator 校验 / server 端 envelope 渲染都用
# 同一 set；DEFAULT_ISOLATION 在 GuestConfig 默认 / 解析 fallback / 写盘"默认不 emit"
# 等 5 处共用，避免字面字符串漂移。
ISOLATION_ROOM: Literal["room"] = "room"
ISOLATION_GLOBAL: Literal["global"] = "global"
DEFAULT_ISOLATION: Literal["room"] = ISOLATION_ROOM
VALID_ISOLATION: frozenset[str] = frozenset({ISOLATION_ROOM, ISOLATION_GLOBAL})

# [debug] 段允许的键（P6.1 / P6.3.B）。同 [room.llm] 口径：未知字段 → RoomConfigError。
# P6.3.B 加 ``max_turns``：按 turn 数截断 ``debug/turns.jsonl`` + ``debug/prompts/<turn_id>/``。
_ALLOWED_DEBUG_KEYS: frozenset[str] = frozenset(
    {"enabled", "capture_prompts", "max_turns"}
)

# ``[debug].max_turns`` 默认值（P6.3.B §8）。普通对话 ~50 turn / 天，500 ≈ 10 天历史；
# 0 关闭 rotation（用户主动选"全量取证"）；负值 → RoomConfigError。
DEBUG_DEFAULT_MAX_TURNS = 500

# 顶层白名单。
_ALLOWED_TOPLEVEL_KEYS: frozenset[str] = frozenset(
    {"room", "guest", "scoring", "summary", "debug"}
)


class RoomConfigError(ValueError):
    """``room.toml`` 解析错误。消息里要说清楚是哪个 toml、哪段出的问题。"""


@dataclass(frozen=True)
class DebugConfig:
    """``[debug]`` 段（P6.1 / P6.3.B，docs/P6-调试与回放.md §2 + P6.3-历史 turn 与磁盘配额.md §8）。

    ``enabled=False`` 时 ``capture_prompts`` 实际效果固定为 ``False`` —— 不变量见
    设计文档「关键不变量」节："recorder.enabled=False ⇒ capture_prompts=False"。

    ``max_turns``：``debug/turns.jsonl`` 行数上限。超额时 :class:`chahua.debug_recorder.TurnRecorder`
    从最老 turn 开始按事务（一条 jsonl 行 + 对应 ``prompts/<turn_id>/`` 子目录）整组删。
    ``0`` 表示不限（"全量取证"）；负值 → :class:`RoomConfigError`。默认值
    :data:`DEBUG_DEFAULT_MAX_TURNS`。
    """

    enabled: bool = True
    capture_prompts: bool = True
    max_turns: int = DEBUG_DEFAULT_MAX_TURNS


@dataclass(frozen=True)
class GuestConfig:
    name: str
    persona_path: Path
    """已解析过的绝对路径，调用方直接 ``read_text`` 即可。"""

    permission: str
    """已校验在 :data:`chahua.permissions.VALID_MODES` 内。"""

    isolation: Literal["room", "global"] = DEFAULT_ISOLATION
    """``"room"``（默认）/ ``"global"`` —— 决定 :meth:`workspace_in` 选哪个根。
    设计 §2.5：room 跟着房间走（删房间一起清）；global 跨房间共享。切换不自动迁移
    旧 ``.agentao/memory.db``（单机本地、用户最懂自己干净不干净，自动迁移要处理
    "两边都有"的 ambiguity 复杂度不值）。"""

    llm: Optional[LLMSpec] = None
    """``[[guest]]`` 里写明的 ``model`` / ``base_url`` / ``api_key_env`` 解析结果；
    缺即走房间默认（:meth:`LLMSpec.from_env`）。P4.1 起每位茶客可独立配 client。"""

    summary: Optional[str] = None
    """``[[guest]].summary`` —— 一句话能力摘要（P8.2-roster-a）。手写可选字段；
    onboarding 的 ``<room>`` 块「在场」行据此渲成带摘要的花名册，让茶客分工时看得见
    别的茶客擅长什么。缺即 ``None``，「在场」行优雅降级为只列名字。"""

    extra_mcp_servers: Optional[dict[str, dict[str, Any]]] = None
    """房间级 inline MCP servers（``[[guest.extra_mcp_servers]]`` 数组表解析结果）。

    ``None`` = 没写；非 ``None`` 是 ``{name -> {command, args?, env?}}``（name 字段被吸收做
    key 后从 cfg 移除）。语义见设计 §2.4：

    - 用户在自己房间 toml 里手写 → 等价用户意图，**自动信任**，不进
      :mod:`chahua.trust` 清单（trust 只挡 persona sidecar mcp，那是可能从 GitHub 导入的
      任意可执行）。
    - 合并到 :class:`agentao.Agentao` 的 ``extra_mcp_servers``，与 persona sidecar mcp
      同名时**房间级覆盖**（房间级是"这里要这么用"的具体配置）。

    同 guest 下重名 → 解析期 :class:`RoomConfigError`（强约束）。不取"后者覆盖"，因为
    toml 数组表的顺序在 UI mutator → 写盘 → reload 之间不稳定（admin 重写按 dict 顺序），
    让用户依赖顺序是雷。"""

    def read_persona(self) -> str:
        return self.persona_path.read_text(encoding="utf-8")

    def read_avatar_data_uri(self) -> Optional[str]:
        """头像约定：与 persona md sibling 同名只换 ``.png``（``chahua/personas/宝总.png``）。

        缺图返 ``None``，前端按缺省渲染（不挂 ``<img>``）。压缩到 128px PNG 时一张
        ~30KB，5 茶客约 150KB on wire 单帧塞得下；将来加更大的人头或更多茶客时
        （>1MB room_info）再改成按需懒拉。结果由模块级 :func:`read_avatar_data_uri`
        memoize（cache 命中跨 sidebar / picker 共享）。
        """
        return read_avatar_data_uri(self.persona_path.with_suffix(".png"))

    def workspace_in(self, *, paths: Paths, room_dir: Path) -> Path:
        """茶客的 ``working_directory``：

        - ``isolation == "room"``（默认）→ ``<room_dir>/guests/<name>/``：跟着房间走，
          删房间一起清；茶客记忆只在本房间留底。
        - ``isolation == "global"`` → ``<user_data_root>/guests/<name>/``：跨房间共享
          的 cwd（包括 ``.agentao/memory.db``）。同一茶客在不同房间装配会复用同 cwd。

        单点定义这个路径 —— 之前 cli.py 的 _build_guests、.gitignore 注释、
        RoomConfig 文档都各自拼一遍，三处口径要同步。
        """
        if self.isolation == ISOLATION_GLOBAL:
            return paths.user_data_root / "guests" / self.name
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

    orchestrator_overrides: dict[str, Any] = field(default_factory=dict)
    """``[room]`` 段里出现的编排参数（``want_threshold`` / ``max_consecutive_ai_turns`` /
    ``speaker_cooldown_turns`` / ``onboarding_threshold``）。**只装载用户实际写了的键** ——
    没写 = 不在 dict 里；session 装配时 patch 到 :class:`OrchestratorConfig()` 默认上。
    这样回写 toml 时也只回写用户填的那几个，不会把"默认值"硬塞进文件。"""

    room_llm: Optional[LLMSpec] = None
    """``[room.llm]`` 段解析结果（P4.9 起）；缺 = 走 env 推断
    （:meth:`LLMSpec.try_from_env`）。"""

    scoring_llm: Optional[LLMSpec] = None
    """``[scoring]`` 段解析结果；缺 = 走房间默认（:attr:`room_llm` 或 env）。打分用
    便宜模型场景。"""

    summary_llm: Optional[LLMSpec] = None
    """``[summary]`` 段解析结果；缺 = 复用 :attr:`scoring_llm`（仍缺则走房间默认）。"""

    debug: DebugConfig = field(default_factory=DebugConfig)
    """``[debug]`` 段解析结果（P6.1）。缺段 → 全字段默认（``enabled=True``、
    ``capture_prompts=True``）。装配期由 :func:`chahua.session.build_room_session`
    传给 :class:`chahua.debug_recorder.TurnRecorder`。"""


# ── 入口 ─────────────────────────────────────────────────────────────────────


def load_room_config(room_dir: Path, *, paths: Paths) -> RoomConfig:
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

    return _build(data, room_dir=room_dir, paths=paths)


# ── 内部 ─────────────────────────────────────────────────────────────────────


def _build(
    data: dict[str, Any],
    *,
    room_dir: Path,
    paths: Paths,
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
        deferred=frozenset(),
        toml_path=toml_path,
    )

    name = _as_str(room_raw.get("name"), label="[room].name", toml_path=toml_path)
    if not name:
        raise RoomConfigError(f"{toml_path}: [room].name 必填")

    topic = _as_str(room_raw.get("topic"), label="[room].topic", toml_path=toml_path)
    rules = _as_str(room_raw.get("rules"), label="[room].rules", toml_path=toml_path)

    user_md_override = _resolve_user_md_override(
        room_raw.get("user_md"),
        paths=paths,
        toml_path=toml_path,
    )

    orchestrator_overrides = _build_orchestrator_overrides(
        room_raw, toml_path=toml_path
    )

    # P4.9：``[room.llm]`` 走 [room] 下的 dotted-table 子表。tomllib 解析后是
    # ``room_raw["llm"] = {...}``；缺即 None，让装配层 fall back 到 env。
    room_llm = _build_room_llm_section(
        room_raw.get("llm"), section="[room.llm]", toml_path=toml_path
    )
    scoring_llm = _build_room_llm_section(
        data.get("scoring"), section="[scoring]", toml_path=toml_path
    )
    summary_llm = _build_room_llm_section(
        data.get("summary"), section="[summary]", toml_path=toml_path
    )

    debug = _build_debug_section(data.get("debug"), toml_path=toml_path)

    guests = _build_guests(
        data.get("guest", []), paths=paths, toml_path=toml_path
    )

    return RoomConfig(
        room_dir=room_dir,
        name=name,
        topic=topic,
        rules=rules,
        guests=guests,
        user_md_override=user_md_override,
        orchestrator_overrides=orchestrator_overrides,
        room_llm=room_llm,
        scoring_llm=scoring_llm,
        summary_llm=summary_llm,
        debug=debug,
    )


def _parse_llm_or_raise(
    raw: Optional[dict], *, label: str, toml_path: Path
) -> Optional[LLMSpec]:
    """:meth:`LLMSpec.from_toml` 的裸 :class:`ValueError` 包成 :class:`RoomConfigError`
    并叠 toml 路径上下文。``[scoring]`` / ``[summary]`` / ``[[guest]]`` 三处共用。
    """
    try:
        return LLMSpec.from_toml(raw, label=label)
    except ValueError as exc:
        raise RoomConfigError(f"{toml_path}: {exc}") from exc


def _build_room_llm_section(
    raw: Any, *, section: str, toml_path: Path
) -> Optional[LLMSpec]:
    """解析 ``[scoring]`` / ``[summary]`` 顶层 LLM 段；缺段 → ``None``。"""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise RoomConfigError(
            f"{toml_path}: {section} 必须是表（table），得到 {type(raw).__name__}"
        )
    return _parse_llm_or_raise(raw, label=section, toml_path=toml_path)


def _build_debug_section(raw: Any, *, toml_path: Path) -> DebugConfig:
    """解析 ``[debug]`` 段（P6.1）。缺段 → :class:`DebugConfig` 默认值。

    未知字段 → :class:`RoomConfigError`（同 ``[room.llm]`` 口径，docs/P6 不变量
    "``[debug]`` section all-or-nothing"）。``enabled=False`` 时 ``capture_prompts``
    在 toml 里仍可显式写，但实际效果由 :class:`chahua.debug_recorder.TurnRecorder`
    强制为 ``False``（不变量"recorder.enabled=False ⇒ capture_prompts=False"）—— 这里
    只做字段解析，不做语义约束。
    """
    if raw is None:
        return DebugConfig()
    if not isinstance(raw, dict):
        raise RoomConfigError(
            f"{toml_path}: [debug] 必须是表（table），得到 {type(raw).__name__}"
        )
    _check_unknown_keys(
        section="[debug]",
        keys=raw,
        allowed=_ALLOWED_DEBUG_KEYS,
        deferred=frozenset(),
        toml_path=toml_path,
    )
    enabled = _as_bool(
        raw.get("enabled", True), label="[debug].enabled", toml_path=toml_path
    )
    capture_prompts = _as_bool(
        raw.get("capture_prompts", True),
        label="[debug].capture_prompts",
        toml_path=toml_path,
    )
    # max_turns（P6.3.B §8）。bool 是 int 子类，先剔；负值越界（与 ORCH_FIELD_BOUNDS
    # 同款"配错宁可炸"口径）；缺省即默认值。0 = 关 rotation。
    max_turns_raw = raw.get("max_turns", DEBUG_DEFAULT_MAX_TURNS)
    if isinstance(max_turns_raw, bool) or not isinstance(max_turns_raw, int):
        raise RoomConfigError(
            f"{toml_path}: [debug].max_turns 必须是整数，得到 "
            f"{type(max_turns_raw).__name__}"
        )
    if max_turns_raw < 0:
        raise RoomConfigError(
            f"{toml_path}: [debug].max_turns={max_turns_raw!r} 越界，要求 >= 0"
        )
    return DebugConfig(
        enabled=enabled,
        capture_prompts=capture_prompts,
        max_turns=max_turns_raw,
    )


def _as_bool(value: Any, *, label: str, toml_path: Path) -> bool:
    """toml 字段必须是 bool。其它类型 → :class:`RoomConfigError`。"""
    if not isinstance(value, bool):
        raise RoomConfigError(
            f"{toml_path}: {label} 必须是布尔值，得到 {type(value).__name__}"
        )
    return value


def _build_orchestrator_overrides(
    room_raw: dict[str, Any], *, toml_path: Path
) -> dict[str, Any]:
    """从 ``[room]`` raw 取出编排参数；不写的键不进 dict（保留 OrchestratorConfig 默认）。

    类型与范围校验按 :data:`ORCH_FIELD_BOUNDS`：``int`` 字段拒绝 ``float``（避免 0.5
    被静默取整成 0）；``float`` 字段允许 ``int`` 输入（TOML 1.0 没有 "0.45 vs 0" 歧义，
    数值能向上 promote）。bool 是 int 子类，所有字段都先剔除 bool 避免 ``True`` 被当 1。
    """
    out: dict[str, Any] = {}
    for key, (expected_type, lo, hi) in ORCH_FIELD_BOUNDS.items():
        if key not in room_raw:
            continue
        raw = room_raw[key]
        label = f"[room].{key}"
        if isinstance(raw, bool):
            raise RoomConfigError(
                f"{toml_path}: {label} 必须是 {expected_type.__name__}，得到 bool"
            )
        if expected_type is float:
            if not isinstance(raw, (int, float)):
                raise RoomConfigError(
                    f"{toml_path}: {label} 必须是数值，得到 {type(raw).__name__}"
                )
            value: Any = float(raw)
        else:
            if not isinstance(raw, int):
                raise RoomConfigError(
                    f"{toml_path}: {label} 必须是整数，得到 {type(raw).__name__}"
                )
            value = raw
        if value < lo or (hi is not None and value > hi):
            bound = f"[{lo}, {hi}]" if hi is not None else f">= {lo}"
            raise RoomConfigError(
                f"{toml_path}: {label}={raw!r} 越界，要求 {bound}"
            )
        out[key] = value
    return out


def _build_extra_mcp_servers(
    raw: Any, *, label: str, toml_path: Path
) -> Optional[dict[str, dict[str, Any]]]:
    """解析 ``[[guest.extra_mcp_servers]]`` 数组表 → ``{name -> cfg}``。

    缺段 / 空数组 → ``None``。每条 entry 必含 ``name`` (str) + ``command`` (str)；
    ``args`` 可选 list[str]；``env`` 可选 dict[str, str]。其它键 → :class:`RoomConfigError`
    （:data:`EXTRA_MCP_ENTRY_KEYS` 之外，防 typo 静默吞）。同 guest 下重名 → raise。

    返回的 dict ``name`` 字段已吸收做 key 移除；保留 ``command`` / ``args`` / ``env``，
    格式与 :class:`agentao.Agentao` 的 ``extra_mcp_servers`` 直接兼容。
    """
    if raw is None:
        return None
    if not isinstance(raw, list):
        raise RoomConfigError(
            f"{toml_path}: {label}.extra_mcp_servers 必须是表数组（[[guest.extra_mcp_servers]]"
            f" 重复段），得到 {type(raw).__name__}"
        )
    if not raw:
        return None
    out: dict[str, dict[str, Any]] = {}
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            raise RoomConfigError(
                f"{toml_path}: {label}.extra_mcp_servers[{i}] 不是表（table）"
            )
        unknown = set(item) - EXTRA_MCP_ENTRY_KEYS
        if unknown:
            raise RoomConfigError(
                f"{toml_path}: {label}.extra_mcp_servers[{i}] 未知字段 {sorted(unknown)}；"
                f"允许：{sorted(EXTRA_MCP_ENTRY_KEYS)}"
            )
        name_raw = item.get("name")
        if not isinstance(name_raw, str) or not name_raw.strip():
            raise RoomConfigError(
                f"{toml_path}: {label}.extra_mcp_servers[{i}].name 必须是非空字符串"
            )
        name = name_raw.strip()
        command_raw = item.get("command")
        if not isinstance(command_raw, str) or not command_raw.strip():
            raise RoomConfigError(
                f"{toml_path}: {label}.extra_mcp_servers[{i}] {name!r}: "
                f"command 必须是非空字符串"
            )
        cfg: dict[str, Any] = {"command": command_raw.strip()}
        args_raw = item.get("args")
        if args_raw is not None:
            if not isinstance(args_raw, list) or not all(
                isinstance(a, str) for a in args_raw
            ):
                raise RoomConfigError(
                    f"{toml_path}: {label}.extra_mcp_servers[{i}] {name!r}: "
                    f"args 必须是字符串列表"
                )
            cfg["args"] = list(args_raw)
        env_raw = item.get("env")
        if env_raw is not None:
            if not isinstance(env_raw, dict) or not all(
                isinstance(k, str) and isinstance(v, str)
                for k, v in env_raw.items()
            ):
                raise RoomConfigError(
                    f"{toml_path}: {label}.extra_mcp_servers[{i}] {name!r}: "
                    f"env 必须是 str→str 字典"
                )
            cfg["env"] = dict(env_raw)
        if name in out:
            raise RoomConfigError(
                f"{toml_path}: {label}.extra_mcp_servers 同 guest 下 name={name!r} 重复"
            )
        out[name] = cfg
    return out


def _build_guests(
    guests_raw: Any, *, paths: Paths, toml_path: Path
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
            removed=_REMOVED_GUEST_KEYS,
            removed_hint=(
                "provider 不再作为独立字段；请写 model = \"<provider>/<model>\""
                "（如 'openai/gpt-5.4'）"
            ),
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
        # 双根搜：user_data 优先 → app fall through。打包后 ship 的 personas 在
        # app_root/chahua/personas/，用户自定义放 user_data_root 同位置即 override。
        hit = paths.find_in_data_then_app(persona_raw)
        if hit is None:
            raise RoomConfigError(
                f"{toml_path}: [[guest]] {name!r} 的 persona 文件不存在：{persona_raw}\n"
                f"已搜：{paths.user_data_root / persona_raw}\n"
                f"     {paths.app_root / persona_raw}"
            )
        persona_path = hit.resolve()

        permission_raw = _as_str(
            g.get("permission") or DEFAULT_MODE,
            label=f"[[guest]] {name!r} permission",
            toml_path=toml_path,
        )
        if not is_valid_mode(permission_raw):
            raise RoomConfigError(
                f"{toml_path}: [[guest]] {name!r} permission={permission_raw!r} "
                f"不在 {VALID_MODES} 内"
            )

        isolation_raw = _as_str(
            g.get("isolation") or DEFAULT_ISOLATION,
            label=f"[[guest]] {name!r} isolation",
            toml_path=toml_path,
        )
        if isolation_raw not in VALID_ISOLATION:
            raise RoomConfigError(
                f"{toml_path}: [[guest]] {name!r} isolation={isolation_raw!r} "
                f"不在 {sorted(VALID_ISOLATION)} 内"
            )

        # LLM 字段从 guest 表里挑出 LLM_TOML_FIELDS 子集再解析 —— 其它键已被
        # _check_unknown_keys 拦过，这里不会混入。
        llm_subset = {k: g[k] for k in LLM_TOML_FIELDS if k in g}
        guest_llm = _parse_llm_or_raise(
            llm_subset or None,
            label=f"[[guest]] {name!r}",
            toml_path=toml_path,
        )

        extra_mcp = _build_extra_mcp_servers(
            g.get("extra_mcp_servers"),
            label=f"[[guest]] {name!r}",
            toml_path=toml_path,
        )

        summary = _as_str(
            g.get("summary"),
            label=f"[[guest]] {name!r} summary",
            toml_path=toml_path,
        )

        out.append(
            GuestConfig(
                name=name,
                persona_path=persona_path,
                permission=permission_raw,
                isolation=isolation_raw,
                llm=guest_llm,
                extra_mcp_servers=extra_mcp,
                summary=summary or None,
            )
        )

    return tuple(out)


def _resolve_user_md_override(
    raw: Any, *, paths: Paths, toml_path: Path
) -> str | None:
    """处理 ``[room].user_md`` 字段。给了的话必须存在 —— typo 不能静默回退。

    返回原字符串（不是 resolved Path）—— ``load_user_md`` 自己会再次拿
    ``user_data_root`` 拼一遍。这里只是早一步校验。

    USER.md 不走 persona 那种"双根搜"路径（user_data_root 优先 + fall through app
    bundle）—— USER.md 是用户私有数据，app bundle 不带；解析只在 user_data_root 下找。
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
    candidate = resolve_under(paths.user_data_root, s)
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
    """顶层未知键（含意外的 section / 标量字段）→ 报错并列出允许集。"""
    for key in data:
        if key in _ALLOWED_TOPLEVEL_KEYS:
            continue
        raise RoomConfigError(
            f"{toml_path}: 顶层未知键 [{key}]。允许：{sorted(_ALLOWED_TOPLEVEL_KEYS)}"
        )


def _check_unknown_keys(
    *,
    section: str,
    keys: Iterable[str],
    allowed: frozenset[str],
    deferred: frozenset[str],
    toml_path: Path,
    removed: frozenset[str] = frozenset(),
    removed_hint: str = "",
) -> None:
    """对一个 section 的键做白名单校验。未知键按三类分诊：

    - ``removed``：已知字段但本 phase 不再接受（如 ``provider`` 被合并到 ``model``）
      —— 出现就给 ``removed_hint`` 引导改写。
    - ``deferred``：后续 phase 会接入的占位 —— 让用户暂时注释掉。
    - 其它：拼写错误 —— 列出 ``allowed`` 帮排查。
    """
    unknown = set(keys) - allowed
    if not unknown:
        return
    in_removed = sorted(unknown & removed)
    in_deferred = sorted(unknown & deferred)
    truly_unknown = sorted(unknown - removed - deferred)
    msgs: list[str] = []
    if in_removed and removed_hint:
        msgs.append(removed_hint)
    if in_deferred:
        msgs.append(f"尚未支持的字段（后续 phase 接入；先注释）：{in_deferred}")
    if truly_unknown:
        msgs.append(
            f"未知字段（拼写错误？）：{truly_unknown}。"
            f"该 section 允许的键：{sorted(allowed)}"
        )
    raise RoomConfigError(
        f"{toml_path}: {section} 字段问题 —— " + "；".join(msgs)
    )
