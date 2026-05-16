"""房间 / 茶客 增删改 + persona 扫描。

`chahua.config` 只读 `room.toml`；本模块管所有把 toml 写回去 / 建房 / 拆房的可变操作。
保持两边职责清晰 —— 加载期严格白名单（错配置宁可炸），运行时 mutator 走这里。

**TOML 写出口径**：自带一个**只覆盖本仓库 schema** 的 mini 序列化器（`[room]` + `[[guest]]`，
basic string 转义）。不引 `tomli_w` 是因为：

- 我们的 schema 极简（两段），手写 ~20 行；引依赖反而把 wheel 体积撑大；
- 任何手工注释 / 字段顺序在重写时无论如何都会丢，标准库 `tomllib` 也只读不带注释 —— 不
  欠用户"保留注释"的承诺，文档里写明"通过 UI 改了房间后 room.toml 会被规范化"即可。

**改完即重载**：所有 mutator 函数返回 `RoomConfig`（已经 `load_room_config` 跑过一遍校验）。
写盘失败 / 再加载失败都 raise 出去；调用方（server.py）负责 emit 错误给前端。
"""

from __future__ import annotations

import base64
import binascii
import logging
import re
import shutil
import tomllib
from pathlib import Path
from typing import Any, Callable, Literal, Optional, Sequence, TypedDict

from ._paths import Paths
from ._persist import write_bytes_atomic, write_text_atomic
from .config import (
    DEFAULT_ISOLATION,
    EXTRA_MCP_ENTRY_KEYS,
    ORCH_FIELD_BOUNDS,
    VALID_ISOLATION,
    _build_extra_mcp_servers,
    _build_orchestrator_overrides,
    RoomConfig,
    RoomConfigError,
    load_room_config,
    read_avatar_data_uri,
)
from .llm_spec import LLM_TOML_FIELDS, LLM_TOML_NUMERIC_FIELDS, LLMSpec
from .permissions import DEFAULT_MODE, VALID_MODES, is_valid_mode
from .persona_assets import persona_relative, search_roots


# ── 完整 room.toml 视图（mutator 内部用，对应 docs/P4-专业茶客配置闭环.md §4.4.1）─
#
# 当前消费 name / topic / rules / user_md / orchestrator_overrides / scoring / summary /
# guests（含 LLM 三件套 + isolation + extra_mcp_servers）。
class GuestSnapshot(TypedDict, total=False):
    """单个 ``[[guest]]`` 表的字段视图。

    ``total=False`` 让后续 phase 加新字段时不破现有构造（mutator 只填它知道的字段；
    snapshot reader 用 ``.get()`` 拿值）。
    """
    name: str
    persona: str
    permission: str
    isolation: str
    # LLM 四件套（all-or-nothing：写 model 才允许 base_url / api_key_env / temperature，
    # 详见 chahua.llm_spec.LLMSpec.from_toml 校验）。temperature 是 float（非字符串），
    # render 时走 scalar literal —— 见 _render_llm_field。
    model: str
    base_url: str
    api_key_env: str
    temperature: float
    # `[[guest.extra_mcp_servers]]` 数组表 —— snapshot 内部按 list[dict]（与 toml 形态
    # 一致），每项含 name / command 必给，args / env 可选。空 list / 缺键 = 不写 toml。
    extra_mcp_servers: list[dict[str, Any]]


class TomlSnapshot(TypedDict, total=False):
    name: str
    topic: str
    rules: str
    user_md: Optional[str]                       # [room].user_md；None / "" = 不写
    orchestrator_overrides: dict[str, Any]       # 空 dict = 不写编排字段（P4.0 消费）
    scoring: Optional[dict]                      # None = 不写 [scoring]（P4.1）
    summary: Optional[dict]                      # None = 不写 [summary]（P4.1）
    guests: list[GuestSnapshot]


# render 时认得的 guest 字段。
_ALLOWED_GUEST_EMIT: frozenset[str] = (
    frozenset({"name", "persona", "permission", "isolation", "extra_mcp_servers"})
    | frozenset(LLM_TOML_FIELDS)
)


_log = logging.getLogger(__name__)


# ── persona 扫描 ──────────────────────────────────────────────────────────


