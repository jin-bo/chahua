""":class:`HandoffDrainOps` —— Orchestrator 的 handoff drain loop slot（P7.1）。

slot 拆分 Step 5：搬 8 个方法 / 算法 ——
- ``run_pending_handoff``（drain 主体）
- ``_advance_to_runnable_handoff``（drain 主体 + ``has_next`` lookahead 共用 helper）
- ``_resolve_handoff_winners`` / ``_build_winner_blocks``（按 ``item.kind`` 算 winners
  和 ``extra_blocks``）
- ``_handoff_cost`` / ``_panel_underfilled``（纯静态判定）
- ``_render_review_block`` / ``_render_panel_block``（review / panel prompt 块）

主类经下列薄转发保 API 兼容：
- ``run_pending_handoff``、``_resolve_handoff_winners``、``_build_winner_blocks``
  —— 多个 ``test_orchestrator_handoff_*`` 直调。
- ``_handoff_cost`` ``@staticmethod`` 在主类同名定义、转调本 slot 静态实现
  （``test_orchestrator_handoff_panel.py`` 走 ``Orchestrator._handoff_cost(...)``
  classmethod-style 调用）。
- ``_render_review_block`` / ``_render_panel_block`` 不外露，未保留主类转发。
- ``_advance_to_runnable_handoff`` 不外露。

不变量保留（CLAUDE.md handoff §）：
- ``run_pending_handoff`` 与 ``_run_ai_chain`` 严格分流、不互相回落；drain 入口
  清零 ``_consecutive_ai_turns`` 一次。
- drain loop 末尾 5 步与 ``_run_ai_chain`` 严格对齐：peek → ``turn_end`` →
  ``flush_turn`` → ``_kick_summarize`` / ``_tick_cooldown`` /
  ``_kick_detect_new_artifacts`` → 收尾。
- ``_advance_to_runnable_handoff`` 是 drain 主体与 ``has_next`` lookahead **共用**
  helper —— 否则 lookahead 拿"将被静默 drop 的项"算 ``has_next``。
- cap 检查按 item ``cost`` 算（panel ≥2），不是 cap=1。
- ``_build_winner_blocks`` 在 runtime 过滤之后调（panel ``<panel_context>`` 按
  存活 panelist 渲染）。
- handoff envelope emit 职责拆分：``HANDOFF_CONSUMED`` 在 ``TURN_START`` 之后
  emit（经 ``self.orch._handoff_ops.emit_handoff_consumed``）。
- MTS（P8.4）：``advance_after_turn`` 在 5 步 ``turn_end`` 之前调一次；``has_next=False``
  + MTS 还活着 + 队列空 → **保持 dormant，drain ``return`` 不终结**（管理者没派活
  ≠ 事情结束；待机由用户下一句话经 ``submit_user_message`` re-drain 续上）。
  ``MANAGER_FINISHED`` reason 已退役。

cancel fixup / ``_emit_turn`` 仍在主类（Step 6 chain slot 才搬）。
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Optional

from ._orchestrator_consts import _PANEL_SUMMARY_BLOCK, _REVIEW_INSTRUCTION
from .context_renderer import render_managed_session_block
from .debug_recorder import (
    SCORING_PATH_HANDOFF_DELEGATE,
    SCORING_PATH_HANDOFF_PANEL,
    SCORING_PATH_HANDOFF_REVIEW,
)
from .events import ChahuaEventType, EnvelopeSink, new_turn_id
from .handoff import HandoffItem, HandoffKind
from .room import format_messages
from .scoring import ScoreKind, ScoreResult
from .task_rendering import score_to_dict as _score_to_dict

if TYPE_CHECKING:
    from .orchestrator import Orchestrator

_log = logging.getLogger(__name__)


class HandoffDrainOps:
    """handoff drain loop + winners / cost / 渲染算法。

    无独立状态——读 ``self.orch._handoff_queue`` / ``_consecutive_ai_turns`` /
    ``_rounds_without_user_or_mention`` / ``config`` / ``_recorder`` / ``_guests``
    / ``room`` / MTS 字段。
    """

    def __init__(self, orch: "Orchestrator") -> None:
        self.orch = orch

    async def run_pending_handoff(
        self, sink: EnvelopeSink, *, task_id: Optional[str],
        reset_cap: bool = True,
    ) -> None:
        """专职消费 handoff 队列；与 :meth:`_run_ai_chain` **严格分流，不互相回落**。

        队列空 / 下一项 cap 撞顶 → emit ``turn_end(next='user')`` 后 return，
        **不回落到 scoring**——这是"delegate 仅本轮独占，下次用户触发才恢复打分"
        的工程基础（docs §4.3 / §8 不变量）。

        **入口清零计数一次**（``reset_cap=True``，默认）：handoff 是用户显式触发
        但不走 ``submit_user_message``，上一轮 AI 到上限时不清零 while 会立刻挡掉
        → handoff 永不执行。只清一次，drain loop 内不再清。

        **P8.4 dormant 复活路径**：``submit_user_message`` 收尾 re-drain 调本方法时
        传 ``reset_cap=False`` —— chain 段已累计的 ``_consecutive_ai_turns`` 不被清零，
        chain + re-drain 段共享同一 ``max_consecutive_ai_turns`` 预算，避免 cap 双扣
        （docs/P8.4 §4.1）。其它调用点（首次 drain / handoff wrapper / bg 续命）
        恒走默认 ``reset_cap=True``，P7.1 / P8.3 / P11 行为零变化。
        """
        orch = self.orch
        if not orch._handoff_queue:
            return
        if reset_cap:
            orch._consecutive_ai_turns = 0
            orch._rounds_without_user_or_mention = 0

        while orch._handoff_queue:
            # 把跑不起来的队首项（死项 / panel 欠员 / winner 全失效）就地清掉，
            # 返回第一个真能产出 turn 的项 + 过滤后 winners + scoring_path
            # （docs §4.1.1 / §4.4）。
            head = self._advance_to_runnable_handoff(sink)
            if head is None:
                break
            item, winners, scoring_path = head
            cost = self._handoff_cost(item)
            # 档② 没超 max 本身、只是此刻 _consecutive_ai_turns 占了预算 → break、
            #    留队首，下次用户触发 + drain 入口清零后能跑。
            if (
                orch._consecutive_ai_turns + cost
                > orch.config.max_consecutive_ai_turns
            ):
                break
            orch._handoff_queue.popleft()

            # winner_blocks 在过滤之后构造——panel 的 <panel_context> 必须按**存活**
            # panelist 渲染，否则被移除的茶客会被列进名单（codex review P2）。winners
            # 与 winner_blocks 由 _build_winner_blocks 逐 winner 构造、天然等长。
            winner_blocks = self._build_winner_blocks(item, winners)
            turn_id = new_turn_id()
            item_dict = item.to_dict()
            orch._recorder.start_turn(
                turn_id=turn_id, task_id=task_id,
                trigger={"kind": "handoff", "handoff_item": item_dict},
            )
            # ``ScoreKind.MENTION`` 让 handoff 与 @ 路由在 inner cap / 调试抽屉
            # 渲染上同口径（确定性单点 / 跳过打分 / 绕 cap）；``scoring_path`` 是
            # 真正区分维度。
            score_results = [
                ScoreResult(
                    guest_name=name, score=1.0,
                    kind=ScoreKind.MENTION, raw=scoring_path,
                )
                for name in winners
            ]
            orch._recorder.record_scoring(
                threshold=None, scorables=[], cooled=[],
                results=[(r, None) for r in score_results],
                winners=winners,
                scoring_path=scoring_path,
            )
            orch._emit_turn(
                sink, turn_id=turn_id, type=ChahuaEventType.TURN_START,
                data={
                    "scores": [_score_to_dict(r) for r in score_results],
                    "scoring_path": scoring_path,
                },
            )
            orch._handoff_ops.emit_handoff_consumed(
                sink, turn_id=turn_id, item_dict=item_dict,
            )

            try:
                # panel = N(+1) 次串行 speak；每位 winner 取自己等长对齐的
                # extra_blocks（panelist 共享 <panel_context>、summarizer 拿
                # <panel_summary_request>；delegate / review 单元素与 P7.2 同）。
                for name, blocks in zip(winners, winner_blocks):
                    # 经主类 ``_let_speak`` 薄转发 —— 保 monkeypatch 可拦截
                    # （同 chain slot 口径）。
                    await orch._let_speak(
                        name, turn_id=turn_id, sink=sink, task_id=task_id,
                        extra_blocks=blocks,
                    )
                    orch._consecutive_ai_turns += 1
            except asyncio.CancelledError:
                orch._cancel_fixup_and_flush(sink, turn_id=turn_id)
                raise

            # P8.3：MTS 续队列 / 收尾判定 —— 在既有 peek / turn_end / 收尾流程**之前**
            # 调一次。可能往 _handoff_queue 续上 delegate(manager)、或结束 MTS；非 MTS
            # 房间（_managed_session is None）该调用立即返回，零行为变化（docs §4）。
            orch._mts_ops.advance_after_turn(item, sink)

            # 末尾顺序对齐 _run_ai_chain（docs §8 不变量）：peek→turn_end→flush→hooks。
            # lookahead 必须走与 drain 主体同一个 _advance_to_runnable_handoff——
            # 否则死项 / 欠员 panel 会让 has_next 误判成"下一轮 AI"，但该项下一轮被
            # 静默 drop、永远不产 turn，客户端卡在 turn_end(next='ai')（codex review P2）。
            next_runnable = self._advance_to_runnable_handoff(sink)
            if next_runnable is not None:
                next_cost = self._handoff_cost(next_runnable[0])
                has_next = (
                    orch._consecutive_ai_turns + next_cost
                    <= orch.config.max_consecutive_ai_turns
                )
            else:
                has_next = False
            next_state = "ai" if has_next else "user"
            orch._emit_turn(
                sink, turn_id=turn_id, type=ChahuaEventType.TURN_END,
                data={"next": next_state},
            )
            orch._recorder.flush_turn()
            orch._kick_summarize()
            orch._tick_cooldown()
            orch._kick_detect_new_artifacts(sink, task_id)
            if not has_next:
                # P8.4：drain 收尾「队列空 + MTS 活」**不再终结 MTS**，保持 dormant ——
                # 管理者没派活 ≠ 事情结束（思考 / 暂停 / 等用户输入 / 卡壳都会这样）。
                # 待机由用户下一句话经 ``submit_user_message`` 末尾 re-drain 续上：
                # chain 中管理者再派活时经 hook 自动入队，re-drain 立即跑（docs/P8.4
                # §3.1 / §4.1）。``MANAGER_FINISHED`` reason 已退役。
                # 终结只能来自 ``stop_reason()`` 5 条（budget / task / cap / user_stopped /
                # user_cancel）或 task 状态变更后的 ``check_after_task_change()``。
                return

        # P8.4：while 经 break 退出（队首死项 drop 光 / cost 撞顶）同理保持 dormant。
        # cost-break 留了能跑的队首项 → 队列非空，下次用户触发 + drain reset_cap 后能续；
        # 死项 drop 后队列空 → MTS 待机等用户下一句话。终结统一交给 ``stop_reason()`` /
        # ``check_after_task_change()`` 收敛到 5 条 reason 之内（docs/P8.4 §3.1）。

    # ── 算法 / 渲染 ────────────────────────────────────────────────────

    @staticmethod
    def _handoff_cost(item: HandoffItem) -> int:
        """一个 handoff item 占的 turn 内 speak 轮数（docs §4.1）。delegate /
        review 恒 1；panel = panelist 数 + 有无 summarizer。声明值——不因 §4.4
        runtime 过滤而变，while-entry cap 检查据此偏保守可接受。"""
        if item.kind is HandoffKind.PANEL:
            assert item.targets is not None
            return len(item.targets) + (item.summarizer is not None)
        return 1

    def _advance_to_runnable_handoff(
        self, sink: EnvelopeSink,
    ) -> Optional[tuple[HandoffItem, list[str], str]]:
        """从 ``_handoff_queue`` 队首弹掉所有**跑不起来**的项，返回第一个真能产出
        turn 的项 ``(item, 过滤后 winners, scoring_path)``；队列耗尽 → ``None``。

        "跑不起来"= runtime 重校验后必被 drop 的四类（docs §4.1.1 / §4.4），
        pop + WARN：① 死项（``cost > max_consecutive_ai_turns``，无论计数都跑不动，
        典型成因：入队后用户调低 max）；② panel 欠员（存活 panelist < 2）；
        ③ winner 全失效（delegate / review target 被 ``remove_guest`` 删掉）；
        ④ **P11 C12：任一 winner 正 busy**（前台 / handoff / bg run 占用）——
        ``let_speak`` 会和既有 ``agent.arun`` 并发跑在同一 ``agentao.Agentao`` 实例，
        撞 session 状态 / message_id / tool_call 顺序，故 drop。覆盖 inbound→drain
        间 race + MTS auto-enqueue 路径（hook 绕过 inbound 直接 enqueue，bg-busy
        target 只能在这里兜底；与 ③ "target 被 remove_guest 删光" 同语义类目）。
        丢弃任意项后重发一次 ``HANDOFF_ENQUEUED`` 权威快照，让前端队列预览不残留
        死项（codex review P2）。

        **不**含"此刻预算够不够"的判断——撞 ``_consecutive_ai_turns`` cap（档②）
        的项**能**跑、只是当下不跑，留在队首不弹，由调用方 cap 检查负责。

        drain loop 主体与 turn 末尾 ``has_next`` lookahead **共用**这一个 helper：
        否则 lookahead 拿"将被静默 drop 的项"算 ``has_next`` → 误报
        ``turn_end(next='ai')`` → 客户端等一个永不到来的 turn（codex review P2）。
        """
        orch = self.orch
        busy: set[str] = orch.active_guest_names or set()
        dropped = False
        try:
            while orch._handoff_queue:
                item = orch._handoff_queue[0]
                if (
                    self._handoff_cost(item)
                    > orch.config.max_consecutive_ai_turns
                ):
                    _log.warning(
                        "handoff item cost exceeds max_consecutive_ai_turns; "
                        "dropping: %r", item,
                    )
                    orch._handoff_queue.popleft()
                    dropped = True
                    continue
                winners, scoring_path = self._resolve_handoff_winners(item)
                winners = [
                    w for w in winners if w is not None and w in orch._guests
                ]
                if self._panel_underfilled(item, winners):
                    _log.warning(
                        "panel item underfilled after revalidation; "
                        "dropping: %r", item,
                    )
                    orch._handoff_queue.popleft()
                    dropped = True
                    continue
                if not winners:
                    _log.warning(
                        "handoff item target unavailable; dropping: %r", item,
                    )
                    orch._handoff_queue.popleft()
                    dropped = True
                    continue
                # P11 C12：任一 winner 正忙 → drop 整项。panel 串行 N(+1) 次 speak
                # 缺一不可，不做「跳过 busy 单人」的 partial 路径（语义模糊 + 与
                # ``_build_winner_blocks`` 等长契约冲突）。busy 名单读 orch 的
                # ``active_guest_names`` 视图（与 ``ScoringOps`` 同源）。
                busy_winners = [w for w in winners if w in busy]
                if busy_winners:
                    _log.warning(
                        "handoff item has busy winner(s) %r; dropping: %r",
                        busy_winners, item,
                    )
                    orch._handoff_queue.popleft()
                    dropped = True
                    continue
                return item, winners, scoring_path
            return None
        finally:
            if dropped:
                orch._handoff_ops.emit_handoff_queue_snapshot(sink)

    def _resolve_handoff_winners(
        self, item: HandoffItem,
    ) -> tuple[list[str], str]:
        """按 ``item.kind`` 算 ``(winners, scoring_path)``（docs §4.1）。

        panel → ``list(targets)[+summarizer]``；delegate / review → ``[target]``。
        纯函数——不渲染、不 emit、不 await。``winner_blocks`` 由
        :meth:`_build_winner_blocks` 在 runtime 过滤**之后**单独构造（panel 块要
        按存活 panelist 渲染，codex review P2）。
        """
        if item.kind is HandoffKind.PANEL:
            assert item.targets is not None
            winners = list(item.targets)
            if item.summarizer is not None:
                winners.append(item.summarizer)
            return winners, SCORING_PATH_HANDOFF_PANEL
        if item.kind is HandoffKind.REVIEW:
            return [item.target], SCORING_PATH_HANDOFF_REVIEW  # type: ignore[list-item]
        return [item.target], SCORING_PATH_HANDOFF_DELEGATE  # type: ignore[list-item]

    def _build_winner_blocks(
        self, item: HandoffItem, winners: list[str],
    ) -> list[Optional[list[str]]]:
        """按 ``item.kind`` + **runtime 过滤后**的 ``winners`` 逐 winner 算
        ``extra_blocks``（docs §4.1）。返回与 ``winners`` 等长——speak 循环 ``zip``
        两者，逐 winner 取自己的块。

        **必须在过滤之后调**：panel 的 ``<panel_context>`` 按存活 panelist 渲染，
        否则被 ``remove_guest`` 移除的茶客仍会被列进名单（codex review P2）。
        panelist 共享一份 ``<panel_context>``、summarizer 取 ``<panel_summary_request>``；
        delegate 无块、review 单块。
        """
        if item.kind is HandoffKind.PANEL:
            panelists = [w for w in winners if w != item.summarizer]
            panel_block = self._render_panel_block(panelists)
            return [
                [_PANEL_SUMMARY_BLOCK] if w == item.summarizer else [panel_block]
                for w in winners
            ]
        if item.kind is HandoffKind.REVIEW:
            return [[self._render_review_block(item)]]
        # P8.3：MTS 管理者回合（delegate 指向管理者）注入 <managed_session> 临时块
        # （docs §7）。worker 回合 / 非 MTS 普通 delegate 无块（与 P7 一致）。
        ms = self.orch._mts_ops._managed_session
        if (
            ms is not None
            and item.target == ms.manager_guest
            and winners == [ms.manager_guest]
        ):
            return [[render_managed_session_block(ms.manager_guest, ms.budget)]]
        return [None]

    @staticmethod
    def _panel_underfilled(item: HandoffItem, kept_winners: list[str]) -> bool:
        """panel 项 runtime 过滤后有效 panelist（不含 summarizer）< 2 → 判欠员
        （docs §4.4）。非 panel 项恒 ``False``。"""
        if item.kind is not HandoffKind.PANEL:
            return False
        kept_panelists = [w for w in kept_winners if w != item.summarizer]
        return len(kept_panelists) < 2

    def _render_review_block(self, item: HandoffItem) -> str:
        """P7.2：合成 review 项的 ``<review_target>`` 临时块（docs §4.2）。

        被审消息走 :func:`format_messages` 渲染——与茶客平时在 ``<recent_messages>`` /
        打分 transcript 看到的 ``<message>`` 包装一致（CLAUDE.md ``format_messages``
        不变量）。块只含被审消息原文 + 审阅指引，**不附任务产物清单**。

        ``message_by_id`` 保证命中：inbound 已校验 message_id 存在，``clear_room`` /
        ``reset_room`` 会清 ``_handoff_queue``——review 项不可能跨"消息消失"存活
        （docs §4.1 不变量）。查不到即 bug，``assert`` 兜底。
        """
        assert item.review_message_id is not None
        msg = self.orch.room.message_by_id(item.review_message_id)
        assert msg is not None, (
            f"review 项 {item.review_message_id!r} 的被审消息缺失——违反 docs §4.1 不变量"
        )
        message_xml = format_messages([msg], self.orch._display_map())
        return (
            "<review_target>\n"
            "请你审阅下面这条发言：\n\n"
            f"{message_xml}\n\n"
            f"{_REVIEW_INSTRUCTION}\n"
            "</review_target>"
        )

    def _render_panel_block(self, panelists: list[str]) -> str:
        """P7.3：合成 panel 项的 ``<panel_context>`` 临时块（docs §4.2）。

        ``panelists`` 列本轮圆桌全体 panelist + 平行发言指引。N 位 panelist
        **共享同一块**，文案写成位置无关（第一个 / 第 N 个 panelist 读到同一句都
        通顺）。**不列 summarizer**——它不是 panelist，拿 :data:`_PANEL_SUMMARY_BLOCK`。

        入参是名字列表（不是 ``HandoffItem``）——调用方在 runtime 过滤后才拼块，
        保证 ``<panel_context>`` 列的是**存活** panelist（codex review P2）。
        """
        dmap = self.orch._display_map()
        names = "、".join(dmap.get(t, t) for t in panelists)
        return (
            "<panel_context>\n"
            f"本轮是一次圆桌平行讨论，参与的茶客是：{names}。\n"
            "请你独立给出自己的观点，不必附和、也不必复述其他茶客已经说过的内容；\n"
            "如果你与前面发言的茶客看法不同，直接说出分歧。这是一次平行表态，"
            "不是接龙。\n"
            "</panel_context>"
        )
