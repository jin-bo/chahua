"""显式 handoff / delegation 数据契约（P7.1，docs/P7-显式 handoff 与 delegation.md §3.1）。

意愿打分（"想接话"自然讨论）之上的**确定性发言指派通道**。P7.1 只接 delegate
一档；review / panel 留给 P7.2 / P7.3 阶段加新字段（``review_message_id`` /
``targets`` / ``panel_group_id``）+ 新 enum 值。

**承重不变量**（doc §8）：

- handoff 是调度层增量，不改对话原语 —— 仍走一根 ``transcript.jsonl``。
- 队列不落盘（与 in-flight ``_current`` 同口径，crash/重启即丢）。``HandoffItem``
  因此没有 ``from_jsonl`` 入口；只走内存 → ``to_dict`` → envelope 一条路。
- ``reason`` 是**内部备注**，仅进 debug record + 队列预览 hover，**不进茶客 prompt**
  （drain loop / context_renderer 端单独保证）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from .events import now_ms


class HandoffKind(str, Enum):
    """handoff 类型。P7.1 仅 ``delegate``；``review`` / ``panel`` 阶段加。"""

    DELEGATE = "delegate"


HANDOFF_ISSUED_BY_USER = "user"
"""``HandoffItem.issued_by`` 默认值。propose-then-adopt（P7.4）采纳后入队时仍
是用户最终触发，所以恒 ``"user"``；想区分原 proposer 写进 ``reason``。

与 :data:`chahua.user_md.USER_SPEAKER_ID` 同字面值但语义不同：前者是 issuer
标签，后者是 transcript speaker_id —— 同 :data:`chahua.task.MARKED_BY_USER`
的命名口径（issuer / marker tag 与 speaker_id 分立）。
"""


@dataclass(frozen=True, slots=True)
class HandoffItem:
    """确定性发言队列的一项。"""

    kind: HandoffKind
    target: Optional[str] = None
    issued_by: str = HANDOFF_ISSUED_BY_USER
    reason: Optional[str] = None
    created_at_ms: int = field(default_factory=now_ms)

    def to_dict(self) -> dict:
        """JSON-safe wire 形态。送 envelope / debug record metadata 用。

        手写字典与 :meth:`chahua.task.Task.to_jsonl_dict` 同口径——字段顺序固定，
        ``kind`` 用 ``.value`` 显式序列化。P7.2 / P7.3 加 ``review_message_id`` /
        ``targets`` / ``panel_group_id`` 字段时一并在此扩。
        """
        return {
            "kind": self.kind.value,
            "target": self.target,
            "issued_by": self.issued_by,
            "reason": self.reason,
            "created_at_ms": self.created_at_ms,
        }