# room.toml 里写法约定就是 `chahua/personas/<name>.md` —— discover 也按这个前缀走，
# 找到的文件相对路径里 persona_rel 字段直接可塞回 [[guest]].persona。
# 公开（无下划线）—— chahua.persona_import 也按这个前缀写盘，单点常量避免漂移。
PERSONAS_REL_DIR = Path("chahua/personas")


def _search_roots(paths: Paths) -> tuple[Path, Path, Path]:
    """与 :meth:`Paths.find_in_data_then_app` 同序的三档 root。

    单点定义委托给 :func:`chahua.persona_assets.search_roots` —— admin.discover_personas
    与 persona_relative 共享同一口径，避免漂移。
    """
    return search_roots(paths)


def _read_persona_toml_name(md_path: Path) -> Optional[str]:
    """从 persona md 的 sibling ``<stem>.toml`` 读 ``[guest].name`` —— picker 的"显示
    用名"来源。缺文件 / 解析失败 / 字段缺失都返 ``None``，调用方走 stem 兜底。

    导入 persona 时（:mod:`chahua.persona_import`）允许带一份 sidecar toml 写明
    人格的"对外名字"（如目录叫 ``Yvonne``，toml 里 ``name = "伊冯"``），picker / 添加
    入房间时显示用 toml 名。

    解析失败一律 WARN + 返 None：不让坏 toml 让整个 persona 在 picker 里消失。
    """
    toml_path = md_path.with_suffix(".toml")
    if not toml_path.is_file():
        return None
    try:
        with toml_path.open("rb") as f:
            data = tomllib.load(f)
    except (tomllib.TOMLDecodeError, OSError) as e:
        _log.warning("persona sidecar toml 解析失败，跳过：%s（%s）", toml_path, e)
        return None
    guest = data.get("guest")
    if not isinstance(guest, dict):
        return None
    name = guest.get("name")
    if not isinstance(name, str):
        return None
    name = name.strip()
    return name or None


def discover_personas(paths: Paths) -> list[dict]:
    """扫所有 persona 候选，按 name 升序、user_data 优先 dedup。

    返回 `[{persona, name, avatar_data_uri}, ...]`：

    - `persona`：相对路径字符串 `chahua/personas/<dir>.md`（flat）或
      `chahua/personas/<dir>/<dir>.md`（dir form，import 出来的包），可塞
      `[[guest]].persona` 字段。
    - `name`：picker 显示用 + 加入房间后 `[[guest]].name` 字段值。优先取 sibling
      `<dir>.toml` 里 `[guest].name`（如 Yvonne.toml 写 ``name = "伊冯"``）；缺
      sidecar 时退到文件名 stem（同时也是大多数内置 persona 的形态）。
    - `avatar_data_uri`：与 md sibling 同名 `.png`；缺图返 `None`。

    给前端 sidebar"添加茶客"picker 用。

    **两种磁盘布局**：flat（`<Name>.md` 直接在 `personas/` 下）+ dir form（`<Name>/<Name>.md`
    在子目录里，sibling 还可能有 `mcp.json` / `skills/`）。dir form 由
    :func:`chahua.persona_import.import_from_folder` / :func:`import_from_github` 写出来；
    flat 是 ship-with-app 的内置 persona 形态。同名时 user_data 胜（dedup by display name）。
    """
    seen_by_name: dict[str, dict] = {}
    # 后写后覆盖等价 user_data 胜 —— 反序遍历保证 user_data 在 dict 里最后落定。
    for root in reversed(_search_roots(paths)):
        personas_dir = root / PERSONAS_REL_DIR
        if not personas_dir.is_dir():
            continue
        # flat：`<Name>.md`
        for md in sorted(personas_dir.glob("*.md")):
            name = _read_persona_toml_name(md) or md.stem
            seen_by_name[name] = {
                "persona": str(PERSONAS_REL_DIR / md.name),
                "name": name,
                "avatar_data_uri": read_avatar_data_uri(md.with_suffix(".png")),
            }
        # dir form：`<Name>/<Name>.md`（与目录同名）；找不到同名 md 时退化为目录里
        # 唯一一份 `*.md`，与 persona_import._derive_name 的兼容口径一致。
        for sub in sorted(personas_dir.iterdir()):
            if not sub.is_dir():
                continue
            md = sub / f"{sub.name}.md"
            if not md.is_file():
                # 兼容：目录名 ≠ md 文件名时，取目录里唯一一份 *.md。
                mds = [p for p in sub.iterdir() if p.is_file() and p.suffix.lower() == ".md"]
                if len(mds) != 1:
                    continue
                md = mds[0]
            name = _read_persona_toml_name(md) or sub.name
            seen_by_name[name] = {
                "persona": str(PERSONAS_REL_DIR / sub.name / md.name),
                "name": name,
                "avatar_data_uri": read_avatar_data_uri(md.with_suffix(".png")),
            }
    return [seen_by_name[n] for n in sorted(seen_by_name)]


