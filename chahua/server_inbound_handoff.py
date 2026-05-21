"""显式 handoff inbound handlers（P7.1.6 / P7.2 / P7.3，docs/P7 §3.2 / §3.4 / §4.4）。

P5.2 起 ``server.py`` 的 inbound handler 按 feature 切到独立 handler 类。本文件持
四个显式 handoff inbound（delegate / review / panel / clear）+ 调度层共享尾段
``_enqueue_handoff_and_maybe_start`` + handoff envelope 助手。

:class:`chahua.server.ChahuaServer` 在 ``__init__`` 里实例化为
``self.handoff = HandoffHandlers(self)``；本类持 ``self.server`` 反向引用，跨模块
协作（``_emit_notice`` / ``_reject_unknown_keys`` / ``_cancel_and_drain_inflight`` /
``_set_inflight`` / ``_run_handoff_turn`` 等）走 ``self.server.xxx`` 显式 hop —— 与
:class:`chahua.server_inbound_task.TaskHandlers` 同口径。
"""

from __future__ import annotations

import asyncio

from ._server_helpers import require_str as _require_str
from .events import (
    ChahuaEnvelope,
    ChahuaEventType,
    EnvelopeSink,
    NOTICE_LEVEL_ERROR,
    NOTICE_LEVEL_INFO,
)
from .handoff import (
    MANAGED_SESSION_REASON_USER_CANCEL,
    MANAGED_SESSION_REASON_USER_STOPPED,
    MAX_MANAGED_BUDGET,
    MAX_PANEL_TARGETS,
    HandoffItem,
    HandoffKind,
)
from .tasks_store import CLOSED_STATUSES


# P7.1.6 / P7.2.6 显式 handoff inbound type 常量（docs/P7 §3.2 / §4.4，docs/P7.2 §3.2）。
# - ``handoff_delegate``：条件性 cancel（in-flight 是 user-turn → drain；
#   是 handoff drain → 只 append 队尾，drain loop while 自然消费）+ 条件性
#   启动 wrapper（已有 handoff drain 在跑 → 不启新 task）。
# - ``handoff_review``：校验完字段后与 delegate 共用
#   ``_enqueue_handoff_and_maybe_start``（抢占 / 入队 / 启动单点维护）。
# - ``handoff_panel``（P7.3）：圆桌——多 panelist + 可选 summarizer，五道校验 +
#   cap 数学；仍是单 item 入队，走同一个 ``_enqueue_handoff_and_maybe_start``。
# - ``handoff_clear``：无差别 cancel + clear（与 cancel 按钮 / add_guest / set_active_task
#   同口径——"全部取消"是 nuclear 操作，反向评审 v3-#3 简化）。
# ``server.py`` 的 ``_INBOUND_ROUTES`` 表 import 这几个常量做 wire 字符串映射。
INBOUND_HANDOFF_DELEGATE = "handoff_delegate"
INBOUND_HANDOFF_REVIEW = "handoff_review"
INBOUND_HANDOFF_PANEL = "handoff_panel"
INBOUND_HANDOFF_CLEAR = "handoff_clear"

# P8.3 托管任务会话（MTS）inbound type 常量（docs/P8.3 §3.2）。归 handoff slot ——
# MTS 跑在 handoff drain loop 上、是同一调度层。``server.py`` 顶 import 再导出。
INBOUND_MANAGED_SESSION_START = "managed_session_start"
INBOUND_MANAGED_SESSION_STOP = "managed_session_stop"

# ``_inflight_kind`` 取值——与 :class:`ChahuaServer` 的 ``Literal["user","handoff"]``
# 类型签名同口径，给 callsite 一个有名字面值，避免散漏改名漂移。``_inflight_kind``
# 三态本身是 server 核心 in-flight 生命周期状态，但这对字面值是 handoff 引入的
# （仅 delegate 抢占判定用得到），故与 handoff inbound 同住。
INFLIGHT_KIND_USER = "user"
INFLIGHT_KIND_HANDOFF = "handoff"

# Payload 白名单——任何不在集合里的顶层键 → NOTICE error + 丢帧（docs §8.1
# "入站严格 / 落盘宽容"）。
_HANDOFF_DELEGATE_ALLOWED = frozenset({"type", "target", "reason"})
_HANDOFF_REVIEW_ALLOWED = frozenset({"type", "target", "message_id"})
_HANDOFF_PANEL_ALLOWED = frozenset({"type", "targets", "summarizer"})
_HANDOFF_CLEAR_ALLOWED = frozenset({"type"})
_MANAGED_SESSION_START_ALLOWED = frozenset(
    {"type", "task_id", "manager_guest", "budget"}
)
_MANAGED_SESSION_STOP_ALLOWED = frozenset({"type"})


