"""共享的"裸 LLMClient 一次性调用"工具。

scoring 和 summarizer 走同一套：把同步的 :meth:`LLMClient.chat` 丢线程池跑 + 失败
返回空串 + 日志。两侧对失败的态度完全一样（"宁可静默不接话也别让 LLM 异常炸房间"），
所以把这套 boilerplate 提到一处定义。

**没有重试 / 没有 backoff**。退避策略是调用方自己的事（比如 summarizer 的"失败块号"机
制就在 :mod:`chahua.summarizer` 里）—— 这里只保证"一次调用，要么拿到字符串，要么拿到空串"。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Mapping, Sequence

from agentao.llm import LLMClient

_log = logging.getLogger(__name__)


async def chat_oneshot(
    llm: LLMClient,
    messages: Sequence[Mapping[str, Any]],
    *,
    max_tokens: int,
    log_label: str,
) -> str:
    """跑一次 LLM 调用，返回 ``content`` 字符串（已 strip）；失败返 ``""``。

    Args:
        llm: 裸 :class:`LLMClient`。**不**会调用 ``Agentao`` 实例，所以茶客记忆不受影响。
        messages: OpenAI-style chat messages。
        max_tokens: 单次调用上限。
        log_label: 失败日志里的标签（如 ``"scoring 玲子"`` / ``"summarize"``），便于排错。
    """
    def _call() -> str:
        try:
            resp = llm.chat(messages=list(messages), max_tokens=max_tokens)
            return (resp.choices[0].message.content or "").strip()
        except Exception:
            _log.exception("%s LLM call failed", log_label)
            return ""

    return await asyncio.to_thread(_call)
