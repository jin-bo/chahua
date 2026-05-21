"""喂茶客 LLM 的 prompt 上下文组装 —— XML 包外层 + Markdown 渲内层（CLAUDE.md 关键不变量）。

从 :mod:`chahua.orchestrator` 抽出，把"调度 / pick / speak / cooldown / cancel"留在
Orchestrator，"如何把房间状态拼成喂茶客的字符串"集中在这里。Orchestrator 持一个
:class:`ContextRenderer` 实例并保留 thin 转发方法（``_build_context_for`` /
``_maybe_render_scoring_task_block`` 等）维持测试入口稳定。

display_map 缓存在 renderer 内；只有 :meth:`Orchestrator.register` 后会变（新茶客加
入），调用方负责 :meth:`invalidate_display_cache` 触发下一次重建。
"""

from __future__ import annotations

from typing import Optional
from xml.sax.saxutils import quoteattr

from .orchestrator_config import OrchestratorConfig
from .persona_summary import clamp_summary
from .room import Message, Room, format_messages
from .summarizer import SummarySpan, Summarizer, TaskSummaries
from .task import Task
from .task_rendering import (
    render_scoring_header,
    render_task_block,
    render_task_header,
    wrap_current_task,
)
from .tasks_store import CLOSED_STATUSES, TasksStore
from .user_md import USER_SPEAKER_ID, UserConfig, strip_top_h1

# speak 阶段的"goal 指定发言顺序"硬约束（scoring.py 的 ``_ORDER_HINT_BLOCK`` 对应物）。
# 措辞与 scoring 不同：scoring 给数字锚点（≤ 0.3 / ≥ 0.7），speak 给行为指令（让位句 vs 完整发言）。
# 仅在 ``task_block`` 非空时注入，无 task 时引用 ``<current_task>`` 反而迷惑 LLM。
# 与 scoring 的 hint 同口径，但**不能合并为同一字符串**：scoring 看到这条会把"输出让位句"
# 当作发言指令污染分数计算，speak 看到 scoring 的"调整想接话的程度"等同空指令。
_SPEAK_ORDER_HINT_BLOCK = (
    "<order_hint>\n"
    "如果 <current_task> 中的目标指定了发言顺序（如\"先 X 再 Y 后 Z\"、\"Z 最后总结\"），"
    "请先判断现在是否轮到你发言：\n"
    "- 没轮到你时**不要按本职角色完整发言**——只输出一句简短的让位句"
    "（如\"等评审专家先依次评完，我再来总结\"），把麦让给该轮到的角色。\n"
    "- 轮到你时按本职完整发言。\n"
    "</order_hint>"
)