# ── room_id 规范化 ────────────────────────────────────────────────────────


# 文件系统不友好的字符全部丢掉 / 替换。中文 / 拉丁 / 数字 / 破折号 / 下划线保留。
# 不允许 `..` / 绝对路径 / 路径分隔符 —— 防止 traversal 写出 rooms/ 目录外。
_FS_NAME_FORBIDDEN = re.compile(r"[\x00-\x1f<>:\"/\\|?*]")
_FS_NAME_TRIM = re.compile(r"^[.\s]+|[.\s]+$")


def sanitize_fs_name(raw: str, *, label: str = "name") -> str:
    """把任意字符串规范成合法目录名。空 / 全点 → ``ValueError``。

    规则：禁用字符 → ``-``；首尾去空白和点；空 / ``.`` / ``..`` → 拒（防 traversal）。
    room_id 与 persona 名（:mod:`chahua.persona_import`）共用同一套 FS 兼容规则。
    """
    s = _FS_NAME_FORBIDDEN.sub("-", raw)
    s = _FS_NAME_TRIM.sub("", s)
    if not s or s in (".", ".."):
        raise ValueError(f"{label} 非法（去掉文件系统禁用字符后为空）：{raw!r}")
    return s


def normalize_room_id(raw: str) -> str:
    """``sanitize_fs_name`` 的 room_id 化身。错误信息走 ``label="room_id"``。"""
    return sanitize_fs_name(raw, label="room_id")


# ── TOML 写 ──────────────────────────────────────────────────────────────


def _toml_basic_string(s: str) -> str:
    """转义一段字符串为 TOML basic string（双引号包裹）。

    覆盖 TOML 1.0 spec 要求的最小转义集 + 控制字符走 `\\uXXXX`。聊天室人名 / topic
    几乎都是 BMP 中日文 + 标点 + 偶尔表情，basic string 比 multi-line literal 更省事。
    """
    out: list[str] = []
    for c in s:
        if c == "\\":
            out.append("\\\\")
        elif c == "\"":
            out.append("\\\"")
        elif c == "\b":
            out.append("\\b")
        elif c == "\t":
            out.append("\\t")
        elif c == "\n":
            out.append("\\n")
        elif c == "\f":
            out.append("\\f")
        elif c == "\r":
            out.append("\\r")
        elif ord(c) < 0x20 or c == "\x7f":
            out.append(f"\\u{ord(c):04X}")
        else:
            out.append(c)
    return '"' + "".join(out) + '"'


