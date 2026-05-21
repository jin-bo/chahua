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
from collections import deque
from dataclasses import dataclass
from typing import Any, Optional

# Sentinel for "task_id arg not passed" —— 区分 None（显式无任务）vs 缺省（要内部 snapshot）。
_UNSET: Any = object()

from .artifact_detector import ArtifactDetector
from .context_renderer import ContextRenderer
from .cursor import GuestCursor
from .debug_recorder import (
    NOOP_RECORDER,
    SCORING_PATH_BROADCAST,
    SCORING_PATH_HANDOFF_DELEGATE,
    SCORING_PATH_HANDOFF_PANEL,
    SCORING_PATH_HANDOFF_REVIEW,
    SCORING_PATH_MENTION,
    SCORING_PATH_SCORING,
    PickDebugMeta,
    TurnRecorder,
)
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
from .handoff import HandoffItem, HandoffKind
from .mentions import BROADCAST_TOKENS, iter_at_positions, matches_at
from .orchestrator_config import OrchestratorConfig
from .room import Message, Room, format_messages
from .scoring import IntentScorer, ScoreKind, ScoreResult
from .summarizer import Summarizer, TaskSummaries
from .task_rendering import score_to_dict as _score_to_dict, wrap_current_task as _wrap_current_task
from .tasks_store import TasksStore
from .user_md import USER_SPEAKER_ID, UserConfig  # noqa: F401  # USER_SPEAKER_ID re-exported (used by tests / callers)

_log = logging.getLogger(__name__)


# ``OrchestratorConfig`` re-export 保留 —— server / session / 测试均走
# ``from chahua.orchestrator import OrchestratorConfig``，定义已搬到 :mod:`.orchestrator_config`
# 以避免与 :class:`ContextRenderer` 循环依赖。
__all__ = ["Orchestrator", "OrchestratorConfig"]


# P7.2 review：``<review_target>`` 块内的审阅指引文案。被审消息原文由
# ``format_messages`` 包装后内联在指引之上（docs §4.2）。
_REVIEW_INSTRUCTION = (
    "请给出你的审阅意见：可以是「通过」「打回」或具体修改建议，并说明理由。"
)

