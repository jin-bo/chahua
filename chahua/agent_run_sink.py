"""P11：``BatchMessageSink`` —— bg run 茶客发言过程的事件过滤 / 缓冲 sink。

设计见 [`docs/P11-后台 Agent.md`](../docs/P11-后台 Agent.md) §「BatchMessageSink 事件白名单」。

后台 Agent run 期间**不流式刷聊天区**：``TeaGuest.speak()`` 经 ``ChahuaTransport.bind``
产生的所有 envelope 都先过本 sink，按白名单分流：

============================  ==============================================
类型                           处理
============================  ==============================================
``message_start`` / ``_delta``  丢（后台 run 不要 UI 闪烁的"打字进度"）
``tool_*`` / ``guest_thinking``  丢（debug 取证另算，不进 UI）
``turn_*``                       丢（bg run 不形成房间 turn）
``message_end(ok)``              放（**补 ``data.run_id``**——前端按它关联结果气泡）
``message_end(error/cancelled)`` 丢（由 ``agent_run_error/cancelled`` 表达）
``task_artifact_added`` /
``task_info`` /
``room_artifact_added``          丢（统一由 wrapper finally 经
                                 ``_kick_detect_new_artifacts`` 走 router 直发）
``handoff_*`` /
``managed_session_*``            丢（bg target = MTS manager 边界场景下 hook 可能
                                 emit；P11.1 不让 bg sink 触发前端调度层 UI 刷新）
``agent_run_*`` /
``room_background_finished``     丢（这些 envelope 由 server / wrapper 主动发出,
                                 绝不该在 bind 期内出现；显式 DROP 防穿越）
``TASK_PROPOSAL``                **缓冲**——run 完成后 ``flush_to(router)``
``NOTICE``                       放
============================  ==============================================

通过设计：wrapper finally 顺序 `detect → flush propose → pop registry → emit terminal`
保证 ``agent_run_finished`` 帧在 propose 卡片之后到达，前端关 UI / 移列表条不会
错过中间产生的 propose。
"""

from __future__ import annotations

from typing import Callable

from .events import ChahuaEnvelope, ChahuaEventType, EnvelopeSink, STATUS_OK


# 白名单中"丢"的类型显式枚举 —— 改这个集合即改 bg run 的事件可见性。
# 其余（不在 PASS / DROP / BUFFER / END 分流中的未来 envelope 类型）走默认 DROP：
# 安全默认是"丢"——比新类型悄悄漏到 UI 引发奇怪闪烁强。
_DROP: frozenset[ChahuaEventType] = frozenset({
    ChahuaEventType.MESSAGE_START,
    ChahuaEventType.MESSAGE_DELTA,
    ChahuaEventType.GUEST_THINKING,
    ChahuaEventType.TOOL_START,
    ChahuaEventType.TOOL_COMPLETE,
    ChahuaEventType.TURN_START,
    ChahuaEventType.TURN_END,
    # artifact / task 系列由 wrapper finally 统一产出，bind 期内丢弃避免重复 emit。
    ChahuaEventType.TASK_ARTIFACT_ADDED,
    ChahuaEventType.TASK_INFO,
    ChahuaEventType.TASK_ARTIFACTS_CLEARED,
    ChahuaEventType.ROOM_ARTIFACT_ADDED,
    # task 状态 hint 同上 —— wrapper finally 经 task_info 整批刷新。
    ChahuaEventType.TASK_OPEN,
    ChahuaEventType.TASK_UPDATE,
    ChahuaEventType.TASK_DECISION_ADDED,
    ChahuaEventType.TASK_CLOSE,
    # 调度层 hint：当 bg target 恰好是 MTS manager 时，MTS hook 可能在 bind 期内
    # 触发 enqueue_handoff_queue_snapshot；P11.1 不让 bg sink 把这些 envelope 顶到
    # 前端调度 UI（避免「bg 跑着却看到 handoff 队列变化」错位）。后台状态变更靠下次
    # 前台 turn 自然重发。
    ChahuaEventType.HANDOFF_ENQUEUED,
    ChahuaEventType.HANDOFF_CONSUMED,
    ChahuaEventType.HANDOFF_CLEARED,
    ChahuaEventType.MANAGED_SESSION_STARTED,
    ChahuaEventType.MANAGED_SESSION_ADVANCED,
    ChahuaEventType.MANAGED_SESSION_ENDED,
    # bg run / 后台 runtime 自身的里程碑 —— 这些 envelope 是 server / wrapper 主动
    # 发出，绝不该在 bind 期内出现。显式 DROP 防穿越（万一未来某 emit 路径漏掉
    # bind 退出检查就让前端看见死循环式的 agent_run_started）。
    ChahuaEventType.AGENT_RUN_STARTED,
    ChahuaEventType.AGENT_RUN_FINISHED,
    ChahuaEventType.AGENT_RUN_CANCELLED,
    ChahuaEventType.AGENT_RUN_ERROR,
    ChahuaEventType.ROOM_BACKGROUND_FINISHED,
})

