"""``room.toml`` 写出层 —— mini 序列化器（P5.2 重构从 admin.py 拆出）。

只覆盖本仓库 schema（``[room]`` + ``[room.llm]`` + ``[scoring]`` + ``[summary]`` +
``[[guest]]`` + ``[[guest.extra_mcp_servers]]``）。**不引 ``tomli_w``**：

- schema 极简，手写 ~50 行；引依赖反而把 wheel 体积撑大。
- 任何手工注释 / 字段顺序在重写时无论如何都会丢，标准库 ``tomllib`` 也只读不带注释 ——
  不欠用户"保留注释"的承诺，文档里写明"通过 UI 改了房间后 room.toml 会被规范化"即可。

调用方（``admin.py`` 各 mutator）拼出 :class:`TomlSnapshot` → :func:`write_room_toml`
落盘。无 IO 部分纯函数（:func:`_render_room_toml`），方便单测。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional, Sequence, TypedDict

from ._persist import write_text_atomic
from .config import ORCH_FIELD_BOUNDS
from .llm_spec import LLM_TOML_FIELDS, LLM_TOML_NUMERIC_FIELDS, LLMSpec
from .permissions import DEFAULT_MODE


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
    room_llm: Optional[dict]                     # None = 不写 [room.llm]（P4.9）
    scoring: Optional[dict]                      # None = 不写 [scoring]（P4.1）
    summary: Optional[dict]                      # None = 不写 [summary]（P4.1）
    debug: Optional[dict]                        # None = 不写 [debug]（P6.1；默认全 True）
    guests: list[GuestSnapshot]


# render 时认得的 guest 字段。
_ALLOWED_GUEST_EMIT: frozenset[str] = (
    frozenset({"name", "persona", "permission", "isolation", "extra_mcp_servers"})
    | frozenset(LLM_TOML_FIELDS)
)


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
    """TOML 字面量。bool 先于 int 判（``True/False`` 是 ``int`` 子类），float 走 ``repr``
    保留精度。bool 走 ``"true"``/``"false"`` —— P6.1 起 ``[debug]`` 段两个字段是 bool，
    单点支持；早先的 ``NotImplementedError`` 是占位（"等用到时再实现"），现在实现了。
    """
    if isinstance(value, bool):
        return "true" if value else "false"
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

    # [room.llm] 房间默认 LLM 段（P4.9）。写在 [room] 标量字段之后、其它 section 之前 ——
    # tomllib 把 ``[room.llm]`` 解析为 ``room["llm"] = {...}`` 子表，物理顺序与 "[room]
    # 标量字段后再开 dotted 子表" 一致，用户读 toml 时 LLM 段不会切断 [room] 标量块。
    room_llm_dict = snapshot.get("room_llm")
    if room_llm_dict:
        lines.append("")
        lines.append("[room.llm]")
        for key in LLM_TOML_FIELDS:
            if key in room_llm_dict:
                lines.append(f"{key} = {_render_llm_field(key, room_llm_dict[key])}")

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

    # [debug] 段（P6.1）。snapshot 只在 **非默认**（用户显式写过）时携带 ——
    # 见 ``_room_config_to_dict`` 的 ``_debug_config_to_snapshot`` 推断逻辑；如此
    # 1) 默认房间 toml 不被结构化重写时塞进 ``[debug]`` 噪声；
    # 2) 用户写过 ``enabled = false`` / ``capture_prompts = false`` 经 mutator
    #    （如 update_guest_permission）round-trip 后保住设定，不被默认 ``true/true``
    #    悄悄覆盖。
    debug_dict = snapshot.get("debug")
    if debug_dict:
        lines.append("")
        lines.append("[debug]")
        # 字段顺序固定 enabled / capture_prompts / max_turns —— 与 DebugConfig 字段
        # 声明顺序一致，用户读 toml 时 [debug] 段视觉稳定（先开关后数值上限）。
        for key in ("enabled", "capture_prompts", "max_turns"):
            if key in debug_dict:
                lines.append(f"{key} = {_format_toml_scalar(debug_dict[key])}")

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
