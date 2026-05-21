"""房间编排参数 —— frozen dataclass。

独立模块以避免 ``context_renderer`` ←→ ``orchestrator`` 循环 import；
``chahua.orchestrator`` 仍 re-export 维持现有 import 路径。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class OrchestratorConfig:
    """房间编排参数。字段对应 ``room.toml`` ``[room]`` 段（P2 才会真读 toml）。"""

    want_threshold: float = 0.45
    """打分 ≥ 此值才允许发言。0.55 起步时招呼/中性话题全员低于阈值导致冷场（实测），
    降到 0.45 让常见话题至少有一人接住；同时配合 ``threshold_decay_per_turn`` 防 AI 跑飞。"""

    max_consecutive_ai_turns: int = 20
    """连续 AI 发言数上限；达到后强制让麦给用户。一次"同回合 1~2 人抢话"算 1~2 轮。
    P8.3 起默认 20（从 4 上调）—— 托管会话（MTS）的 worker↔管理者自循环要在这个硬护栏
    下跑得开，4 轮放不下「kickoff + 几轮派活复查」；MTS 预算 ``budget`` 仍是用户旋钮。"""

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
