"""``Orchestrator.set_config`` 同步 ContextRenderer 内嵌 config ref 的回归测试。

P5.7 把 prompt 装配抽到 :class:`ContextRenderer` 后，它持自己的 ``self.config`` ref
（读 ``onboarding_threshold`` / ``onboarding_recent_messages`` / ``onboarding_recent_summaries``
/ ``scoring_transcript_recent``）。``session.RoomSession.swap_room_config`` 走
:meth:`Orchestrator.set_config` 一次性更新两处；否则用户在 UI 改编排参数后这几个
字段会沿用旧值，行为静默不一致。
"""

from __future__ import annotations

from chahua.orchestrator import OrchestratorConfig
from tests.conftest import build_orch


def test_set_config_updates_renderer_too():
    orch = build_orch("A")
    new_cfg = OrchestratorConfig(
        want_threshold=0.9,
        onboarding_threshold=5,
        scoring_transcript_recent=3,
        onboarding_recent_messages=2,
        onboarding_recent_summaries=1,
    )
    orch.set_config(new_cfg)
    assert orch.config is new_cfg
    assert orch._renderer.config is new_cfg