# P7.3 panel：``<panel_summary_request>`` 块——summarizer 专属，模块常量（不含
# per-item 数据）。summarizer 是 panel turn 的末位 speaker，N 位 panelist 的发言
# 已串行 append 进 transcript、紧贴这一发之前，summarizer 的 ``<recent_messages>``
# 自然包含——块只需告诉它"刚结束一轮圆桌、请汇总"（docs/P7.3 §4.3）。
_PANEL_SUMMARY_BLOCK = (
    "<panel_summary_request>\n"
    "房间刚结束一轮圆桌平行讨论——多位茶客各自发表了一条独立观点。\n"
    "请你把最近这几条圆桌发言汇总成一条简洁的综述：提炼共识、点出分歧、"
    "列出仍待决定的问题。\n"
    "不要逐条复述，要给出结构化的归纳。\n"
    "</panel_summary_request>"
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
        tasks_store: Optional[TasksStore] = None,
        task_summaries: Optional[TaskSummaries] = None,
        recorder: TurnRecorder = NOOP_RECORDER,
    ) -> None:
        self.room = room
        self.user_config = user_config
        self.scorer = scorer
        self.summarizer = summarizer
        self.cursor = cursor
        self.config = config
        # P6.1：每个 pick 周期一行 turns.jsonl + 可选 prompt 文件。``NOOP_RECORDER`` =
        # 测试 / [debug] enabled=false 时不动盘。``capture_prompts`` 单条件分支决定
        # 是否走 ``IntentScorer.score_with_prompt`` 拿 prompt 字符串落盘。
        self._recorder = recorder
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
        # prompt 装配 / display_map / scoring transcript 全部委托给 ContextRenderer
        # —— Orchestrator 留调度 / pick / speak / cooldown / cancel 状态机。
        # display_map 缓存在 renderer 内；register() 后必须 invalidate。
        self._renderer = ContextRenderer(
            room=room,
            user_config=user_config,
            summarizer=summarizer,
            config=config,
            tasks_store=tasks_store,
            task_summaries=task_summaries,
        )
        # 后台摘要任务：每轮发言后被 ``_kick_summarize`` 启动；下次再 kick 时如果还在跑
        # 就跳过，避免堆积。摘要慢点不影响当前回合，所以**不 await**。
        self._summary_task: Optional[asyncio.Task[None]] = None

        # P5.4 茶客自动归集：每个 pick 周期末尾扫 active task 的 ``artifacts/``，
        # diff "上次扫到的文件名 set" emit hint + ``task_info``。逻辑搬到
        # :class:`ArtifactDetector`；本类经 ``_seen_artifacts`` 属性向测试暴露内部 dict。
        self._artifact_detector = ArtifactDetector(
            room=self.room, tasks_store=tasks_store,
        )

        # P7.1 显式 handoff 队列：FIFO，新指派 append 队尾，drain loop 取队首。
        # 内存瞬态——crash/重启即丢，用户重指即可；落盘的复杂度（与 transcript
        # 顺序一致性 / cancel 时机）不值（docs/P7 §3.1）。``reset_room`` 同清。
        self._handoff_queue: deque[HandoffItem] = deque()

    # ── 注册 / 信息 ────────────────────────────────────────────────────

    def register(self, guest: TeaGuest, persona_md: str) -> None:
        """加入一位茶客。同名重复注册抛错。"""
        if guest.name in self._guests:
            raise ValueError(f"茶客 {guest.name!r} 已经注册过")
        self._guests[guest.name] = _GuestEntry(guest=guest, persona_md=persona_md)
        self.room.add_participant(guest.name)
        self._renderer.invalidate_display_cache()

    def set_config(self, config: OrchestratorConfig) -> None:
        """热替换编排参数 —— 同步更新 Orchestrator 自身 + 内嵌 ContextRenderer 的 ref。

        ContextRenderer 持独立 ``self.config`` ref（读 onboarding_threshold /
        onboarding_recent_messages / scoring_transcript_recent 等）；只改
        ``orchestrator.config`` 会让这几个字段沿用旧值，导致 UI 改编排参数后行为不一致。
        ``swap_room_config`` 的唯一调用方走这里。
        """
        self.config = config
        self._renderer.config = config

    @property
    def guest_names(self) -> tuple[str, ...]:
        return tuple(self._guests)

    def get_guest(self, name: str) -> Optional[TeaGuest]:
        """按 name 取茶客实例；不在场返 ``None``。

        ``_guests`` 私有，外部查茶客走这个公开访问器 —— 与 :attr:`guest_names`
        同源，反映运行时 add/remove guest，不读 ``RoomSession.guests`` boot 快照。
        """
        entry = self._guests.get(name)
        return entry.guest if entry is not None else None

    # ── 清空 ──────────────────────────────────────────────────────────

    def reset_room(self) -> None:
        """清空房间公共状态：transcript / 摘要 / 游标 + 自身运行时计数器。

        分清两层"茶客记忆"：

        - 进程内对话窗口 = ``agent.messages`` —— 跨 ``arun`` 累加的 user / assistant
          列表，**必须随 clear 同步清**。否则下一次发言时 LLM 看到的是"clear 前全部
          对话 + onboarding 重新介绍房间"，前后矛盾，茶客容易出戏。
        - 跨重启长期记忆 = ``<workdir>/.agentao/memory.db`` —— ``clear_history()`` 不
          动盘，所以茶客对用户的人设印象 / 自有笔记仍保留，与"清空聊天 ≠ 重置茶客
          认知"的语义一致。

        ``agent.clear_history()`` 同时清 active skills / todos / token counter ——
        这几样都是会话窗口内状态（"刚刚还在做的事"），与"重启房间公共视图"语义自洽。
        异常按茶客隔离吞（一个 agent 坏掉不阻断其它茶客 reset），WARN 落日志。

        游标归零意味着下一条消息会重新走 onboarding 路径（重新介绍房间 + 当前在场）。

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
        for name, entry in self._guests.items():
            try:
                entry.guest.agent.clear_history()
            except Exception:
                _log.warning("clear_history failed for guest %r", name, exc_info=True)
        if self._summary_task is not None and not self._summary_task.done():
            self._summary_task.cancel()
        self._summary_task = None
        # P7.1 handoff 队列也属于房间瞬态：用户清房后保留旧指派会让"清空"语义被
        # 破坏（下一句还是 clear 前的 delegate 目标）。与 transcript / cursor /
        # summarizer 同口径。
        self._handoff_queue.clear()

    # ── handoff 队列（P7.1，docs/P7 §3.4）────────────────────────────────

    def enqueue_handoff(self, item: HandoffItem) -> list[HandoffItem]:
        """append 到队尾；**不**启动执行 / 不 emit envelope。

        envelope emit 职责由 server inbound handler 负责（P7.1.6 加）：handler
        拿到本方法返回的队列快照后 emit ``HANDOFF_ENQUEUED``；orchestrator 持
        sink 是 P7 反向评审 v3-#4 明确避免的越权耦合。
        """
        self._handoff_queue.append(item)
        return list(self._handoff_queue)

    def clear_handoff_queue(self) -> list[HandoffItem]:
        """清空队列；返回被丢项给 server emit ``HANDOFF_CLEARED``。"""
        dropped = list(self._handoff_queue)
        self._handoff_queue.clear()
        return dropped

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
        # P7.1: 先 drain 残留 handoff 队列（FIFO）。若上次 drain 因 ``max_consecutive_ai_turns``
        # cap 撞顶留下队首，"下次用户触发"应当恢复消费——发普通消息也是用户触发，
        # 不能让 leftover delegate 被无限跳过（docs §3.4 "下次用户触发"）。``run_pending_handoff``
        # 入口会再 reset 计数一次，与本函数 4 行前的 reset 是 no-op；drain 用完的 cap 名额
        # 由 ``_consecutive_ai_turns`` 透传给 ``_run_ai_chain``，总 AI 工作量仍受单一 cap 约束。
        # 空队列时 ``run_pending_handoff`` 早 return，零开销。
        await self.run_pending_handoff(sink, task_id=active_task_id)
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
            # **scoring 阶段无 recorder turn 在飞**：turn_id 在 pick 之后才 mint，所以
            # 此处 cancel 不需要 flush_turn。要记本周期"为什么没说"得等 pick 跑完后。
            try:
                winners, scores, prompts_by_guest, debug_meta = await self._pick_next_speaker(
                    respect_at_mention=self._consecutive_ai_turns == 0,
                    active_task_id=active_task_id,
                )
            except asyncio.CancelledError:
                if last_open_turn_id is not None:
                    self._emit_cancel_fixup(sink, turn_id=last_open_turn_id)
                raise

            # 始终 mint turn_id + start_turn + record_scoring —— 无人接话也记一行
            # （不变量"每个 pick 周期一行 turns.jsonl"，docs §数据模型 P6）。
            turn_id = new_turn_id()
            self._recorder.start_turn(
                turn_id=turn_id,
                task_id=active_task_id,
                trigger=self._compute_trigger(),
            )
            self._recorder.record_scoring(
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
                self._recorder.flush_turn()
                if last_open_turn_id is not None:
                    self._emit_turn(
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
            if self._recorder.capture_prompts:
                # 关键：``@guest`` / ``@all`` 路径绕过 LLM 打分 ⇒ ``prompts_by_guest`` 为空。
                # 必须 **始终** 写 key（哪怕值是 ``{}``），让前端区分"capture 已关"（key
                # 缺）与"capture 开但本 turn 无 LLM 打分"（空字典）—— 否则 mention /
                # broadcast turn 会被误标"prompt 捕获已关"。
                turn_data["scoring_prompts"] = {
                    g: p for g, p in prompts_by_guest.items() if p
                }
            self._emit_turn(
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
                    if not bypass_inner_cap and self._consecutive_ai_turns >= self.config.max_consecutive_ai_turns:
                        break
                    await self._let_speak(
                        guest_name, turn_id=turn_id, sink=sink,
                        task_id=active_task_id,
                    )
                    self._consecutive_ai_turns += 1
                    self._rounds_without_user_or_mention += 1
            except asyncio.CancelledError:
                self._cancel_fixup_and_flush(sink, turn_id=turn_id)
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

            # 正常 turn 完结 —— flush 一行到 turns.jsonl 后清 in-flight（docs P6）。
            self._recorder.flush_turn()

            # 摘要 / 冷却递减每"pick 周期"一次（不是每个发言者一次）—— 否则同回合 2 位
            # 同时说后第一位的冷却被立刻 tick 掉，下个 pick 就能再次入选，违背"刚发言不接自己"。
            self._kick_summarize()
            self._tick_cooldown()
            # P5.4 自动归集：扫 active task 的 artifacts/，emit 新文件的 hint + task_info。
            self._kick_detect_new_artifacts(sink, active_task_id)

    # ── handoff drain（P7.1.5，docs/P7 §3.4）─────────────────────────────

    async def run_pending_handoff(
        self, sink: EnvelopeSink, *, task_id: Optional[str],
    ) -> None:
        """专职消费 handoff 队列；与 :meth:`_run_ai_chain` **严格分流，不互相回落**。

        队列空 / 下一项 cap 撞顶 → emit ``turn_end(next='user')`` 后 return，
        **不回落到 scoring**——这是"delegate 仅本轮独占，下次用户触发才恢复打分"
        的工程基础（docs §4.3 / §8 不变量）。

        **入口清零计数一次**：handoff 是用户显式触发但不走 ``submit_user_message``，
        上一轮 AI 到上限时不清零 while 会立刻挡掉 → handoff 永不执行。只清一次，
        drain loop 内不再清。
        """
        if not self._handoff_queue:
            return
        self._consecutive_ai_turns = 0
        self._rounds_without_user_or_mention = 0

        while self._handoff_queue:
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
                self._consecutive_ai_turns + cost
                > self.config.max_consecutive_ai_turns
            ):
                break
            self._handoff_queue.popleft()

            # winner_blocks 在过滤之后构造——panel 的 <panel_context> 必须按**存活**
            # panelist 渲染，否则被移除的茶客会被列进名单（codex review P2）。winners
            # 与 winner_blocks 由 _build_winner_blocks 逐 winner 构造、天然等长。
            winner_blocks = self._build_winner_blocks(item, winners)
            turn_id = new_turn_id()
            item_dict = item.to_dict()
            self._recorder.start_turn(
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
            self._recorder.record_scoring(
                threshold=None, scorables=[], cooled=[],
                results=[(r, None) for r in score_results],
                winners=winners,
                scoring_path=scoring_path,
            )
            self._emit_turn(
                sink, turn_id=turn_id, type=ChahuaEventType.TURN_START,
                data={
                    "scores": [_score_to_dict(r) for r in score_results],
                    "scoring_path": scoring_path,
                },
            )
            self._emit_handoff_consumed(sink, turn_id=turn_id, item_dict=item_dict)

            try:
                # panel = N(+1) 次串行 speak；每位 winner 取自己等长对齐的
                # extra_blocks（panelist 共享 <panel_context>、summarizer 拿
                # <panel_summary_request>；delegate / review 单元素与 P7.2 同）。
                for name, blocks in zip(winners, winner_blocks):
                    await self._let_speak(
                        name, turn_id=turn_id, sink=sink, task_id=task_id,
                        extra_blocks=blocks,
                    )
                    self._consecutive_ai_turns += 1
            except asyncio.CancelledError:
                self._cancel_fixup_and_flush(sink, turn_id=turn_id)
                raise

            # 末尾顺序对齐 _run_ai_chain（docs §8 不变量）：peek→turn_end→flush→hooks。
            # lookahead 必须走与 drain 主体同一个 _advance_to_runnable_handoff——
            # 否则死项 / 欠员 panel 会让 has_next 误判成"下一轮 AI"，但该项下一轮被
            # 静默 drop、永远不产 turn，客户端卡在 turn_end(next='ai')（codex review P2）。
            next_runnable = self._advance_to_runnable_handoff(sink)
            if next_runnable is not None:
                next_cost = self._handoff_cost(next_runnable[0])
                has_next = (
                    self._consecutive_ai_turns + next_cost
                    <= self.config.max_consecutive_ai_turns
                )
            else:
                has_next = False
            next_state = "ai" if has_next else "user"
            self._emit_turn(
                sink, turn_id=turn_id, type=ChahuaEventType.TURN_END,
                data={"next": next_state},
            )
            self._recorder.flush_turn()
            self._kick_summarize()
            self._tick_cooldown()
            self._kick_detect_new_artifacts(sink, task_id)
            if not has_next:
                return

    async def _pick_next_speaker(
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
        all_guests = list(self._guests.keys())

        # 1a) @broadcast → 全员一次（用户意图明确，绕过冷却 + 打分）
        if respect_at_mention and self._find_user_broadcast():
            self._rounds_without_user_or_mention = 0
            winners = list(all_guests)  # 注册顺序，确定性
            scores = [
                ScoreResult(guest_name=n, score=1.0, kind=ScoreKind.MENTION)
                for n in winners
            ]
            return winners, scores, {}, PickDebugMeta(
                threshold=None, scoring_path=SCORING_PATH_BROADCAST,
            )

        # 1b) @ 提及确定性路由（仅当回应用户那条时）
        if respect_at_mention:
            mention = self._find_user_mention()
            if mention is not None and self._cooldown.get(mention, 0) == 0:
                self._rounds_without_user_or_mention = 0
                scores = [
                    ScoreResult(
                        guest_name=mention, score=1.0, kind=ScoreKind.MENTION
                    )
                ]
                return [mention], scores, {}, PickDebugMeta(
                    threshold=None, scoring_path=SCORING_PATH_MENTION,
                )

        # 2) 并发打分（冷却中的茶客直接当 0 分，省 LLM 调用）
        scorables = [
            name for name in all_guests if self._cooldown.get(name, 0) == 0
        ]
        cooled = [name for name in all_guests if name not in scorables]

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
        # 不进 _score_one —— 那样 N 茶客就要 N 次 get_task。closed / missing → "".
        rendered = self._maybe_render_scoring_task_block(active_task_id)
        task_block = (
            "\n" + _wrap_current_task(rendered) + "\n" if rendered else ""
        )

        transcript_text, recent = self._scoring_transcript()
        scored: list[tuple[ScoreResult, Optional[str]]] = list(
            await asyncio.gather(
                *(
                    self._score_one(name, transcript_text, recent, task_block)
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
            self.config.want_threshold
            + self.config.threshold_decay_per_turn
            * self._rounds_without_user_or_mention,
        )
        debug_meta = PickDebugMeta(
            threshold=threshold, scorables=scorables, cooled=cooled,
            scoring_path=SCORING_PATH_SCORING,
        )
        passed = [r for r in results if r.score >= threshold]
        if not passed:
            return [], results, prompts_by_guest, debug_meta
        passed.sort(key=lambda r: r.score, reverse=True)
        winners = [r.guest_name for r in passed[: self.config.max_speakers_per_pick]]
        return winners, results, prompts_by_guest, debug_meta

    async def _score_one(
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
        entry = self._guests[guest_name]
        mention_count = self._count_self_mentions(guest_name, recent)
        if self._recorder.capture_prompts:
            return await self.scorer.score_with_prompt(
                guest_name=guest_name,
                persona=entry.persona_md,
                transcript_text=transcript_text,
                user_config=self.user_config,
                subject_mention_count=mention_count,
                task_block=task_block,
            )
        result = await self.scorer.score(
            guest_name=guest_name,
            persona=entry.persona_md,
            transcript_text=transcript_text,
            user_config=self.user_config,
            subject_mention_count=mention_count,
            task_block=task_block,
        )
        return result, None

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
        for at_idx in iter_at_positions(text):
            start = at_idx + 1
            # 此 @ 是个 broadcast token？跳过（broadcast 由调用方独立判定）。
            if any(matches_at(text, start, tok, case_insensitive=True) for tok in BROADCAST_TOKENS):
                continue
            for name in names_by_length:
                if matches_at(text, start, name):
                    return name
        return None

    def _find_user_broadcast(self) -> bool:
        """扫 transcript 最后一条消息里有没有 @broadcast 词（all / everyone / 大家 /
        各位 / 所有人 —— 见 :data:`BROADCAST_TOKENS`，英文大小写无关）。

        匹配带词边界 —— ``@allies`` 不会被当成 ``@all``，因为 ``e`` 不是名字边界。

        broadcast 走"全员发言一次"路径，独立于 :meth:`_find_user_mention` 的 @ 单点路由。
        """
        last = self.room.last_message()
        if last is None or last.speaker_id != USER_SPEAKER_ID:
            return False
        text = last.text
        for at_idx in iter_at_positions(text):
            start = at_idx + 1
            if any(matches_at(text, start, tok, case_insensitive=True) for tok in BROADCAST_TOKENS):
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
        extra_blocks: Optional[list[str]] = None,
    ) -> None:
        entry = self._guests[guest_name]
        ctx = self._build_context_for(
            guest_name, task_id=task_id, extra_blocks=extra_blocks,
        )
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

    def _cancel_fixup_and_flush(
        self, sink: EnvelopeSink, *, turn_id: str,
    ) -> None:
        """speak 阶段 cancel 共用 fixup：补 turn_end(cancelled) + flush 半截
        turns.jsonl 行（``_run_ai_chain`` / ``run_pending_handoff`` 两条 drain
        路径同步走，避免两处各写一遍漂移）。
        """
        self._emit_cancel_fixup(sink, turn_id=turn_id)
        self._recorder.flush_turn()

    def _emit_handoff_consumed(
        self, sink: EnvelopeSink, *, turn_id: str, item_dict: dict,
    ) -> None:
        """``HANDOFF_CONSUMED`` envelope：顶层 ``turn_id`` 与本轮 turn_start / turn_end
        同值（关联取证），``data={"item": <HandoffItem.to_dict()>}``。
        """
        emit_to_sink(
            sink,
            ChahuaEnvelope(
                room_id=self.room.name, turn_id=turn_id,
                guest_name=None, message_id=None,
                type=ChahuaEventType.HANDOFF_CONSUMED,
                data={"item": item_dict},
            ),
        )

    def _emit_handoff_queue_snapshot(self, sink: EnvelopeSink) -> None:
        """重发 ``HANDOFF_ENQUEUED`` 权威快照（连接级，``turn_id`` 全 None）。

        drain 内静默 drop 跑不起来的队列项后必须调一次——前端队列镜像只认
        ``HANDOFF_ENQUEUED`` / ``HANDOFF_CONSUMED`` / ``HANDOFF_CLEARED`` 三事件，
        ``_advance_to_runnable_handoff`` 直接 ``popleft`` 死项不发事件会让队列预览
        残留死项、后续 consumed 又对不上位（codex review P2）。``HANDOFF_ENQUEUED``
        的语义本就是"权威快照、整体替换"（见 ``handoff_state.js``），重发即修正。
        """
        emit_to_sink(
            sink,
            ChahuaEnvelope(
                room_id=self.room.name, turn_id=None,
                guest_name=None, message_id=None,
                type=ChahuaEventType.HANDOFF_ENQUEUED,
                data={"queue": [i.to_dict() for i in self._handoff_queue]},
            ),
        )

    def _compute_trigger(self) -> dict[str, Any]:
        """构造本周期 turns.jsonl 的 ``trigger`` 字段（docs §数据模型）。

        - ``kind="user_msg"``：本周期是用户消息触发的第一轮（``_consecutive_ai_turns==0``）。
        - ``kind="ai_chain"``：AI 接力中（前一轮 next='ai'）。
        - ``ref_seq``：transcript 最后一条消息的 seq —— 用户路径指向用户那条，AI 链路径
          指向上一位 AI 的发言。``None`` = transcript 全空（极少；onboarding 期）。

        ``kind`` 只分 ``user_msg`` / ``ai_chain`` 两态 —— 全部 turn 都由真用户消息
        （``_inbound_user_message``）触发；P5.8 起 task 事件不再合成 user turn。
        """
        last = self.room.last_message()
        return {
            "kind": "user_msg" if self._consecutive_ai_turns == 0 else "ai_chain",
            "ref_seq": last.seq if last is not None else None,
        }

    def _tick_cooldown(self) -> None:
        for name in list(self._cooldown):
            self._cooldown[name] = max(0, self._cooldown[name] - 1)

    # ── 上下文喂养（委托 ContextRenderer）─────────────────────────────

    def _sync_renderer(self) -> None:
        """同步 ``user_config`` 到 renderer。测试场景里直接 mutate ``orch.user_config``
        要让下一次 render 看到新值；display_map 缓存里也固化了 display_name，所以一并
        invalidate。生产路径不调，user_config 启动后即不动。
        """
        if self._renderer.user_config is not self.user_config:
            self._renderer.user_config = self.user_config
            self._renderer.invalidate_display_cache()

    def _build_context_for(
        self,
        guest_name: str,
        *,
        task_id: Optional[str] = None,
        extra_blocks: Optional[list[str]] = None,
    ) -> str:
        """转发到 :meth:`ContextRenderer.build_context_for`。"""
        self._sync_renderer()
        return self._renderer.build_context_for(
            guest_name, self.cursor.get(guest_name),
            task_id=task_id, extra_blocks=extra_blocks,
        )

    def _maybe_render_scoring_task_block(
        self, task_id: Optional[str]
    ) -> Optional[tuple[str, str]]:
        """转发到 :meth:`ContextRenderer.maybe_render_scoring_task_block`。"""
        return self._renderer.maybe_render_scoring_task_block(task_id)

    def _display_map(self) -> dict[str, str]:
        """转发到 :meth:`ContextRenderer.display_map`。"""
        self._sync_renderer()
        return self._renderer.display_map()

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
        msg = self.room.message_by_id(item.review_message_id)
        assert msg is not None, (
            f"review 项 {item.review_message_id!r} 的被审消息缺失——违反 docs §4.1 不变量"
        )
        message_xml = format_messages([msg], self._display_map())
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
        dmap = self._display_map()
        names = "、".join(dmap.get(t, t) for t in panelists)
        return (
            "<panel_context>\n"
            f"本轮是一次圆桌平行讨论，参与的茶客是：{names}。\n"
            "请你独立给出自己的观点，不必附和、也不必复述其他茶客已经说过的内容；\n"
            "如果你与前面发言的茶客看法不同，直接说出分歧。这是一次平行表态，"
            "不是接龙。\n"
            "</panel_context>"
        )

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

        "跑不起来"= runtime 重校验后必被 drop 的三类（docs §4.1.1 / §4.4），
        pop + WARN：① 死项（``cost > max_consecutive_ai_turns``，无论计数都跑不动，
        典型成因：入队后用户调低 max）；② panel 欠员（存活 panelist < 2）；
        ③ winner 全失效（delegate / review target 被 ``remove_guest`` 删掉）。
        丢弃任意项后重发一次 ``HANDOFF_ENQUEUED`` 权威快照，让前端队列预览不残留
        死项（codex review P2）。

        **不**含"此刻预算够不够"的判断——撞 ``_consecutive_ai_turns`` cap（档②）
        的项**能**跑、只是当下不跑，留在队首不弹，由调用方 cap 检查负责。

        drain loop 主体与 turn 末尾 ``has_next`` lookahead **共用**这一个 helper：
        否则 lookahead 拿"将被静默 drop 的项"算 ``has_next`` → 误报
        ``turn_end(next='ai')`` → 客户端等一个永不到来的 turn（codex review P2）。
        """
        dropped = False
        try:
            while self._handoff_queue:
                item = self._handoff_queue[0]
                if (
                    self._handoff_cost(item)
                    > self.config.max_consecutive_ai_turns
                ):
                    _log.warning(
                        "handoff item cost exceeds max_consecutive_ai_turns; "
                        "dropping: %r", item,
                    )
                    self._handoff_queue.popleft()
                    dropped = True
                    continue
                winners, scoring_path = self._resolve_handoff_winners(item)
                winners = [
                    w for w in winners if w is not None and w in self._guests
                ]
                if self._panel_underfilled(item, winners):
                    _log.warning(
                        "panel item underfilled after revalidation; "
                        "dropping: %r", item,
                    )
                    self._handoff_queue.popleft()
                    dropped = True
                    continue
                if not winners:
                    _log.warning(
                        "handoff item target unavailable; dropping: %r", item,
                    )
                    self._handoff_queue.popleft()
                    dropped = True
                    continue
                return item, winners, scoring_path
            return None
        finally:
            if dropped:
                self._emit_handoff_queue_snapshot(sink)

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
        return [None]

    @staticmethod
    def _panel_underfilled(item: HandoffItem, kept_winners: list[str]) -> bool:
        """panel 项 runtime 过滤后有效 panelist（不含 summarizer）< 2 → 判欠员
        （docs §4.4）。非 panel 项恒 ``False``。"""
        if item.kind is not HandoffKind.PANEL:
            return False
        kept_panelists = [w for w in kept_winners if w != item.summarizer]
        return len(kept_panelists) < 2

    def _scoring_transcript(self) -> tuple[str, list[Message]]:
        """转发到 :meth:`ContextRenderer.scoring_transcript`。"""
        self._sync_renderer()
        return self._renderer.scoring_transcript()

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

    # ── 茶客自动归集（委托 ArtifactDetector）────────────────────────────

    @property
    def _seen_artifacts(self) -> dict[str, set[str]]:
        """``task_id → 上次扫到的 artifact 文件名 set``。

        测试通过本属性读 detector 内部状态；属性返 detector 持有的 dict 引用，
        ``orch._seen_artifacts[task_id] == {...}`` 与原本一致。
        """
        return self._artifact_detector.seen

    def _kick_detect_new_artifacts(
        self, sink: EnvelopeSink, active_task_id: Optional[str]
    ) -> None:
        """转发到 :meth:`ArtifactDetector.detect`。

        触发：``_run_ai_chain`` 每个 pick 周期末尾。茶客直接写 ``./task/<name>``
        软链后，落在 ``tasks/<active>/artifacts/<name>``。
        """
        self._artifact_detector.detect(sink, active_task_id)

