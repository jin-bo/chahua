"""意愿打分主循环 + @ 路由 + 冷却 + 阈值衰减 + 轮数上限（设计文档 §3.3）。

单房间一个 :class:`Orchestrator`。对外只一个入口：
:meth:`Orchestrator.submit_user_message`。它做的事：

1. 把用户消息追到房间 transcript。
2. 进入 AI 链：

   - 选下一位发言者：
     - 用户消息里 ``@<茶客名>`` → 确定性路由，跳过打分（``score = 1.0``）；
     - 否则对每个不在冷却中的茶客并发跑一遍 :class:`IntentScorer`，取最高分 ≥ 阈值的那位。
   - 选不出来 → ``turn_end(status=ok, data.next="user")`` 后跳出 AI 链，等用户。
   - 选中 → emit ``turn_start({scores})``，串行调用每位 :class:`TeaGuest.speak`
     （它们各自合成 ``message_start`` / ``message_end``），随后 ``turn_end``，
     推进游标，启动冷却。
   - 连续 AI 轮数到 ``max_consecutive_ai_turns`` 或没人达到阈值 → 跳出，等用户。

3. 每轮发言后**异步**踢一次 :meth:`Summarizer.maybe_summarize`，让摘要随聊天增长 ——
   摘要 LLM 调用不挡用户等回复的路径，只供下次 onboarding 用。

P2.2：所有前端事件都套 :class:`ChahuaEnvelope`，单一 :data:`EnvelopeSink` 出口。
``turn_start`` / ``turn_end`` 在本文件合成；``message_*`` 由 :class:`TeaGuest.speak`
合成。这两类事件不一一对应：一个 turn 可包含 1~2 个 messages（top-1~2 抢话）。
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Optional

from .cursor import GuestCursor
from .events import (
    NOOP_SINK,
    STATUS_CANCELLED,
    STATUS_OK,
    ChahuaEnvelope,
    ChahuaEventType,
    EnvelopeSink,
    emit_to_sink,
    new_turn_id,
)
from .guest import TeaGuest
from .room import Message, Room, format_messages
from .scoring import IntentScorer, ScoreKind, ScoreResult
from .summarizer import Summarizer
from .user_md import USER_SPEAKER_ID, UserConfig, strip_top_h1

_log = logging.getLogger(__name__)


# ── 配置 ─────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class OrchestratorConfig:
    """房间编排参数。字段对应 ``room.toml`` ``[room]`` 段（P2 才会真读 toml）。"""

    want_threshold: float = 0.45
    """打分 ≥ 此值才允许发言。0.55 起步时招呼/中性话题全员低于阈值导致冷场（实测），
    降到 0.45 让常见话题至少有一人接住；同时配合 ``threshold_decay_per_turn`` 防 AI 跑飞。"""

    max_consecutive_ai_turns: int = 4
    """连续 AI 发言数上限；达到后强制让麦给用户。一次"同回合 1~2 人抢话"算 1~2 轮。"""

    max_speakers_per_pick: int = 2
    """同一回合最多几位茶客同时抢话。设计文档 §3.3 「取分数 ≥ 阈值的前 1~2 名」。
    设 1 退回 P1 初版"一回合一人"行为。"""

    speaker_cooldown_turns: int = 1
    """刚发言的茶客在多少"AI 轮"内打分自动归零（防止自己接自己）。"""

    threshold_decay_per_turn: float = 0.1
    """每多一轮没有用户发言、没有 @ 提及，阈值线性抬升的量。"""

    onboarding_threshold: int = 20
    """增量超过此条数 → 切回 onboarding 路径（含摘要、近 K 条原文）。"""

    onboarding_recent_messages: int = 6
    """onboarding 末尾"最近原文"块塞几条增量。仅 onboarding 路径用。"""

    onboarding_recent_summaries: int = 5
    """onboarding"近期梗概"块塞最近几条 :class:`SummarySpan` —— 防止长会话时所有
    历史摘要全往 prompt 里堆。"""

    summary_block_size: int = 20
    """transcript 自上次摘要后累积多少条触发新摘要。"""

    scoring_transcript_recent: int = 20
    """打分 prompt 里塞最近多少条 transcript（保持小，便宜模型也能吃下）。"""


# ── @ 路由 ───────────────────────────────────────────────────────────────────


# 匹配 ``@<name>``，``<name>`` 为非空白且非中英标点字符串。**故意宽匹配**
# —— ``@email@x.com`` 也会捕到 ``email``，但下游 ``name in self._guests`` 过滤掉
# 非茶客名。把"什么算合法名字"的责任放到注册表里，比在正则里硬编更稳。
_AT_PATTERN = re.compile(r"@([^\s，。！？,!?；;：:]+)")
_BROADCAST_TOKENS: frozenset[str] = frozenset(
    {"all", "everyone", "大家", "各位", "所有人"}
)


# ── 茶客注册项 ───────────────────────────────────────────────────────────────


@dataclass
class _GuestEntry:
    guest: TeaGuest
    persona_md: str


# ── 编排器 ───────────────────────────────────────────────────────────────────


class Orchestrator:
    """单房间编排器。持麦串行：一次只有一个茶客在打字冒泡。"""

    def __init__(
        self,
        *,
        room: Room,
        user_config: UserConfig,
        scorer: IntentScorer,
        summarizer: Summarizer,
        cursor: GuestCursor,
        config: OrchestratorConfig = OrchestratorConfig(),
    ) -> None:
        self.room = room
        self.user_config = user_config
        self.scorer = scorer
        self.summarizer = summarizer
        self.cursor = cursor
        self.config = config

        self._guests: dict[str, _GuestEntry] = {}
        # 茶客名 → 剩余冷却轮数（每个"AI 子轮"递减 1，到 0 解冻）。
        self._cooldown: dict[str, int] = {}
        # 同步增长的两个计数器，但在 @ 路由时只有一个会清零：
        #   _consecutive_ai_turns —— 撞硬上限用（用户一发言就清零）
        #   _rounds_without_user_or_mention —— 驱动阈值衰减（@ 也清零）
        self._consecutive_ai_turns = 0
        self._rounds_without_user_or_mention = 0
        # 缓存 ``_display_map`` —— 它只随 register() / user_config 变化，每轮被 4 次调用。
        self._display_for: Optional[dict[str, str]] = None
        # 后台摘要任务：每轮发言后被 ``_kick_summarize`` 启动；下次再 kick 时如果还在跑
        # 就跳过，避免堆积。摘要慢点不影响当前回合，所以**不 await**。
        self._summary_task: Optional[asyncio.Task[None]] = None

    # ── 注册 / 信息 ────────────────────────────────────────────────────

    def register(self, guest: TeaGuest, persona_md: str) -> None:
        """加入一位茶客。同名重复注册抛错。"""
        if guest.name in self._guests:
            raise ValueError(f"茶客 {guest.name!r} 已经注册过")
        self._guests[guest.name] = _GuestEntry(guest=guest, persona_md=persona_md)
        self.room.add_participant(guest.name)
        self._display_for = None  # 失效缓存

    @property
    def guest_names(self) -> tuple[str, ...]:
        return tuple(self._guests)

    # ── 清空 ──────────────────────────────────────────────────────────

    def reset_room(self) -> None:
        """清空房间公共状态：transcript / 摘要 / 游标 + 自身运行时计数器。

        茶客实例 / agentao session 不动 —— 茶客对用户的人设印象、自有笔记保留，但他们
        在"房间公共记录"里看不到之前发生过什么；游标归零意味着下一条消息会重新走
        onboarding 路径（重新介绍房间 + 当前在场）。

        ``_summary_task`` 若在跑就 cancel —— 它读了 clear 前的 transcript 切片，跑完
        会把陈旧 SummarySpan append 到刚清空的列表里。cancel 不 await（同 _kick_summarize
        的"摘要不挡路径"原则）；极端竞态下落进一条陈旧 span 也无伤大雅，下次 clear 也能
        重新覆盖。

        调用语义：server 端 ``_clear_room`` 入口在 ``async for raw in ws`` 串行消费里，
        与 ``submit_user_message`` 互斥，所以本函数不需要自己加锁。
        """
        self.room.clear()
        self.summarizer.clear()
        self.cursor.clear()
        self._consecutive_ai_turns = 0
        self._rounds_without_user_or_mention = 0
        self._cooldown.clear()
        if self._summary_task is not None and not self._summary_task.done():
            self._summary_task.cancel()
        self._summary_task = None

    # ── 主入口 ─────────────────────────────────────────────────────────

    async def submit_user_message(
        self,
        text: str,
        *,
        sink: Optional[EnvelopeSink] = None,
    ) -> None:
        """处理一条用户消息，跑到本回合结束（轮上限 / 没人想接话 / 失败）。

        所有前端事件走 ``sink``；``None`` = :data:`NOOP_SINK`（程序化驱动 / 测试常用）。
        本入口不 emit 用户消息事件 —— 用户消息进 transcript 是同步行为，CLI / UI 不
        依赖事件回放（前端打了字就是打了字）。
        """
        if not text:
            return
        if sink is None:
            sink = NOOP_SINK
        self.room.append(USER_SPEAKER_ID, text)
        self._consecutive_ai_turns = 0
        self._rounds_without_user_or_mention = 0
        self._tick_cooldown()
        self._kick_summarize()
        await self._run_ai_chain(sink=sink)

    # ── AI 链 ─────────────────────────────────────────────────────────

    async def _run_ai_chain(self, *, sink: EnvelopeSink) -> None:
        # 上一轮已经 emit ``turn_end(next='ai')``、UI 仍守在"停止"按钮等下一个 turn_start
        # 的那个 turn_id；下一次 pick 成功（emit 新 turn_start / turn_end）就清零，pick 失败
        # 则用这个 id 补一帧 ``turn_end(next='user')`` 让 UI 收回按钮。None = 无未结链。
        last_open_turn_id: Optional[str] = None
        while self._consecutive_ai_turns < self.config.max_consecutive_ai_turns:
            pick = await self._pick_next_speaker(
                respect_at_mention=self._consecutive_ai_turns == 0
            )
            if pick is None:
                # 没人想接话。两种子情况：
                #   a) 本回合一上来就 None（用户消息触发，但全员低分 / 全员冷却）——
                #      没 emit 过 turn_start，UI 状态干净，直接返回。
                #   b) AI 链中途 None（前一轮 next='ai'，但下一轮打分全 miss）——
                #      UI 上一轮收到 turn_end(next='ai') 守在"停止"按钮等永远不来的
                #      下一个 turn_start，补一帧 turn_end(next='user') 让它回到"发送"。
                if last_open_turn_id is not None:
                    self._emit_turn(
                        sink,
                        turn_id=last_open_turn_id,
                        type=ChahuaEventType.TURN_END,
                        data={"next": "user"},
                    )
                return

            winners, scores = pick
            turn_id = new_turn_id()
            # turn_start / turn_end 是轮级事件，跨多位茶客；guest_name=None。winners
            # 都在 data.scores 里，前端按 turn_id 聚合。
            self._emit_turn(
                sink,
                turn_id=turn_id,
                type=ChahuaEventType.TURN_START,
                data={"scores": [_score_to_dict(s) for s in scores]},
            )

            # 同回合"抢话"的多位茶客串行发言；第 2 位发言时，第 1 位的回复已经在
            # transcript 里，靠 cursor 增量自然喂入。每位都计 1 轮（max 卡死整体上限）。
            # @ 路径（单点 / broadcast）绕过本回合的内层 cap —— 用户已显式指定谁该说话，
            # 不该被 max_consecutive_ai_turns 截断；下一次 outer while 检查时 cap 自然
            # 终止 AI 链续接。
            #
            # try/except CancelledError 在这里而不是外层 —— 唯一需要补 turn_end 的窗口
            # 是 turn_start 已 emit 之后。scoring 阶段被 cancel 时 CancelledError 直穿。
            bypass_inner_cap = all(s.kind == ScoreKind.MENTION for s in scores)
            try:
                for guest_name in winners:
                    if not bypass_inner_cap and self._consecutive_ai_turns >= self.config.max_consecutive_ai_turns:
                        break
                    await self._let_speak(guest_name, turn_id=turn_id, sink=sink)
                    self._consecutive_ai_turns += 1
                    self._rounds_without_user_or_mention += 1
            except asyncio.CancelledError:
                self._emit_turn(
                    sink,
                    turn_id=turn_id,
                    type=ChahuaEventType.TURN_END,
                    data={"next": "user"},
                    status=STATUS_CANCELLED,
                )
                raise

            next_state = (
                "ai"
                if self._consecutive_ai_turns < self.config.max_consecutive_ai_turns
                else "user"
            )
            self._emit_turn(
                sink,
                turn_id=turn_id,
                type=ChahuaEventType.TURN_END,
                data={"next": next_state},
            )
            # next='ai' 时记下本 turn_id，下一轮 pick 若失败要回来补 fixup；
            # next='user' 时 UI 已自行收回按钮，无 pending 状态。
            last_open_turn_id = turn_id if next_state == "ai" else None

            # 摘要 / 冷却递减每"pick 周期"一次（不是每个发言者一次）—— 否则同回合 2 位
            # 同时说后第一位的冷却被立刻 tick 掉，下个 pick 就能再次入选，违背"刚发言不接自己"。
            self._kick_summarize()
            self._tick_cooldown()

    async def _pick_next_speaker(
        self, *, respect_at_mention: bool
    ) -> Optional[tuple[list[str], list[ScoreResult]]]:
        """选下一回合发言者；返回 ``(winners, all_scores)``，选不出 → ``None``。

        ``winners`` 是按分数倒序的茶客名列表，长度 1 ~ ``max_speakers_per_pick``
        （设计文档 §3.3「取分数 ≥ 阈值的前 1~2 名」）。@ 提及命中时只返回 1 位。

        ``respect_at_mention=True``：检查 transcript 最后一条（用户消息）里的 ``@``。
        AI 间接力时（``_consecutive_ai_turns > 0``）不再认 @ —— 否则一个茶客在自己发言里
        @ 别人会形成"自动续接"，与"持麦串行 + 上限"的设计相冲。

        优先级：``@broadcast``（@all / @所有人 等）→ 全员发言一次（含冷却中的）；
        否则 ``@<guest>`` 单点路由；否则走打分。
        """
        # 1a) @broadcast → 全员一次（用户意图明确，绕过冷却 + 打分）
        if respect_at_mention and self._find_user_broadcast():
            self._rounds_without_user_or_mention = 0
            winners = list(self._guests.keys())  # 注册顺序，确定性
            scores = [
                ScoreResult(guest_name=n, score=1.0, kind=ScoreKind.MENTION)
                for n in winners
            ]
            return winners, scores

        # 1b) @ 提及确定性路由（仅当回应用户那条时）
        if respect_at_mention:
            mention = self._find_user_mention()
            if mention is not None and self._cooldown.get(mention, 0) == 0:
                self._rounds_without_user_or_mention = 0
                return [mention], [
                    ScoreResult(
                        guest_name=mention, score=1.0, kind=ScoreKind.MENTION
                    )
                ]

        # 2) 并发打分（冷却中的茶客直接当 0 分，省 LLM 调用）
        scorables = [
            name for name in self._guests if self._cooldown.get(name, 0) == 0
        ]
        if not scorables:
            return None

        transcript_text = self._render_transcript_for_scoring()
        results: list[ScoreResult] = list(
            await asyncio.gather(
                *(self._score_one(name, transcript_text) for name in scorables)
            )
        )

        # 把冷却中那些也填进 results 让 UI 看到"为什么没选他"。
        for name in self._guests:
            if name not in scorables:
                results.append(
                    ScoreResult(
                        guest_name=name, score=0.0, kind=ScoreKind.COOLDOWN
                    )
                )

        threshold = min(
            1.0,
            self.config.want_threshold
            + self.config.threshold_decay_per_turn
            * self._rounds_without_user_or_mention,
        )
        passed = [r for r in results if r.score >= threshold]
        if not passed:
            return None
        passed.sort(key=lambda r: r.score, reverse=True)
        winners = [r.guest_name for r in passed[: self.config.max_speakers_per_pick]]
        return winners, results

    async def _score_one(
        self, guest_name: str, transcript_text: str
    ) -> ScoreResult:
        entry = self._guests[guest_name]
        return await self.scorer.score(
            guest_name=guest_name,
            persona=entry.persona_md,
            transcript_text=transcript_text,
            user_config=self.user_config,
        )

    def _find_user_mention(self) -> Optional[str]:
        """扫 transcript 最后一条消息里的 @ 提及；返回首个匹配的在场茶客名。

        @broadcast（@all / @所有人 等）不在这里处理 —— 由 :meth:`_find_user_broadcast`
        独立检查，调用方在该方法前查 broadcast，因此这里遇到 broadcast token 时
        ``continue`` 看后续是否有具体茶客名（同条消息里 ``@all @宝总`` 的极少数情况
        broadcast 优先，调用方不会走到 @ 单点路由）。
        """
        last = self.room.last_message()
        if last is None or last.speaker_id != USER_SPEAKER_ID:
            # 只承认用户消息里的 @；AI 互相 @ 不走确定性路由（见 _run_ai_chain 注释）。
            return None
        for m in _AT_PATTERN.finditer(last.text):
            name = m.group(1).strip()
            if name.lower() in _BROADCAST_TOKENS:
                continue
            if name in self._guests:
                return name
        return None

    def _find_user_broadcast(self) -> bool:
        """扫 transcript 最后一条消息里有没有 @broadcast 词（all / everyone / 大家 /
        各位 / 所有人 —— 见 :data:`_BROADCAST_TOKENS`，英文大小写无关）。

        broadcast 走"全员发言一次"路径，独立于 :meth:`_find_user_mention` 的 @ 单点路由。
        """
        last = self.room.last_message()
        if last is None or last.speaker_id != USER_SPEAKER_ID:
            return False
        for m in _AT_PATTERN.finditer(last.text):
            if m.group(1).strip().lower() in _BROADCAST_TOKENS:
                return True
        return False

    # ── 发言 ──────────────────────────────────────────────────────────

    async def _let_speak(
        self, guest_name: str, *, turn_id: str, sink: EnvelopeSink
    ) -> None:
        entry = self._guests[guest_name]
        ctx = self._build_context_for(guest_name)
        # speak() 内部负责 message_start / message_end 合成 + transcript 写入。
        # 返回 None = 失败（速度内已 emit message_end(error)）；CancelledError 透传。
        msg = await entry.guest.speak(ctx, turn_id=turn_id, sink=sink)
        if msg is None:
            # 失败的发言不进 transcript（§3.5.2），冷却也不启动 —— 让他下一轮还有机会。
            return
        self.cursor.set(guest_name, msg.seq)
        # cooldown 进入下个"AI 子轮"前先减一次，所以 +1 抵消，得到"持续 N 个 AI 子轮"。
        self._cooldown[guest_name] = self.config.speaker_cooldown_turns + 1

    def _emit_turn(
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
                room_id=self.room.name,
                turn_id=turn_id,
                guest_name=None,
                message_id=None,
                type=type,
                status=status,
                data=data,
            ),
        )

    def _tick_cooldown(self) -> None:
        for name in list(self._cooldown):
            self._cooldown[name] = max(0, self._cooldown[name] - 1)

    # ── 上下文喂养 ─────────────────────────────────────────────────────

    def _build_context_for(self, guest_name: str) -> str:
        last_seen = self.cursor.get(guest_name)
        increment = self.room.messages_since(last_seen)
        if last_seen == 0 or len(increment) > self.config.onboarding_threshold:
            return self._render_onboarding(guest_name, increment)
        return self._render_incremental(guest_name, increment)

    def _render_onboarding(
        self, guest_name: str, increment: list[Message]
    ) -> str:
        display_for = self._display_map()
        parts: list[str] = [f"[群聊·{self.room.name}]"]
        if self.room.topic:
            parts.append(f"当前话题：{self.room.topic}")
        if self.room.rules:
            parts.append(f"房间规则：{self.room.rules}")
        participants = ", ".join(
            display_for.get(p, p) for p in self.room.participants
        )
        parts.append(f"当前在场：{participants}（含人类参与者）。")

        if self.user_config.has_persona and self.user_config.full_md:
            body = strip_top_h1(self.user_config.full_md).strip()
            parts.append(
                f"\n关于「{self.user_config.display_name}」（房间里的人类参与者）：\n{body}"
            )

        if self.summarizer.summaries:
            # 只塞最近 K 段：长会话历史摘要堆爆 prompt 是真实风险。
            recent = self.summarizer.summaries[
                -self.config.onboarding_recent_summaries :
            ]
            bullets = "\n\n".join(s.text for s in recent)
            parts.append(f"\n近期梗概：\n{bullets}")

        tail = increment[-self.config.onboarding_recent_messages :]
        if tail:
            parts.append(f"\n最近原文：\n{format_messages(tail, display_for)}")

        parts.append("\n" + self._speak_instruction(guest_name))
        return "\n".join(parts) + "\n"

    def _render_incremental(
        self, guest_name: str, increment: list[Message]
    ) -> str:
        # 增量空（理论不会发生 —— 编排器只在 transcript 有新消息时调）：兜底成只指令。
        body = (
            format_messages(increment, self._display_map())
            if increment
            else "（无新消息）"
        )
        return (
            f"（房间·{self.room.name}·继续）\n"
            f"{body}\n\n"
            f"{self._speak_instruction(guest_name)}"
        )

    def _speak_instruction(self, guest_name: str) -> str:
        return (
            f"（请以「{guest_name}」的身份发言。只说你要说的内容，"
            f"不要复述别人的话，不要加引号或前缀。）"
        )

    def _render_transcript_for_scoring(self) -> str:
        """打分 prompt 里塞的 transcript 段。取最近 K 条。"""
        latest = self.room.latest_seq
        last_seen = max(0, latest - self.config.scoring_transcript_recent)
        recent = self.room.messages_since(last_seen)
        return format_messages(recent, self._display_map())

    def _display_map(self) -> dict[str, str]:
        """``speaker_id → display_name`` 映射。缓存：只随 register/user_config 变。"""
        if self._display_for is None:
            self._display_for = {
                USER_SPEAKER_ID: self.user_config.display_name,
                **{n: n for n in self._guests},
            }
        return self._display_for

    # ── 摘要（后台）────────────────────────────────────────────────────

    def _kick_summarize(self) -> None:
        """启动一次后台摘要尝试。已在跑就跳过；失败由 Summarizer 自己退避。

        **不 await** —— 摘要的产出只供"下次 onboarding"用，让它挡当前回合的 LLM 调用
        是评审里的 high 级问题。最坏情况：摘要还没出来时下次 onboarding 看到的还是上一版，
        完全可接受。
        """
        if self._summary_task is not None and not self._summary_task.done():
            return
        self._summary_task = asyncio.create_task(self._summarize_safe())

    async def _summarize_safe(self) -> None:
        try:
            await self.summarizer.maybe_summarize(
                self.room,
                self._display_map(),
                block_size=self.config.summary_block_size,
            )
        except Exception:
            _log.exception("summarize iteration failed")


# ── 序列化 ───────────────────────────────────────────────────────────────────


def _score_to_dict(r: ScoreResult) -> dict:
    """:class:`ScoreResult` → JSON-safe dict（``turn_start.data.scores`` 里塞 N 条）。"""
    return {
        "guest_name": r.guest_name,
        "score": r.score,
        "kind": r.kind.value,
    }
