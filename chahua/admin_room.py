"""房间级 CRUD + ``room.toml`` snapshot 读写 + ``[room]`` / ``[room.llm]`` / ``[scoring]``
/ ``[summary]`` / ``[debug]`` 顶层段落 mutator。

P6.x 重构：从 :mod:`chahua.admin` 抽出。本模块管"房间整体"层的可变操作：建房 /
拆房 / 改房间默认 LLM / 改编排参数。每位茶客的 mutator（add/remove/permission/llm/
isolation/extra_mcp）放在 :mod:`chahua.admin_guest`；USER.md / 头像 / raw room.toml
编辑入口在 :mod:`chahua.admin_user`。

通用 helper（共享给 ``admin_guest`` / ``admin_user``）：

- :func:`_read_existing_for_mutate` — 读 ``room.toml`` 当前内容 → :class:`TomlSnapshot`。
- :func:`_write_text_and_validate` — 写盘 + 重 load 校验 + 失败回滚旧 bytes。
- :func:`_rewrite_and_validate` — snapshot → toml 文本 → 上一条。

外部 import 通过 :mod:`chahua.admin` reexport，老路径全部保留可用。
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Any, Literal, Optional, Sequence

from ._paths import Paths
from ._persist import write_bytes_atomic, write_text_atomic
from .admin_persona import normalize_room_id
from .admin_toml import (
    GuestSnapshot,
    TomlSnapshot,
    _extra_mcp_dict_to_list,
    _llm_spec_to_dict,
    _render_room_toml,
    write_room_toml,
)
from .config import (
    DEBUG_DEFAULT_MAX_TURNS,
    DEFAULT_ISOLATION,
    ORCH_FIELD_BOUNDS,
    _build_orchestrator_overrides,
    DebugConfig,
    RoomConfig,
    RoomConfigError,
    load_room_config,
)
from .llm_spec import LLMSpec
from .permissions import DEFAULT_MODE
from .persona_assets import persona_relative

_log = logging.getLogger(__name__)


# ── room.toml 读现状（mutator 用，区别于 config.load_room_config 的严格校验路径）─


def _read_existing_for_mutate(room_dir: Path, paths: Paths) -> TomlSnapshot:
    """读 room.toml 当前内容，返回完整 :class:`TomlSnapshot`。

    走 :func:`chahua.config.load_room_config` 而非裸 tomllib —— 保证 mutator 路径
    上看到的快照与启动期等价（白名单严格）；用户如果手 edit 出非法 toml，本函数也会
    抛 RoomConfigError，避免 mutator 把更乱的 toml 写回去。
    """
    rc = load_room_config(room_dir, paths=paths)
    return _room_config_to_dict(rc, paths)


def _room_config_to_dict(rc: RoomConfig, paths: Paths) -> TomlSnapshot:
    """`RoomConfig` → :class:`TomlSnapshot`。

    `persona` 字段尽量还原成"相对 chahua/personas/"的形式 —— 用户在 toml 里那么写，
    我们 mutator 写回去也保持一致；但 `RoomConfig.GuestConfig.persona_path` 已 resolve
    成绝对路径，丢了原始相对写法。回退策略：如果 absolute path 在某个搜索根下，重算
    相对值；否则保留 absolute（兼容用户硬编绝对路径的极少数场景）。

    还原范围：name / topic / rules / user_md_override / orchestrator_overrides /
    room_llm / scoring_llm / summary_llm + 每位 guest 的 name / persona / permission /
    isolation / LLM 三件套 + extra_mcp_servers。

    ``isolation`` 走"默认值不进 snapshot"约定 —— 等于 ``"room"`` 时键不出现，emit 路径
    不写到 toml，保留用户那条 toml 行的简洁度。``extra_mcp_servers`` 同理 —— 内部 dict
    形态在 snapshot 里反序列化为 list（数组表与 toml 形态一致），空 dict / None → 不出现。
    """
    guests: list[GuestSnapshot] = []
    for gc in rc.guests:
        g: GuestSnapshot = {
            "name": gc.name,
            "persona": _persona_to_relative(gc.persona_path, paths),
            "permission": gc.permission,
        }
        if gc.isolation != DEFAULT_ISOLATION:
            g["isolation"] = gc.isolation
        if gc.llm is not None:
            g.update(_llm_spec_to_dict(gc.llm))
        if gc.extra_mcp_servers:
            g["extra_mcp_servers"] = _extra_mcp_dict_to_list(gc.extra_mcp_servers)
        guests.append(g)
    return {
        "name": rc.name,
        "topic": rc.topic,
        "rules": rc.rules,
        "user_md": rc.user_md_override,
        "orchestrator_overrides": dict(rc.orchestrator_overrides),
        "room_llm": _llm_spec_to_dict(rc.room_llm) if rc.room_llm else None,
        "scoring": _llm_spec_to_dict(rc.scoring_llm) if rc.scoring_llm else None,
        "summary": _llm_spec_to_dict(rc.summary_llm) if rc.summary_llm else None,
        # [debug] 段：只在非默认时携带（默认 enabled=True / capture_prompts=True）。
        # 这样默认房间结构化重写不会塞进 ``[debug]`` 噪声；用户显式 ``= false`` 经
        # mutator round-trip 后保住设定，不被默认 true/true 悄悄覆盖。
        "debug": _debug_config_to_dict(rc.debug),
        "guests": guests,
    }


def _debug_config_to_dict(debug: DebugConfig) -> Optional[dict]:
    """``DebugConfig`` → snapshot dict；全默认时返 ``None``（不 emit ``[debug]``）。

    P6.3.B：``max_turns`` 与 ``enabled`` / ``capture_prompts`` 同款"只在非默认时携带"
    口径 —— mutator round-trip 用户设过的 ``max_turns = 200`` / ``= 0`` 保得住，
    默认 500 不会被写成 ``max_turns = 500`` 噪声塞回 toml。
    """
    fields = {}
    if not debug.enabled:
        fields["enabled"] = False
    if not debug.capture_prompts:
        fields["capture_prompts"] = False
    if debug.max_turns != DEBUG_DEFAULT_MAX_TURNS:
        fields["max_turns"] = debug.max_turns
    return fields or None


def _persona_to_relative(persona_path: Path, paths: Paths) -> str:
    """绝对 persona_path → 相对 `chahua/personas/<x>.md`（如可能），保持 toml 写法稳定。

    单点委托给 :func:`chahua.persona_assets.persona_relative` —— admin 写 toml 与
    trust 索引、room_info envelope 三处的 persona 字符串 key 共享同一口径，避免漂移。
    """
    return persona_relative(persona_path, paths)


# ── room CRUD ────────────────────────────────────────────────────────────


def create_room(
    *,
    paths: Paths,
    room_id: str,
    name: str,
    topic: str = "",
    rules: str = "",
    guests: Sequence[dict],
) -> RoomConfig:
    """新建一个房间，落盘 + 校验。返回 `RoomConfig`。

    `room_id` 走 :func:`normalize_room_id` 规范化；目录已存在 → 拒绝（不覆盖用户已有
    房间）。`guests` 至少 1 位（与 `_build_guests` 的硬约束一致）。

    创建后立即 `load_room_config` 校验：persona 文件不存在等问题在这里就抛 ——
    避免把坏房间留在磁盘上。校验失败时 rmtree 回滚。
    """
    room_id = normalize_room_id(room_id)
    if not name.strip():
        raise ValueError("房间名不能为空")
    if not guests:
        raise ValueError("至少要有一位茶客")

    # name 缺省 = persona 文件名（与 add_guest 同口径）。前端可只传 persona 一个字段。
    normalized_guests: list[dict] = []
    for g in guests:
        gname = (g.get("name") or "").strip() or Path(str(g["persona"])).stem
        normalized_guests.append({
            "name": gname,
            "persona": str(g["persona"]),
            "permission": g.get("permission") or DEFAULT_MODE,
        })

    room_dir = paths.user_data_root / "rooms" / room_id
    # mkdir(exist_ok=False) 自身就会 raise FileExistsError；不另设 if exists 兜底。
    room_dir.mkdir(parents=True, exist_ok=False)
    snapshot: TomlSnapshot = {
        "name": name,
        "topic": topic,
        "rules": rules,
        "guests": normalized_guests,
    }
    try:
        write_room_toml(room_dir, snapshot)
        return load_room_config(room_dir, paths=paths)
    except Exception:
        # 校验 / 写失败 → 回滚整个目录，避免半成品房间挂在 sidebar 列表里。
        shutil.rmtree(room_dir, ignore_errors=True)
        raise


def delete_room(*, paths: Paths, room_id: str, current_room_id: Optional[str]) -> None:
    """删除一个房间目录。当前房间不能删 —— 调用方需先切走。

    rmtree 整个目录，包含 transcript / cursor / summary / guests/ 子工作区。
    用户 confirm 已在前端做；这里只防御"删自己脚下的椅子"。
    """
    room_id = normalize_room_id(room_id)
    if room_id == current_room_id:
        raise ValueError(f"不能删除当前房间：{room_id}（请先切到别的房间）")
    # rmtree 自身在路径不存在时抛 FileNotFoundError；不另设 is_dir 兜底。
    shutil.rmtree(paths.user_data_root / "rooms" / room_id)


# ── 房间默认 LLM / 编排参数 mutator ──────────────────────────────────────


def _validate_llm_spec_dict(
    spec_dict: dict[str, Any], *, label: str
) -> dict[str, Any]:
    """走 :meth:`LLMSpec.from_toml` 的 all-or-nothing 校验，回吐规范化后的 dict。

    写盘前 pre-validate 是必要的 —— 否则 bad type（如 bool）会走到
    :func:`_render_room_toml` 才被 :class:`TypeError` 拦，绕过 :func:`_write_text_and_validate`
    那条只 catch :class:`RoomConfigError` 的回滚分支。
    """
    try:
        spec = LLMSpec.from_toml(spec_dict, label=label)
    except ValueError as exc:
        raise RoomConfigError(str(exc)) from exc
    if spec is None:
        # 空 dict / 缺 model（且没非 model 字段触发 all-or-nothing 报错）—— 语义不明。
        raise RoomConfigError(
            f"{label}: spec 不含 model 字段（要清掉这段 LLM 配置请传 None，不要传空 dict）"
        )
    return _llm_spec_to_dict(spec)


_ROOM_LLM_SECTIONS: dict[str, tuple[str, str]] = {
    # section param → (snapshot key, toml label)
    "room": ("room_llm", "[room.llm]"),
    "scoring": ("scoring", "[scoring]"),
    "summary": ("summary", "[summary]"),
}


def update_room_llm(
    *, paths: Paths, room_dir: Path,
    section: Literal["room", "scoring", "summary"],
    spec_dict: Optional[dict[str, Any]],
) -> RoomConfig:
    """覆盖 ``[room.llm]`` / ``[scoring]`` / ``[summary]`` 顶层 LLM 段。
    ``spec_dict=None`` → 删整段（房间默认段被删后 fallback 到 env，
    见 :func:`chahua.session._resolve_room_default_spec`）。

    非 None 走 :meth:`LLMSpec.from_toml` 预校验；失败 raise 不动盘。
    """
    if section not in _ROOM_LLM_SECTIONS:
        raise RoomConfigError(
            f"update_room_llm: section 必须是 "
            f"{sorted(_ROOM_LLM_SECTIONS)}，得到 {section!r}"
        )
    snapshot_key, label = _ROOM_LLM_SECTIONS[section]
    snapshot = _read_existing_for_mutate(room_dir, paths)
    if spec_dict is None:
        snapshot[snapshot_key] = None  # type: ignore[literal-required]
    else:
        snapshot[snapshot_key] = _validate_llm_spec_dict(  # type: ignore[literal-required]
            spec_dict, label=label,
        )
    return _rewrite_and_validate(room_dir, snapshot, paths)


def update_room_orchestrator(
    *, paths: Paths, room_dir: Path, overrides: dict[str, Any]
) -> RoomConfig:
    """整体覆盖 ``[room]`` 段的编排参数键集，返回新的 ``RoomConfig``。

    语义是**整体覆盖**而非"merge 进现有 overrides"：传入空 dict 即清掉 ``[room]`` 下
    所有编排键（让 OrchestratorConfig 默认接管）。这避免"前端只想撤回 X 但忘传 Y =
    Y 被静默保留"的 surprise。

    校验**写盘前**：未知键 + 越界 / 类型错都在这里报 :class:`RoomConfigError` —— 不依赖
    "写到 toml 再 load 时被拒 → 回滚旧 bytes"那条路径，因为 bad type（如 bool）会先撞
    到 :func:`_format_toml_scalar` 的 ``NotImplementedError`` 让错路径绕过回滚 except 分支。
    """
    unknown = set(overrides) - set(ORCH_FIELD_BOUNDS)
    if unknown:
        raise RoomConfigError(
            f"update_room_orchestrator: 未知编排键 {sorted(unknown)}；"
            f"允许：{sorted(ORCH_FIELD_BOUNDS)}"
        )
    toml_path = room_dir / "room.toml"
    validated = _build_orchestrator_overrides(overrides, toml_path=toml_path)
    snapshot = _read_existing_for_mutate(room_dir, paths)
    snapshot["orchestrator_overrides"] = validated
    return _rewrite_and_validate(room_dir, snapshot, paths)


# ── 写盘 + 校验 + 回滚 helper（admin_guest / admin_user / 本模块共用）────


def _write_text_and_validate(
    room_dir: Path, new_content: str, *, paths: Paths
) -> RoomConfig:
    """共用写盘 + 校验 + 回滚 helper。结构化 mutator 与 raw editor 都走这条路：

    1. 读旧 bytes（缺文件 → ``None``，意味着是首次创建场景）。
    2. 写新文本（``write_text_atomic``）。
    3. 重 ``load_room_config`` 校验。
    4. 失败 → 回写旧 bytes（或删空 toml）后 raise。

    P4.-1 把"结构化 mutator 走 dict-snapshot 重写回滚"路径改成"统一走旧 bytes 回滚"
    —— 避免 "snapshot 自身就 corrupt 再写回去" 的边角，也保留用户原始格式（multi-table
    顺序 / 留白）。
    """
    toml_path = room_dir / "room.toml"
    try:
        old_bytes: Optional[bytes] = toml_path.read_bytes()
    except FileNotFoundError:
        old_bytes = None
    write_text_atomic(toml_path, new_content)
    try:
        return load_room_config(room_dir, paths=paths)
    except RoomConfigError:
        if old_bytes is not None:
            write_bytes_atomic(toml_path, old_bytes)
        else:
            toml_path.unlink(missing_ok=True)
        raise


def _rewrite_and_validate(
    room_dir: Path, snapshot: TomlSnapshot, paths: Paths
) -> RoomConfig:
    """结构化 mutator 入口：``snapshot`` → 渲染 toml 文本 → 走 :func:`_write_text_and_validate`。

    snapshot 经 :func:`_render_room_toml` 转文本（emit 路径里若携带 P4.0+ 才支持的字段
    会 :class:`NotImplementedError`，避免写出运行时未消费的字段）。
    """
    new_text = _render_room_toml(snapshot)
    return _write_text_and_validate(room_dir, new_text, paths=paths)
