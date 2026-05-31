""":class:`AIChainOps` —— Orchestrator 的 AI 链 + turn-级 envelope emit slot。

slot 拆分 Step 6（最后一块）：搬 4 个方法 ——
- ``run_ai_chain``（``_run_ai_chain`` 实现主体）
- ``emit_turn``（turn_start / turn_end envelope 合成）
- ``emit_cancel_fixup``（cancel 路径补一帧 turn_end(next='user', cancelled)）
- ``cancel_fixup_and_flush``（speak 阶段 cancel 共用 fixup：补 fixup + flush 半截
  turns.jsonl 行）

主类经下列薄转发保 API 兼容：
- ``_run_ai_chain`` —— ``submit_user_message`` 调用 + ``tests/conftest.py:211`` /
  ``tests/test_orchestrator_run_pending_handoff.py:155`` ``monkeypatch.setattr(Orchestrator,
  "_run_ai_chain", ...)`` 替换。**承重契约**：主类必须保留同名 method，否则
  monkeypatch 替换的是主类符号、``submit_user_message`` 仍走转发后的真正实现。
- ``_emit_turn`` / ``_emit_cancel_fixup`` / ``_cancel_fixup_and_flush`` —— drain slot
  调用，主类薄转发保 ``self.orch._emit_turn(...)`` 引用稳定。

不变量保留（CLAUDE.md handoff / chain §）：
- ``_run_ai_chain`` 与 ``run_pending_handoff`` 严格分流、不互相回落；本 slot 不
  call drain（``submit_user_message`` 是唯一编排两者的入口）。
- 每个 pick 周期一行 ``turns.jsonl`` —— ``start_turn`` / ``record_scoring`` / 走完
  对应分支后 ``flush_turn``，cancel 路径经 ``cancel_fixup_and_flush`` 也 flush。
- scoring 阶段 cancel 用 ``last_open_turn_id`` 补 fixup（first-iter ``None`` 时不补，
  此时 UI 还在「发送」状态）；speak 阶段 cancel 用当前 turn_id。
- ``capture_prompts=True`` 时 piggyback ``scoring_prompts`` 字段（``schema_version``
  不 bump，老前端忽略未知字段）；``False`` 时整字段不写（"capture 关" vs "空" 双
  语义）。
- @ 路径（mention / broadcast）绕过本回合的内层 cap —— ``bypass_inner_cap = all(s.kind
  == ScoreKind.MENTION for s in scores)``；下一次 outer while 检查时 cap 自然终止
  AI 链续接。
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, Optional

from .events import (
    STATUS_CANCELLED,
    STATUS_OK,
    ChahuaEnvelope,
    ChahuaEventType,
    EnvelopeSink,
    emit_to_sink,
    new_turn_id,
)
from .scoring import ScoreKind
from .task_rendering import score_to_dict as _score_to_dict

if TYPE_CHECKING:
    from .orchestrator import Orchestrator


class AIChainOps:
    """AI 链主循环 + turn-级 envelope emit。

    无独立状态——读 ``self.orch._consecutive_ai_turns`` / ``_rounds_without_user_or_mention``
    / ``config`` / ``_recorder`` / ``room``。
    """

    def __init__(self, orch: "Orchestrator") -> None:
        self.orch = orch

    async def run_ai_chain(
        self, *, sink: EnvelopeSink, active_task_id: Optional[str] = None,
        images_rel: tuple[str, ...] = (),
    ) -> None:
        orch = self.orch
        # 上一轮已经 emit ``turn_end(next='ai')``、UI 仍守在"停止"按钮等下一个 turn_start
        # 的那个 turn_id；下一次 pick 成功（emit 新 turn_start / turn_end）就清零，pick 失败
        # 则用这个 id 补一帧 ``turn_end(next='user')`` 让 UI 收回按钮。None = 无未结链。
        last_open_turn_id: Optional[str] = None
        # P13：视觉附图只给 chain 第一个真正发言的 pick 周期（直接回应用户消息那批）。
        # 之后是 AI→AI 接力（「后续 turn」），按不变量「历史图对后续 turn 退回文本引用」
        # 只看 ``<./share/...>`` 标记、不再重发 base64 —— 省 token、语义上像素只属于用户
        # 那条消息。**本地 flag 而非 ``_consecutive_ai_turns == 0``**：pre-drain 会先
        # bump 共享计数器（单一 cap），用计数器判会误伤「pre-drain 后才回应用户的首发者」。
        first_cycle = True
        while orch._consecutive_ai_turns < orch.config.max_consecutive_ai_turns:
            round_images = images_rel if first_cycle else ()
            # scoring 阶段被 cancel：上一轮已 emit turn_end(next='ai')，UI 守在「停止」
            # 按钮等下一个 turn_start。不补 fixup 的话 UI 永远收不到链终态。first-iter
            # （last_open_turn_id=None）UI 还在「发送」状态，无须补。
            # **scoring 阶段无 recorder turn 在飞**：turn_id 在 pick 之后才 mint，所以
            # 此处 cancel 不需要 flush_turn。要记本周期"为什么没说"得等 pick 跑完后。
            try:
                # 经主类 ``_pick_next_speaker`` 薄转发而非直调 ``_scoring_ops.pick_next_speaker``
                # —— 保 ``monkeypatch.setattr(Orchestrator, "_pick_next_speaker", ...)`` 可
                # 拦截内部调用链（与 ``submit_user_message`` → ``self._run_ai_chain`` 同口径）。
                winners, scores, prompts_by_guest, debug_meta = await orch._pick_next_speaker(
                    respect_at_mention=orch._consecutive_ai_turns == 0,
                    active_task_id=active_task_id,
                )
            except asyncio.CancelledError:
                if last_open_turn_id is not None:
                    self.emit_cancel_fixup(sink, turn_id=last_open_turn_id)
                raise

            # 始终 mint turn_id + start_turn + record_scoring —— 无人接话也记一行
            # （不变量"每个 pick 周期一行 turns.jsonl"，docs §数据模型 P6）。
            turn_id = new_turn_id()
            orch._recorder.start_turn(
                turn_id=turn_id,
                task_id=active_task_id,
                trigger=orch._compute_trigger(),
            )
            orch._recorder.record_scoring(
                threshold=debug_meta.threshold,
                scorables=debug_meta.scorables,
                cooled=debug_meta.cooled,
                results=[
                    (r, prompts_by_guest.get(r.guest_name)) for r in scores
                ],
                winners=winners,
                scoring_path=debug_meta.scoring_path,
            )

            if not winners:
                # 没人想接话。两种子情况（envelope 协议不变 —— turn_start 不 emit）：
                #   a) 本回合一上来就空（全员低分 / 全员冷却）—— UI 状态干净，直接返回。
                #   b) AI 链中途空（前一轮 next='ai'）—— 补一帧 turn_end(next='user')。
                # turns.jsonl 仍 flush 一行（最需调试的就是"为什么没人说话"）。
                orch._recorder.flush_turn()
                if last_open_turn_id is not None:
                    self.emit_turn(
                        sink,
                        turn_id=last_open_turn_id,
                        type=ChahuaEventType.TURN_END,
                        data={"next": "user"},
                    )
                return

            # turn_start / turn_end 是轮级事件，跨多位茶客；guest_name=None。winners
            # 都在 data.scores 里，前端按 turn_id 聚合。
            # ``capture_prompts=True`` 时 piggyback scoring prompt（前端调试抽屉拿了即用，
            # 不另开 envelope 类型 / 不 bump schema_version；老前端忽略未知字段）。
            # ``False`` 时整字段不写 key（区分"prompt 捕获关了" vs "prompt 是空"两种
            # 语义，docs §不变量）。
            turn_data: dict[str, Any] = {
                "scores": [_score_to_dict(s) for s in scores],
                # 前端调试抽屉折叠态显示 scoring_path 徽标（与 room_history 索引行同款），
                # 同时透给 piggyback 调试视图。schema_version 不 bump，老前端忽略未知字段。
                "scoring_path": debug_meta.scoring_path,
            }
            if orch._recorder.capture_prompts:
                # 关键：``@guest`` / ``@all`` 路径绕过 LLM 打分 ⇒ ``prompts_by_guest`` 为空。
                # 必须 **始终** 写 key（哪怕值是 ``{}``），让前端区分"capture 已关"（key
                # 缺）与"capture 开但本 turn 无 LLM 打分"（空字典）—— 否则 mention /
                # broadcast turn 会被误标"prompt 捕获已关"。
                turn_data["scoring_prompts"] = {
                    g: p for g, p in prompts_by_guest.items() if p
                }
            self.emit_turn(
                sink,
                turn_id=turn_id,
                type=ChahuaEventType.TURN_START,
                data=turn_data,
            )

            # 同回合"抢话"的多位茶客串行发言；第 2 位发言时，第 1 位的回复已经在
            # transcript 里，靠 cursor 增量自然喂入。每位都计 1 轮（max 卡死整体上限）。
            # @ 路径（单点 / broadcast）绕过本回合的内层 cap —— 用户已显式指定谁该说话，
            # 不该被 max_consecutive_ai_turns 截断；下一次 outer while 检查时 cap 自然
            # 终止 AI 链续接。
            #
            # 发言阶段被 cancel：本 turn 的 turn_start 已 emit、message_* 可能在流。
            # scoring 阶段的 cancel 在外层 while 的 try 里独立处理。
            # **P6.1 cancel 也 flush**：半截 prompt / 工具 / partial message 都是取证证据。
            bypass_inner_cap = all(s.kind == ScoreKind.MENTION for s in scores)
            try:
                for guest_name in winners:
                    if not bypass_inner_cap and orch._consecutive_ai_turns >= orch.config.max_consecutive_ai_turns:
                        break
                    # 经主类 ``_let_speak`` 薄转发 —— 保 monkeypatch 可拦截。
                    # P13：本轮触发用户图只流入 chain 第一周期的 let_speak（``round_images``
                    # 在 AI 接力周期为空）。pre-drain / re-drain / dormant MTS kickoff 不接像素。
                    await orch._let_speak(
                        guest_name, turn_id=turn_id, sink=sink,
                        task_id=active_task_id, images_rel=round_images,
                    )
                    orch._consecutive_ai_turns += 1
                    orch._rounds_without_user_or_mention += 1
            except asyncio.CancelledError:
                self.cancel_fixup_and_flush(sink, turn_id=turn_id)
                raise

            # P13：第一周期（回应用户那批）已发言完毕 —— 后续 while 迭代是 AI 接力，
            # 不再带像素。``winners`` 非空才走到这里（``not winners`` 早 return）。
            first_cycle = False

            next_state = (
                "ai"
                if orch._consecutive_ai_turns < orch.config.max_consecutive_ai_turns
                else "user"
            )
            self.emit_turn(
                sink,
                turn_id=turn_id,
                type=ChahuaEventType.TURN_END,
                data={"next": next_state},
            )
            # next='ai' 时记下本 turn_id，下一轮 pick 若失败要回来补 fixup；
            # next='user' 时 UI 已自行收回按钮，无 pending 状态。
            last_open_turn_id = turn_id if next_state == "ai" else None

            # 正常 turn 完结 —— flush 一行到 turns.jsonl 后清 in-flight（docs P6）。
            orch._recorder.flush_turn()

            # 摘要 / 冷却递减每"pick 周期"一次（不是每个发言者一次）—— 否则同回合 2 位
            # 同时说后第一位的冷却被立刻 tick 掉，下个 pick 就能再次入选，违背"刚发言不接自己"。
            orch._kick_summarize()
            orch._tick_cooldown()
            # P5.4 自动归集：扫 active task 的 artifacts/，emit 新文件的 hint + task_info。
            orch._kick_detect_new_artifacts(sink, active_task_id)

    # ── turn-级 envelope emit ────────────────────────────────────────

    def emit_turn(
        self,
        sink: EnvelopeSink,
        *,
        turn_id: str,
        type: ChahuaEventType,
        data: dict,
        status: str = STATUS_OK,
    ) -> None:
        """合成轮级 envelope（turn_start / turn_end）走 sink。message-级事件经
        :class:`ChahuaTransport` emit。``status`` 默认 ok；cancel 路径走 ``cancelled``。
        """
        emit_to_sink(
            sink,
            ChahuaEnvelope(
                room_id=self.orch.room.name,
                turn_id=turn_id,
                guest_name=None,
                message_id=None,
                type=type,
                status=status,
                data=data,
            ),
        )

    def emit_cancel_fixup(self, sink: EnvelopeSink, *, turn_id: str) -> None:
        """补一帧 turn_end(next='user', cancelled) —— 两条 cancel 路径共用：
        scoring 阶段 cancel 走 last_open_turn_id；speak 阶段 cancel 走当前 turn_id。
        """
        self.emit_turn(
            sink,
            turn_id=turn_id,
            type=ChahuaEventType.TURN_END,
            data={"next": "user"},
            status=STATUS_CANCELLED,
        )

    def cancel_fixup_and_flush(
        self, sink: EnvelopeSink, *, turn_id: str,
    ) -> None:
        """speak 阶段 cancel 共用 fixup：补 turn_end(cancelled) + flush 半截
        turns.jsonl 行（``_run_ai_chain`` / ``run_pending_handoff`` 两条 drain
        路径同步走，避免两处各写一遍漂移）。
        """
        self.emit_cancel_fixup(sink, turn_id=turn_id)
        self.orch._recorder.flush_turn()
