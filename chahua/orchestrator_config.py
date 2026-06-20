"""房间编排参数 —— frozen dataclass。

独立模块以避免 ``context_renderer`` ←→ ``orchestrator`` 循环 import；
``chahua.orchestrator`` 仍 re-export 维持现有 import 路径。
"""

from __future__ import annotations

from dataclasses import dataclass


# P16 调度档取值（``[room].schedule_mode``）。``scoring``=默认意愿打分自发接话；
# ``manual``=auto-pick 恒空、零打分 LLM 调用（只 @/broadcast/handoff/MTS 能让茶客说话）。
# ``round_robin`` / ``pooled`` 设计上暂缓（docs/P16 §暂缓事项），不入白名单。
# 常量住此模块（``OrchestratorConfig`` 字段默认 + config.py 校验 + 调度层比较三处共享），
# 与 ``OrchestratorConfig`` 同生命周期、不引循环 import（本模块仅依赖 ``dataclasses``）。
SCHEDULE_MODE_SCORING: str = "scoring"
SCHEDULE_MODE_MANUAL: str = "manual"
VALID_SCHEDULE_MODES: frozenset[str] = frozenset(
    {SCHEDULE_MODE_SCORING, SCHEDULE_MODE_MANUAL}
)


@dataclass(frozen=True)
class OrchestratorConfig:
    """房间编排参数。字段对应 ``room.toml`` ``[room]`` 段（P2 才会真读 toml）。"""

    schedule_mode: str = SCHEDULE_MODE_SCORING
    """P16 房间调度档。``scoring``（默认）= 意愿打分自发接话；``manual`` =
    ``pick_next_speaker`` auto-pick 第 3 档恒返 ``[]``、零打分 —— 只有 ``@`` /
    broadcast / handoff / MTS 能让茶客说话。**只换 auto-pick 这一档**：前两档（``@`` 路由）
    与 handoff / MTS（走 ``run_pending_handoff`` drain loop）档无关、字面不变。
    白名单 :data:`VALID_SCHEDULE_MODES` 严格校验在 ``config.py``。"""

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
