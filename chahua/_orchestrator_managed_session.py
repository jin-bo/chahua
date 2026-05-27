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

        **P8.4 §4.2 扩展**：MTS 必须绑定**当前** active task —— 用户开新任务 / 切走
        active 让 MTS 任务不再是 active 也归 ``TASK_CLOSED``。折叠进既有 reason 沿用
        「5 reason 收敛」原则；前端文案扩展成「任务关闭或切换」覆盖三种来源
        （关 MTS 任务 / 采纳 propose_status("done") / 切走 active）。
        ``tasks_store is None`` 的房间（非任务房）整段 task 判定跳过。
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
            # P8.4：MTS 任务不再 active（用户开新任务自动 set_active / 显式切走）→ 归
            # TASK_CLOSED。advance_after_turn / advance_after_bg_completion 都经
            # stop_reason 收尾，故只需在这里加一条即可覆盖「dormant + 用户切 active」
            # / 「正常推进中用户切 active」两条路径。
            if self.orch.tasks_store.active_task_id != ms.task_id:
                return MANAGED_SESSION_REASON_TASK_CLOSED
        if self.orch._consecutive_ai_turns >= self.orch.config.max_consecutive_ai_turns:
            return MANAGED_SESSION_REASON_CAP_REACHED
        return None

    def check_after_task_change(self, sink: EnvelopeSink) -> None:
        """P8.4 §4.2：task 状态变更后主动跑一次 ``stop_reason()`` 检查 + 收尾。

        dormant 期间 ``advance_after_turn`` / ``advance_after_bg_completion`` 都不
        触发，必须在 task 状态变更（``open_task`` 自动 set_active / ``update_task``
        改 status / ``set_active_task`` / ``close_task``）路径主动调一次，命中即
        ``end_managed_session(reason)``。

        替代 P8.3 在 ``server_inbound_task.py`` 各 handler 早期的 eager end 块（已删）
        —— 交给 ``stop_reason()`` 统一判定，未来加新停止条件只改一处。``_managed_session
        is None``（非托管房间 / 已结束）整段 no-op，零行为变化。
        """
        if self._managed_session is None:
            return
        stop = self.stop_reason()
        if stop is not None:
            self.end_managed_session(sink, reason=stop)

    def advance_after_bg_completion(
        self,
        sink: EnvelopeSink,
        *,
        bg_guest_name: str,
        mts_manager_at_spawn: Optional[str] = None,
        mts_session_id_at_spawn: Optional[int] = None,
    ) -> bool:
        """P11.2.X：bg run 完成后续命 MTS —— 扣 1 budget + enqueue 管理者复查 +
        emit ``managed_session_advanced``。返 ``True`` = MTS 续上（调用方需保证有
        drain 在跑或主动起一个）；``False`` = 跳过（MTS 已不活 / 换主 / 已耗尽 /
        task 关 / cap 撞）。

        语义对齐：bg run（manager spawned）扮演 worker 角色，完成相当于 worker→
        manager 桥；与现有 :meth:`advance_after_turn` 的 worker 路径完全等价
        （budget-=1 + enqueue + emit_advanced）。**budget 是「管理者复查回合数」语义**
        —— 每次 bg 完成触发一次复查就该扣 1。

        **四档跳过分支**（code-review F2 + F3）：
        ① MTS 不活（``_managed_session is None``）—— bg 跑期间已被
           ``end_managed_session``（user_cancel / task_closed 等）独立结束。
        ② **manager 身份不匹配**（``mts_manager_at_spawn != ms.manager_guest``）——
           spawn 时管理者 Alice，bg 跑期间用户 stop MTS A、重启 MTS B 换主 Bob。本
           bg 不该把 Alice 的工作硬塞给 Bob 复查（reason 文案也指向错对象）。
        ③ **`stop_reason()` 命中**（budget/task_closed/cap_reached）—— 与
           :meth:`advance_after_turn` 同口径预判：bg 完成时若 MTS 已该结束（如另一
           个并发 bg 桥刚把 budget 扣到 0），直接走 ``end_managed_session(reason)``
           而非继续 enqueue 一个无用复查 turn 烧管理者一次 LLM 调用。
        ④ **budget 已 ≤0**（极端竞态：stop_reason 没显示但本地预判）—— ``stop_reason``
           走 ``budget <= 0`` 判定，这里冗余但显式：避免 ``managed_session_advanced``
           emit ``remaining_budget: -1`` 反人类数。

        正常路径：① stop_reason 不命中 → ② 扣 1 budget → ③ enqueue 管理者复查 →
        ④ emit_advanced + queue_snapshot → 返 True。
        """
        ms = self._managed_session
        if ms is None:
            return False
        # F3 守卫：spawn 时刻 manager vs 当前 manager 不匹配 → 跳过。
        if (
            mts_manager_at_spawn is not None
            and mts_manager_at_spawn != ms.manager_guest
        ):
            _log.warning(
                "advance_after_bg_completion: spawn-time manager %r != current "
                "manager %r — bg run is orphaned across MTS sessions, skipping",
                mts_manager_at_spawn, ms.manager_guest,
            )
            return False
        # Codex round 5 P2 守卫：spawn 时刻 MTS session_id（``ManagedSession.session_id``
        # 单调递增）vs 当前 ms.session_id 不匹配 → 跳过。覆盖「同管理者重启 MTS」漏洞
        # —— manager_guest 一致但 ManagedSession 是新实例（session_id 不同），旧 MTS
        # 的 bg 不能续命到新 MTS。用 counter 而非 ``id()`` 因 CPython 内存地址会被 GC 复用。
        if (
            mts_session_id_at_spawn is not None
            and mts_session_id_at_spawn != ms.session_id
        ):
            _log.warning(
                "advance_after_bg_completion: spawn-time MTS session_id %r != "
                "current %r — bg run belongs to a previous MTS instance, skipping",
                mts_session_id_at_spawn, ms.session_id,
            )
            return False
        # F2 + Codex P2 (round 3/4) 守卫：stop_reason 命中分两类处理：
        #
        # - ``BUDGET_EXHAUSTED``：多 bg deferred callback 串跑时的特定 race ——
        #   ① cb_bg1: budget=1>0, decrement=0, enqueue review_bob, start drain1。
        #   ② cb_bg2 紧接跑: advance → stop_reason → budget=0 → BUDGET_EXHAUSTED。
        #   若直接 end_managed_session 会清队列 → drain1 还没启动就看到队列空 →
        #   review_bob 丢失。加 queue-empty 守卫：cb_bg2 见队列非空（review_bob）→
        #   不 end 不动队列，仅 return False 跳过自己 enqueue（budget 已耗尽），drain1
        #   正常跑 review_bob，review_bob 完成后下一轮 advance_after_turn 按
        #   BUDGET_EXHAUSTED 正确收尾。
        #
        # - ``TASK_CLOSED`` / ``CAP_REACHED``：任务关 / cap 撞 —— 这些 stop reason
        #   与队列内容无关，队列里残留 review 跑也无意义（任务都关了 / cap 撞了再跑
        #   会破 cap 不变量）。**Codex round 4 P2-A**：原版误把 BUDGET 守卫推广到所有
        #   stop reason 会让 task_closed 时 MTS 卡住不 end，drain 也不会被起 → MTS
        #   永远活；窄化守卫仅 BUDGET_EXHAUSTED 才分流。
        #
        # **这条守卫只能加在 bg path，不能加在 advance_after_turn**（后者动了会破
        # 「budget=N → 最多 N worker turn」严格语义，见
        # test_budget_one_runs_one_worker_then_exhausted）。
        stop = self.stop_reason()
        if stop is not None:
            if (
                stop == MANAGED_SESSION_REASON_BUDGET_EXHAUSTED
                and self.orch._handoff_ops._queue
            ):
                # 多 bg race 场景：让队列里的 review 跑完，advance_after_turn 收尾。
                return False
            # TASK_CLOSED / CAP_REACHED / (BUDGET + queue 空) → 正常 end。
            self.end_managed_session(sink, reason=stop)
            return False
        ms.budget -= 1
        self.orch._handoff_ops.enqueue_handoff(
            HandoffItem(
                kind=HandoffKind.DELEGATE,
                target=ms.manager_guest,
                reason=f"bg {bg_guest_name} 已完成 · 托管复查",
            )
        )
        self.orch._handoff_ops.emit_handoff_queue_snapshot(sink)
        self._emit_advanced(sink, ms)
        return True

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
        if stop is not None:
            self.end_managed_session(sink, reason=stop)
            return
        just_ran_manager = (
            item.kind is HandoffKind.DELEGATE and item.target == ms.manager_guest
        )
        if just_ran_manager:
            # 管理者回合：提议已在回合内由 hook 入队；没派活则队列此刻为空，drain
            # 走到收尾即 ``return``，MTS 保持 dormant 等用户下一句话（P8.4：
            # ``MANAGER_FINISHED`` reason 已退役）。
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
        # P8.4.11（Codex round 5 P2）：自指派提早拦下 —— ``_handoff_item_from_proposal``
        # 的 delegate 路径用全集 busy 校验，``let_speak`` 已把 manager 标 busy →
        # ``target == manager`` 落 busy 集走 ``return None`` → 本入口 ``item is None``
        # 退化渲卡 → 用户点采纳又会真的 enqueue 一轮 manager。把自指派早早识别 + swallow，
        # 与既有「不入队不渲卡」语义保持一致（docs §5.1）。
        payload = data.get("payload")
        if (
            kind == TASK_PROPOSAL_KIND_HANDOFF_DELEGATE
            and isinstance(payload, dict)
            and payload.get("target") == ms.manager_guest
        ):
            _log.warning("MTS 管理者 %r 自指派，跳过（不入队不渲卡）", ms.manager_guest)
            return True
        item = self._handoff_item_from_proposal(
            kind, payload, manager=ms.manager_guest,
        )
        if item is None:
            # 形状坏（工具已保证形状、理论不达）→ 不拦，照常渲卡兜底。
            return False
        # 余留 belt-and-suspenders：上面已早返，但 ``_handoff_item_from_proposal``
        # 若未来改回返非 None 也走这条路 —— 保留兜底防自指派漏进队列。
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
        # P11 C12：busy（前台 / handoff / bg run 占用）的茶客与 ``_inbound_handoff_*``
        # 同口径拒绝；不在这里拦下来则 hook 直接 enqueue，drain 走第 4 档 busy filter
        # 静默 drop —— 用户看不到任何反馈。降级渲采纳卡让用户/管理者可见、bg 跑完后
        # 用户点采纳即走 inbound 正常路径。读 orch 的 ``active_guest_names``。
        busy: set[str] = self.orch.active_guest_names or set()
        # P8.4.9（Codex round 3 P2）：本 hook 在 manager 自己的 ``speak()`` 内跑，
        # ``let_speak`` 已把 manager 标为 busy；但被提议的 handoff item 会在 manager
        # 这一轮 ``speak()`` finally 把 busy 标解除**之后**才被 drain 消费。最常见的
        # 「workers 讨论、我（manager）汇总」panel 模式（summarizer=manager）原 P11 C12
        # 用全集 busy 会把它当采纳卡降级，等用户手动点—— P8.4 MTS 自治流被打破。把
        # manager 从 summarizer 检查的 busy 集合里剔除；delegate target=manager 与
        # panel panelist=manager 保留原拒绝（语义闭环 / 圆桌应是 worker 间讨论）。
        busy_excl_manager = busy - {manager}
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
            if target in busy:
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
            if any(t in busy for t in targets):
                return None
            summarizer = payload.get("summarizer")
            if summarizer is not None:
                if not isinstance(summarizer, str) or not summarizer:
                    return None
                if summarizer not in self.orch._guests or summarizer in targets:
                    return None
                if summarizer in busy_excl_manager:
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
