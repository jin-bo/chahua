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
from ._persist import append_jsonl, read_jsonl_skip_bad
from .room import Message, Room, format_messages

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


_SUMMARY_SYSTEM = (
    "你是房间记录员。把下面这段群聊压成 3~5 条要点，每条不超过 30 字。"
    "保留主语（谁说了什么、谁回应了谁），不复述废话，不评论。"
    "输出纯文本，每条以 '- ' 开头；不要 markdown 代码块。"
)

# 当连续失败 → ``_next_eligible_seq`` 至少要再过这么多条才重试；上限封顶防呆。
_BACKOFF_MAX_SEQS: int = 32


class Summarizer:
    """房间摘要器。状态：已产出 :class:`SummarySpan` 列表 + 失败退避计数。

    单房间一份。:meth:`maybe_summarize` 自管"上次到哪了"，调用方只需在每轮发言后调一次。
    """

    _MAX_TOKENS: int = 512

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

        start = self.covered_until + 1
        if latest - start + 1 < block_size:
            return None

        block = room.messages_since(self.covered_until)
        if not block:
            return None

        # 防过大块：失败累积或长时间没摘要时，块可能很长。截断到 2*block_size，
        # 保证单次 prompt 体积稳定（也让 LLM 真把"3~5 条要点"做得出）。
        if len(block) > 2 * block_size:
            block = block[: 2 * block_size]

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
