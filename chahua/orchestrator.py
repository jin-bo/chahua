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
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterator, Optional

# Sentinel for "task_id arg not passed" —— 区分 None（显式无任务）vs 缺省（要内部 snapshot）。
_UNSET: Any = object()

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
from .summarizer import SummarySpan, Summarizer, TaskSummaries
from .task import Decision, Task
from .tasks_store import TasksStore
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


_BROADCAST_TOKENS: frozenset[str] = frozenset(
    {"all", "everyone", "大家", "各位", "所有人"}
)

# 短语边界标点 —— ``@`` 提及的左右两侧用 ``str.isspace()`` + 这个集合。规则：必须是
# **短语级别**分隔（空白 / 句末标点 / 开闭括号引号），不是**词内**衔接字符（``-``
# ``_`` ``/`` 等）。
#
# 用白名单而非 "isalnum 取反" 是为了过滤：
#   - URL：``https://x.com/@Elon/status``（``@`` 左边是 ``/`` → 不起匹配）
#   - 复合词：``@all-hands``（``-`` 不是边界 → 不算 broadcast）
#   - email：``support@all.com``（``@`` 左边是字母 → 不起匹配）
#
# 空白单独走 ``str.isspace()`` 以覆盖全 Unicode 空白（CJK 全角空格 ``　``、
# nbsp ``\xa0`` 等），不必手工补 codepoint。
_PHRASE_BOUNDARY_PUNCT: frozenset[str] = frozenset(
    "，。！？、；：…"  # 中文句末
    ",.!?;:"  # 英文句末
    "（）「」『』【】《》"  # 中文括号
    "()[]{}"  # 英文括号
    "\"'“”‘’"  # 引号
    "~～"  # tilde
)


def _is_phrase_boundary_char(ch: str) -> bool:
    return ch.isspace() or ch in _PHRASE_BOUNDARY_PUNCT


def _is_phrase_boundary(text: str, idx: int) -> bool:
    """``text[idx]`` 是不是短语边界（end-of-string 或空白 / 句末标点 / 括号引号）。"""
    if idx >= len(text):
        return True
    return _is_phrase_boundary_char(text[idx])


def _iter_at_positions(text: str) -> Iterator[int]:
    """yield ``text`` 里**作为提及起点**的 ``@`` 下标。

    要求 ``@`` 左边是字符串起点或短语边界字符，过滤 email / URL 里 ``@`` 紧跟字母或
    路径符的情况（``foo@x.com``、``https://x.com/@Elon`` 都不算提及）。
    """
    i = text.find("@")
    while i >= 0:
        if i == 0 or _is_phrase_boundary_char(text[i - 1]):
            yield i
        i = text.find("@", i + 1)


