"""``@`` 提及解析（broadcast token + 短语边界 + 名字匹配）。

从 :mod:`chahua.orchestrator` 抽出。规则细节见原模块顶部注释：边界用白名单（不是
``isalnum`` 取反）以过滤 URL / email / 复合词；空白单独走 ``str.isspace()`` 以覆盖
全 Unicode 空白（CJK 全角 ``　``、nbsp ``\\xa0`` 等）。
"""

from __future__ import annotations

from typing import Iterator

BROADCAST_TOKENS: frozenset[str] = frozenset(
    {"all", "everyone", "大家", "各位", "所有人"}
)
"""``@all`` / ``@大家`` 等触发全员发言一次的 broadcast token，英文大小写无关。"""


# 短语边界标点 —— 必须是**短语级别**分隔（空白 / 句末标点 / 开闭括号引号），不是**词内**
# 衔接字符（``-`` ``_`` ``/`` 等）。用白名单而非 isalnum 取反是为了过滤：
#   - URL：``https://x.com/@Elon/status``（``@`` 左边是 ``/`` → 不起匹配）
#   - 复合词：``@all-hands``（``-`` 不是边界 → 不算 broadcast）
#   - email：``support@all.com``（``@`` 左边是字母 → 不起匹配）
_PHRASE_BOUNDARY_PUNCT: frozenset[str] = frozenset(
    "，。！？、；：…"  # 中文句末
    ",.!?;:"  # 英文句末
    "（）「」『』【】《》"  # 中文括号
    "()[]{}"  # 英文括号
    "\"'“”‘’"  # 引号
    "~～"  # tilde
)


def is_phrase_boundary_char(ch: str) -> bool:
    return ch.isspace() or ch in _PHRASE_BOUNDARY_PUNCT


def is_phrase_boundary(text: str, idx: int) -> bool:
    """``text[idx]`` 是不是短语边界（end-of-string 或空白 / 句末标点 / 括号引号）。"""
    if idx >= len(text):
        return True
    return is_phrase_boundary_char(text[idx])


def iter_at_positions(text: str) -> Iterator[int]:
    """yield ``text`` 里**作为提及起点**的 ``@`` 下标。

    要求 ``@`` 左边是字符串起点或短语边界字符，过滤 email / URL 里 ``@`` 紧跟字母或
    路径符的情况（``foo@x.com``、``https://x.com/@Elon`` 都不算提及）。
    """
    i = text.find("@")
    while i >= 0:
        if i == 0 or is_phrase_boundary_char(text[i - 1]):
            yield i
        i = text.find("@", i + 1)


def matches_at(text: str, start: int, token: str, *, case_insensitive: bool = False) -> bool:
    """``text[start:]`` 是否以 ``token`` 起头且后面是名字边界。``case_insensitive``
    给 broadcast token 用（``@ALL`` 也算）；茶客名走默认大小写敏感。"""
    end = start + len(token)
    slice_ = text[start:end]
    if case_insensitive:
        slice_ = slice_.lower()
    if slice_ != token:
        return False
    return is_phrase_boundary(text, end)
