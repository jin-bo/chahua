"""P13：打分永不吃图 —— scoring 路径不解析 / 不附图。

钉死「打分输入不可信 / 省视觉 token」不变量：即便本轮带 images_rel，scoring 阶段
（无人达阈值、不发言）也绝不调 ``resolve_images``。
"""

from __future__ import annotations

import chahua.guest as guest_mod
from chahua.cursor import GuestCursor
from chahua.orchestrator import Orchestrator, OrchestratorConfig
from chahua.room import Room
from chahua.scoring import IntentScorer
from chahua.user_md import USER_SPEAKER_ID, UserConfig

from conftest import FixedScorer, NoopSummarizer


def test_intent_scorer_score_has_no_images_param() -> None:
    import inspect

    sig = inspect.signature(IntentScorer.score)
    assert "images" not in sig.parameters
    assert "images_rel" not in sig.parameters


async def test_scoring_only_turn_never_resolves_images(monkeypatch) -> None:
    """全员低分 → 无人发言 → resolve_images 不被调（scoring 不吃图）。"""
    calls: list = []

    def _boom(*a, **k):
        calls.append((a, k))
        raise AssertionError("scoring 路径不应调 resolve_images")

    monkeypatch.setattr(guest_mod, "resolve_images", _boom)

    room = Room(name="t")
    room.add_participant(USER_SPEAKER_ID)
    orch = Orchestrator(
        room=room,
        user_config=UserConfig(display_name="老金", full_md=None, source=None),
        scorer=FixedScorer({"A": 0.0}),  # 低于阈值 → 无人发言
        summarizer=NoopSummarizer(),
        cursor=GuestCursor(),
        config=OrchestratorConfig(
            max_consecutive_ai_turns=2, max_speakers_per_pick=1,
            speaker_cooldown_turns=0, summary_block_size=999,
        ),
    )

    # 用一个不会发言的 stub（只暴露 name）—— scoring 选不出来就不进 speak。
    class _NameOnly:
        def __init__(self, name): self.name = name

    orch.register(_NameOnly("A"), persona_md="A persona")  # type: ignore[arg-type]

    await orch.submit_user_message("看图", images_rel=("share/x.png",))
    assert calls == []
