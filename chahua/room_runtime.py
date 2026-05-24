"""P9：进程内房间运行态层 —— ``RoomRuntime`` + ``RoomEventRouter``。

设计见 [`docs/P9-切房后房间后台续跑.md`](../docs/P9-切房后房间后台续跑.md)。
本模块是 P9 阶段 9.1.1 的「地基」骨架：纯新增、未接线，server 仍走旧的
``self._session`` / ``_inflight_*`` 路径。后续阶段（9.1.2~9.1.3）才把 server
切到注册表 + 把 turn 的 sink 改传 ``runtime.router``。

两个核心概念：

- :class:`RoomEventRouter` —— **per-room 可变路由 sink**。turn 启动时拿到的不是裸
  ws sink，而是这个稳定对象；切房只翻它的 ``mode``，in-flight turn 调用栈里捕获的
  仍是同一 router，事件路由自动跟着 ``mode`` 变 —— 解决「sink 是栈参数、运行中的
  turn 无法中途换 sink」这个最硬的卡点。
- :class:`RoomRuntime` —— 把「一个房间的运行态」（``session`` + ``router`` +
  in-flight 槽）打包，让 server 从「持 1 个 session」升级为「持多个 runtime」。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Literal, Optional

from .events import ChahuaEnvelope, ChahuaEventType, EnvelopeSink
from .session import RoomSession

# in-flight turn 的类型标签。与 :data:`chahua.server.INFLIGHT_KIND_*` 同值域 ——
# 此处用 ``Literal`` 表达，运行期值由 server 传入。
InflightKind = Literal["user", "handoff"]

# RoomEventRouter 的两种路由模式。
ROUTER_MODE_FOREGROUND = "foreground"
ROUTER_MODE_BACKGROUND = "background"
RouterMode = Literal["foreground", "background"]

# 后台房间事件白名单（设计文档 §4 / 阶段 9.3.1）。后台 router 只放行**里程碑**事件
# —— 房间列表「进行中」徽标 / 任务产物变化 / MTS 推进 / runtime 自毁通知；高频流式
# （``message_start`` / ``message_delta`` / ``guest_thinking`` / ``tool_*``）一律丢
# 弃。后台发言的**内容**不靠这些事件，靠切回时的 ``room_history`` 快照（从盘重建、
# 完整无损）。改这个集合即改后台房间「能感知到什么」，与设计文档 §4 的表格同步。
_BACKGROUND_WHITELIST: frozenset[ChahuaEventType] = frozenset({
    ChahuaEventType.TURN_START,
    ChahuaEventType.TURN_END,
    ChahuaEventType.MESSAGE_END,
    ChahuaEventType.TASK_INFO,
    ChahuaEventType.TASK_ARTIFACT_ADDED,
    ChahuaEventType.ROOM_ARTIFACT_ADDED,
    ChahuaEventType.MANAGED_SESSION_STARTED,
    ChahuaEventType.MANAGED_SESSION_ADVANCED,
    ChahuaEventType.MANAGED_SESSION_ENDED,
    ChahuaEventType.ROOM_BACKGROUND_FINISHED,
})


class RoomEventRouter:
    """per-room 可变路由 sink —— 本身就是一个 :data:`~chahua.events.EnvelopeSink`。

    turn / handoff drain 启动时传的 sink 永远是某个 runtime 的 router（一个稳定对
    象），不是裸 ws sink。切房 = 翻 router 的 ``mode``：

    - ``foreground``：全量透传到 ``ws_sink`` —— 与 P9 之前的行为完全一致。
    - ``background``：只放行 :data:`_BACKGROUND_WHITELIST` 里的里程碑事件（阶段
      9.3.1），高频流式事件丢弃；后台发言**内容**靠切回时的 ``room_history`` 快照
      补全。

    单房间场景下 router 永远 ``foreground``，所以 9.1 全程行为不变。

    注意：``ws_sink`` 是**连接级**的真实 sink，可在 ws 重连后被 server 换新；
    turn 调用栈持有的是 router 对象本身，故换 ``ws_sink`` 不影响 in-flight turn。
    """

    __slots__ = ("mode", "ws_sink")

    def __init__(
        self, ws_sink: EnvelopeSink, *, mode: RouterMode = ROUTER_MODE_FOREGROUND,
    ) -> None:
        self.mode: RouterMode = mode
        self.ws_sink: EnvelopeSink = ws_sink

    def __call__(self, env: ChahuaEnvelope) -> None:
        """投递一条 envelope —— ``foreground`` 全量透传，``background`` 只放行里程碑。"""
        if self.mode == ROUTER_MODE_FOREGROUND:
            self.ws_sink(env)
        elif env.type in _BACKGROUND_WHITELIST:
            # background：只放行里程碑事件，高频流式一律丢弃（设计文档 §4）。
            self.ws_sink(env)


@dataclass
class RoomRuntime:
    """一个房间的进程内运行态 —— 封装 server 上原本散落的 per-room 状态。

    ``session`` 仍是 :func:`~chahua.session.build_room_session` 的 frozen 产物，
    ``RoomRuntime`` 只在它外面包一层。

    **关键不变量**：server 注册表里一个 ``room_id`` 最多 1 个 ``RoomRuntime``
    —— 不允许同一房间「前台一份 + 后台一份」并存。
    """

    room_id: str
    session: RoomSession
    router: RoomEventRouter
    # 当前在跑的 turn / handoff drain task。``None`` 表示该房间空闲。
    inflight_task: Optional[asyncio.Task[None]] = None
    # 与 ``inflight_task`` 同生命周期的类型标签，两字段必须 same-time live or
    # same-time None（见 :meth:`set_inflight` 的 assertion）。
    inflight_kind: Optional[InflightKind] = None
    # 转入后台的 Unix 毫秒时间戳；前台房间为 ``None``。``max_background_rooms``
    # 超限淘汰时取最小者（阶段 9.4.1）。
    background_since_ms: Optional[int] = None

    def inflight_alive(self) -> bool:
        """该房间是否有 turn / handoff drain 正在跑。"""
        return self.inflight_task is not None and not self.inflight_task.done()

    def set_inflight(
        self,
        task: Optional[asyncio.Task[None]],
        kind: Optional[InflightKind],
    ) -> None:
        """单点设/清 ``(inflight_task, inflight_kind)`` 这对耦合状态。

        两字段必须 same-time live or same-time None —— assertion 把漂移在写入点
        炸出来（原 ``ChahuaServer._set_inflight`` 的语义，P9 下沉到 runtime）。
        """
        assert (task is None) == (kind is None), (task, kind)
        self.inflight_task = task
        self.inflight_kind = kind

    def cancel_inflight(self) -> None:
        """通知当前在跑的 turn task 退场，不 await —— cancel 入口要尽快返回。

        task 完成由 turn wrapper 的 ``finally`` 清 ``inflight_task``。
        """
        task = self.inflight_task
        if task is not None and not task.done():
            task.cancel()

    async def cancel_and_drain_inflight(self) -> None:
        """cancel 当前 turn task **并等它收尾**。

        切房 / clear_room / 连接断开走这条路径 —— 它们要在 task 完全退出后再继续
        操作 session，否则 orchestrator 还在写 transcript / cursor，新 session
        装配会撞上。重复调用幂等（task 已 done 直接返回）。
        """
        task = self.inflight_task
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            # turn wrapper 自己 swallow 了 Cancelled / Exception，正常情况这里
            # await 不会再抛；保留兜底是为 cancel→reraise 极小竞态窗口。
            pass

    def close(self) -> None:
        """关停该房间 session（所有茶客 agentao close）。重复调用应静默放过。"""
        self.session.close()