def _extra_mcp_dict_to_list(
    servers: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """``{name -> cfg}``（``GuestConfig.extra_mcp_servers`` 形态）→ list[dict]（snapshot
    / toml 数组表形态）。name 重新写回到每条 entry 里。

    保持 dict 插入顺序 → 输出列表顺序稳定 —— round-trip 写盘 / reload / 再写不会无意义打乱。
    """
    out: list[dict[str, Any]] = []
    for name, cfg in servers.items():
        entry: dict[str, Any] = {"name": name, "command": cfg["command"]}
        if "args" in cfg:
            entry["args"] = list(cfg["args"])
        if "env" in cfg:
            entry["env"] = dict(cfg["env"])
        out.append(entry)
    return out


def _llm_spec_to_dict(spec: LLMSpec) -> dict[str, Any]:
    """``LLMSpec`` → toml 表层 dict（仅含非 ``None`` 字段）。

    重新拼回 ``provider/model`` 合并写法（设计 §2.1）；``temperature`` 走 P4.8 起的 UI
    可编辑字段，非 None 时出现在结果里（值是 ``float``，与其它 ``str`` 字段混居 —— emit
    路径靠 :data:`LLM_TOML_NUMERIC_FIELDS` 分流走 scalar literal）。
    """
    out: dict[str, Any] = {"model": spec.model_id}
    if spec.base_url:
        out["base_url"] = spec.base_url
    if spec.api_key_env:
        out["api_key_env"] = spec.api_key_env
    if spec.temperature is not None:
        out["temperature"] = spec.temperature
    return out


def _render_llm_field(key: str, value: Any) -> str:
    """单个 LLM 字段的 TOML 字面量：``temperature`` 走 scalar（数值不引号），其它走
    basic string。:data:`LLM_TOML_NUMERIC_FIELDS` 单点声明谁是数值，加新数值字段时
    只动那里。
    """
    if key in LLM_TOML_NUMERIC_FIELDS:
        return _format_toml_scalar(value)
    return _toml_basic_string(str(value))


def _format_toml_scalar(value: Any) -> str:
    """TOML 数值字面量。float 走 ``repr`` 保留精度；bool 显式拒避免被当 int 写出 1/0。"""
    if isinstance(value, bool):
        raise NotImplementedError("bool 字面量当前 schema 不用，请加在需要时再实现")
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    raise TypeError(f"_format_toml_scalar 不支持类型 {type(value).__name__}")


def _toml_inline_string_array(items: Sequence[str]) -> str:
    """TOML inline array of basic strings：``["a", "b"]``。空 list 写成 ``[]``。"""
    return "[" + ", ".join(_toml_basic_string(s) for s in items) + "]"


def _toml_inline_string_table(d: dict[str, str]) -> str:
    """TOML inline table：``{ "K" = "V", "K2" = "V2" }``。空 dict 写成 ``{}``。

    key 也走 basic string 转义 —— TOML bare key 只允许 ``[A-Za-z0-9_-]``，env 名 / mcp
    arg 里出现别的字符（如 ``.`` / 中文）就会被拒。统一引号化最稳。
    """
    if not d:
        return "{}"
    parts = [
        f"{_toml_basic_string(k)} = {_toml_basic_string(v)}" for k, v in d.items()
    ]
    return "{ " + ", ".join(parts) + " }"


def _render_room_toml(snapshot: TomlSnapshot) -> str:
    """``TomlSnapshot`` → ``room.toml`` 文本。纯函数（无 IO）。

    当前 emit 范围：``[room].{name, topic, rules, user_md, 编排参数}``、可选 ``[scoring]``
    / ``[summary]`` 顶层 LLM 段、``[[guest]].{name, persona, permission, model, base_url,
    api_key_env}``。剩下的字段位（guest isolation / extra_mcp_servers）由 P4.2+ 加
    schema 解析的同 PR 一并加 emit —— 若提前出现在 ``[[guest]]`` 里，下方未知键 guard
    会 :class:`NotImplementedError` 防御，避免 "snapshot 接受 / 写盘但 load_room_config 拒"
    的中间态写坏 toml。
    """
    lines: list[str] = ["[room]"]
    lines.append(f"name  = {_toml_basic_string(snapshot['name'])}")
    lines.append(f"topic = {_toml_basic_string(snapshot.get('topic') or '')}")
    lines.append(f"rules = {_toml_basic_string(snapshot.get('rules') or '')}")
    user_md = snapshot.get("user_md")
    if user_md:
        lines.append(f"user_md = {_toml_basic_string(user_md)}")

    # 编排参数走 [room] 段，按 ORCH_FIELD_BOUNDS 顺序写出（设计文档 §3 示例的顺序，
    # 用户读 toml 时编排参数总是连成一块好认）。snapshot 只携带用户实际设过的键 ——
    # 没设的不写，保留 OrchestratorConfig() 默认。
    orch = snapshot.get("orchestrator_overrides") or {}
    for key in ORCH_FIELD_BOUNDS:
        if key in orch:
            lines.append(f"{key} = {_format_toml_scalar(orch[key])}")

    # [scoring] / [summary] 顶层 LLM 段（§3 示例顺序：room → scoring → summary → guest）。
    for section in ("scoring", "summary"):
        spec_dict = snapshot.get(section)
        if not spec_dict:
            continue
        lines.append("")
        lines.append(f"[{section}]")
        for key in LLM_TOML_FIELDS:
            if key in spec_dict:
                lines.append(f"{key} = {_render_llm_field(key, spec_dict[key])}")

    for g in snapshot["guests"]:
        gname = str(g["name"])
        unknown = set(g) - _ALLOWED_GUEST_EMIT
        if unknown:
            raise NotImplementedError(
                f"[[guest]] {gname!r} 携带字段 {sorted(unknown)} 的 toml 写出由 P4.x 加；"
                f"snapshot 暂不应携带"
            )
        lines.append("")
        lines.append("[[guest]]")
        lines.append(f"name       = {_toml_basic_string(gname)}")
        lines.append(f"persona    = {_toml_basic_string(str(g['persona']))}")
        lines.append(
            f"permission = {_toml_basic_string(str(g.get('permission') or DEFAULT_MODE))}"
        )
        # 字段对齐到 11 字符（与上面 name/persona/permission 的视觉宽度匹配，
        # 让 [[guest]] 段读起来连成一块）；api_key_env 自己 11 字符，会占满。
        if "isolation" in g:
            lines.append(f"isolation  = {_toml_basic_string(str(g['isolation']))}")
        for key in LLM_TOML_FIELDS:
            if key in g:
                lines.append(f"{key:<11}= {_render_llm_field(key, g[key])}")
        for entry in g.get("extra_mcp_servers") or []:
            lines.append("")
            lines.append("[[guest.extra_mcp_servers]]")
            lines.append(f"name    = {_toml_basic_string(str(entry['name']))}")
            lines.append(f"command = {_toml_basic_string(str(entry['command']))}")
            if "args" in entry:
                lines.append(
                    f"args    = {_toml_inline_string_array(list(entry['args']))}"
                )
            if "env" in entry:
                lines.append(
                    f"env     = {_toml_inline_string_table(dict(entry['env']))}"
                )
    return "\n".join(lines) + "\n"


def write_room_toml(room_dir: Path, snapshot: TomlSnapshot) -> None:
    """把 ``snapshot`` 整段渲染并重写 ``room_dir/room.toml``（不校验）。

    原文件如有手工注释 / 排版会被丢弃（见模块 docstring）。校验路径走
    :func:`_rewrite_and_validate`（mutator 端）或 :func:`update_room_toml`（raw editor 端），
    本函数仅是落盘。
    """
    write_text_atomic(room_dir / "room.toml", _render_room_toml(snapshot))


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
    scoring_llm / summary_llm + 每位 guest 的 name / persona / permission / isolation /
    LLM 三件套 + extra_mcp_servers。

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
        "scoring": _llm_spec_to_dict(rc.scoring_llm) if rc.scoring_llm else None,
        "summary": _llm_spec_to_dict(rc.summary_llm) if rc.summary_llm else None,
        "guests": guests,
    }


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


