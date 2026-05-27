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
from pathlib import Path
from typing import Any, Callable, Optional

from ._orchestrator_chain import AIChainOps
from ._orchestrator_consts import _UNSET
from ._orchestrator_handoff_drain import HandoffDrainOps
from ._orchestrator_handoff_queue import HandoffQueueOps
from ._orchestrator_managed_session import ManagedSessionOps
from ._orchestrator_scoring import ScoringOps
from .artifact_detector import ArtifactDetector
from .context_renderer import ContextRenderer
from .cursor import GuestCursor
from .debug_recorder import (
    NOOP_RECORDER,
    PickDebugMeta,
    TurnRecorder,
)
from .events import (
    NOOP_SINK,
    STATUS_OK,
    ChahuaEnvelope,
    ChahuaEventType,
    EnvelopeSink,
)
from .guest import TeaGuest
from .message_artifacts import MessageArtifactRegistry
from .handoff import (
    HandoffItem,
    HandoffKind,
    ManagedSession,
)
from .orchestrator_config import OrchestratorConfig
from .room import Message, Room
from .scoring import IntentScorer, ScoreResult
from .summarizer import Summarizer, TaskSummaries
from .tasks_store import TasksStore
from .user_md import USER_SPEAKER_ID, UserConfig  # noqa: F401  # USER_SPEAKER_ID re-exported (used by tests / callers)

_log = logging.getLogger(__name__)