# 白名单中"放"的类型 —— bg run 也想让前端看见的少数实时事件。
_PASS: frozenset[ChahuaEventType] = frozenset({
    ChahuaEventType.NOTICE,
})


class BatchMessageSink:
    """envelope 中转 sink —— 包真 router，按白名单分流。

    ``run_id``：所有放行的 ``message_end(ok)`` envelope 补 ``data.run_id``，前端拿来
    把结果气泡与运行列表条解关联。``flush_to(router)`` 把缓冲的 ``TASK_PROPOSAL``
    一次性下发；wrapper finally 的「flush propose」步骤经它走。

    线程模型：bind / speak 全程在同一 asyncio 单线程内调用，buffer 不加锁。
    """

    def __init__(self, *, run_id: str, downstream: EnvelopeSink) -> None:
        self._run_id = run_id
        self._downstream = downstream
        # TASK_PROPOSAL 缓冲。flush 后整体清空——重复 flush 是 no-op。
        self._proposal_buffer: list[ChahuaEnvelope] = []

    def __call__(self, env: ChahuaEnvelope) -> None:
        """投递一条 envelope —— 由 ``ChahuaTransport.emit_chahua`` 转译后落到此。"""
        t = env.type
        if t in _DROP:
            return
        if t is ChahuaEventType.MESSAGE_END:
            # 只放成功路径；失败 / 取消由 wrapper finally emit AGENT_RUN_ERROR/CANCELLED。
            if env.status != STATUS_OK:
                return
            self._downstream(_with_run_id(env, self._run_id))
            return
        if t is ChahuaEventType.TASK_PROPOSAL:
            # 缓冲到 run 完成后 flush —— 让 agent_run_finished 早于 propose 卡到达
            # （前端去掉运行列表条比渲采纳卡先到位，UI 顺序更顺）。
            self._proposal_buffer.append(env)
            return
        if t in _PASS:
            self._downstream(env)
            return
        # 默认 DROP —— 未来新 envelope 类型若需放行，必须显式加入 _PASS。

    def flush_to(self, router: EnvelopeSink) -> None:
        """把缓冲的 ``TASK_PROPOSAL`` 一次性投到 ``router`` —— wrapper finally 唯一调用方。

        ``router`` 通常 ≡ ``self._downstream``（同一 ``runtime.router``）；签名分开是为
        测试 / wrapper finally 的语义清晰：bind 已退出，sink 不再可用、必须经 runtime
        router 直发。重复 flush 是 no-op（buffer 已清）。
        """
        if not self._proposal_buffer:
            return
        for env in self._proposal_buffer:
            router(env)
        self._proposal_buffer.clear()


def _with_run_id(env: ChahuaEnvelope, run_id: str) -> ChahuaEnvelope:
    """返回 ``data`` 加上 ``run_id`` 后的新 envelope —— ``ChahuaEnvelope`` 是
    ``frozen=True`` 的 dataclass，不能原地改。

    既有 ``data`` 字段优先保留；``run_id`` 若已存在（理论上不该）以现值为准。
    """
    if "run_id" in env.data:
        return env
    new_data = dict(env.data)
    new_data["run_id"] = run_id
    return ChahuaEnvelope(
        room_id=env.room_id,
        turn_id=env.turn_id,
        guest_name=env.guest_name,
        message_id=env.message_id,
        type=env.type,
        status=env.status,
        ts_ms=env.ts_ms,
        data=new_data,
        seq=env.seq,
    )
