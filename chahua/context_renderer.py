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
from xml.sax.saxutils import escape, quoteattr

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
    "If <current_task> specifies a speaking order (e.g. \"X then Y then Z\", "
    "\"Z summarizes last\"), check whether it is your turn:\n"
    "- Not your turn? Yield in one short line (e.g. \"I'll wrap up after the "
    "reviewers finish\") — do NOT speak in full.\n"
    "- Your turn? Speak in full.\n"
    "</order_hint>"
)


def render_agent_run_block(
    *, instruction: str, issued_by: str, source_guest: Optional[str] = None,
) -> str:
    """P11：bg run 茶客的 ``<agent_run_task>`` 临时块。

    纯函数 —— 措辞固定、不读 store、不背状态。经 ``extra_blocks`` 注入到
    ``<speak_instruction>`` 之前；只对被指派 bg run 的茶客那一次 speak 注入。

    告诉茶客三件事：① 这次发言由用户后台指派（或某茶客经 ``spawn_*`` 工具发起），
    指令在 ``instruction=...`` 里；② 后台执行无流式刷屏，请一次性把结论写完；③ 完成
    后房间内不需再有"等你下一句"的语义 —— 用户会另外发起新轮。

    ``instruction`` / ``source_guest`` / ``issued_by`` 都走 XML 转义防注入 ——
    ``issued_by`` / ``source_guest`` 在 attr 走 ``quoteattr``；``instruction`` 在
    text-node 走 :func:`xml.sax.saxutils.escape` —— 用户指令含 ``</instruction>``
    无法逃出块边界（与 ``<room>`` / ``<managed_session>`` 等块同口径）。
    """
    src_attr = (
        f" source_guest={quoteattr(source_guest)}" if source_guest else ""
    )
    return (
        f"<agent_run_task issued_by={quoteattr(issued_by)}{src_attr}>\n"
        "You are running independently as a background Agent run.\n"
        "- This turn is driven directly by the instruction below, not by room "
        "scoring / @-mentions.\n"
        "- Background execution does NOT stream to the chat — give your full "
        "conclusion / artifact paths in one shot, no placeholder lines.\n"
        "- When done, the room needs no \"waiting for your next line\" — the user / "
        "scheduler decides what follows.\n"
        "Always reply in the same language as the conversation (default: Chinese).\n"
        f"<instruction>{escape(instruction)}</instruction>\n"
        "</agent_run_task>"
    )


