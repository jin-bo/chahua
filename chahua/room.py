"""Room —— 房间公共 transcript（设计文档 §3.2 / §3.8）。

P0 是**纯内存** transcript。落盘到 ``transcript.jsonl`` 是 P2 的事。

设计要点：

- ``speaker_id`` 存**稳定 ID**（``"user"`` / 茶客名），渲染时再查 display_name。
  改 USER.md 改名不污染历史。
- ``seq`` 单调递增，从 1 开始。``guest_cursor`` 用它判断"上次喂到哪了"。
- ``message_id`` 是茶话室自己分配的、整条流式消息共享的 ID（前端 envelope 用，§3.5.1）；
  在 :meth:`append` 阶段就生成，不依赖外部传入。
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field
from typing import Optional


def _new_message_id() -> str:
    """msg_<10字节 hex> —— 比 UUID 短，比 uuid4 更易扫读。"""
    return "msg_" + secrets.token_hex(10)


@dataclass(frozen=True)
class Message:
    """房间公共 transcript 的一条记录。"""

    seq: int
    speaker_id: str
    """稳定 ID。`"user"` 或茶客 name。**不存 display_name**，渲染时再解析（§3.8.3）。"""

    text: str
    ts_ms: int
    """毫秒级时间戳，前端排序用。"""

    message_id: str


@dataclass
class Room:
    """房间。P0 只关心 transcript 和参与者；持久化/广播都是后面阶段。"""

    name: str
    """房间显示名，如「深夜茶话室」。"""

    topic: str = ""
    rules: str = ""
    """房间规则的自由文本，注入 onboarding 用（P1 才用到，P0 先占字段）。"""

    _messages: list[Message] = field(default_factory=list, init=False, repr=False)
    _participants: list[str] = field(default_factory=list, init=False, repr=False)
    """参与者顺序保留：用户在最前（speaker_id="user"），茶客按加入顺序。"""

    # ── 参与者管理 ──────────────────────────────────────────────────────────

    def add_participant(self, speaker_id: str) -> None:
        """把一个稳定 ID 加进参与者列表。重复加无副作用（幂等）。"""
        if speaker_id not in self._participants:
            self._participants.append(speaker_id)

    @property
    def participants(self) -> tuple[str, ...]:
        """当前在场的稳定 ID 列表（顺序保留）。返回 tuple 避免外部改。"""
        return tuple(self._participants)

    # ── transcript 读写 ─────────────────────────────────────────────────────

    def append(self, speaker_id: str, text: str) -> Message:
        """追加一条发言，返回带 seq / message_id 的 :class:`Message`。

        ``speaker_id`` 必须在 :attr:`participants` 里 —— 不允许凭空冒出一个名字，
        这是防 "茶客 A 在 transcript 里假冒茶客 B" 的最便宜兜底（注入打分以外的另一类风险）。
        """
        if speaker_id not in self._participants:
            raise ValueError(
                f"speaker_id={speaker_id!r} 不在房间参与者 {self._participants} 中；"
                "请先 add_participant"
            )

        msg = Message(
            seq=len(self._messages) + 1,
            speaker_id=speaker_id,
            text=text,
            ts_ms=int(time.time() * 1000),
            message_id=_new_message_id(),
        )
        self._messages.append(msg)
        return msg

    def messages_since(self, last_seq: int) -> list[Message]:
        """返回 ``seq > last_seq`` 的所有消息。``last_seq=0`` 表示"从头"。"""
        # seq 从 1 起密集递增 → _messages[i].seq == i + 1，直接切片 O(k)。
        offset = max(last_seq, 0)
        return list(self._messages[offset:])

    @property
    def latest_seq(self) -> int:
        """最后一条消息的 seq；空房间返回 0。"""
        return self._messages[-1].seq if self._messages else 0

    def __len__(self) -> int:
        return len(self._messages)
