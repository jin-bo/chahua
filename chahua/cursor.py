"""guest_cursor —— 每个茶客在房间 transcript 上的"看到哪了"游标（§3.2.2 / §3.7）。

P2.1 落盘形态：单房间一份 ``rooms/<id>/cursor.json``，整体重写。设计文档里写"每茶客
自己的 ``.agentao/chahua_cursor.json``"是为了 isolation=global 茶客跨房间留游标 ——
P2.1 还没接 isolation 分流，单文件够用；P4 拆 isolation=global 时再分。

语义：

- ``last_seen_seq = 0`` —— 没看过任何消息，下次喂走 onboarding 路径。
- ``last_seen_seq = N`` —— 看过 ``seq <= N`` 的所有消息。
- 茶客自己刚发的那条也算"看过"，``set()`` 推到自己那条 seq（不需要再回喂）。

游标**只前进不后退** —— 防止某次 bug 把已喂过的内容又喂一遍。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from ._persist import read_json_or_none, write_json_atomic

_log = logging.getLogger(__name__)


@dataclass
class GuestCursor:
    """茶客名 → last_seen_seq 的映射。可选落盘到 ``cursor_path``。"""

    cursor_path: Optional[Path] = None
    """``rooms/<id>/cursor.json``。``None`` = 纯内存（测试）。"""

    _last_seen: dict[str, int] = field(default_factory=dict, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.cursor_path is None:
            return
        self.cursor_path.parent.mkdir(parents=True, exist_ok=True)
        raw = read_json_or_none(self.cursor_path)
        if not isinstance(raw, dict):
            return
        # 一一过滤，坏值（非 int / 非 str key）一律忽略 + WARN，避免 toml 手改后炸。
        for k, v in raw.items():
            if isinstance(k, str) and isinstance(v, int) and v >= 0:
                self._last_seen[k] = v
            else:
                _log.warning(
                    "cursor.json: ignore entry %r: %r (need str→int>=0)", k, v
                )

    def get(self, guest_name: str) -> int:
        """未知茶客返回 0（= 走 onboarding）。"""
        return self._last_seen.get(guest_name, 0)

    def set(self, guest_name: str, seq: int) -> None:
        """推进游标。比当前值小则忽略（保单调）。改动时整体重写 ``cursor.json``。"""
        if seq <= self._last_seen.get(guest_name, 0):
            return
        self._last_seen[guest_name] = seq
        if self.cursor_path is not None:
            # 文件 <1KB，整体原子重写最省心；append-only json 单行重写反而要锁。
            write_json_atomic(self.cursor_path, self._last_seen)

    def snapshot(self) -> dict[str, int]:
        """供持久化 / 调试用的只读副本。"""
        return dict(self._last_seen)