# ── guest CRUD ───────────────────────────────────────────────────────────


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
    snapshot: TomlSnapshot,
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


def update_room_llm(
    *, paths: Paths, room_dir: Path,
    section: Literal["scoring", "summary"],
    spec_dict: Optional[dict[str, Any]],
) -> RoomConfig:
    """覆盖 ``[scoring]`` / ``[summary]`` 顶层 LLM 段。``spec_dict=None`` → 删整段。

    非 None 走 :meth:`LLMSpec.from_toml` 预校验；失败 raise 不动盘。
    """
    if section not in ("scoring", "summary"):
        raise RoomConfigError(
            f"update_room_llm: section 必须是 'scoring' / 'summary'，得到 {section!r}"
        )
    snapshot = _read_existing_for_mutate(room_dir, paths)
    if spec_dict is None:
        snapshot[section] = None  # type: ignore[literal-required]
    else:
        snapshot[section] = _validate_llm_spec_dict(  # type: ignore[literal-required]
            spec_dict, label=f"[{section}]"
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


# ── USER.md / 头像 mutator ────────────────────────────────────────────────


# USER.md 体量上限：64KB —— USER.md 本质是几段偏好 + 自我介绍，正常用户写 1KB 不到。
# 设上限是防御浏览器端 textarea 被脚本灌进去 / 误粘整个文档；超就拒，不静默截断。
_USER_MD_MAX_BYTES = 64 * 1024
# 头像上限：1.5MB（前端已经 canvas 压缩到 256px PNG，正常 ~50KB；上限是兜底防御客户端
# 没压就直传整张原图 + base64 体积膨胀 4/3 倍）。
_AVATAR_MAX_BYTES = 1_500_000
# PNG 文件头 magic bytes（RFC 2083 §3.1）。`with_suffix(".png")` 的搜索约定让我们只接
# 受 PNG —— 其他格式（JPEG / WebP）写进 USER.png 会让 sidebar 渲染 broken image。
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _user_md_target(paths: Paths, source: Optional[Path]) -> Path:
    """USER.md 写盘目标：优先沿用 load_user_md 命中的 source（保留用户已有布局，
    比如 room 级 USER.md 或 explicit override）；都没有则落到 user_data_root/USER.md。
    """
    return source if source is not None else paths.user_data_root / "USER.md"


def update_user_md(paths: Paths, content: str, *, source: Optional[Path] = None) -> Path:
    """覆盖 USER.md 内容，返回实际写入的路径。

    内容超 :data:`_USER_MD_MAX_BYTES` → ValueError（防误粘整文档）。
    tmp + rename 保证写一半被 kill 不会留下半截 md。
    """
    encoded = content.encode("utf-8")
    if len(encoded) > _USER_MD_MAX_BYTES:
        raise ValueError(
            f"USER.md 太大（{len(encoded)} bytes，上限 {_USER_MD_MAX_BYTES}）"
        )
    target = _user_md_target(paths, source)
    write_text_atomic(target, content)
    return target


def parse_png_data_uri(data_uri: str) -> bytes:
    """`data:image/png;base64,...` → raw PNG bytes。

    严格验：必须是 PNG data URI、base64 合法、magic bytes 命中、体积在上限内。
    任何一项不过 → ValueError，调用方决定怎么 emit 给前端。
    """
    prefix = "data:image/png;base64,"
    if not data_uri.startswith(prefix):
        raise ValueError("仅接受 PNG data URI（前端 canvas.toDataURL('image/png')）")
    try:
        png = base64.b64decode(data_uri[len(prefix):], validate=True)
    except binascii.Error as e:
        raise ValueError(f"base64 解码失败：{e}") from e
    if len(png) > _AVATAR_MAX_BYTES:
        raise ValueError(
            f"头像太大（{len(png)} bytes，上限 {_AVATAR_MAX_BYTES}）"
        )
    if not png.startswith(_PNG_MAGIC):
        raise ValueError("文件头不是 PNG（magic bytes 不匹配）")
    return png


# room.toml 体量上限：64KB。正常房间 < 2KB；上限挡误粘整个文档。
_ROOM_TOML_MAX_BYTES = 64 * 1024


def update_room_toml(room_dir: Path, content: str, *, paths: Paths) -> RoomConfig:
    """覆盖 ``room_dir/room.toml`` 全文 + 重加载校验；失败回滚到旧文本。

    与 add_guest / update_guest_permission 那条「结构化 mutator」路径并存 —— 这里是
    「raw editor」入口（前端 textarea 直编），让用户能改 [room] / [[guest]] 任何字段，
    包括 add/remove_guest 无法直接表达的 rules 修订。底层共用 :func:`_write_text_and_validate`
    的"写 + 校验 + 回滚旧 bytes" helper。
    """
    encoded = content.encode("utf-8")
    if len(encoded) > _ROOM_TOML_MAX_BYTES:
        raise ValueError(
            f"room.toml 太大（{len(encoded)} bytes，上限 {_ROOM_TOML_MAX_BYTES}）"
        )
    return _write_text_and_validate(room_dir, content, paths=paths)


def update_user_avatar(
    paths: Paths, png_bytes: bytes, *, source: Optional[Path] = None
) -> Path:
    """覆盖用户头像 PNG，返回实际写入的路径。

    sibling 约定：`USER.md` 同名 sibling `.png`。source 缺省时落 user_data_root/USER.png。
    写完清 :func:`read_avatar_data_uri` 的 lru_cache —— 否则 sidebar 还会显示旧编码。
    """
    md_target = _user_md_target(paths, source)
    target = md_target.with_suffix(".png")
    write_bytes_atomic(target, png_bytes)
    # 没法按 key evict（lru_cache API 限制），全清最便宜：6 张图（5 茶客 + 1 用户）
    # 重 base64 不到 1ms。前提是用户头像也走同一 cache —— 见 user_md.py 的委托。
    read_avatar_data_uri.cache_clear()
    return target
