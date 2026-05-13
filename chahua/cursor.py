"""guest_cursor —— 每个茶客在房间 transcript 上的"看到哪了"游标（§3.2.2）。

P1 用内存版。P2 落盘到 ``working_directory/.agentao/chahua_cursor.json``
（room_id → last_seen_seq），重启房间能续。

语义：

- ``last_seen_seq = 0`` —— 没看过任何消息，下次喂走 onboarding 路径。
- ``last_seen_seq = N`` —— 看过 ``seq <= N`` 的所有消息。
- 茶客自己刚发的那条也算"看过"，``set()`` 推到自己那条 seq（不需要再回喂）。

游标**只前进不后退** —— 防止某次 bug 把已喂过的内容又喂一遍。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class GuestCursor:
    """茶客名 → last_seen_seq 的映射。P1 单房间一份；P2 跨房间时按 room_id 嵌一层。"""

    _last_seen: dict[str, int] = field(default_factory=dict, init=False, repr=False)

    def get(self, guest_name: str) -> int:
        """未知茶客返回 0（= 走 onboarding）。"""
        return self._last_seen.get(guest_name, 0)

    def set(self, guest_name: str, seq: int) -> None:
        """推进游标。比当前值小则忽略（保单调）。"""
        if seq > self._last_seen.get(guest_name, 0):
            self._last_seen[guest_name] = seq

    def snapshot(self) -> dict[str, int]:
        """供持久化 / 调试用的只读副本。"""
        return dict(self._last_seen)
