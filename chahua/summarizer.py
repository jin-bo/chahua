"""房间摘要增量产出（设计文档 §3.2.3 / §3.7）。

P1 行为：当 transcript 自上次摘要之后累积 ``>= block_size`` 条新发言时，把这段
区间 ``[start_seq, end_seq]`` 压成几条要点。摘要文本存在内存，注入到 onboarding
窗口的"近期梗概"段。

P2.1 加 ``summary.jsonl`` 落盘 / 启动加载。

走裸 LLMClient.chat（经 :func:`chahua._llm_oneshot.chat_oneshot`）—— 失败一律返
空摘要，但**做指数退避**：连续失败时拉长重试间隔，防止 LLM 长期故障情况下每轮都
冲一次大 prompt（块会越积越大）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional

from agentao.llm import LLMClient

from ._llm_oneshot import chat_oneshot
from ._persist import (
    append_jsonl,
    read_json_or_none,
    read_jsonl_skip_bad,
    write_json_atomic,
    write_jsonl_atomic,
)
from .room import Message, Room, format_messages
from .tasks_store import TasksStore

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class SummarySpan:
    """一段已被摘要覆盖的 transcript 区间。``start_seq`` / ``end_seq`` 均包含。

    ``text`` 是要点形式的纯文本，每行 ``- `` 开头，方便直接拼到 onboarding 里。
    """

    start_seq: int
    end_seq: int
    text: str

    def to_jsonl_dict(self) -> dict:
        return {"start_seq": self.start_seq, "end_seq": self.end_seq, "text": self.text}

    @classmethod
    def from_jsonl(cls, obj: dict) -> Optional["SummarySpan"]:
        try:
            return cls(
                start_seq=int(obj["start_seq"]),
                end_seq=int(obj["end_seq"]),
                text=str(obj["text"]),
            )
        except (KeyError, TypeError, ValueError):
            _log.warning("skip malformed summary record: %r", obj)
            return None


# P14：指令英文化，但**输出必须保留中文**（summary 喂回 <room_summary> 且给用户看）。
_SUMMARY_SYSTEM = (
    "You are the room's note-taker. Compress the group chat below into 3-5 bullet "
    "points, each at most 30 Chinese characters. Keep the subject (who said what, "
    "who replied to whom); do not restate filler; do not comment. "
    "Output the bullets IN CHINESE as plain text, each starting with '- '; no "
    "markdown code block."
)

# 当连续失败 → ``_next_eligible_seq`` 至少要再过这么多条才重试；上限封顶防呆。
_BACKOFF_MAX_SEQS: int = 32


class Summarizer:
    """房间摘要器。状态：已产出 :class:`SummarySpan` 列表 + 失败退避计数。

    单房间一份。:meth:`maybe_summarize` 自管"上次到哪了"，调用方只需在每轮发言后调一次。
    """

    # 摘要本身 ~200-400 token；Gemini 2.5 thinking 模型把 thinking budget 也算
    # 进 max_tokens，旧值 512 会被截。2048 给 thinking + output 都留足，对无
    # thinking 模型不会多消耗（实际输出长度由摘要 prompt 控制）。
    _MAX_TOKENS: int = 2048

    def __init__(
        self,
        llm_client: LLMClient,
        *,
        summary_path: Optional[Path] = None,
    ) -> None:
        self._llm = llm_client
        self._summary_path = summary_path
        self._summaries: list[SummarySpan] = []
        # 退避：连续失败次数 + 在该 seq 之前不再尝试。
        self._failures: int = 0
        self._next_eligible_seq: int = 0
        if summary_path is not None:
            summary_path.parent.mkdir(parents=True, exist_ok=True)
            for obj in read_jsonl_skip_bad(summary_path):
                span = SummarySpan.from_jsonl(obj)
                if span is not None:
                    self._summaries.append(span)
            # 历史里可能没保证 end_seq 单调（人工编辑过 jsonl），按 end_seq 排一下，
            # ``covered_until`` 才能正确返回最大值。
            self._summaries.sort(key=lambda s: s.end_seq)

    # ── 只读 ────────────────────────────────────────────────────────────

    @property
    def summaries(self) -> tuple[SummarySpan, ...]:
        return tuple(self._summaries)

    @property
    def covered_until(self) -> int:
        """已被摘要覆盖的最大 seq；空则 0。"""
        return self._summaries[-1].end_seq if self._summaries else 0

    # ── 清空 ────────────────────────────────────────────────────────────

    def clear(self) -> None:
        """清空摘要 —— 内存 + summary.jsonl + 退避状态全归零。

        清空 transcript 时一并调用：原 SummarySpan 的 ``start_seq/end_seq`` 指向已不存在的
        seq 区间，保留只会污染下次 onboarding 的"近期梗概"段。
        """
        self._summaries.clear()
        self._failures = 0
        self._next_eligible_seq = 0
        if self._summary_path is not None:
            with self._summary_path.open("w", encoding="utf-8"):
                pass

    def truncate_after(self, seq: int) -> None:
        """P18 撤回 ``seq`` 收尾：丢掉覆盖 ``seq`` 的 span（``end_seq >= seq``）并原子
        重写 ``summary.jsonl``；保留的仍是 ``end_seq < seq`` 的完整前缀。

        **常为 no-op**（末条未回复通常还没被摘要成 span），故只在 span 真变化时才写盘。
        但退避游标若已越过被撤 seq，必须复位 —— 否则 ``_next_eligible_seq`` 停在已不存在
        的 seq 之后，让后续摘要被无限推迟。
        """
        kept = [s for s in self._summaries if s.end_seq < seq]
        changed = len(kept) != len(self._summaries)
        self._summaries = kept
        if self._next_eligible_seq >= seq:
            self._next_eligible_seq = 0
            self._failures = 0
        if changed and self._summary_path is not None:
            write_jsonl_atomic(
                self._summary_path, [s.to_jsonl_dict() for s in kept]
            )

    # ── 主入口 ──────────────────────────────────────────────────────────

    async def maybe_summarize(
        self,
        room: Room,
        display_for: Mapping[str, str],
        *,
        block_size: int = 20,
    ) -> Optional[SummarySpan]:
        """累计够 ``block_size`` 条就压一段。否则什么都不做返回 None。

        失败时**不推进 covered_until**，但记录失败次数；下次只有等 transcript 又
        长了至少 ``min(2 ** failures, 32)`` 条才会重试 —— 避免 LLM 长期故障时
        每轮都把越来越大的块塞回去。
        """
        latest = room.latest_seq
        if latest < self._next_eligible_seq:
            return None
        block = self._collect_block(room, block_size)
        if not block:
            return None
        return await self._summarize_block(block, display_for, latest=latest)

    # ── 内部 hook（:class:`TaskSummarizer` 子类化扩展点）─────────

    def _collect_block(
        self, room: Room, block_size: int
    ) -> Optional[list[Message]]:
        """房间级：``covered_until`` 之后累计够 ``block_size`` 条就返回。

        子类（任务级）按 ``task_id`` 过滤 + 用独立 cursor 推进。
        """
        if room.latest_seq - self.covered_until < block_size:
            return None
        block = room.messages_since(self.covered_until)
        if not block:
            return None
        # 防过大块：失败累积或长时间没摘要时，块可能很长。截断到 2*block_size，
        # 保证单次 prompt 体积稳定（也让 LLM 真把"3~5 条要点"做得出）。
        if len(block) > 2 * block_size:
            block = block[: 2 * block_size]
        return block

    async def _summarize_block(
        self,
        block: list[Message],
        display_for: Mapping[str, str],
        *,
        latest: int,
    ) -> Optional[SummarySpan]:
        """对已选好的 ``block`` 跑一次 LLM —— 失败退避，成功落盘 + 重置。

        ``latest`` 是触发本次的 ``room.latest_seq``，失败退避按它算 next_eligible。
        """
        body = format_messages(block, display_for)
        text = await chat_oneshot(
            self._llm,
            [
                {"role": "system", "content": _SUMMARY_SYSTEM},
                {"role": "user", "content": body},
            ],
            max_tokens=self._MAX_TOKENS,
            log_label="summarize",
        )

        if not text:
            self._failures += 1
            backoff = min(2 ** self._failures, _BACKOFF_MAX_SEQS)
            self._next_eligible_seq = latest + backoff
            _log.warning(
                "summarize backing off after %d failures; next attempt at seq>=%d",
                self._failures, self._next_eligible_seq,
            )
            return None

        # 成功 → 推进游标 + 重置退避。
        span = SummarySpan(
            start_seq=block[0].seq, end_seq=block[-1].seq, text=text
        )
        self._summaries.append(span)
        if self._summary_path is not None:
            append_jsonl(self._summary_path, span.to_jsonl_dict())
        self._failures = 0
        self._next_eligible_seq = 0
        return span


# ── 任务级摘要 ───────────────────────────────────────────────────────────


_TASK_CURSOR_KEY = "considered_until_seq"


class TaskSummarizer(Summarizer):
    """每任务一份的 Summarizer：``_collect_block`` 过滤 ``task_id`` + 独立 cursor。

    cursor (``considered_until_seq``) = 已扫过的最大 ``room.seq``。下次只看更高的 ——
    避免大房间多任务时每轮重扫整本 transcript。失败 / 块未满也推进 cursor（已扫过
    就是已扫过），区别于 ``covered_until`` 只在成功摘要后推进。

    本阶段**只落盘**：``maybe_summarize`` 跑 LLM 写 ``summary.jsonl`` + 内存推
    cursor；cursor 文件由 ``session.close()`` 通过 :class:`TaskSummaries.close`
    统一 flush。onboarding 注入将读 ``summary.jsonl`` + cursor（P5.3 起）。
    """

    def __init__(
        self,
        llm_client: LLMClient,
        *,
        summary_path: Path,
        cursor_path: Path,
        task_id: str,
    ) -> None:
        super().__init__(llm_client, summary_path=summary_path)
        self._task_id = task_id
        self._cursor_path = cursor_path
        self._considered_until_seq = self._load_cursor()

    @property
    def task_id(self) -> str:
        return self._task_id

    @property
    def considered_until_seq(self) -> int:
        return self._considered_until_seq

    def _load_cursor(self) -> int:
        obj = read_json_or_none(self._cursor_path)
        if not isinstance(obj, dict):
            return 0
        try:
            return max(0, int(obj.get(_TASK_CURSOR_KEY, 0)))
        except (TypeError, ValueError):
            _log.warning(
                "task %s: summary_cursor.json %s 字段不合法，从 0 重算",
                self._task_id, _TASK_CURSOR_KEY,
            )
            return 0

    def flush_cursor(self) -> None:
        """把 ``_considered_until_seq`` 落到 ``summary_cursor.json``。

        :meth:`TaskSummaries.close` 在 session 关闭时统一调；中途 ``maybe_summarize``
        每轮都写一遍小 json 没必要（成功摘要本身就 append summary.jsonl，cursor 丢
        最多回退到上次重启时的值，下次重扫 N 条多花一次 LLM —— 远低于每轮 fsync 成本）。
        """
        self._cursor_path.parent.mkdir(parents=True, exist_ok=True)
        write_json_atomic(self._cursor_path, {_TASK_CURSOR_KEY: self._considered_until_seq})

    def truncate_after(self, seq: int) -> None:
        """P18 撤回 ``seq`` 收尾（task 级）：先 super 丢覆盖 span + 重写 task summary.jsonl，
        再把 ``_considered_until_seq`` clamp 回 ``seq-1`` 并**立即** flush cursor。

        Why clamp 且立即 flush：``_collect_block`` 在候选不够一块时会把 cursor 直接推到
        ``latest``（``summarizer.py`` 内），所以撤回时任何被 kick 过的任务 cursor 都可能
        停在被撤 seq；不回退则 ``latest <= considered`` 让后续摘要永久跳过。立即落盘防崩溃
        后盘上仍是被撤 seq（``flush_cursor`` 平时只在 session close 时批量落）。
        """
        super().truncate_after(seq)
        if self._considered_until_seq > seq - 1:
            self._considered_until_seq = seq - 1
            self.flush_cursor()

    def _collect_block(
        self, room: Room, block_size: int
    ) -> Optional[list[Message]]:
        latest = room.latest_seq
        if latest <= self._considered_until_seq:
            return None
        candidates = [
            m for m in room.messages_since(self._considered_until_seq)
            if m.task_id == self._task_id
        ]
        if len(candidates) < block_size:
            # 不够一块：cursor 推到 latest 让下次只看更新的 —— 否则重扫已知不匹配的
            # 那段（多任务房间里这段可能很长，每轮都白扫）。
            self._considered_until_seq = latest
            return None
        if len(candidates) > 2 * block_size:
            candidates = candidates[: 2 * block_size]
        return candidates

    async def _summarize_block(
        self,
        block: list[Message],
        display_for: Mapping[str, str],
        *,
        latest: int,
    ) -> Optional[SummarySpan]:
        span = await super()._summarize_block(block, display_for, latest=latest)
        if span is not None:
            # 成功 → 已考察到块末（task-msg 的 seq），保险也推到 latest 以省下轮扫描。
            self._considered_until_seq = max(self._considered_until_seq, latest)
        return span


class TaskSummaries:
    """每房间一份的 per-task summarizer 池 —— lazy 装配（首次 ``kick`` 时按 store
    当前的任务列表建实例）。每位茶客的 ``task_id`` 一份。

    ``kick`` 在 :class:`Orchestrator._summarize_safe` 里跟在房间级 summarize 之后跑；
    ``close`` 在 session 关闭时 flush 所有 cursor。P5.2 落盘 only —— 没人消费这些
    ``summary.jsonl`` 内容（P5.3 onboarding 注入会读）。
    """

    def __init__(self, llm_client: LLMClient, tasks_store: TasksStore) -> None:
        self._llm = llm_client
        self._tasks_store = tasks_store
        self._by_id: dict[str, TaskSummarizer] = {}

    def _ensure(self, task_id: str) -> TaskSummarizer:
        s = self._by_id.get(task_id)
        if s is None:
            task_dir = self._tasks_store.task_dir(task_id)
            s = TaskSummarizer(
                self._llm,
                summary_path=task_dir / "summary.jsonl",
                cursor_path=task_dir / "summary_cursor.json",
                task_id=task_id,
            )
            self._by_id[task_id] = s
        return s

    async def kick(
        self, room: Room, display_for: Mapping[str, str], *, block_size: int,
    ) -> None:
        """按 store 当前任务列表逐个 ``maybe_summarize``。

        任意一个 task 抛错不阻断其他（与 :class:`Orchestrator._summarize_safe` 同口
        径，摘要失败不该把房间 turn 状态弄花）。
        """
        for task in self._tasks_store.list_tasks():
            summarizer = self._ensure(task.id)
            try:
                await summarizer.maybe_summarize(
                    room, display_for, block_size=block_size,
                )
            except Exception:
                _log.exception("task summarize iteration failed: task=%s", task.id)

    def close(self) -> None:
        """flush 所有 task cursor —— session 关闭时调一次。

        失败 WARN 不抛：cursor 丢最多让下次启动多扫一次 transcript，不影响正确性。
        """
        for s in self._by_id.values():
            try:
                s.flush_cursor()
            except OSError as e:
                _log.warning(
                    "task %s flush cursor 失败：%s（下次启动会从旧 cursor 重扫）",
                    s.task_id, e,
                )

    def truncate_after(self, seq: int) -> None:
        """P18 撤回 ``seq`` 收尾：**所有任务**的 summarizer 都收尾（丢覆盖 span + clamp
        cursor），不只 active、也不只本会话已实例化的。

        **为什么遍历 store 而非 ``_by_id``**：``_collect_block`` 在候选不够一块时把任意
        任务 cursor 推到 latest，且该 cursor 会在 session close 时 flush 到
        ``summary_cursor.json``。跨会话场景 —— 上个会话把某任务 cursor 推到了（现在要撤的）
        末条 seq、落了盘；本会话重开后用户「第一件事就是撤回」，此时 ``_by_id`` 还空。
        只遍历 ``_by_id`` 会漏掉这些任务，让其盘上 cursor 永久停在被撤 seq（下条消息复用
        该 seq 时被永久跳过摘要）。故用 ``_ensure`` 按 store 全量任务实例化（构造即从盘
        load cursor）后逐个收尾。
        """
        for task in self._tasks_store.list_tasks():
            self._ensure(task.id).truncate_after(seq)

    def get(self, task_id: str) -> Optional[TaskSummarizer]:
        """已装配的 summarizer 句柄 —— 仅给测试 / inspector 用，业务流不取。"""
        return self._by_id.get(task_id)
