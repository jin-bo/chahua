"""P9 阶段 9.1.1 / 9.3.1：:class:`RoomEventRouter` 单测。

覆盖路由模式两态：

- ``foreground``：全量透传到 ``ws_sink``。
- ``background``：精确里程碑白名单 —— 放行 :data:`_BACKGROUND_WHITELIST`、丢弃其余
  （阶段 9.3.1，替换 9.1.1 的 NOOP 占位）。
- ``mode`` 翻转后路由立即跟着变（切房支点：in-flight turn 持同一 router 对象）。
- ``ws_sink`` 可换新（连接级 sink，ws 重连场景）。
"""

from __future__ import annotations

import pytest

from chahua.events import ChahuaEnvelope, ChahuaEventType
from chahua.room_runtime import (
    _BACKGROUND_WHITELIST,
    ROUTER_MODE_BACKGROUND,
    ROUTER_MODE_FOREGROUND,
    RoomEventRouter,
)


def _env(kind: ChahuaEventType = ChahuaEventType.MESSAGE_DELTA) -> ChahuaEnvelope:
    return ChahuaEnvelope(
        room_id="r1",
        turn_id=None,
        guest_name=None,
        message_id=None,
        type=kind,
        data={},
    )


def test_foreground_passes_through() -> None:
    seen: list[ChahuaEnvelope] = []
    router = RoomEventRouter(seen.append)
    assert router.mode == ROUTER_MODE_FOREGROUND  # 默认前台

    e1, e2 = _env(), _env(ChahuaEventType.TURN_END)
    router(e1)
    router(e2)

    assert seen == [e1, e2]


# 后台白名单放行的里程碑事件 —— 与设计文档 §4 表格 / ``_BACKGROUND_WHITELIST`` 同步。
_BACKGROUND_PASS = [
    ChahuaEventType.TURN_START,
    ChahuaEventType.TURN_END,
    ChahuaEventType.MESSAGE_END,
    ChahuaEventType.TASK_INFO,
    ChahuaEventType.TASK_ARTIFACT_ADDED,
    ChahuaEventType.MANAGED_SESSION_STARTED,
    ChahuaEventType.MANAGED_SESSION_ADVANCED,
    ChahuaEventType.MANAGED_SESSION_ENDED,
    ChahuaEventType.ROOM_BACKGROUND_FINISHED,
]

# 后台丢弃的高频流式 / 非里程碑事件。
_BACKGROUND_DROP = [
    ChahuaEventType.MESSAGE_START,
    ChahuaEventType.MESSAGE_DELTA,
    ChahuaEventType.GUEST_THINKING,
    ChahuaEventType.TOOL_START,
    ChahuaEventType.TOOL_COMPLETE,
    ChahuaEventType.TASK_PROPOSAL,
    ChahuaEventType.HANDOFF_CONSUMED,
]


@pytest.mark.parametrize("kind", _BACKGROUND_PASS)
def test_background_passes_milestones(kind: ChahuaEventType) -> None:
    seen: list[ChahuaEnvelope] = []
    router = RoomEventRouter(seen.append, mode=ROUTER_MODE_BACKGROUND)

    e = _env(kind)
    router(e)

    assert seen == [e]


@pytest.mark.parametrize("kind", _BACKGROUND_DROP)
def test_background_drops_streaming(kind: ChahuaEventType) -> None:
    seen: list[ChahuaEnvelope] = []
    router = RoomEventRouter(seen.append, mode=ROUTER_MODE_BACKGROUND)

    router(_env(kind))

    assert seen == []


def test_background_whitelist_matches_doc() -> None:
    """``_BACKGROUND_WHITELIST`` 与本测试枚举的放行集合必须一致 —— 改一处炸另一处。"""
    assert _BACKGROUND_WHITELIST == frozenset(_BACKGROUND_PASS)
    # 放行与丢弃两集合不相交。
    assert _BACKGROUND_WHITELIST.isdisjoint(frozenset(_BACKGROUND_DROP))


def test_mode_flip_changes_routing_live() -> None:
    """切房支点：同一 router 对象，翻 mode 即改路由 —— 无需触碰 in-flight turn。"""
    seen: list[ChahuaEnvelope] = []
    router = RoomEventRouter(seen.append)

    fg = _env()
    router(fg)  # 前台：透传

    router.mode = ROUTER_MODE_BACKGROUND
    router(_env())  # 后台：丢弃

    router.mode = ROUTER_MODE_FOREGROUND
    back = _env()
    router(back)  # 切回前台：恢复透传

    assert seen == [fg, back]


def test_ws_sink_swappable() -> None:
    """``ws_sink`` 是连接级 sink，ws 重连后可被 server 换新。"""
    first: list[ChahuaEnvelope] = []
    second: list[ChahuaEnvelope] = []
    router = RoomEventRouter(first.append)

    a = _env()
    router(a)

    router.ws_sink = second.append
    b = _env()
    router(b)

    assert first == [a]
    assert second == [b]
