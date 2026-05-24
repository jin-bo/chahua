""":class:`ManagedSessionOps` —— Orchestrator 的托管任务会话（MTS, P8.3）slot。

slot 拆分 Step 3：把 ``_managed_session`` 状态 + 12 个 MTS 方法（生命周期 / 推进 /
proposal 拦截 / 4 个 emit helper）搬进本 slot。主类经 ``@property _managed_session``
（含 setter，测试直赋）+ ``@property managed_session``（只读公开 API）+ 6 个薄转发
方法保 API 兼容。

不变量保留（CLAUDE.md MTS §）：
- ``_managed_session is None`` 即「无托管」。
- 结束 MTS 必清 ``_handoff_queue`` + 重发空队列快照。
- MTS 跑在 handoff drain loop 上、不新开调度路径 —— ``advance_after_turn`` 由 drain
  每轮 turn 跑完调一次；非 MTS 房间零行为变化。
- ``intercept_task_proposal`` 经 ``TeaGuest.transport.set_task_proposal_hook`` 注入
  （:mod:`.session` ``set_task_proposal_hook`` 调用点 = 主类 ``_intercept_task_proposal``
  薄转发的 bound method，保兼容）。

MTS ↔ handoff queue 强耦合：本 slot **不直接** 持 queue 引用，写队列 / emit 全部
经 ``self.orch._handoff_ops.xxx`` —— MTS 是 queue 的客户、不是 queue 的所有者。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Optional

from .events import (
    TASK_PROPOSAL_KIND_HANDOFF_DELEGATE,
    TASK_PROPOSAL_KIND_HANDOFF_PANEL,
    ChahuaEnvelope,
    ChahuaEventType,
    EnvelopeSink,
    emit_to_sink,
)
from .handoff import (
    MANAGED_SESSION_REASON_BUDGET_EXHAUSTED,
    MANAGED_SESSION_REASON_CAP_REACHED,
    MANAGED_SESSION_REASON_TASK_CLOSED,
    MAX_PANEL_TARGETS,
    HandoffItem,
    HandoffKind,
    ManagedSession,
)
from .tasks_store import CLOSED_STATUSES

if TYPE_CHECKING:
    from .orchestrator import Orchestrator

_log = logging.getLogger(__name__)


class ManagedSessionOps:
    """托管任务会话（MTS）状态机 + 生命周期 emit。

    P8.3：每房间最多 1 个 MTS、瞬态不落盘（crash / reset_room / ws 断开即清）。
    切房（P9）改为转后台续跑 —— 旧前台 busy 时 MTS 不随切房结束、留后台跑到
    budget/task/cap 自然收尾。
    """

    def __init__(self, orch: "Orchestrator") -> None:
        self.orch = orch
        self._managed_session: Optional[ManagedSession] = None

    # ── 公开 API（主类经薄转发暴露） ─────────────────────────────────────

    @property
    def managed_session(self) -> Optional[ManagedSession]:
        """当前托管会话运行态；``None`` = 无托管。server inbound 校验 / 生命周期
        分支读这个公开访问器（``_managed_session`` 私有）。"""
        return self._managed_session

    def emit_managed_session_snapshot(self, sink: EnvelopeSink) -> None:
        """切回托管中的房间时重发 MTS 快照（P9 阶段 9.3.2）。

        P9 之前「``emit_room_snapshot`` 不重投 MTS」成立的前提是「MTS 不跨断线
        存活」；P9 让 MTS 能在后台续跑，前提被推翻 —— 切回一个「托管中」的后台
        房间，前端「托管中」状态条 / 「停止托管」按钮要能自给自足重建，不能依赖
        前端缓存早先收过的 ``managed_session_started``（那只在「MTS 启动时恰好在
        前台」才成立，脆弱）。

        无 MTS → 空操作。复用 ``managed_session_started`` envelope，``budget`` 是
        **当前剩余**预算（已被 ``advance_after_turn`` 扣减过）—— 前端按收到的值复位倒计时。
        """
        ms = self._managed_session
        if ms is not None:
            self._emit_started(sink, ms)

    def start_managed_session(
        self, sink: EnvelopeSink, *, task_id: str, manager_guest: str, budget: int,
    ) -> None:
        """建立 MTS 运行态 + emit ``managed_session_started``（docs §3.2）。

        kickoff 的 ``delegate(manager)`` 入队 + 启动 drain 由 server inbound handler
        负责（走既有 ``_enqueue_handoff_and_maybe_start``）—— 本方法只管会话状态。
        调用方已校验「当前无 MTS」（docs §6.6）。
        """
        self._managed_session = ManagedSession(
            task_id=task_id, manager_guest=manager_guest, budget=budget,
        )
        self._emit_started(sink, self._managed_session)

    def end_managed_session(self, sink: EnvelopeSink, *, reason: str) -> None:
        """结束 MTS（server inbound stop / clear / cancel 入口用，docs §3.3）。

        守卫：无 MTS 时空操作。结束 = 清 ``_managed_session`` + 清 ``_handoff_queue``
        + emit ``managed_session_ended``（docs §4.2 硬约束「结束 MTS 必清待跑队列」）。
        """
        if self._managed_session is None:
            return
        self._managed_session = None
        # 结束 MTS = 不再自动说话：清掉所有待跑项（几乎都是 hook 自动入队的
        # worker / panel）。清后重发空队列快照，前端队列预览不残留（docs §4.2）。
        if self.orch._handoff_ops._queue:
            self.orch._handoff_ops._queue.clear()
            self.orch._handoff_ops.emit_handoff_queue_snapshot(sink)
        self._emit_ended(sink, reason)

    def stop_reason(self) -> Optional[str]:
        """按 docs §2.2 顺序返首个命中的停止 reason，或 ``None``（继续推进）。

        budget → task_closed → cap，谁先命中报谁（docs §4.3「预算 vs cap」）。
        """
        ms = self._managed_session
        if ms is None:
            return None
        if ms.budget <= 0:
            return MANAGED_SESSION_REASON_BUDGET_EXHAUSTED
        if self.orch.tasks_store is not None:
            task = self.orch.tasks_store.get_task(ms.task_id)
            if task is None or task.status in CLOSED_STATUSES:
                return MANAGED_SESSION_REASON_TASK_CLOSED
        if self.orch._consecutive_ai_turns >= self.orch.config.max_consecutive_ai_turns:
            return MANAGED_SESSION_REASON_CAP_REACHED
        return None

    def advance_after_turn(
        self, item: HandoffItem, sink: EnvelopeSink,
    ) -> None:
        """drain loop 每轮 turn 跑完后调一次：续队列 / 收尾（docs §4.1）。

        ``_managed_session is None`` → 立即返回，对非 MTS 房间零行为变化。
        按「刚跑完的是不是管理者回合」分流：管理者回合不做事（其 delegate / panel
        提议已被 ``intercept_task_proposal`` hook 在回合内入队）；worker 回合且队列
        空 → ``budget-=1`` + 把管理者放回队列复查。停止条件优先（budget / task / cap）。
        """
        ms = self._managed_session
        if ms is None:
            return
        stop = self.stop_reason()
        if stop:
            self.end_managed_session(sink, reason=stop)
            return
        just_ran_manager = (
            item.kind is HandoffKind.DELEGATE and item.target == ms.manager_guest
        )
        if just_ran_manager:
            # 管理者回合：提议已在回合内由 hook 入队；没派活则队列此刻为空，drain
            # 走到收尾、由 run_pending_handoff 收尾兜底归 manager_finished。
            return
        # worker 回合：worker 不会自动入队，队列空即「这一棒没有后续」→ 回调管理者。
        if not self.orch._handoff_ops._queue:
            ms.budget -= 1
            self.orch._handoff_ops.enqueue_handoff(
                HandoffItem(
                    kind=HandoffKind.DELEGATE,
                    target=ms.manager_guest,
                    reason="托管复查",
                )
            )
            self.orch._handoff_ops.emit_handoff_queue_snapshot(sink)
            self._emit_advanced(sink, ms)

    def intercept_task_proposal(
        self, env: ChahuaEnvelope, sink: EnvelopeSink,
    ) -> bool:
        """``ChahuaTransport.task_proposal_hook`` 实现（docs §5.1）。

        MTS 内管理者的 ``handoff_delegate`` / ``handoff_panel`` 提议 → 直接
        ``enqueue_handoff`` 并返回 ``True``（拦下该 ``TASK_PROPOSAL`` 不下发前端、
        不渲采纳卡）。非 MTS / 非管理者 / review / decision / status 提议 → 返
        ``False``，照 P7.4 既有路径渲采纳卡。
        """
        ms = self._managed_session
        if ms is None:
            return False
        data = env.data
        if data.get("proposer") != ms.manager_guest:
            return False
        kind = data.get("kind")
        if kind not in (
            TASK_PROPOSAL_KIND_HANDOFF_DELEGATE,
            TASK_PROPOSAL_KIND_HANDOFF_PANEL,
        ):
            return False
        item = self._handoff_item_from_proposal(
            kind, data.get("payload"), manager=ms.manager_guest,
        )
        if item is None:
            # 形状坏（工具已保证形状、理论不达）→ 不拦，照常渲卡兜底。
            return False
        if (
            item.kind is HandoffKind.DELEGATE
            and item.target == ms.manager_guest
        ):
            _log.warning("MTS 管理者 %r 自指派，跳过（不入队不渲卡）", ms.manager_guest)
            return True
        self.orch._handoff_ops.enqueue_handoff(item)
        self.orch._handoff_ops.emit_handoff_queue_snapshot(sink)
        return True

    # ── slot 内部 ──────────────────────────────────────────────────────

    def _handoff_item_from_proposal(
        self, kind: str, payload: object, *, manager: str,
    ) -> Optional[HandoffItem]:
        """``TASK_PROPOSAL.payload`` → ``HandoffItem``；形状 / 校验不过返 ``None``（docs §5.1）。

        ``issued_by`` 恒 ``HANDOFF_ISSUED_BY_USER``（沿用 P7.4 §5.7，不为 MTS 加新
        取值）；provenance 走 ``reason`` 前缀「托管 · <manager> 指派」保留。

        delegate / panel 路径**自带 ``_inbound_handoff_*`` 的在场 / 形状校验**——
        MTS 自动入队绕开了 inbound handler，不在这里校验则非法指派（target 不在场、
        圆桌重复 / 超员 / summarizer 自指）会被直接拦截入队，drain 只能静默 drop、
        且 MTS 收尾时给不出可操作的报错（Codex review P2）。任一校验不过返 ``None``
        → 调用方不拦截、照常渲采纳卡，用户采纳时由 inbound 给出明确报错。

        与前端 ``proposal_card.js::buildAcceptInbound`` 是同一份 payload 契约的两侧
        实现（JS 拼 inbound 帧、本函数直接造 ``HandoffItem``，因 MTS 在 emit 前就拦截
        故无法共用代码）—— 改 ``payload`` 形状两处同步。
        """
        if not isinstance(payload, dict):
            return None
        note = f"托管 · {manager} 指派"
        if kind == TASK_PROPOSAL_KIND_HANDOFF_DELEGATE:
            target = payload.get("target")
            if not isinstance(target, str) or not target:
                return None
            # 与 _inbound_handoff_delegate 同口径：target 不在场 → 不拦截、渲采纳卡，
            # 用户采纳时由 inbound 报「target 不在场」。否则 MTS 静默吞掉坏指派、
            # drain drop 后无可操作报错地结束会话（Codex review P2）。
            if target not in self.orch._guests:
                return None
            extra = payload.get("reason")
            if isinstance(extra, str) and extra:
                note = f"{note}：{extra}"
            return HandoffItem(
                kind=HandoffKind.DELEGATE, target=target, reason=note,
            )
        if kind == TASK_PROPOSAL_KIND_HANDOFF_PANEL:
            targets = payload.get("targets")
            if not isinstance(targets, list) or not all(
                isinstance(t, str) and t for t in targets
            ):
                return None
            # 与 _inbound_handoff_panel 五道校验同口径（docs/P7.3 §3.3）：≥2 人 /
            # 无重复 / 全在场 / summarizer 在场且不在 targets / cap 数学。
            if len(targets) < 2 or len(set(targets)) != len(targets):
                return None
            if any(t not in self.orch._guests for t in targets):
                return None
            summarizer = payload.get("summarizer")
            if summarizer is not None:
                if not isinstance(summarizer, str) or not summarizer:
                    return None
                if summarizer not in self.orch._guests or summarizer in targets:
                    return None
            cap = min(
                MAX_PANEL_TARGETS,
                self.orch.config.max_consecutive_ai_turns - (1 if summarizer else 0),
            )
            if len(targets) > cap:
                return None
            return HandoffItem(
                kind=HandoffKind.PANEL,
                targets=tuple(targets),
                summarizer=summarizer,
                reason=note,
            )
        return None

    def _emit_started(
        self, sink: EnvelopeSink, ms: ManagedSession,
    ) -> None:
        self._emit(
            sink, ChahuaEventType.MANAGED_SESSION_STARTED,
            {
                "task_id": ms.task_id,
                "manager_guest": ms.manager_guest,
                "budget": ms.budget,
            },
        )

    def _emit_advanced(
        self, sink: EnvelopeSink, ms: ManagedSession,
    ) -> None:
        self._emit(
            sink, ChahuaEventType.MANAGED_SESSION_ADVANCED,
            {"manager_guest": ms.manager_guest, "remaining_budget": ms.budget},
        )

    def _emit_ended(
        self, sink: EnvelopeSink, reason: str,
    ) -> None:
        self._emit(
            sink, ChahuaEventType.MANAGED_SESSION_ENDED, {"reason": reason},
        )

    def _emit(
        self, sink: EnvelopeSink, type: ChahuaEventType, data: dict,
    ) -> None:
        """连接级 ``managed_session_*`` envelope（``turn_id`` / ``guest_name`` /
        ``message_id`` 全 None —— MTS 是调度层瞬态，不属任一 turn）。"""
        emit_to_sink(
            sink,
            ChahuaEnvelope(
                room_id=self.orch.room.name, turn_id=None,
                guest_name=None, message_id=None,
                type=type, data=data,
            ),
        )
