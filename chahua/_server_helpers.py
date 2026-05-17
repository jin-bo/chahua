"""Server 模块级 inbound payload 校验小工具（P5.2 重构抽出）。

`server.py` 与各 ``server_inbound_*.py`` mixin 都用 —— 集中放这避免每个 mixin 文件
重新声明，也避免 mixin ←→ server.py 循环 import。运行时只读 ``data`` dict + 写日志，
无 server / session 状态依赖。
"""

from __future__ import annotations

import logging
from typing import Optional

_log = logging.getLogger(__name__)


def require_str(
    data: dict, key: str, *, where: str, allow_empty: bool = False
) -> Optional[str]:
    """从入站 payload 取一个 str 字段，校验失败 → WARN + 返回 ``None``。

    ``where`` 取 ``INBOUND_*`` 常量值 —— 让 WARN 日志一眼看出是哪条 wire 帧不合法。
    ``allow_empty=True`` 给 ``content`` 这种允许空串的字段（用户清空 USER.md 等）。
    """
    v = data.get(key)
    if not isinstance(v, str) or (not allow_empty and not v):
        _log.warning(
            "ignoring %s: %s 必须是%sstr，收到 %r",
            where, key, "" if allow_empty else "非空 ", v,
        )
        return None
    return v


def require_bool(data: dict, key: str, *, where: str) -> Optional[bool]:
    """同 :func:`require_str` —— 取 bool 字段，非 bool → WARN + None。"""
    v = data.get(key)
    if not isinstance(v, bool):
        _log.warning("ignoring %s: %s 必须是 bool，收到 %r", where, key, type(v))
        return None
    return v


def require_list(data: dict, key: str, *, where: str) -> Optional[list]:
    """同 :func:`require_str` —— 取 list 字段。空 list 是合法值（语义"清整段"），不拒。"""
    v = data.get(key)
    if not isinstance(v, list):
        _log.warning("ignoring %s: %s 必须是 list，收到 %r", where, key, type(v))
        return None
    return v


def check_optional_dict(data: dict, key: str, *, where: str) -> bool:
    """``data[key]`` 必须是 dict 或缺/null；其它类型 → WARN + ``False``（让 caller 丢弃帧）。
    本身不取值 —— 用 ``data.get(key)`` 拿；只表态校验过了。
    """
    v = data.get(key)
    if v is not None and not isinstance(v, dict):
        _log.warning(
            "ignoring %s: %s 必须是对象 / null，收到 %r", where, key, type(v)
        )
        return False
    return True


def check_keys_whitelist(
    data: dict,
    allowed: frozenset[str],
    *,
    where: str,
) -> Optional[str]:
    """严格白名单：payload 顶层只接 ``allowed`` 里的字段（``type`` 已被 dispatcher 吃）。

    合法 → ``None``；多余键 → 返回错误文案（caller emit NOTICE + 丢帧）。任务 inbound
    用 —— 等价 ``require_str`` 同款 "校验失败给个反馈" 接口。
    """
    extra = set(data) - allowed - {"type"}
    if not extra:
        return None
    return f"{where}: 未知字段 {sorted(extra)!r}"