class HandoffHandlers:
    """四个显式 handoff inbound + 调度层共享尾段 + handoff envelope 助手。

    持 :class:`chahua.server.ChahuaServer` 反向引用做依赖注入 —— 所有跨 server 状态
    （``_session`` / ``_emit_notice`` / ``_reject_unknown_keys`` / in-flight 生命周期
    ``_inflight_turn_task`` / ``_inflight_kind`` / ``_set_inflight`` /
    ``_cancel_and_drain_inflight`` / ``_run_handoff_turn``）走 ``self.server.xxx``。
    """

    def __init__(self, server: "ChahuaServer") -> None:  # type: ignore[name-defined]
        self.server = server

    def _emit_handoff_envelope(
        self, sink: EnvelopeSink, *, type: ChahuaEventType, data: dict,
    ) -> None:
        """连接级 handoff envelope（``turn_id`` / ``message_id`` / ``guest_name``
        全 None）。``HANDOFF_ENQUEUED`` / ``HANDOFF_CLEARED`` 共用；
        ``HANDOFF_CONSUMED`` 由 orchestrator 自己 emit（带 turn_id）。
        """
        sink(ChahuaEnvelope(
            room_id=self.server._session.room.name,
            turn_id=None, guest_name=None, message_id=None,
            type=type, data=data,
        ))

    async def _inbound_handoff_delegate(
        self, data: dict, sink: EnvelopeSink,
    ) -> None:
        """``{"type":"handoff_delegate","target":"<name>","reason":"<optional>"}``。

        条件性 cancel（in-flight 是 user-turn → drain；handoff drain → 不动）+
        入队 + emit ``HANDOFF_ENQUEUED`` + 条件性启动 wrapper（已有 drain 在跑
        则不启）。docs §4.4 反向评审 v3-#1。
        """
        if not self.server._reject_unknown_keys(
            data, _HANDOFF_DELEGATE_ALLOWED,
            where=INBOUND_HANDOFF_DELEGATE, sink=sink,
        ):
            return
        target = _require_str(data, "target", where=INBOUND_HANDOFF_DELEGATE)
        if target is None:
            return
        reason = data.get("reason")
        if reason is not None and not isinstance(reason, str):
            self.server._emit_notice(
                sink, level=NOTICE_LEVEL_ERROR,
                text=f"{INBOUND_HANDOFF_DELEGATE}: reason 必须是 str 或缺省",
            )
            return
        if target not in self.server._session.orchestrator.guest_names:
            self.server._emit_notice(
                sink, level=NOTICE_LEVEL_ERROR,
                text=f"{INBOUND_HANDOFF_DELEGATE}: target={target!r} 不在场",
            )
            return

        item = HandoffItem(
            kind=HandoffKind.DELEGATE, target=target, reason=reason,
        )
        await self._enqueue_handoff_and_maybe_start(item, sink)

    async def _enqueue_handoff_and_maybe_start(
        self, item: HandoffItem, sink: EnvelopeSink,
    ) -> None:
        """handoff inbound 的共享尾段：抢占 in-flight → 入队 → emit
        ``HANDOFF_ENQUEUED`` → 条件性启动 drain wrapper。delegate / review
        （/ P7.3 panel）各 handler 校验完自己的字段、构造好 ``HandoffItem`` 后
        都走这里——承重逻辑单点维护（docs/P7 §4.4 反向评审 v3-#1）。

        抢占判定：① user-turn 始终抢；② 已被 cancel / 已 done 的 stale slot 也抢——
        ``_cancel_inflight``（``_inbound_cancel`` 走的就是它）只 ``task.cancel()``
        不 await，slot 在 wrapper finally 跑完前仍指着 dying task；下一拍 inbound
        若先来一帧 handoff，``_inflight_kind == "user"`` 判否、``_inflight_turn_task
        is None`` 也判否 → 新入队项无人消费，队列僵死。``_cancel_and_drain_inflight``
        自身对 done() 早返、对未 done 的 cancel+await，兼容两种 stale 形态。

        已有健康 handoff drain 在跑 → 不启新 task，drain loop while 自然在当前项
        结束后看到队尾新项；stale slot 已在上面被 drain，slot 已 None 会启新 task。
        """
        task = self.server._inflight_turn_task
        stale = task is not None and (task.done() or task.cancelling())
        if self.server._inflight_kind == INFLIGHT_KIND_USER or stale:
            await self.server._cancel_and_drain_inflight()

        snapshot = self.server._session.orchestrator.enqueue_handoff(item)
        self._emit_handoff_envelope(
            sink, type=ChahuaEventType.HANDOFF_ENQUEUED,
            data={"queue": [i.to_dict() for i in snapshot]},
        )

        if self.server._inflight_turn_task is None:
            snapshot_task_id = self.server.task._snapshot_active_task_id()
            self.server._set_inflight(
                asyncio.create_task(
                    self.server._run_handoff_turn(sink, task_id=snapshot_task_id),
                    name="chahua-handoff-turn",
                ),
                INFLIGHT_KIND_HANDOFF,
            )

    async def _inbound_handoff_review(
        self, data: dict, sink: EnvelopeSink,
    ) -> None:
        """``{"type":"handoff_review","target":"<name>","message_id":"<msg>"}``。

        review（请审）：B 下一轮被指派审阅某条已落 transcript 的发言。校验完字段后
        与 delegate 共用 :meth:`_enqueue_handoff_and_maybe_start`（抢占 / 入队 / 启动）。

        三道校验（任一失败 → NOTICE error + 不入队）：target 非空 str / target 在场 /
        message_id 非空 str 且 ``room.message_by_id`` 命中（拒 stale / 伪造 id，
        同时是 docs/P7.2 §4.1"drain 时被审消息保证存在"不变量的工程基础）。
        """
        if not self.server._reject_unknown_keys(
            data, _HANDOFF_REVIEW_ALLOWED,
            where=INBOUND_HANDOFF_REVIEW, sink=sink,
        ):
            return
        # target / message_id 非法都走 NOTICE error（不复用只 WARN 的 _require_str）——
        # docs/P7.2 §3.2 三道校验"任一失败 → NOTICE error"，静默丢帧会让坏 payload
        # "看起来什么都没发生"。
        target = data.get("target")
        if not isinstance(target, str) or not target:
            self.server._emit_notice(
                sink, level=NOTICE_LEVEL_ERROR,
                text=f"{INBOUND_HANDOFF_REVIEW}: target 必须是非空 str",
            )
            return
        if target not in self.server._session.orchestrator.guest_names:
            self.server._emit_notice(
                sink, level=NOTICE_LEVEL_ERROR,
                text=f"{INBOUND_HANDOFF_REVIEW}: target={target!r} 不在场",
            )
            return
        message_id = data.get("message_id")
        if not isinstance(message_id, str) or not message_id:
            self.server._emit_notice(
                sink, level=NOTICE_LEVEL_ERROR,
                text=f"{INBOUND_HANDOFF_REVIEW}: message_id 必须是非空 str",
            )
            return
        if self.server._session.room.message_by_id(message_id) is None:
            self.server._emit_notice(
                sink, level=NOTICE_LEVEL_ERROR,
                text=f"{INBOUND_HANDOFF_REVIEW}: message_id={message_id!r} 不存在",
            )
            return

        item = HandoffItem(
            kind=HandoffKind.REVIEW, target=target, review_message_id=message_id,
        )
        await self._enqueue_handoff_and_maybe_start(item, sink)

    async def _inbound_handoff_panel(
        self, data: dict, sink: EnvelopeSink,
    ) -> None:
        """``{"type":"handoff_panel","targets":[...],"summarizer":"<name>"?}``。

        圆桌（P7.3）：N 位 panelist + 可选 summarizer 在同一个 turn 串行各发一次。
        五道校验（任一失败 → NOTICE error + 不入队，docs/P7.3 §3.3）：targets 是
        ``list[非空 str]`` / ``len ≥ 2`` / 无重复且全在场 / summarizer（若有）在场
        且不在 targets / cap 数学（panel turn 的 N(+1) 轮 ≤
        ``max_consecutive_ai_turns``）。校验通过后构造**一个** ``HandoffItem``，与
        delegate / review 共用 :meth:`_enqueue_handoff_and_maybe_start`。
        """
        if not self.server._reject_unknown_keys(
            data, _HANDOFF_PANEL_ALLOWED,
            where=INBOUND_HANDOFF_PANEL, sink=sink,
        ):
            return

        def _err(text: str) -> None:
            self.server._emit_notice(
                sink, level=NOTICE_LEVEL_ERROR,
                text=f"{INBOUND_HANDOFF_PANEL}: {text}",
            )

        orch = self.server._session.orchestrator
        guest_names = orch.guest_names
        # ① targets 是 list[非空 str]。
        targets = data.get("targets")
        if not isinstance(targets, list) or not all(
            isinstance(t, str) and t for t in targets
        ):
            _err("targets 必须是非空字符串的列表")
            return
        # ② panel 至少 2 人（单人圆桌语义为零、等价 delegate，应当用 /delegate）。
        if len(targets) < 2:
            _err("圆桌至少需要 2 位茶客")
            return
        # ③ 无重复 + 全在场。
        if len(set(targets)) != len(targets):
            _err("targets 含重复茶客")
            return
        absent = [t for t in targets if t not in guest_names]
        if absent:
            _err(f"targets 含不在场茶客：{absent}")
            return
        # ④ summarizer（若有）非空 str、在场、不在 targets——summarizer 汇总的是
        #    **别人**的圆桌发言，自指会让 <panel_summary_request> 语义混乱。
        summarizer = data.get("summarizer")
        if summarizer is not None:
            if not isinstance(summarizer, str) or not summarizer:
                _err("summarizer 必须是非空 str 或缺省")
                return
            if summarizer not in guest_names:
                _err(f"summarizer={summarizer!r} 不在场")
                return
            if summarizer in targets:
                _err("summarizer 不能同时是 panelist")
                return
        # ⑤ cap 数学：panel turn 的 cost=N(+1) 必须 ≤ max_consecutive_ai_turns，
        #    叠加 MAX_PANEL_TARGETS token 上限，取小。这是"能不能跑"的静态判断；
        #    "此刻跑不跑得动"由 drain while-entry cap 检查负责（docs §3.3）。
        has_summ = 1 if summarizer else 0
        max_ai = orch.config.max_consecutive_ai_turns
        cap = min(MAX_PANEL_TARGETS, max_ai - has_summ)
        if len(targets) > cap:
            _err(
                f"圆桌人数 {len(targets)} 超出上限 {cap}（受 "
                f"max_consecutive_ai_turns={max_ai} 与 "
                f"MAX_PANEL_TARGETS={MAX_PANEL_TARGETS} 约束）；"
                "请减少茶客或去掉汇总者"
            )
            return

        item = HandoffItem(
            kind=HandoffKind.PANEL,
            targets=tuple(targets),
            summarizer=summarizer,
        )
        await self._enqueue_handoff_and_maybe_start(item, sink)

    async def _inbound_handoff_clear(
        self, data: dict, sink: EnvelopeSink,
    ) -> None:
        """``{"type":"handoff_clear"}``——无差别 cancel + clear。

        "全部取消"是 nuclear 操作（docs §3.3 反向评审 v3-#3）：cancel 当前
        in-flight（无论 user-turn 还是 handoff drain，复用 cancel 按钮 / add_guest
        同口径）+ 清队列剩余 + emit ``HANDOFF_CLEARED``。
        """
        if not self.server._reject_unknown_keys(
            data, _HANDOFF_CLEAR_ALLOWED,
            where=INBOUND_HANDOFF_CLEAR, sink=sink,
        ):
            return
        await self.server._cancel_and_drain_inflight()
        orch = self.server._session.orchestrator
        dropped = orch.clear_handoff_queue()
        self._emit_handoff_envelope(
            sink, type=ChahuaEventType.HANDOFF_CLEARED,
            data={"items_dropped": [i.to_dict() for i in dropped]},
        )
        # P8.3：用户「全部取消」中途介入托管会话 → 一并结束 MTS（user_cancel）。
        # 队列已上面清空，end_managed_session 此时只补 managed_session_ended（docs §3.3）。
        if orch.managed_session is not None:
            orch.end_managed_session(
                sink, reason=MANAGED_SESSION_REASON_USER_CANCEL,
            )

    async def _inbound_managed_session_start(
        self, data: dict, sink: EnvelopeSink,
    ) -> None:
        """``{"type":"managed_session_start","task_id","manager_guest","budget"}``。

        开启托管任务会话（MTS，docs/P8.3 §3.2）。四道校验（任一失败 → NOTICE error
        + 不开启）：① ``task_id`` 非空 str 且 == 当前 active task 且任务非终结态；
        ② ``manager_guest`` 非空 str 且在场（茶客）；③ ``budget`` 是
        ``1..MAX_MANAGED_BUDGET`` 整数；④ 当前无 MTS 在跑。

        校验通过：建 ``ManagedSession`` → ``enqueue_handoff(delegate(manager))`` →
        走既有 ``_enqueue_handoff_and_maybe_start`` 启动 drain。
        """
        if not self.server._reject_unknown_keys(
            data, _MANAGED_SESSION_START_ALLOWED,
            where=INBOUND_MANAGED_SESSION_START, sink=sink,
        ):
            return

        def _err(text: str) -> None:
            self.server._emit_notice(
                sink, level=NOTICE_LEVEL_ERROR,
                text=f"{INBOUND_MANAGED_SESSION_START}: {text}",
            )

        orch = self.server._session.orchestrator
        # ④ 已有 MTS：直接拒绝、不替换（换管理者 / 预算先 stop 再 start，docs §6.6）。
        if orch.managed_session is not None:
            _err("已有托管会话在运行；请先停止再开启")
            return
        # ⑤ 调度起点必须干净：已有 handoff 在排队 / drain 时拒绝。否则 MTS kickoff 被
        # append 到既有 handoff 之后，那些既有项跑完会被 `_advance_managed_session_after_turn`
        # 误当 MTS worker 回合计入预算 —— 要求用户先「全部取消」再开（Codex review P2）。
        srv = self.server
        inflight_is_handoff = (
            srv._inflight_kind == INFLIGHT_KIND_HANDOFF
            and srv._inflight_turn_task is not None
            and not srv._inflight_turn_task.done()
        )
        if orch.has_pending_handoff or inflight_is_handoff:
            _err("已有指派 / 圆桌在排队或运行 —— 请先「全部取消」再开启托管")
            return
        # ① task_id 非空 str 且 == 当前 active task 且非终结态。
        task_id = data.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            _err("task_id 必须是非空 str")
            return
        store = self.server._session.tasks_store
        if task_id != store.active_task_id:
            _err(f"task_id={task_id!r} 不是当前 active 任务")
            return
        task = store.get_task(task_id)
        if task is None or task.status in CLOSED_STATUSES:
            _err(f"task_id={task_id!r} 不存在或已是终结态")
            return
        # ② manager_guest 非空 str 且在场。
        manager_guest = data.get("manager_guest")
        if not isinstance(manager_guest, str) or not manager_guest:
            _err("manager_guest 必须是非空 str")
            return
        if manager_guest not in orch.guest_names:
            _err(f"manager_guest={manager_guest!r} 不在场")
            return
        # ③ budget 是 1..MAX_MANAGED_BUDGET 整数（bool 是 int 子类，显式排除）。
        budget = data.get("budget")
        if (
            not isinstance(budget, int)
            or isinstance(budget, bool)
            or not (1 <= budget <= MAX_MANAGED_BUDGET)
        ):
            _err(f"budget 必须是 1..{MAX_MANAGED_BUDGET} 的整数")
            return

        orch.start_managed_session(
            sink, task_id=task_id, manager_guest=manager_guest, budget=budget,
        )
        item = HandoffItem(
            kind=HandoffKind.DELEGATE, target=manager_guest, reason="托管启动",
        )
        await self._enqueue_handoff_and_maybe_start(item, sink)

    async def _inbound_managed_session_stop(
        self, data: dict, sink: EnvelopeSink,
    ) -> None:
        """``{"type":"managed_session_stop"}``——结束托管会话（docs/P8.3 §3.3）。

        一种 stop 行为：结束 MTS（``user_stopped``）+ 清 ``_handoff_queue``，
        **不取消**当前 in-flight turn（自然跑完）。无 MTS 时空操作 + NOTICE info。
        要立即掐断当前发言走既有 ``cancel``。
        """
        if not self.server._reject_unknown_keys(
            data, _MANAGED_SESSION_STOP_ALLOWED,
            where=INBOUND_MANAGED_SESSION_STOP, sink=sink,
        ):
            return
        orch = self.server._session.orchestrator
        if orch.managed_session is None:
            self.server._emit_notice(
                sink, level=NOTICE_LEVEL_INFO,
                text=f"{INBOUND_MANAGED_SESSION_STOP}: 当前无托管会话",
            )
            return
        orch.end_managed_session(
            sink, reason=MANAGED_SESSION_REASON_USER_STOPPED,
        )
