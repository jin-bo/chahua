"""P9 阶段 9.1.1：:class:`RoomEventRouter` 单测。

覆盖路由模式两态：

- ``foreground``：全量透传到 ``ws_sink``。
- ``background``：阶段 9.1.1 NOOP 占位 —— 丢弃全部事件（精确白名单留到 9.3.1）。
- ``mode`` 翻转后路由立即跟着变（切房支点：in-flight turn 持同一 router 对象）。
- ``ws_sink`` 可换新（连接级 sink，ws 重连场景）。
"""

from __future__ import annotations

from chahua.events import ChahuaEnvelope, ChahuaEventType
from chahua.room_runtime import (
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


def test_background_drops_everything() -> None:
    seen: list[ChahuaEnvelope] = []
    router = RoomEventRouter(seen.append, mode=ROUTER_MODE_BACKGROUND)

    router(_env())
    router(_env(ChahuaEventType.TURN_END))

    # 阶段 9.1.1 background = NOOP，连里程碑都丢（白名单留到 9.3.1）。
    assert seen == []


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
