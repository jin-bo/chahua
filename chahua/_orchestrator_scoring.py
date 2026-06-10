""":class:`ScoringOps` —— Orchestrator 的意愿打分 / @ 路由 / 发言 slot。

slot 拆分 Step 4：把 6 个 scoring/pick/speak 方法搬进本 slot：
``pick_next_speaker`` / ``score_one`` / ``count_self_mentions`` /
``find_user_mention`` / ``find_user_broadcast`` / ``let_speak``。

主类经下列薄转发保 API 兼容：
- ``_pick_next_speaker`` / ``_let_speak`` —— 主类 ``_run_ai_chain`` 与 drain
  ``run_pending_handoff`` 调用点（drain Step 5 出去前继续走主类转发）。
- ``_count_self_mentions`` / ``_find_user_mention`` / ``_find_user_broadcast`` ——
  ``test_orchestrator_at_mention.py`` / ``test_scoring_subject_hint.py`` 直接调。
- ``_score_one`` 仅 ``pick_next_speaker`` 内调，主类不再转发。

不变量保留（CLAUDE.md scoring §）：
- 打分输入不可信：score 严格 JSON + 解析失败降级 0 + clamp ``[0,1]`` —— 本 slot
  只搬调度逻辑，``IntentScorer.score`` 行为不动。
- ``@提及`` 走确定性路由不进打分；``@broadcast`` 绕过冷却 + 打分全员发言一次。

context 装配（``_build_context_for`` / ``_maybe_render_scoring_task_block`` /
``_display_map``）留主类，drain slot 也要用，从 slot 经 ``self.orch.xxx`` 调。
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Optional

from .debug_recorder import (
    SCORING_PATH_BROADCAST,
    SCORING_PATH_MENTION,
    SCORING_PATH_SCORING,
    PickDebugMeta,
)
from .events import EnvelopeSink
from .mentions import BROADCAST_TOKENS, iter_at_positions, matches_at
from .room import Message
from .scoring import ScoreKind, ScoreResult
from .task_rendering import wrap_current_task as _wrap_current_task
from .user_md import USER_SPEAKER_ID

if TYPE_CHECKING:
    from .orchestrator import Orchestrator


class ScoringOps:
    """打分 / @ 路由 / 单茶客 speak。

    无独立状态——读 ``self.orch._guests`` / ``_cooldown`` / ``_rounds_without_user_or_mention``
    / ``scorer`` / ``user_config`` / ``_recorder`` / ``config`` / ``cursor`` / ``room``。
    """

    def __init__(self, orch: "Orchestrator") -> None:
        self.orch = orch

    async def pick_next_speaker(
        self,
        *,
        respect_at_mention: bool,
        active_task_id: Optional[str] = None,
    ) -> tuple[list[str], list[ScoreResult], dict[str, Optional[str]], PickDebugMeta]:
        """选下一回合发言者；返回 ``(winners, scores, prompts_by_guest, debug_meta)``。

        - ``winners``：按分数倒序的茶客名列表，长度 0 ~ ``max_speakers_per_pick``。
          **空 list = 无人接话**（旧版返 None 的两种场景：scorables=[]、no passed）；
          P6 起改用 ``not winners`` 控制流（始终给 recorder 一条 turn 行可追溯）。
        - ``scores``：含所有候选 ScoreResult（含冷却中那批 0 分占位）。
        - ``prompts_by_guest``：``capture_prompts=True`` 时为 ``{guest_name: prompt}``，
          否则空 dict（debug 落盘单点用；envelope piggyback 留给 step 3）。
        - ``debug_meta``：:class:`PickDebugMeta`（threshold / scorables / cooled /
          scoring_path）—— 给 ``recorder.record_scoring`` 用，避免上层重算。

        ``respect_at_mention=True``：检查 transcript 最后一条（用户消息）里的 ``@``。
        AI 间接力时（``_consecutive_ai_turns > 0``）不再认 @ —— 否则一个茶客在自己发言里
        @ 别人会形成"自动续接"，与"持麦串行 + 上限"的设计相冲。

        优先级：``@broadcast``（@all / @所有人 等）→ 全员发言一次（含冷却中的）；
        否则 ``@<guest>`` 单点路由；否则走打分。
        """
        orch = self.orch
        all_guests = list(orch._guests.keys())
        # P11：bg run 占用的茶客既不接 @，也不参与打分 —— ``active_guest_names`` 是
        # 唯一数据源（前台 / handoff `let_speak` 也写）。空 set 兜底兼容裸构 Orchestrator
        # 的旧测试夹具（``active_guest_names is None``）。
        busy: set[str] = orch.active_guest_names or set()

        # 1a) @broadcast → 全员一次（用户意图明确，绕过冷却 + 打分）；busy 的不带上
        if respect_at_mention and self.find_user_broadcast():
            orch._rounds_without_user_or_mention = 0
            winners = [n for n in all_guests if n not in busy]  # 注册顺序，确定性
            scores = [
                ScoreResult(guest_name=n, score=1.0, kind=ScoreKind.MENTION)
                for n in winners
            ]
            return winners, scores, {}, PickDebugMeta(
                threshold=None, scoring_path=SCORING_PATH_BROADCAST,
            )

        # 1b) @ 提及确定性路由（仅当回应用户那条时）；@busy 目标在 find_user_mention
        # 内被跳过（继续往下一个 @ 找），落到这里的 mention 必非 busy。
        if respect_at_mention:
            mention = self.find_user_mention()
            if mention is not None and orch._cooldown.get(mention, 0) == 0:
                orch._rounds_without_user_or_mention = 0
                scores = [
                    ScoreResult(
                        guest_name=mention, score=1.0, kind=ScoreKind.MENTION
                    )
                ]
                return [mention], scores, {}, PickDebugMeta(
                    threshold=None, scoring_path=SCORING_PATH_MENTION,
                )

        # 2) 并发打分（冷却中的茶客直接当 0 分，省 LLM 调用；busy 整段不进入两边任一
        # 桶 —— 与 scorables/cooled 是三档分流，bg run 跑完自然回到 scorables 池）
        scorables = [
            name for name in all_guests
            if orch._cooldown.get(name, 0) == 0 and name not in busy
        ]
        cooled = [
            name for name in all_guests
            if name not in scorables and name not in busy
        ]

        # 全员冷却：无 LLM 调用，所有 guest 0 分占位记一行后由上层判 winners=[]。
        if not scorables:
            results = [
                ScoreResult(guest_name=name, score=0.0, kind=ScoreKind.COOLDOWN)
                for name in cooled
            ]
            return [], results, {}, PickDebugMeta(
                threshold=None, cooled=cooled,
                scoring_path=SCORING_PATH_SCORING,
            )

        # P5.6：每 pick 周期渲染一次 task_block，N 个 scorer 共享同一字符串。
        # 不进 score_one —— 那样 N 茶客就要 N 次 get_task。closed / missing → "".
        rendered = orch._maybe_render_scoring_task_block(active_task_id)
        task_block = (
            "\n" + _wrap_current_task(rendered) + "\n" if rendered else ""
        )

        transcript_text, recent = orch._scoring_transcript()
        scored: list[tuple[ScoreResult, Optional[str]]] = list(
            await asyncio.gather(
                *(
                    self.score_one(name, transcript_text, recent, task_block)
                    for name in scorables
                )
            )
        )
        results = [r for r, _ in scored]
        prompts_by_guest: dict[str, Optional[str]] = {
            r.guest_name: p for r, p in scored if p is not None
        }

        # 冷却中那些也填进 results 让 UI / recorder 看到"为什么没选他"。
        for name in cooled:
            results.append(
                ScoreResult(
                    guest_name=name, score=0.0, kind=ScoreKind.COOLDOWN
                )
            )

        threshold = min(
            1.0,
            orch.config.want_threshold
            + orch.config.threshold_decay_per_turn
            * orch._rounds_without_user_or_mention,
        )
        debug_meta = PickDebugMeta(
            threshold=threshold, scorables=scorables, cooled=cooled,
            scoring_path=SCORING_PATH_SCORING,
        )
        passed = [r for r in results if r.score >= threshold]
        if not passed:
            return [], results, prompts_by_guest, debug_meta
        passed.sort(key=lambda r: r.score, reverse=True)
        # 重读 ``active_guest_names`` 的当前值再选 winner：上面 ``await gather`` 期间
        # 可能有 bg run 起来，行 84 的 ``busy`` 快照对此盲。先 filter 再 cap，避免
        # 顶分刚 busy 时少给 winner；不剔掉的话 ``_let_speak`` 会与 bg run 共享同一
        # 茶客名，俩 finally 的 discard 会让仍跑的那路看起来 idle。
        current_busy = orch.active_guest_names or set()
        not_busy = [r for r in passed if r.guest_name not in current_busy]
        winners = [r.guest_name for r in not_busy[: orch.config.max_speakers_per_pick]]
        return winners, results, prompts_by_guest, debug_meta

    async def score_one(
        self,
        guest_name: str,
        transcript_text: str,
        recent: list[Message],
        task_block: str = "",
    ) -> tuple[ScoreResult, Optional[str]]:
        """单茶客打分 —— 返回 ``(ScoreResult, prompt|None)``。

        ``recorder.capture_prompts=True`` 时走 :meth:`IntentScorer.score_with_prompt`
        拿 prompt 字符串落 ``debug/prompts/<turn_id>/scoring_<guest>.txt``；否则走
        :meth:`IntentScorer.score` 不材化 prompt（避免内存里存在但不落盘的"半捕获"）。
        """
        orch = self.orch
        entry = orch._guests[guest_name]
        mention_count = self.count_self_mentions(guest_name, recent)
        if orch._recorder.capture_prompts:
            return await orch.scorer.score_with_prompt(
                guest_name=guest_name,
                persona=entry.persona_md,
                transcript_text=transcript_text,
                user_config=orch.user_config,
                subject_mention_count=mention_count,
                task_block=task_block,
            )
        result = await orch.scorer.score(
            guest_name=guest_name,
            persona=entry.persona_md,
            transcript_text=transcript_text,
            user_config=orch.user_config,
            subject_mention_count=mention_count,
            task_block=task_block,
        )
        return result, None

    def count_self_mentions(
        self, guest_name: str, recent: list[Message]
    ) -> int:
        """统计 ``recent`` 窗口里 ``guest_name`` 被**其他人**提到的次数。

        给打分 prompt 注入 ``<context_hint>`` 用：名字出现在最近发言里是"话题在讨论你"
        的 deterministic 信号，弥补单靠 LLM 评分时把"被讨论但没 @"判低分的问题。

        排除 ``m.speaker_id == guest_name`` 的自我消息——茶客自己复读自己名字不算被讨论。
        包含 ``@guest_name`` 形式的出现（调用方语义里 @ 走的是确定性路由，能到这里说明
        本轮没 @，但更早的 @ 仍是有效的"刚被讨论"信号）。

        简单子串匹配；接受名字是别人字符串子串的极少数误命中（"Elon" 撞到 "Elonomics"），
        因为它只是 soft hint，不是硬规则。
        """
        count = 0
        for m in recent:
            if m.speaker_id == guest_name:
                continue
            if guest_name in m.text:
                count += 1
        return count

    def find_user_mention(self) -> Optional[str]:
        """扫 transcript 最后一条消息里的 @ 提及；返回首个匹配的**非 busy** 在场茶客名。

        对每个 ``@`` 位置按**注册名长度倒序**做最长前缀匹配 + 词边界校验，所以含空格的
        名字（``Elon Musk``）也能命中——把"什么算合法名字"完全交给注册表。

        @broadcast（@all / @所有人 等）由 :meth:`find_user_broadcast` 独立检查，
        调用方先查 broadcast，因此这里遇到 broadcast token 时跳过当前 ``@``——避免
        ``@all @宝总`` 极少数情况下被当成单点提及。

        P11：busy（bg run 占用）的茶客 @ 视为忽略 —— 匹配到 busy 名直接跳过当前 @
        token、继续看下一个 @；用户输 ``@busy_guest @ok_guest`` 仍能路由到第二位。
        全 @ 都打在 busy 上 → 返 ``None`` 落到打分（busy 也不参与，自然等用户消息）。
        """
        last = self.orch.room.last_message()
        if last is None or last.speaker_id != USER_SPEAKER_ID:
            # 只承认用户消息里的 @；AI 互相 @ 不走确定性路由（见 _run_ai_chain 注释）。
            return None
        text = last.text
        busy: set[str] = self.orch.active_guest_names or set()
        # 注册名按长度倒序：``Elon Musk`` 排在 ``Elon`` 之前，最长前缀优先。
        names_by_length = sorted(self.orch._guests, key=len, reverse=True)
        for at_idx in iter_at_positions(text):
            start = at_idx + 1
            # 此 @ 是个 broadcast token？跳过（broadcast 由调用方独立判定）。
            if any(matches_at(text, start, tok, case_insensitive=True) for tok in BROADCAST_TOKENS):
                continue
            for name in names_by_length:
                if matches_at(text, start, name):
                    if name in busy:
                        # 命中 busy 名：整个 @ token 忽略（用户显式打的就是这位，
                        # 不退回去试更短前缀），继续看下一个 @ 位置。
                        break
                    return name
        return None

    def find_user_broadcast(self) -> bool:
        """扫 transcript 最后一条消息里有没有 @broadcast 词（all / everyone / 大家 /
        各位 / 所有人 —— 见 :data:`BROADCAST_TOKENS`，英文大小写无关）。

        匹配带词边界 —— ``@allies`` 不会被当成 ``@all``，因为 ``e`` 不是名字边界。

        broadcast 走"全员发言一次"路径，独立于 :meth:`find_user_mention` 的 @ 单点路由。
        """
        last = self.orch.room.last_message()
        if last is None or last.speaker_id != USER_SPEAKER_ID:
            return False
        text = last.text
        for at_idx in iter_at_positions(text):
            start = at_idx + 1
            if any(matches_at(text, start, tok, case_insensitive=True) for tok in BROADCAST_TOKENS):
                return True
        return False

    async def let_speak(
        self,
        guest_name: str,
        *,
        turn_id: str,
        sink: EnvelopeSink,
        task_id: Optional[str] = None,
        extra_blocks: Optional[list[str]] = None,
        images_rel: tuple[str, ...] = (),
    ) -> None:
        orch = self.orch
        entry = orch._guests[guest_name]
        ctx = orch._build_context_for(
            guest_name, task_id=task_id, extra_blocks=extra_blocks,
        )
        # P11 C2：前台 / handoff 路径也参与 ``RoomRuntime.active_guest_names`` 维护
        # （唯一数据源是 ``RoomRuntime.guest_busy(name)``）。``add`` 必须先于任何
        # ``await``，否则同 target 的第二条 ``agent_run_start`` inbound 可能在
        # ``speak()`` 期间挤进来漏过 busy 校验。``active_guest_names is None`` ——
        # 测试夹具裸构 Orchestrator 的兼容路径，整段跳过、保持 P11 前测试零改。
        names = orch.active_guest_names
        if names is not None:
            names.add(guest_name)
        try:
            # speak() 内部负责 message_start / message_end 合成 + transcript 写入。
            # 返回 None = 失败（速度内已 emit message_end(error)）；CancelledError 透传。
            # P13：``images_rel`` 仅由 ``_run_ai_chain`` 内的 let_speak 传非空（本轮触发
            # 用户图）；drain / dormant MTS kickoff 路径的 let_speak 传默认空 tuple ——
            # 那些路径只看 ``<attachment .../>`` 文本标记、不接像素（见 P13 不变量）。
            msg = await entry.guest.speak(
                ctx, turn_id=turn_id, sink=sink, task_id=task_id,
                images_rel=images_rel,
            )
        finally:
            if names is not None:
                names.discard(guest_name)
        if msg is None:
            # 失败的发言不进 transcript（§3.5.2），冷却也不启动 —— 让他下一轮还有机会。
            return
        orch.cursor.set(guest_name, msg.seq)
        # cooldown 进入下个"AI 子轮"前先减一次，所以 +1 抵消，得到"持续 N 个 AI 子轮"。
        orch._cooldown[guest_name] = orch.config.speaker_cooldown_turns + 1
