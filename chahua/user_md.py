"""USER.md 解析（设计文档 §3.8）。

USER.md 是用户自己的角色卡。三级位置回退：

1. ``room.toml`` 里 ``[room].user_md`` 显式指定的路径
2. ``rooms/<room-id>/USER.md``（房间级覆盖）
3. 仓库顶层 ``USER.md``（全局默认）

唯一硬要求字段是 ``## 显示名`` —— 它驱动 transcript 渲染和 UI 上"用户"那行显示成什么。
缺失或全部三层都没文件时，``display_name`` 回退为字面 "用户"，茶客也就不会知道你是谁，
但聊天能跑。

P0 在 REPL 启动时加载一次；P1 接入意愿打分主循环后改为按需 reload（设计 §3.8.5 的
"每轮 reload"，让 USER.md 编辑后下一条消息生效，不重启房间）。本模块**不缓存** ——
缓存是上层 orchestrator 的事。本期函数实现得很便宜（一次 read_text + 一遍正则）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from ._paths import resolve_under

# 稳定 ID —— transcript.jsonl 的 speaker 字段存这个，渲染时再查 display_name。
# 改 USER.md 改名不会污染历史 jsonl（§3.8.3）。
USER_SPEAKER_ID = "user"

# 没有任何 USER.md / 没有 ## 显示名 段时的回退显示名。
DEFAULT_DISPLAY_NAME = "用户"

# 匹配 ``## 显示名\n<content>\n`` 段。锚定 ^/$ 以多行模式，避免误吞下一段标题。
_DISPLAY_NAME_RE = re.compile(
    r"^##\s*显示名\s*$\n+([^\n#][^\n]*)",
    re.MULTILINE,
)

# 打分 prompt 注入的"用户偏好"段排除哪些 H2（## 显示名 已经体现在显示名字段里，
# 反复出现在 prompt 浪费 token）。可在未来扩展更多过滤项。
_PREFERENCE_OMIT_H2: frozenset[str] = frozenset({"显示名"})


@dataclass(frozen=True)
class UserConfig:
    """USER.md 解析结果。"""

    display_name: str
    """用户在 transcript / UI / 喂养 prompt 里的显示名。无配置时回退 "用户"。"""

    full_md: Optional[str]
    """USER.md 完整原文（含一级标题），用于 onboarding 注入。无文件则 None。"""

    source: Optional[Path]
    """实际命中的文件路径；都没命中则 None。供 UI / 日志显示用。"""

    preferences_block: str = ""
    """``full_md`` 中除 :data:`_PREFERENCE_OMIT_H2` 外的 ## 段，预拼好的字符串。

    打分 prompt 每次都要这个；预算一次，省每轮 N 次正则。``load_user_md`` 写入。
    无 USER.md → 空串。
    """

    @property
    def has_persona(self) -> bool:
        """是否有可注入的用户角色信息（决定 onboarding 要不要附"关于「X」"块）。"""
        return self.full_md is not None


def strip_top_h1(md: str) -> str:
    """去掉 USER.md 的首行 ``# xxx``（一级标题）；其余原样保留。

    Onboarding 与打分注入都不希望反复出现"# USER.md"这种装饰性标题 —— 这里集中处理。
    一级标题后紧跟的空行也吃掉，避免拼出来视觉上有大空隙。
    """
    lines = md.splitlines()
    if lines and lines[0].lstrip().startswith("# "):
        lines = lines[1:]
        while lines and not lines[0].strip():
            lines = lines[1:]
    return "\n".join(lines)


def _resolve_path(
    repo_root: Path,
    room_dir: Optional[Path],
    explicit: Optional[str],
) -> Optional[Path]:
    """三级回退按顺序探测，返回第一个存在的文件。"""
    candidates: list[Path] = []

    if explicit:
        # explicit 可以是绝对路径，也可以是相对于 repo_root 的相对路径
        # （room.toml 在 rooms/<id>/ 下，但 user_md 路径以 repo 根为基准更直观）。
        candidates.append(resolve_under(repo_root, explicit))

    if room_dir is not None:
        candidates.append(room_dir / "USER.md")

    candidates.append(repo_root / "USER.md")

    for c in candidates:
        if c.is_file():
            return c
    return None


def _extract_display_name(md: str) -> Optional[str]:
    """从 USER.md 里抓 ## 显示名 段的第一行内容。找不到返回 None。"""
    m = _DISPLAY_NAME_RE.search(md)
    if not m:
        return None
    name = m.group(1).strip()
    return name or None


def _extract_preferences_block(md: str) -> str:
    """把 USER.md 里**用户偏好相关**的 ## 段拼成一段字符串。

    切分按 H2（``## XXX``）走；跳过 :data:`_PREFERENCE_OMIT_H2` 内的标题。
    用于打分 prompt 注入（§3.8.3）—— 让"想不想接话"考虑用户偏好（语气 / 忌讳……）。
    """
    body = strip_top_h1(md)
    chunks = re.split(r"(?m)^##\s+", body)
    kept: list[str] = []
    for c in chunks:
        c = c.strip()
        if not c:
            continue
        title_line, _, rest = c.partition("\n")
        title = title_line.strip()
        if title in _PREFERENCE_OMIT_H2:
            continue
        kept.append(f"## {title}\n{rest.strip()}")
    return "\n\n".join(kept).strip()


def load_user_md(
    repo_root: Path,
    room_dir: Optional[Path] = None,
    explicit: Optional[str] = None,
) -> UserConfig:
    """读取 USER.md（三级回退），返回 :class:`UserConfig`。

    Args:
        repo_root: 仓库根目录（顶层 USER.md 的所在）。
        room_dir: 房间目录 ``rooms/<room-id>/``，若有房间级 USER.md 优先。
        explicit: ``room.toml`` ``[room].user_md`` 字段的值，最高优先级。

    永远不抛异常 —— 文件读取失败按"没有 USER.md"处理。茶话室聊天能跑是底线。
    """
    path = _resolve_path(repo_root, room_dir, explicit)
    if path is None:
        return UserConfig(
            display_name=DEFAULT_DISPLAY_NAME,
            full_md=None,
            source=None,
        )

    try:
        md = path.read_text(encoding="utf-8")
    except OSError:
        # 文件存在但读不了（权限、编码……）—— 按"没有"处理，不让 IO 错误炸掉房间。
        return UserConfig(
            display_name=DEFAULT_DISPLAY_NAME,
            full_md=None,
            source=None,
        )

    display = _extract_display_name(md)
    return UserConfig(
        display_name=display or DEFAULT_DISPLAY_NAME,
        full_md=md,
        source=path,
        preferences_block=_extract_preferences_block(md),
    )
