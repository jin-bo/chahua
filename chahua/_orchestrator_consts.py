"""Orchestrator 内部常量 —— 与 :mod:`.orchestrator` 同生命周期的纯文本块 / sentinel。

物理拆出来是为了让按子域拆分的 slot 模块（`_orchestrator_handoff_drain.py` /
`_orchestrator_managed_session.py` 等）能共享同一份常量，避免主模块成为常量
回流点。**不**对外暴露——所有名字都带 ``_`` 前缀。
"""

from __future__ import annotations

from typing import Any

# ``submit_user_message`` 的 ``task_id`` 参数 sentinel —— ``None`` 是合法值（显式
# 无任务），不能复用 None 表示"缺省"。``Any`` 而非 ``object`` 让类型注解既能写
# ``Optional[str]`` 又能默认 ``_UNSET``。
_UNSET: Any = object()


# P7.2 review：``<review_target>`` 块内的审阅指引文案。被审消息原文由
# ``format_messages`` 包装后内联在指引之上（docs §4.2）。P14：英文化 + 语言锚点。
_REVIEW_INSTRUCTION = (
    "Give your review: approve, reject, or concrete change requests, with reasons. "
    "Always reply in the same language as the conversation (default: Chinese)."
)


# P7.3 panel：``<panel_summary_request>`` 块——summarizer 专属，模块常量（不含
# per-item 数据）。summarizer 是 panel turn 的末位 speaker，N 位 panelist 的发言
# 已串行 append 进 transcript、紧贴这一发之前，summarizer 的 ``<recent_messages>``
# 自然包含——块只需告诉它"刚结束一轮圆桌、请汇总"（docs/P7.3 §4.3）。
# P14：英文化 + 精练（散文转祈使）+ 语言锚点。
_PANEL_SUMMARY_BLOCK = (
    "<panel_summary_request>\n"
    "A round-table parallel discussion just ended — several guests each gave one "
    "independent view.\n"
    "Summarize the recent round-table messages into one concise synthesis: distill "
    "consensus, point out disagreements, list open questions. Synthesize "
    "structurally — do NOT restate each message one by one.\n"
    "Always reply in the same language as the conversation (default: Chinese).\n"
    "</panel_summary_request>"
)