def _matches_at(text: str, start: int, token: str, *, case_insensitive: bool = False) -> bool:
    """``text[start:]`` 是否以 ``token`` 起头且后面是名字边界。``case_insensitive``
    给 broadcast token 用（``@ALL`` 也算）；茶客名走默认大小写敏感。"""
    end = start + len(token)
    slice_ = text[start:end]
    if case_insensitive:
        slice_ = slice_.lower()
    if slice_ != token:
        return False
    return _is_phrase_boundary(text, end)


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
        tasks_store: Optional[TasksStore] = None,
        task_summaries: Optional[TaskSummaries] = None,
    ) -> None:
        self.room = room
        self.user_config = user_config
        self.scorer = scorer
        self.summarizer = summarizer
        self.cursor = cursor
        self.config = config
        # ``None`` = 测试 / 程序化驱动场景；正常装配（session.py）注入房间唯一 store。
        # orchestrator 仅在 ``submit_user_message`` 入口 snapshot 一次 ``active_task_id``，
        # 保证整轮归属同一 task；用户在 turn 中改 active 不回追已开的发言（docs §4.4）。
        self.tasks_store = tasks_store
        # per-task summarizer 池。``None`` = 测试 / 无任务房间，跳过任务级摘要 kick。
        # session.py 装配时必带（与 tasks_store 同生命周期）。
        self.task_summaries = task_summaries

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

    def snapshot_active_task_id(self) -> Optional[str]:
        """读当前 active task id —— 给服务端在 inbound 接帧时同步快照本轮归属用。

        必须在 inbound 接帧的同步上下文里调（accept 那一刻就锁定），不能延到
        ``submit_user_message`` 里再读 —— 否则在 server 已 ``create_task`` 但 task 还没
        被调度的窗口里，一条 ``open_task`` inbound 会偷胜把后续 chat tag 改成新任务。
        """
        return self.tasks_store.active_task_id if self.tasks_store is not None else None

    async def submit_user_message(
        self,
        text: str,
        *,
        sink: Optional[EnvelopeSink] = None,
        task_id: Any = _UNSET,
    ) -> None:
        """处理一条用户消息，跑到本回合结束（轮上限 / 没人想接话 / 失败）。

        所有前端事件走 ``sink``；``None`` = :data:`NOOP_SINK`（程序化驱动 / 测试常用）。
        本入口不 emit 用户消息事件 —— 用户消息进 transcript 是同步行为，CLI / UI 不
        依赖事件回放（前端打了字就是打了字）。

        ``task_id`` 为本轮的 task 归属，sentinel-distinguished：

        - 未传 = 入口现读 ``snapshot_active_task_id()`` —— CLI / 测试这类无 race 的同步
          调用走这条路。
        - 显式传值（含 ``None``）= 用传入值，不回读 store —— server 端必须在接帧同步上下文
          先 snapshot 再传，防止 schedule 与协程实际运行之间 ``open_task`` 偷胜改 active。
        """
        if not text:
            return
        if sink is None:
            sink = NOOP_SINK
        active_task_id: Optional[str] = (
            self.snapshot_active_task_id() if task_id is _UNSET else task_id
        )
        self.room.append(USER_SPEAKER_ID, text, task_id=active_task_id)
        self._consecutive_ai_turns = 0
        self._rounds_without_user_or_mention = 0
        self._tick_cooldown()
        self._kick_summarize()
        await self._run_ai_chain(sink=sink, active_task_id=active_task_id)

    # ── AI 链 ─────────────────────────────────────────────────────────

    async def _run_ai_chain(
        self, *, sink: EnvelopeSink, active_task_id: Optional[str] = None
    ) -> None:
        # 上一轮已经 emit ``turn_end(next='ai')``、UI 仍守在"停止"按钮等下一个 turn_start
        # 的那个 turn_id；下一次 pick 成功（emit 新 turn_start / turn_end）就清零，pick 失败
        # 则用这个 id 补一帧 ``turn_end(next='user')`` 让 UI 收回按钮。None = 无未结链。
        last_open_turn_id: Optional[str] = None
        while self._consecutive_ai_turns < self.config.max_consecutive_ai_turns:
            # scoring 阶段被 cancel：上一轮已 emit turn_end(next='ai')，UI 守在「停止」
            # 按钮等下一个 turn_start。不补 fixup 的话 UI 永远收不到链终态。first-iter
            # （last_open_turn_id=None）UI 还在「发送」状态，无须补。
            try:
                pick = await self._pick_next_speaker(
                    respect_at_mention=self._consecutive_ai_turns == 0
                )
            except asyncio.CancelledError:
                if last_open_turn_id is not None:
                    self._emit_cancel_fixup(sink, turn_id=last_open_turn_id)
                raise
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
            # 发言阶段被 cancel：本 turn 的 turn_start 已 emit、message_* 可能在流。
            # scoring 阶段的 cancel 在外层 while 的 try 里独立处理。
            bypass_inner_cap = all(s.kind == ScoreKind.MENTION for s in scores)
            try:
                for guest_name in winners:
                    if not bypass_inner_cap and self._consecutive_ai_turns >= self.config.max_consecutive_ai_turns:
                        break
                    await self._let_speak(
                        guest_name, turn_id=turn_id, sink=sink,
                        task_id=active_task_id,
                    )
                    self._consecutive_ai_turns += 1
                    self._rounds_without_user_or_mention += 1
            except asyncio.CancelledError:
                self._emit_cancel_fixup(sink, turn_id=turn_id)
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

        transcript_text, recent = self._scoring_transcript()
        results: list[ScoreResult] = list(
            await asyncio.gather(
                *(
                    self._score_one(name, transcript_text, recent)
                    for name in scorables
                )
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
        self,
        guest_name: str,
        transcript_text: str,
        recent: list[Message],
    ) -> ScoreResult:
        entry = self._guests[guest_name]
        mention_count = self._count_self_mentions(guest_name, recent)
        return await self.scorer.score(
            guest_name=guest_name,
            persona=entry.persona_md,
            transcript_text=transcript_text,
            user_config=self.user_config,
            subject_mention_count=mention_count,
        )

    def _count_self_mentions(
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

    def _find_user_mention(self) -> Optional[str]:
        """扫 transcript 最后一条消息里的 @ 提及；返回首个匹配的在场茶客名。

        对每个 ``@`` 位置按**注册名长度倒序**做最长前缀匹配 + 词边界校验，所以含空格的
        名字（``Elon Musk``）也能命中——把"什么算合法名字"完全交给注册表。

        @broadcast（@all / @所有人 等）由 :meth:`_find_user_broadcast` 独立检查，
        调用方先查 broadcast，因此这里遇到 broadcast token 时跳过当前 ``@``——避免
        ``@all @宝总`` 极少数情况下被当成单点提及。
        """
        last = self.room.last_message()
        if last is None or last.speaker_id != USER_SPEAKER_ID:
            # 只承认用户消息里的 @；AI 互相 @ 不走确定性路由（见 _run_ai_chain 注释）。
            return None
        text = last.text
        # 注册名按长度倒序：``Elon Musk`` 排在 ``Elon`` 之前，最长前缀优先。
        names_by_length = sorted(self._guests, key=len, reverse=True)
        for at_idx in _iter_at_positions(text):
            start = at_idx + 1
            # 此 @ 是个 broadcast token？跳过（broadcast 由调用方独立判定）。
            if any(_matches_at(text, start, tok, case_insensitive=True) for tok in _BROADCAST_TOKENS):
                continue
            for name in names_by_length:
                if _matches_at(text, start, name):
                    return name
        return None

    def _find_user_broadcast(self) -> bool:
        """扫 transcript 最后一条消息里有没有 @broadcast 词（all / everyone / 大家 /
        各位 / 所有人 —— 见 :data:`_BROADCAST_TOKENS`，英文大小写无关）。

        匹配带词边界 —— ``@allies`` 不会被当成 ``@all``，因为 ``e`` 不是名字边界。

        broadcast 走"全员发言一次"路径，独立于 :meth:`_find_user_mention` 的 @ 单点路由。
        """
        last = self.room.last_message()
        if last is None or last.speaker_id != USER_SPEAKER_ID:
            return False
        text = last.text
        for at_idx in _iter_at_positions(text):
            start = at_idx + 1
            if any(_matches_at(text, start, tok, case_insensitive=True) for tok in _BROADCAST_TOKENS):
                return True
        return False

    # ── 发言 ──────────────────────────────────────────────────────────

    async def _let_speak(
        self,
        guest_name: str,
        *,
        turn_id: str,
        sink: EnvelopeSink,
        task_id: Optional[str] = None,
    ) -> None:
        entry = self._guests[guest_name]
        ctx = self._build_context_for(guest_name)
        # speak() 内部负责 message_start / message_end 合成 + transcript 写入。
        # 返回 None = 失败（速度内已 emit message_end(error)）；CancelledError 透传。
        msg = await entry.guest.speak(
            ctx, turn_id=turn_id, sink=sink, task_id=task_id,
        )
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

    def _emit_cancel_fixup(self, sink: EnvelopeSink, *, turn_id: str) -> None:
        """补一帧 turn_end(next='user', cancelled) —— 两条 cancel 路径共用：
        scoring 阶段 cancel 走 last_open_turn_id；speak 阶段 cancel 走当前 turn_id。
        """
        self._emit_turn(
            sink,
            turn_id=turn_id,
            type=ChahuaEventType.TURN_END,
            data={"next": "user"},
            status=STATUS_CANCELLED,
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

    def _scoring_transcript(self) -> tuple[str, list[Message]]:
        """打分用的 transcript 切片，返回 ``(格式化文本, 原始 Message 列表)``。

        文本喂打分 prompt；list 给 :meth:`_count_self_mentions` 用——后者要按
        ``speaker_id`` 排除"茶客自己之前发言里出现自己名字"的伪计数，所以不能只看
        格式化文本。
        """
        latest = self.room.latest_seq
        last_seen = max(0, latest - self.config.scoring_transcript_recent)
        recent = self.room.messages_since(last_seen)
        return format_messages(recent, self._display_map()), recent

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
        display = self._display_map()
        try:
            await self.summarizer.maybe_summarize(
                self.room, display, block_size=self.config.summary_block_size,
            )
        except Exception:
            _log.exception("summarize iteration failed")
        # 任务级摘要：与房间级共享同一后台 task，单次 kick 顺序走完。
        if self.task_summaries is not None:
            await self.task_summaries.kick(
                self.room, display, block_size=self.config.summary_block_size,
            )


# ── task 块渲染（P5.3.1，docs §6.1）──────────────────────────────────────────
#
# 纯字符串 renderer：不读 store、不接 self、不背状态分支。closed task / task 不存在的
# 判断全部在调用方 ``_build_context_for``（P5.3.2）里做 —— 取到不该注入的 task 时调用方
# 直接跳过本函数，本函数只负责"把已取好的纯数据拼成喂 LLM 的字符串"。
#
# 两态：``compact=False`` 走 onboarding 完整块（在"近期梗概"与"最近原文"之间插入）；
# ``compact=True`` 走 incremental 短 header（1-3 行）。预算控制见模块顶 _FULL_*_CAP 常量。
# ``task.owner`` 直接渲染原值（raw speaker_id，"user" 或茶客名）—— display name 映射不
# 属于渲染层，调用方按需在传入前做。

_FULL_DECISIONS_CAP = 5
"""完整块最多渲染几条决策（取最近 N 条）；超出截断。预算 ≈ 5 × 50 字。"""

_FULL_ARTIFACTS_CAP = 10
"""完整块 artifact 清单条数上限（首 N 个按名字排序的产物）。"""

_FULL_SUMMARY_TAIL_CAP = 3
"""完整块"任务近期进展"取 task summary 末几段。"""

_STATUS_DISPLAY: dict[str, str] = {
    "open": "未开始",
    "in_progress": "进行中",
    "blocked": "被阻塞",
    "done": "已完成",
    "abandoned": "已放弃",
}
"""task.status 字面值 → 中文 label。与前端 ``app/renderer/events.js`` ``TASK_STATUS_OPTIONS``
同源（手抄但单测会撞，新增 status 时两边都补）。"""


def _format_artifact_size(size: int) -> str:
    """字节数 → 人眼可读（B / KB / MB），artifact 清单展示用。"""
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / (1024 * 1024):.1f} MB"


def _format_artifact_mtime(mtime_ms: int) -> str:
    """``YYYY-MM-DD HH:MM`` 本地时区；artifact 清单展示用。"""
    return datetime.fromtimestamp(mtime_ms / 1000).strftime("%Y-%m-%d %H:%M")


def _render_task_block(
    task: Task,
    decisions: list[Decision],
    artifacts: list[dict],
    summary_tail: list[SummarySpan],
    *,
    compact: bool,
) -> str:
    """把任务上下文渲染成给茶客 LLM 的文本块（P5.3.1）。

    纯函数：不读 store、不接 self、不背状态分支。closed task / 不存在 task 的判断由
    调用方 ``_build_context_for``（P5.3.2）处理，调用方决定是否调本函数。
    """
    title = task.title or "(无标题)"
    if compact:
        first_line = task.goal.split("\n", 1)[0].strip() if task.goal else ""
        lines = [f"当前任务：{title}"]
        if first_line:
            lines.append(f"目标：{first_line}")
        lines.append("产物可从 ./task/ 读取。")
        return "\n".join(lines)

    parts: list[str] = [f"当前任务：{title}"]
    if task.goal:
        parts.append(f"目标：\n{task.goal}")
    status_display = _STATUS_DISPLAY.get(task.status, task.status)
    if task.owner:
        parts.append(f"状态：{status_display}，负责人：{task.owner}")
    else:
        parts.append(f"状态：{status_display}")

    if decisions:
        cap = decisions[-_FULL_DECISIONS_CAP:]
        bullets = "\n".join(f"- {d.summary}" for d in cap)
        parts.append(f"近期决策（最近 {len(cap)} 条）：\n{bullets}")

    if artifacts:
        cap = artifacts[:_FULL_ARTIFACTS_CAP]
        bullets = "\n".join(
            f"- {a['name']} ({_format_artifact_size(a['size'])}, "
            f"{_format_artifact_mtime(a['mtime_ms'])})"
            for a in cap
        )
        parts.append(f"当前产物清单（不嵌内容，按需走 ./task/ 读取）：\n{bullets}")

    if summary_tail:
        cap = summary_tail[-_FULL_SUMMARY_TAIL_CAP:]
        bullets = "\n\n".join(s.text for s in cap)
        parts.append(f"任务近期进展：\n{bullets}")

    return "\n\n".join(parts)


# ── 序列化 ───────────────────────────────────────────────────────────────────


def _score_to_dict(r: ScoreResult) -> dict:
    """:class:`ScoreResult` → JSON-safe dict（``turn_start.data.scores`` 里塞 N 条）。"""
    return {
        "guest_name": r.guest_name,
        "score": r.score,
        "kind": r.kind.value,
    }