# ``OrchestratorConfig`` re-export 保留 —— server / session / 测试均走
# ``from chahua.orchestrator import OrchestratorConfig``，定义已搬到 :mod:`.orchestrator_config`
# 以避免与 :class:`ContextRenderer` 循环依赖。``_UNSET`` / ``_REVIEW_INSTRUCTION`` /
# ``_PANEL_SUMMARY_BLOCK`` 见 :mod:`._orchestrator_consts`（slot 拆分 Step 1）。
__all__ = ["Orchestrator", "OrchestratorConfig"]


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
        roster: Optional[dict[str, str]] = None,
        message_artifacts: Optional[MessageArtifactRegistry] = None,
        share_dir: Optional[Path] = None,
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
            roster=roster,
        )
        # 后台摘要任务：每轮发言后被 ``_kick_summarize`` 启动；下次再 kick 时如果还在跑
        # 就跳过，避免堆积。摘要慢点不影响当前回合，所以**不 await**。
        self._summary_task: Optional[asyncio.Task[None]] = None

        # P11 C2：``RoomRuntime.active_guest_names`` 的窄注入点 ——
        # ``ScoringOps.let_speak`` 进 ``speak()`` 前 ``add(name)``、finally
        # ``discard(name)``，让前台 / handoff 路径也参与 ``guest_busy()`` 视图。
        # server 装 RoomRuntime 时经 ``_attach_runtime_state`` 把同一 set 引用注入；
        # 旧测试夹具裸构 ``Orchestrator``（active_guest_names is None）时 let_speak
        # 跳过 set 维护——保持 P11 之前测试零改。
        self.active_guest_names: Optional[set[str]] = None
        # P11.2.X：``RoomRuntime.has_pending_mts_bg`` 的窄注入点 —— drain 收尾
        # （``run_pending_handoff``）凭此判断「MTS 还要不要等」，若仍有 manager-
        # attributed bg 未完成，不触发 ``MANAGER_FINISHED`` 收尾。
        # server ``_attach_runtime_state`` 重绑指向当前 runtime 的 bound method；
        # 测试 / 裸构默认 ``lambda: False``（无 bg run）保 P11 之前行为零改。
        self._has_pending_mts_bg: Callable[[], bool] = lambda: False

        # P5.4 茶客自动归集：每个 pick 周期末尾扫 active task 的 ``artifacts/``，
        # diff "上次扫到的文件名 set" emit hint + ``task_info``。逻辑搬到
        # :class:`ArtifactDetector`；本类经 ``_seen_artifacts`` 属性向测试暴露内部 dict。
        # 同时挂上房间级 ``message_artifacts``，让 detect 在 emit envelope 时挂上
        # ``originated_message_id`` + 落 ``message_artifacts.jsonl``（"气泡后挂图片
        # / 下载链"）。``None`` = 测试 / 未装配 —— 路径退化到 envelope 不带该字段。
        self._message_artifacts = message_artifacts
        # P10.2：``share_dir`` 透传给 detector 做房间公共桌面增量扫描。``None`` =
        # 测试 / 未装配 —— share/ 扫描整段跳过，行为与本变更前一致。
        self._artifact_detector = ArtifactDetector(
            room=self.room, tasks_store=tasks_store,
            share_dir=share_dir,
            message_artifacts=message_artifacts,
        )

        # slot 拆分：handoff 队列状态 + 队列相关 emit 搬到 :class:`HandoffQueueOps`
        # （详见 :mod:`._orchestrator_handoff_queue`）。主类经 ``_handoff_queue``
        # ``@property`` + 5 个薄转发方法保 API 兼容。slot 实例化在 ``__init__``
        # 末尾的 ``_install_orchestrator_slots`` 内统一装配。

        # slot 拆分：MTS 状态 + 12 个生命周期 / 推进 / proposal 拦截方法搬到
        # :class:`ManagedSessionOps`（详见 :mod:`._orchestrator_managed_session`）。
        # 主类经 ``_managed_session`` ``@property`` (含 setter, 测试直赋) +
        # ``managed_session`` ``@property`` + 6 个薄转发方法保 API 兼容。

        # slot 拆分骨架：按子域 slot 模块（handoff_queue / managed_session /
        # scoring / handoff_drain / chain）的装配入口。Step 0 仅占位，后续 step
        # 逐步把方法搬进各 slot 并在此装配。装配位必须在所有 ``self.xxx`` 初始化
        # 之后 + ``register`` / ``set_task_proposal_hook`` 之前（hook 注入要求 slot
        # 已就绪）。
        _install_orchestrator_slots(self)

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

    def snapshot_inflight_message(self) -> Optional[dict]:
        """房间里当前正在流式输出的那条消息快照；无则 ``None``。

        P9 切回一个 turn 在后台续跑的房间时，``emit_room_snapshot`` 据此补发进行中
        消息的 ``message_start`` + ``message_delta(partial_text)``。轮内发言串行
        （含 panel），任一时刻至多一条 in-flight —— 遍历茶客取第一条非 None。
        """
        for entry in self._guests.values():
            snap = entry.guest.inflight_snapshot()
            if snap is not None:
                return snap
        return None

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
        # P8.3：MTS 与 handoff 队列同瞬态——clear / 切房直接丢。不 emit
        # managed_session_ended（reset_room 后 server 重发整份 room snapshot，
        # 前端状态条随快照复位；切房 / clear 不是 MTS 自身的「结束」语义）。
        self._managed_session = None
        # 「气泡后挂图片 / 下载链」注册表与 transcript 同生命周期：transcript 清光后
        # 任何 ``originated_message_id`` 都会指向已不存在的消息，需同时截断；同样的
        # 截断不删盘上 ``tasks/<id>/artifacts/`` 内的文件（``/clear task`` 走独立路径）。
        if self._message_artifacts is not None:
            self._message_artifacts.clear()

    # ── handoff 队列（P7.1，docs/P7 §3.4）────────────────────────────────
    # slot 拆分：实现搬到 :class:`HandoffQueueOps`；下列方法 / property 为
    # 薄转发，保兼容外部调用方（server inbound / 测试 / MTS / drain）。

    @property
    def _handoff_queue(self) -> deque[HandoffItem]:
        """返回 slot 内的 deque 引用 —— ``.append`` / ``.clear`` / ``.popleft`` /
        ``len`` / ``[0]`` / bool 全部经引用直接作用于同一 deque，行为不变。"""
        return self._handoff_ops._queue

    @_handoff_queue.setter
    def _handoff_queue(self, value: deque[HandoffItem]) -> None:
        """rebind 兼容：原 plain field 支持 ``orch._handoff_queue = deque(items)``，
        property 化后保留同语义 —— 整队替换为新 deque 实例（与 ``_managed_session``
        setter 对称）。"""
        self._handoff_ops._queue = value

    def enqueue_handoff(self, item: HandoffItem) -> list[HandoffItem]:
        return self._handoff_ops.enqueue_handoff(item)

    def clear_handoff_queue(self) -> list[HandoffItem]:
        return self._handoff_ops.clear_handoff_queue()

    @property
    def has_pending_handoff(self) -> bool:
        return self._handoff_ops.has_pending_handoff

    # ── 托管任务会话（MTS，P8.3）────────────────────────────────────────
    # slot 拆分：实现搬到 :class:`ManagedSessionOps`；下列 property / 方法为
    # 薄转发。``_managed_session`` 走 getter + setter（测试直赋 ``orch._managed_session=...``
    # 兼容）；session.py 经 ``orchestrator._intercept_task_proposal`` 注入 hook，
    # 故主类必须保留同名薄转发。

    @property
    def _managed_session(self) -> Optional[ManagedSession]:
        return self._mts_ops._managed_session

    @_managed_session.setter
    def _managed_session(self, value: Optional[ManagedSession]) -> None:
        self._mts_ops._managed_session = value

    @_managed_session.deleter
    def _managed_session(self) -> None:
        """``del orch._managed_session`` 语义：等同于 ``= None``（清空 MTS）。
        原 plain field 支持 ``del``；property 化后 deleter 保 API 不变 —— 写盘
        / emit 都不发生（与 setter 同口径，纯状态翻转）。"""
        self._mts_ops._managed_session = None

    @property
    def managed_session(self) -> Optional[ManagedSession]:
        return self._mts_ops.managed_session

    def emit_managed_session_snapshot(self, sink: EnvelopeSink) -> None:
        self._mts_ops.emit_managed_session_snapshot(sink)

    def start_managed_session(
        self, sink: EnvelopeSink, *, task_id: str, manager_guest: str, budget: int,
    ) -> None:
        self._mts_ops.start_managed_session(
            sink, task_id=task_id, manager_guest=manager_guest, budget=budget,
        )

    def end_managed_session(self, sink: EnvelopeSink, *, reason: str) -> None:
        self._mts_ops.end_managed_session(sink, reason=reason)

    def check_managed_session_after_task_change(
        self, sink: EnvelopeSink,
    ) -> None:
        """slot 拆分：薄转发到 :class:`ManagedSessionOps` —— task inbound handler
        在 mutation + ``_emit_task_info`` 之后调一次，命中 ``stop_reason()`` 即收尾
        MTS（P8.4 §4.2）。与 ``start_managed_session`` / ``end_managed_session``
        同口径在主类暴露，避免 callsite 穿 ``_mts_ops`` 私 slot。"""
        self._mts_ops.check_after_task_change(sink)

    def _managed_session_stop_reason(self) -> Optional[str]:
        return self._mts_ops.stop_reason()

    def _advance_managed_session_after_turn(
        self, item: HandoffItem, sink: EnvelopeSink,
    ) -> None:
        self._mts_ops.advance_after_turn(item, sink)

    def _intercept_task_proposal(
        self, env: ChahuaEnvelope, sink: EnvelopeSink,
    ) -> bool:
        """``ChahuaTransport.task_proposal_hook`` 注入点（``session.py`` 经
        ``orchestrator._intercept_task_proposal`` 注入）。slot 拆分：实际逻辑在
        :class:`ManagedSessionOps`，主类保留同名薄转发保证 bound method 引用稳定。"""
        return self._mts_ops.intercept_task_proposal(env, sink)

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
        # P8.4.7（Codex review P8.4 round 1 P2）：MTS dormant 复活必走 drain 路径。
        #
        # 原 P8.4.1 设计 —— chain 内管理者经 hook 自动入队 → re-drain 兜底 —— 在 dormant
        # 时不成立：① 队列空 + MTS 活，drain 早 return；② chain scoring 是统计行为，
        # **不保证 manager 胜出**；③ 即便 manager 胜出，``<managed_session>`` 块是
        # ``_build_winner_blocks`` 专属（drain 独占注入点），chain 路径拿不到 → manager
        # 不知道自己处于托管中。结果：dormant 永远卡在待机，用户发什么都不复活。
        #
        # 修：用户消息进来时若 MTS 活 + 队列空 + manager 在场 + manager 不忙 → 同步
        # 补一个 manager DELEGATE kickoff 到队列 + emit ``HANDOFF_ENQUEUED`` 快照，
        # 然后既有 ``run_pending_handoff`` drain 会像 ``managed_session_start`` 路径一样
        # 消费它（manager 速跑、``<managed_session>`` 块注入、hook 自动入队 worker、
        # drain loop 自然续转）。dormant kickoff 触发后跳过 ``_run_ai_chain``：与
        # ``_inbound_managed_session_start`` 走 ``_run_handoff_turn`` wrapper 不跑 chain
        # 同口径——MTS 内对话走 manager 一线，不让 bystander 经 scoring 横插。
        #
        # manager 不在场 / 正忙 → 不补 kickoff、走常规 chain（MTS 仍 dormant，下条用户
        # 消息再试；中途 ``check_after_task_change`` / 用户停止仍能正常收尾）。
        dormant_mts_kickoff = False
        ms = self.managed_session
        if (
            ms is not None
            and not self._handoff_queue
            and ms.manager_guest in self.guest_names
            and not (
                self.active_guest_names
                and ms.manager_guest in self.active_guest_names
            )
        ):
            self._handoff_queue.append(HandoffItem(
                kind=HandoffKind.DELEGATE,
                target=ms.manager_guest,
                reason="用户消息续推",
            ))
            self._handoff_ops.emit_handoff_queue_snapshot(sink)
            dormant_mts_kickoff = True

        # P7.1: 先 drain 残留 handoff 队列（FIFO）。若上次 drain 因 ``max_consecutive_ai_turns``
        # cap 撞顶留下队首，"下次用户触发"应当恢复消费——发普通消息也是用户触发，
        # 不能让 leftover delegate 被无限跳过（docs §3.4 "下次用户触发"）。``run_pending_handoff``
        # 入口会再 reset 计数一次，与本函数 4 行前的 reset 是 no-op；drain 用完的 cap 名额
        # 由 ``_consecutive_ai_turns`` 透传给 ``_run_ai_chain``，总 AI 工作量仍受单一 cap 约束。
        # 空队列时 ``run_pending_handoff`` 早 return，零开销。
        await self.run_pending_handoff(sink, task_id=active_task_id)
        if not dormant_mts_kickoff:
            await self._run_ai_chain(sink=sink, active_task_id=active_task_id)
        # P8.4：MTS 收尾 re-drain —— chain 内管理者（被 ``@`` 强制 / scoring 胜出）经
        # ``intercept_task_proposal`` hook 自动入队 delegate / panel，chain ↔ drain
        # 严格分流不知道要切回 drain；本段在 chain 后补一次 re-drain。dormant kickoff
        # 路径上 chain 跳了 → 队列恒空 → 整段跳过零开销（docs/P8.4 §4.1）。
        #
        # ``reset_cap=False``：chain 段已累计的 ``_consecutive_ai_turns`` 不被清零，
        # chain + re-drain 段共享同一 ``max_consecutive_ai_turns`` 预算。否则会有
        # 「chain 跑 N 轮 + re-drain 又跑满 max 轮」的 cap 双扣，违反 P7.1「总 AI
        # 工作量受单一 cap 约束」不变量。读 MTS 状态走主类公开 ``managed_session``
        # property 而非穿 ``_mts_ops`` 私 slot（与本文件其它 MTS 访问点同口径）。
        if (
            self.managed_session is not None
            and self._handoff_queue
        ):
            await self.run_pending_handoff(
                sink, task_id=active_task_id, reset_cap=False,
            )

    # ── AI 链 ─────────────────────────────────────────────────────────
    # slot 拆分：``_run_ai_chain`` 实现搬到 :class:`AIChainOps`。**主类必须保留
    # 同名 method**：``tests/conftest.py`` / ``tests/test_orchestrator_run_pending_handoff.py``
    # 经 ``monkeypatch.setattr(Orchestrator, "_run_ai_chain", ...)`` 替换本方法
    # 拦截 ``submit_user_message`` 的调用 —— 主类必须是真正的实现位置（薄转发也可，
    # 但符号要在 ``Orchestrator`` 上）。

    async def _run_ai_chain(
        self, *, sink: EnvelopeSink, active_task_id: Optional[str] = None
    ) -> None:
        await self._chain_ops.run_ai_chain(
            sink=sink, active_task_id=active_task_id,
        )

    # ── handoff drain（P7.1.5，docs/P7 §3.4）─────────────────────────────

    async def run_pending_handoff(
        self, sink: EnvelopeSink, *, task_id: Optional[str],
        reset_cap: bool = True,
    ) -> None:
        """slot 拆分：薄转发到 :class:`HandoffDrainOps`。

        ``reset_cap``（P8.4）透传：默认 ``True`` 保 P7.1 既有调用语义；
        ``submit_user_message`` 末尾 re-drain 传 ``False``，让 chain + drain 段
        共享同一 cap 预算避免双扣（docs/P8.4 §4.1）。
        """
        await self._drain_ops.run_pending_handoff(
            sink, task_id=task_id, reset_cap=reset_cap,
        )

    # slot 拆分：scoring / @ 路由 / speak 搬到 :class:`ScoringOps`；下列为薄转发，
    # 保 ``_run_ai_chain`` / drain / test_orchestrator_at_mention / test_scoring_subject_hint
    # 的现有调用点不变。

    async def _pick_next_speaker(
        self,
        *,
        respect_at_mention: bool,
        active_task_id: Optional[str] = None,
    ) -> tuple[list[str], list[ScoreResult], dict[str, Optional[str]], PickDebugMeta]:
        return await self._scoring_ops.pick_next_speaker(
            respect_at_mention=respect_at_mention,
            active_task_id=active_task_id,
        )

    def _count_self_mentions(
        self, guest_name: str, recent: list[Message]
    ) -> int:
        return self._scoring_ops.count_self_mentions(guest_name, recent)

    def _find_user_mention(self) -> Optional[str]:
        return self._scoring_ops.find_user_mention()

    def _find_user_broadcast(self) -> bool:
        return self._scoring_ops.find_user_broadcast()

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
        await self._scoring_ops.let_speak(
            guest_name,
            turn_id=turn_id,
            sink=sink,
            task_id=task_id,
            extra_blocks=extra_blocks,
        )

    # slot 拆分：turn-级 envelope emit + cancel fixup 搬到 :class:`AIChainOps`；
    # 下列为薄转发，drain slot 经 ``self.orch._emit_turn(...)`` /
    # ``self.orch._cancel_fixup_and_flush(...)`` 引用保稳定。

    def _emit_turn(
        self,
        sink: EnvelopeSink,
        *,
        turn_id: str,
        type: ChahuaEventType,
        data: dict,
        status: str = STATUS_OK,
    ) -> None:
        self._chain_ops.emit_turn(
            sink, turn_id=turn_id, type=type, data=data, status=status,
        )

    def _emit_cancel_fixup(self, sink: EnvelopeSink, *, turn_id: str) -> None:
        self._chain_ops.emit_cancel_fixup(sink, turn_id=turn_id)

    def _cancel_fixup_and_flush(
        self, sink: EnvelopeSink, *, turn_id: str,
    ) -> None:
        self._chain_ops.cancel_fixup_and_flush(sink, turn_id=turn_id)

    def _emit_handoff_consumed(
        self, sink: EnvelopeSink, *, turn_id: str, item_dict: dict,
    ) -> None:
        """slot 拆分：薄转发到 :class:`HandoffQueueOps`。"""
        self._handoff_ops.emit_handoff_consumed(
            sink, turn_id=turn_id, item_dict=item_dict,
        )

    def _emit_handoff_queue_snapshot(self, sink: EnvelopeSink) -> None:
        """slot 拆分：薄转发到 :class:`HandoffQueueOps`。"""
        self._handoff_ops.emit_handoff_queue_snapshot(sink)

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

    # slot 拆分：drain 算法搬到 :class:`HandoffDrainOps`；保留下列薄转发覆盖
    # ``test_orchestrator_handoff_panel`` 的直调 + ``Orchestrator._handoff_cost``
    # classmethod-style 调用。``_advance_to_runnable_handoff`` / ``_panel_underfilled``
    # / ``_render_review_block`` / ``_render_panel_block`` 不外露，无主类转发。

    @staticmethod
    def _handoff_cost(item: HandoffItem) -> int:
        return HandoffDrainOps._handoff_cost(item)

    def _resolve_handoff_winners(
        self, item: HandoffItem,
    ) -> tuple[list[str], str]:
        return self._drain_ops._resolve_handoff_winners(item)

    def _build_winner_blocks(
        self, item: HandoffItem, winners: list[str],
    ) -> list[Optional[list[str]]]:
        return self._drain_ops._build_winner_blocks(item, winners)

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

    def mark_share_seen(self, rel: str) -> None:
        """公开访问器：把单个 ``share/`` rel 增量记进 detector 的 share_seen。

        P10.3 review 修：``server_inbound_io._upload_file`` 等 inbound 路径不应
        穿透 ``orchestrator._artifact_detector`` 私属性 —— 后者一次重命名就会让 inbound
        代码 silently AttributeError、把整条 ``file_uploaded`` echo 链条扼断、前端
        upload 队列永挂。走这层封装避免耦合到具体 detector 字段名。
        """
        self._artifact_detector.mark_share_seen(rel)

    def mark_task_artifact_seen(self, task_id: str, name: str) -> None:
        """公开访问器：把单个任务 artifact 名增量记进 detector 的 ``seen``（P10.4
        code review §3 fix）。

        与 :meth:`mark_share_seen` 同位面：``server_inbound_task._inbound_attach_artifact``
        在 P10.3 仍直接 ``orchestrator._artifact_detector.mark_seen(...)`` 穿透私属性，
        与 ``_upload_file`` 走 ``mark_share_seen`` facade 的口径不对称——下次 detector
        字段重命名会让 ``attach_artifact`` 路径 silently AttributeError，下一轮 detect()
        把用户上传的文件当 ``new_names`` 重发 ``TASK_ARTIFACT_ADDED`` 且打 ``created_by=guest``
        —— 就是 P5.8 §5.4 ``mark_seen`` 存在的原因。统一走 facade 把这个回归口子堵住。
        """
        self._artifact_detector.mark_seen(task_id, name)


# ── slot 拆分骨架（Step 0 占位） ─────────────────────────────────────────────


def _install_orchestrator_slots(orch: Orchestrator) -> None:
    """按子域 slot 模块装配 :class:`Orchestrator` 的辅助 ops 对象。

    slot 拆分：唯一真理源 + 单点 jump（仿 :func:`chahua.server._install_handler_slots`）。

    顺序约束：``_mts_ops`` / ``_drain_ops`` 写队列经 ``_handoff_ops``，故
    ``_handoff_ops`` 必先装配；``_chain_ops`` / ``_drain_ops`` 调 speak 经
    ``_scoring_ops``，故 ``_scoring_ops`` 在它们之前。
    """
    orch._handoff_ops = HandoffQueueOps(orch)
    orch._mts_ops = ManagedSessionOps(orch)
    orch._scoring_ops = ScoringOps(orch)
    orch._drain_ops = HandoffDrainOps(orch)
    orch._chain_ops = AIChainOps(orch)

