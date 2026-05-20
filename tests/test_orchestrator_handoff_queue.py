"""``Orchestrator._handoff_queue`` + ``enqueue_handoff`` / ``clear_handoff_queue``
+ ``reset_room`` 清队列（P7.1.4，docs/P7 §3.4）。

P7.1.4 单纯把队列字段 + 两个 pure 方法落进 orchestrator，**不**起 drain（drain 在
P7.1.5）。所以这里的测点只覆盖队列状态变更 + 与 ``reset_room`` 的同步关系。
"""

from __future__ import annotations

from chahua.handoff import HandoffItem, HandoffKind
from chahua.orchestrator import Orchestrator

from tests.conftest import build_orch


def _delegate(target: str, reason: str | None = None) -> HandoffItem:
    return HandoffItem(kind=HandoffKind.DELEGATE, target=target, reason=reason)


def test_enqueue_returns_full_snapshot() -> None:
    """每次 enqueue 返回的 snapshot 都是当前**整个**队列（不是单项）—— inbound
    handler 拿这个直接 emit ``HANDOFF_ENQUEUED.data.queue`` 整体覆盖前端 state。"""
    orch = build_orch("A", "B")
    snap_a = orch.enqueue_handoff(_delegate("A"))
    snap_b = orch.enqueue_handoff(_delegate("B", "B 收尾"))
    assert [i.target for i in snap_a] == ["A"]
    assert [i.target for i in snap_b] == ["A", "B"]
    # snapshot 是 list 拷贝——caller 改 list 不影响 orchestrator 内部 deque。
    snap_b.clear()
    assert len(orch._handoff_queue) == 2


def test_clear_returns_dropped_and_empties_queue() -> None:
    """``clear_handoff_queue`` 返回被丢项 list（给 envelope ``items_dropped``
    用）；返回后队列内部清空。"""
    orch = build_orch("A", "B")
    orch.enqueue_handoff(_delegate("A"))
    orch.enqueue_handoff(_delegate("B"))
    dropped = orch.clear_handoff_queue()
    assert [i.target for i in dropped] == ["A", "B"]
    assert len(orch._handoff_queue) == 0
    # 再清一次空队列 → 空 list（不抛）。
    assert orch.clear_handoff_queue() == []


def test_reset_room_clears_handoff_queue() -> None:
    """``reset_room`` 必须同步清队列（与 transcript / cursor / summarizer 同口径）。
    否则用户点"清空"后下一句仍是 clear 前的 delegate 目标。"""
    orch = build_orch("A", "B")
    orch.enqueue_handoff(_delegate("A"))
    orch.enqueue_handoff(_delegate("B"))
    assert len(orch._handoff_queue) == 2
    orch.reset_room()
    assert len(orch._handoff_queue) == 0


def test_enqueue_handoff_signature_is_sinkless() -> None:
    """``enqueue_handoff`` 必须**不**接 sink 形参 —— P7 反向评审 v3-#4 钉死的
    职责边界：envelope emit 由 server inbound handler 负责，orchestrator 持
    sink 是越权耦合。改方法签名时回归这条。"""
    import inspect
    params = inspect.signature(Orchestrator.enqueue_handoff).parameters
    assert list(params) == ["self", "item"]
