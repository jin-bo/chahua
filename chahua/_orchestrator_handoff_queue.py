""":class:`HandoffQueueOps` —— Orchestrator 的 handoff 队列 slot（P7.1）。

slot 拆分 Step 2：把 ``_handoff_queue`` 状态 + ``enqueue_handoff`` /
``clear_handoff_queue`` / ``has_pending_handoff`` 三方法 + 队列相关的两个 emit
helper（``_emit_handoff_queue_snapshot`` / ``_emit_handoff_consumed``）搬进
本 slot。主类经 ``@property _handoff_queue`` + 5 个薄转发方法保 API 兼容。

模式仿 :mod:`.server_inbound_handoff` 的 ``HandoffHandlers`` —— 组合而非多继承，
slot 持 ``self.orch`` 反向引用，跨 orchestrator 状态走 ``self.orch.xxx``
显式 hop。

不变量保留（CLAUDE.md handoff §）：
- ``enqueue_handoff`` / ``clear_handoff_queue`` **不 emit**；envelope emit 是
  server inbound handler / drain / MTS 各自的职责。
- ``_emit_handoff_queue_snapshot`` 重发整份权威快照（``turn_id=None`` 连接级）。
- ``_emit_handoff_consumed`` 顶层 ``turn_id`` 与本轮 turn_start / turn_end 同值。
"""

from __future__ import annotations

from collections import deque
from typing import TYPE_CHECKING

from .events import (
    ChahuaEnvelope,
    ChahuaEventType,
    EnvelopeSink,
    emit_to_sink,
)
from .handoff import HandoffItem

if TYPE_CHECKING:
    from .orchestrator import Orchestrator


class HandoffQueueOps:
    """handoff 队列状态 + 队列相关 envelope emit。

    P7.1 显式 handoff 队列：FIFO，新指派 append 队尾，drain loop 取队首。
    内存瞬态——crash/重启即丢，用户重指即可；落盘的复杂度（与 transcript
    顺序一致性 / cancel 时机）不值（docs/P7 §3.1）。``reset_room`` 同清。
    """

    def __init__(self, orch: "Orchestrator") -> None:
        self.orch = orch
        self._queue: deque[HandoffItem] = deque()

    # ── 队列管理 ───────────────────────────────────────────────────────

    def enqueue_handoff(self, item: HandoffItem) -> list[HandoffItem]:
        """append 到队尾；**不**启动执行 / 不 emit envelope。

        envelope emit 职责由 server inbound handler 负责（P7.1.6 加）：handler
        拿到本方法返回的队列快照后 emit ``HANDOFF_ENQUEUED``；orchestrator 持
        sink 是 P7 反向评审 v3-#4 明确避免的越权耦合。
        """
        self._queue.append(item)
        return list(self._queue)

    def clear_handoff_queue(self) -> list[HandoffItem]:
        """清空队列；返回被丢项给 server emit ``HANDOFF_CLEARED``。"""
        dropped = list(self._queue)
        self._queue.clear()
        return dropped

    @property
    def has_pending_handoff(self) -> bool:
        """``_queue`` 是否有待跑项。MTS 开启前的「调度起点干净」校验读它
        （docs/P8.3 §3.2）—— 队列里残留的手动 delegate 会被 MTS 的 `_advance` 误当
        worker 回合计入预算。"""
        return bool(self._queue)

    # ── envelope emit helpers ────────────────────────────────────────

    def emit_handoff_consumed(
        self, sink: EnvelopeSink, *, turn_id: str, item_dict: dict,
    ) -> None:
        """``HANDOFF_CONSUMED`` envelope：顶层 ``turn_id`` 与本轮 turn_start / turn_end
        同值（关联取证），``data={"item": <HandoffItem.to_dict()>}``。
        """
        emit_to_sink(
            sink,
            ChahuaEnvelope(
                room_id=self.orch.room.name, turn_id=turn_id,
                guest_name=None, message_id=None,
                type=ChahuaEventType.HANDOFF_CONSUMED,
                data={"item": item_dict},
            ),
        )

    def emit_handoff_queue_snapshot(self, sink: EnvelopeSink) -> None:
        """重发 ``HANDOFF_ENQUEUED`` 权威快照（连接级，``turn_id`` 全 None）。

        drain 内静默 drop 跑不起来的队列项后必须调一次——前端队列镜像只认
        ``HANDOFF_ENQUEUED`` / ``HANDOFF_CONSUMED`` / ``HANDOFF_CLEARED`` 三事件，
        ``_advance_to_runnable_handoff`` 直接 ``popleft`` 死项不发事件会让队列预览
        残留死项、后续 consumed 又对不上位（codex review P2）。``HANDOFF_ENQUEUED``
        的语义本就是"权威快照、整体替换"（见 ``handoff_state.js``），重发即修正。
        """
        emit_to_sink(
            sink,
            ChahuaEnvelope(
                room_id=self.orch.room.name, turn_id=None,
                guest_name=None, message_id=None,
                type=ChahuaEventType.HANDOFF_ENQUEUED,
                data={"queue": [i.to_dict() for i in self._queue]},
            ),
        )