def render_managed_session_block(manager: str, budget: int) -> str:
    """P8.3 / P8.4：管理者 MTS 回合的 ``<managed_session>`` 临时块（docs §7 / P8.4 §5）。

    纯函数 —— 措辞固定、不读 store、不背状态。经 ``extra_blocks`` 注入到
    ``<speak_instruction>`` 之前；只管理者回合注入，worker 回合不注入。

    告诉管理者（与渲染顺序一致）：① 复查后**直接** ``propose_delegate`` /
    ``propose_panel`` 指派下一步，托管期自动生效、不要交回用户；② 工具 ack 若说
    「等用户采纳」在托管会话中请忽略（第 ② 条专门作废 P7.4 ``propose_*`` 工具 ack
    的等待语义 —— 为保 ``handoff_tools.py`` 零改动不改工具，改由本块纠偏）；
    ③ P11.2：多人并发用 ``spawn_agent_runs`` —— 绕开 budget 做并发分发
    （budget 控制管理者**串行**复查深度；``spawn_*`` 是并行后台调度，两者正交）；
    ④ **P8.4 / P8.6**：**没有可派的下一步**时（需要再读资料 / 等用户输入 / 卡壳等），
    **直接讲完当前发言并结束本轮，不要为了维持会话而硬派活**；MTS 会自动等用户下一句话
    再续、不因这一轮没派活而结束。**P8.6 防过早放弃**：首次受阻不要 propose blocked ——
    同一阻塞连续 ≥3 复查回合才可标 blocked（codex `continuation.md` 同款节流）；⑤ **P8.6
    反目标缩水**：Goal 须逐条核验**全部** task scope 后才算达成（**不得**把目标缩成更易
    通过的子集），再用 ``task_propose_status("done")`` 收尾（该提议给用户确认、不自动生效）。

    **CLAUDE.md MTS 不变量「块第 ② 条作废 propose_* 等待语义」** —— 渲染顺序中
    第 ② 条仍是「忽略 propose_* 工具 ack 的等待语义」；后续若调整顺序需同步
    CLAUDE.md 该不变量的条目编号。**P14 英文化 + 精练后 5 条 bullet 顺序保持不变**
    （rationale 移本 docstring，bullet 体精简为祈使句）。**P8.6**：第 ④ 条加 blocked
    防过早放弃节流、第 ⑤ 条加反目标缩水审计，**仍是 5 条、顺序不变**——不触发「5 条
    顺序」不变量的双写要求（CLAUDE.md / `docs/INVARIANTS.md`），故只更新本 docstring。

    ``manager`` 走 ``quoteattr`` 转义防 XML 属性注入（与 ``<room>`` 等块同口径）。
    """
    rounds = max(0, budget)
    return (
        f"<managed_session manager={quoteattr(manager)} remaining_budget=\"{rounds}\">\n"
        "You autonomously drive this task within budget.\n"
        "- After reviewing, directly call propose_delegate / propose_panel; in this "
        "session they auto-queue and take effect — do NOT wait for user approval, do "
        "NOT say \"let the user decide\".\n"
        "- Ignore any \"proposed, awaiting user approval\" wording in a propose_delegate "
        "/ propose_panel ack here — your delegate / panel already took effect; keep going.\n"
        "- For parallel dispatch (multi-reviewer, multi-thread investigation) use "
        "spawn_agent_runs — off-budget background dispatch. Do NOT use propose_panel for "
        "parallelism (panel drains serially).\n"
        "- No next step (need to read more, need user input, stuck)? Just finish your "
        "turn — the session idles and waits for the user; it does NOT end because you "
        "did not delegate this round. Do NOT task_propose_status(\"blocked\") on the "
        "first obstacle — only mark blocked when the SAME blocker has recurred across "
        "3+ consecutive review rounds; a one-off setback is an idle round, not a blocked goal.\n"
        "- Goal met only after verifying the FULL task scope requirement-by-requirement "
        "— do NOT shrink the goal to an easier-to-pass subset just because it is closer "
        "to done. Only then call task_propose_status(\"done\") to wrap up (the proposal "
        "goes to the user for confirmation, not auto-applied) and stop delegating.\n"
        f"Budget left: {rounds} review rounds. The session ends on budget exhausted "
        "(budget=0) / task closed / user stop; otherwise a round with no delegation "
        "just idles.\n"
        "Always reply in the same language as the conversation (default: Chinese).\n"
        "</managed_session>"
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
            return f"{name} (human user)" if p == USER_SPEAKER_ID else name

        entries = [
            (label(p), clamp_summary(self.roster.get(p, "")))
            for p in self.room.participants
        ]
        if not any(summary for _, summary in entries):
            return "Present: " + ", ".join(name for name, _ in entries)
        lines = ["Present:"]
        for name, summary in entries:
            lines.append(f"- {name} — {summary}" if summary else f"- {name}")
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
            room_lines.append(f"Topic: {self.room.topic}")
        if self.room.rules:
            room_lines.append(f"Rules: {self.room.rules}")
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
                    f"Here is what {display_name} says about themselves:\n\n"
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
            else "(no new messages)"
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
        # P14：英文化 + 精练 + 新增 recall 段落（详见 docs/P14）。承重职责三层：
        # ① 身份 + chat 纠偏：agentao base prompt 把茶客定位成「知识工作执行者」，默认
        #    产出「结论/证据/局限」「分步骤交付物」「待办清单」；群聊闲聊轮照此会跑偏成
        #    长篇文档。这里纠偏成「角色化短消息」，条件化豁免：用户明确要 文档/分析/代码/
        #    任务产物、或被显式指派（如 <agent_run_task> bg run 要求一次写完整产物）时切回。
        # ② recall：早期轮次的 tool 结果（文件全文 / 查询输出 / 命令结果）多数仍在
        #    agent.messages 里，但每轮新上下文 + summary 压缩让 LLM 不主动翻历史、倾向
        #    「不记得」。显式要求先回顾历史 tool 结果；仅当被截断/缺失才回 source 重读。
        # ③ 语言锚点：指令英文化后，显式锚定「输出语言随对话（默认中文）」，否则茶客会
        #    开始用英文回复中文房间（中文指令此前隐性锚定了中文输出）。
        body = (
            f"(Speak as \"{guest_name}\". Say only what you mean to say — "
            f"do not repeat others' messages, and do not add quotes or prefixes.\n"
            f"This is a group chat: respond by default with short, in-character "
            f"conversational messages. Do not fall back on formal output formats "
            f"such as \"conclusion / evidence / limitations\", \"step-by-step "
            f"deliverables\", or \"to-do lists\" unless the user explicitly asks "
            f"for a document / analysis / code / task deliverable, or you have been "
            f"explicitly assigned work — only then switch to full deliverable mode.\n"
            f"Before answering, review your own conversation history. Your earlier "
            f"turns contain tool-call results — file contents, query results, command "
            f"output — that the user may now be referring to. Ground your reply in "
            f"those prior results rather than guessing or assuming you never saw them. "
            f"If the relevant content appears truncated or missing from history, "
            f"re-fetch it from its source (e.g. re-read the file under ./share/) before "
            f"answering.\n"
            f"Always reply in the same language as the conversation (default: Chinese).)"
        )
        return f"<speak_instruction>\n{body}\n</speak_instruction>"
