"""显式 handoff / delegation 数据契约（P7.1 / P7.2 / P7.3，docs/P7-显式 handoff 与 delegation.md §3.1）。

意愿打分（"想接话"自然讨论）之上的**确定性发言指派通道**。P7.1 接 delegate，
P7.2 加 review（``review_message_id``）；P7.3 加 panel（圆桌）—— ``targets``
（N 位 panelist 元组）+ ``summarizer``（可选汇总者）两字段 + ``PANEL`` enum 值。

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
    """handoff 类型。P7.1 ``delegate``，P7.2 ``review``，P7.3 ``panel``。"""

    DELEGATE = "delegate"
    REVIEW = "review"
    PANEL = "panel"


MAX_PANEL_TARGETS = 4
"""panel 一项的 panelist 数硬上限（P7.3 起步阶段模块常量，不进 toml，docs §5.5）。
不含 summarizer —— summarizer 是第 N+1 位，cap 数学单独把它算进 turn 预算（docs §3.3）。"""


HANDOFF_ISSUED_BY_USER = "user"
"""``HandoffItem.issued_by`` 默认值。propose-then-adopt（P7.4）采纳后入队时仍
是用户最终触发，所以恒 ``"user"``；想区分原 proposer 写进 ``reason``。

与 :data:`chahua.user_md.USER_SPEAKER_ID` 同字面值但语义不同：前者是 issuer
标签，后者是 transcript speaker_id —— 同 :data:`chahua.task.MARKED_BY_USER`
的命名口径（issuer / marker tag 与 speaker_id 分立）。
"""


MAX_MANAGED_BUDGET = 20
"""``managed_session_start`` inbound 的 ``budget`` 上限硬护栏（P8.3，docs §3.1）。
托管会话自循环烧 token 的兜底之一——预算无论用户填多大都不越过这个数。"""

MANAGED_SESSION_DEFAULT_BUDGET = 6
"""前端「托管运行」弹窗的预填预算（P8.3，docs §3.1 / §8.1）。仅 UI 默认值，
后端不依赖——校验只认 ``1..MAX_MANAGED_BUDGET``。"""


# MTS 结束 reason —— ``managed_session_ended`` envelope 的 ``data.reason``（docs §2.2）。
# 封闭 6 值集合，散在 orchestrator / server inbound 多处 emit；立常量同 ``INFLIGHT_KIND_*``
# 的理由——避免字面值散漏改名漂移。前端 ``events.js`` 按字面值镜像。
MANAGED_SESSION_REASON_MANAGER_FINISHED = "manager_finished"
MANAGED_SESSION_REASON_BUDGET_EXHAUSTED = "budget_exhausted"
MANAGED_SESSION_REASON_TASK_CLOSED = "task_closed"
MANAGED_SESSION_REASON_CAP_REACHED = "cap_reached"
MANAGED_SESSION_REASON_USER_STOPPED = "user_stopped"
MANAGED_SESSION_REASON_USER_CANCEL = "user_cancel"


_MTS_SESSION_ID_COUNTER: int = 0


def _mint_mts_session_id() -> int:
    """Codex round 5 P2：MTS 实例的进程内单调递增 id —— 区分跨 MTS 实例的 bg 续命
    场景。用 ``id()`` 不行（CPython 内存地址会被复用），用 counter 保证每个新建的
    ``ManagedSession`` 拿到独一无二的 id（即便前一个刚被 end + GC）。
    """
    global _MTS_SESSION_ID_COUNTER
    _MTS_SESSION_ID_COUNTER += 1
    return _MTS_SESSION_ID_COUNTER


@dataclass
class ManagedSession:
    """托管任务会话（MTS）运行态（P8.3，docs/P8.3-原生自动推进.md §3.1）。

    与 :class:`Orchestrator` 的 ``_handoff_queue`` 同瞬态语义：**不落盘**、crash 即丢、
    ``reset_room`` / 切房即清。单房间最多一个。``_managed_session is None`` 即「无托管」
    ——不另存 ``enabled`` bool（避免「enabled=False 但对象还在」的二义态）。

    非 ``frozen``：``budget`` 每次「worker→管理者」回调扣 1（docs §4.1）。
    """

    task_id: str
    """MTS 驱动的任务 id（= 开启时的 active task）。"""

    manager_guest: str
    """注册的管理者茶客 name（= speaker_id）。"""

    budget: int
    """剩余复查回合数。每次「worker→管理者」回调扣 1；kickoff 不耗（docs §2.2）。"""

    session_id: int = field(default_factory=_mint_mts_session_id)
    """进程内单调递增 id —— Codex round 5 P2 修：``advance_after_bg_completion`` 用
    本字段区分跨 MTS 实例（旧 MTS 的 bg 不能续命到同管理者的新 MTS）。``id()`` 会
    随 GC 复用地址，必须用 counter。"""


@dataclass(frozen=True, slots=True)
class HandoffItem:
    """确定性发言队列的一项。"""

    kind: HandoffKind
    target: Optional[str] = None
    targets: Optional[tuple[str, ...]] = None
    """panel 专用：N 位 panelist 名（≥2，docs §3.1）。delegate / review 项恒
    ``None``。用 ``tuple`` 不用 ``list``——``frozen`` dataclass 只锁字段重绑、不锁
    内部容器，``list`` 会被外部 caller ``append`` 改掉队列里的 targets。"""

    summarizer: Optional[str] = None
    """panel 专用：可选的汇总者名（圆桌末位 speaker）。delegate / review 项恒
    ``None``；panel 项可空可非空。summarizer 是 panel item 自己的字段（不拆成独立
    入队的第二个 item，docs §5.3），与 panel 同生共死，不会留"孤儿 summarizer"。"""

    issued_by: str = HANDOFF_ISSUED_BY_USER
    reason: Optional[str] = None
    review_message_id: Optional[str] = None
    """review 专用：被审消息的 message_id。delegate / panel 项恒 ``None``；review
    项与 ``target`` 同时非空、``reason`` 恒 ``None``（review 无自由文本备注，§2）。
    不做 dataclass 级 kind / 字段互斥校验——构造点只有 inbound handler 一处。"""

    created_at_ms: int = field(default_factory=now_ms)

    def to_dict(self) -> dict:
        """JSON-safe wire 形态。送 envelope / debug record metadata 用。

        手写字典与 :meth:`chahua.task.Task.to_jsonl_dict` 同口径——字段顺序固定，
        ``kind`` 用 ``.value`` 显式序列化；``targets`` 元组转 ``list`` 给 JSON。
        """
        return {
            "kind": self.kind.value,
            "target": self.target,
            "targets": list(self.targets) if self.targets is not None else None,
            "summarizer": self.summarizer,
            "issued_by": self.issued_by,
            "reason": self.reason,
            "review_message_id": self.review_message_id,
            "created_at_ms": self.created_at_ms,
        }