class ContextRenderer:
    """房间状态 → 喂茶客 LLM 的 user message 字符串。

    持房间 / 用户配置 / 摘要器 / 任务 store 等引用，**不持状态机** —— 不知道谁在发言、
    谁在冷却、连续 AI 轮数等。display_map 缓存是唯一内部状态。
    """

    def __init__(
        self,
        *,
        room: Room,
        user_config: UserConfig,
        summarizer: Summarizer,
        config: OrchestratorConfig,
        tasks_store: Optional[TasksStore] = None,
        task_summaries: Optional[TaskSummaries] = None,
        roster: Optional[dict[str, str]] = None,
    ) -> None:
        self.room = room
        self.user_config = user_config
        self.summarizer = summarizer
        self.config = config
        self.tasks_store = tasks_store
        self.task_summaries = task_summaries
        # P8.2-roster-a：茶客名 → 一句话能力摘要。session 装配时一次性解析（手写
        # [[guest]].summary 优先，缺则查中央缓存——roster-b）；运行期增删茶客会整体
        # 重建 session，故 renderer 持的是不可变快照。仅 onboarding 的 <room> 块用。
        self.roster = roster or {}
        self._display_for: Optional[dict[str, str]] = None

    def invalidate_display_cache(self) -> None:
        """注册新茶客后必须调一次 —— display_map 缓存只随 participants 变。"""
        self._display_for = None

    # ── prompt 装配入口 ────────────────────────────────────────────────

    def build_context_for(
        self,
        guest_name: str,
        last_seen: int,
        *,
        task_id: Optional[str] = None,
        extra_blocks: Optional[list[str]] = None,
    ) -> str:
        """组 prompt 上下文。``task_id`` 是 Orchestrator 入口的 snapshot 值（不读当前
        store.active），P5.3.2 起注入到 task block；不存在 / 终结态由
        :meth:`maybe_render_task_block` 过滤后返 ``None``。

        ``extra_blocks``（P7.2）：调用方现场合成的临时块（如 ``<review_target>``），
        按列表顺序原样注入到 ``<speak_instruction>`` 之前；onboarding / incremental
        两条路径同步生效。渲染器纯转发——不认识块语义、不重排。与永久块
        （``<current_task>`` / ``<order_hint>``）是两个独立机制：后者由 ``_render_*``
        内部按 ``task_id`` 决定，不走 ``extra_blocks``。
        """
        increment = self.room.messages_since(last_seen)
        use_onboarding = (
            last_seen == 0 or len(increment) > self.config.onboarding_threshold
        )
        task_block = self.maybe_render_task_block(
            task_id, compact=not use_onboarding
        )
        if use_onboarding:
            return self._render_onboarding(
                guest_name, increment,
                task_block=task_block, extra_blocks=extra_blocks,
            )
        return self._render_incremental(
            guest_name, increment,
            task_block=task_block, extra_blocks=extra_blocks,
        )

    # ── task block 取数 + 渲染 ─────────────────────────────────────────

    def resolve_renderable_task(self, task_id: Optional[str]) -> Optional[Task]:
        """``task_id`` → ``Task``，``None`` 表示本轮不该注入任何 task 视野。

        四个 None 路径：① 缺 task_id；② 缺 store；③ store 无该任务；④ 任务终结态
        （done / abandoned）。onboarding / incremental / scoring 三条 render 路径共用
        这个 gate —— 一次 ``get_task`` 就出，不触碰 list_decisions / list_artifacts /
        task_summaries.get 三个 read API（闲聊回合 / closed task 时省掉三次 IO）。
        """
        if task_id is None or self.tasks_store is None:
            return None
        task = self.tasks_store.get_task(task_id)
        if task is None or task.status in CLOSED_STATUSES:
            return None
        return task

    def maybe_render_task_block(
        self, task_id: Optional[str], *, compact: bool
    ) -> Optional[tuple[str, str]]:
        """两步取数 + 调纯 renderer：``None`` 表示本轮不注入 task 块。

        返回 ``(body, status_display)`` 元组（调用方拼到 ``<current_task status="...">``
        XML 属性里）或 ``None``。

        compact 路径短路：renderer 在 compact=True 时只读 task.title / task.goal，
        丢弃 decisions / artifacts / summary —— 提前不取这三个数据，省掉 incremental 主
        路径每位发言茶客一次 artifacts/ 目录磁盘扫。
        """
        task = self.resolve_renderable_task(task_id)
        if task is None:
            return None
        if compact:
            return render_task_block(task, [], [], [], compact=True)
        assert self.tasks_store is not None  # resolve_renderable_task gate
        decisions = self.tasks_store.list_decisions(task_id)
        artifacts = self.tasks_store.list_artifacts(task_id)
        summary_tail: list[SummarySpan] = []
        if self.task_summaries is not None:
            handle = self.task_summaries.get(task_id)
            if handle is not None:
                summary_tail = list(handle.summaries)
        return render_task_block(
            task, decisions, artifacts, summary_tail, compact=False
        )

    def maybe_render_scoring_task_block(
        self, task_id: Optional[str]
    ) -> Optional[tuple[str, str]]:
        """P5.6：scoring 专用极简 task body —— title + **完整 goal** + status。

        与 :meth:`maybe_render_task_block(compact=True)` 区别：**不含** ``./task/`` 写入
        指引（那是发言阶段执行语义，混进打分会污染"想接话吗"的相关性信号）；并且 goal 走
        **完整内容**而非首行 —— goal 里常写"先 X 再 Y 后 Z"这类角色顺序，砍到首行会让
        打分模型把"应该最后说话"的角色误判成"现在就该说话"。每 pick 周期调一次，N 个
        scorer 共享结果，goal 膨胀不放大成本。``None`` 路径同 :meth:`resolve_renderable_task`。
        """
        task = self.resolve_renderable_task(task_id)
        if task is None:
            return None
        lines, status_display = render_scoring_header(task)
        return "\n".join(lines), status_display

    # ── 打分用 transcript ──────────────────────────────────────────────

    def scoring_transcript(self) -> tuple[str, list[Message]]:
        """打分用的 transcript 切片，返回 ``(格式化文本, 原始 Message 列表)``。

        文本喂打分 prompt；list 给 ``_count_self_mentions`` 用 —— 后者要按
        ``speaker_id`` 排除"茶客自己之前发言里出现自己名字"的伪计数，所以不能只看
        格式化文本。
        """
        latest = self.room.latest_seq
        last_seen = max(0, latest - self.config.scoring_transcript_recent)
        recent = self.room.messages_since(last_seen)
        return format_messages(recent, self.display_map()), recent

    # ── display_map（缓存）─────────────────────────────────────────────

    def display_map(self) -> dict[str, str]:
        """``speaker_id → display_name`` 映射。缓存：只随 register/user_config 变。

        从 ``room.participants`` 构建（``add_participant`` 时已含 user + 茶客名），
        USER_SPEAKER_ID 映射到 ``user_config.display_name``，其他 speaker 用本名。
        """
        if self._display_for is None:
            self._display_for = {
                p: (self.user_config.display_name if p == USER_SPEAKER_ID else p)
                for p in self.room.participants
            }
        return self._display_for

    def _render_roster(self) -> str:
        """``<room>`` 的「在场」行（P8.2-roster-a）。

        任一茶客有摘要 → 渲成 bullet 花名册（无摘要的茶客只列名）；全员无摘要 →
        退回逗号单行（roster-a 前的行为，优雅降级）。用户无摘要。
        """
        display_for = self.display_map()

        def label(p: str) -> str:
            name = display_for.get(p, p)
            return f"{name}（人类用户）" if p == USER_SPEAKER_ID else name

        entries = [
            (label(p), clamp_summary(self.roster.get(p, "")))
            for p in self.room.participants
        ]
        if not any(summary for _, summary in entries):
            return "在场：" + ", ".join(name for name, _ in entries)
        lines = ["在场："]
        for name, summary in entries:
            lines.append(f"- {name} —— {summary}" if summary else f"- {name}")
        return "\n".join(lines)

    # ── XML 块拼装 ────────────────────────────────────────────────────

    def _render_onboarding(
        self,
        guest_name: str,
        increment: list[Message],
        *,
        task_block: Optional[tuple[str, str]] = None,
        extra_blocks: Optional[list[str]] = None,
    ) -> str:
        """首次 / 长间隔回归路径：5+ 个 XML 块拼成 user message。

        XML 标签包外层 + Markdown 渲内层是茶话室喂茶客 LLM 的固定形态（见
        CLAUDE.md 关键不变量）。块顺序：``<room>`` → ``<user_persona>`` →
        ``<room_summary>`` → ``<current_task>`` → ``<order_hint>`` →
        ``<recent_messages>`` → ``<speak_instruction>``。可选块按"无内容则整块省略"
        裁剪；``<order_hint>`` 与 ``<current_task>`` 同生共灭。
        """
        display_for = self.display_map()
        blocks: list[str] = []

        # 属性值走 quoteattr 转义 —— room.name / display_name 来自用户配置，含 `"` / `<` /
        # `&` 时若裸插入会破坏 XML 边界（如 `<room name="a"&gt;<inject"`），整段块边界被
        # 篡改、LLM 看到的结构错位。quoteattr 自带外层引号，f-string 里 attr 名后直接拼。
        room_lines = [f"<room name={quoteattr(self.room.name)}>"]
        if self.room.topic:
            room_lines.append(f"话题：{self.room.topic}")
        if self.room.rules:
            room_lines.append(f"规则：{self.room.rules}")
        room_lines.append(self._render_roster())
        room_lines.append("</room>")
        blocks.append("\n".join(room_lines))

        # USER.md 自带的 H2（"## 身份" 等）被 <user_persona> 包住后自然降级为段内标题，
        # 不再与外层 XML 结构同视觉级 —— 这是 XML 化包外层的核心收益。
        #
        # 介绍语用 display_name 显式称呼（替代旧"该参与者"绕过指代）：XML 属性已经把
        # display_name 给了 LLM，body 里再用"该参与者"会让模型多做一步指代消解；直接
        # 用名字也更自然。display_name 在 body 文本（非属性）位置出现已有先例（``<room>``
        # 块的"在场"行也是裸 display_name），不引入新的注入面 —— 含 ``<`` / ``&`` 时影响
        # 的是 LLM 阅读体验而非 XML 边界（边界承重的是 ``quoteattr`` 包过的属性值）。
        if self.user_config.has_persona and self.user_config.full_md:
            body = strip_top_h1(self.user_config.full_md).strip()
            if body:
                display_name = self.user_config.display_name
                blocks.append(
                    f"<user_persona display_name={quoteattr(display_name)}>\n"
                    f"以下是 {display_name} 关于自己的说明：\n\n"
                    f"{body}\n"
                    "</user_persona>"
                )

        # 长会话历史摘要堆爆 prompt 是真实风险，只塞最近 K 段。
        if self.summarizer.summaries:
            recent = self.summarizer.summaries[
                -self.config.onboarding_recent_summaries :
            ]
            bullets = "\n\n".join(s.text for s in recent)
            blocks.append(f"<room_summary>\n{bullets}\n</room_summary>")

        if task_block:
            blocks.append(wrap_current_task(task_block))
            blocks.append(_SPEAK_ORDER_HINT_BLOCK)

        tail = increment[-self.config.onboarding_recent_messages :]
        if tail:
            blocks.append(
                f'<recent_messages count="{len(tail)}">\n'
                f"{format_messages(tail, display_for)}\n"
                "</recent_messages>"
            )

        if extra_blocks:
            blocks.extend(extra_blocks)
        blocks.append(self._speak_instruction_block(guest_name))

        return "\n\n".join(blocks) + "\n"

    def _render_incremental(
        self,
        guest_name: str,
        increment: list[Message],
        *,
        task_block: Optional[tuple[str, str]] = None,
        extra_blocks: Optional[list[str]] = None,
    ) -> str:
        """短间隔回归路径：``<room_update>`` + 可选 ``<current_task>`` + ``<order_hint>`` + ``<speak_instruction>``。

        与 onboarding 同样走 XML 包外层 + markdown 渲内层。``<room_update>`` 标签
        + name 属性已表达"房间继续"语义，不再额外加口语 header。``<order_hint>``
        与 ``<current_task>`` 同生共灭（仅在 task_block 非空时注入）。
        """
        # 增量空（理论不会发生 —— 编排器只在 transcript 有新消息时调）：兜底成只指令。
        body = (
            format_messages(increment, self.display_map())
            if increment
            else "（无新消息）"
        )
        blocks: list[str] = [
            f"<room_update name={quoteattr(self.room.name)}>\n{body}\n</room_update>"
        ]
        if task_block:
            blocks.append(wrap_current_task(task_block))
            blocks.append(_SPEAK_ORDER_HINT_BLOCK)
        if extra_blocks:
            blocks.extend(extra_blocks)
        blocks.append(self._speak_instruction_block(guest_name))
        return "\n\n".join(blocks) + "\n"

    def _speak_instruction_block(self, guest_name: str) -> str:
        body = (
            f"（请以「{guest_name}」的身份发言。只说你要说的内容，"
            f"不要复述别人的话，不要加引号或前缀。）"
        )
        return f"<speak_instruction>\n{body}\n</speak_instruction>"
