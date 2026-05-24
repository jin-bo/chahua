"""P11：bg run 占用（``active_guest_names``）的茶客不接 @、不参与打分。

覆盖：

- @ 单点：``@busy_guest`` 命中即忽略当前 @ token，找下一个 @；全 @ 都打 busy → None；
- @ 含多个 @：busy 在前、ok 在后 → 路由到 ok；
- broadcast：busy 的从 winners 里剔除；全员 busy → 空 winners；
- scoring：busy 的不进 scorables、也不进 cooled（第三档），bg run 跑完自然回 scorables；
- 兼容：``active_guest_names is None``（裸构 orch）整段 fallthrough，行为同 P11 前。
"""

from __future__ import annotations

import pytest

from chahua.orchestrator import Orchestrator
from chahua.scoring import ScoreKind
from chahua.user_md import USER_SPEAKER_ID

from conftest import FixedScorer, build_orch


def _say(orch: Orchestrator, text: str) -> None:
    orch.room.append(USER_SPEAKER_ID, text)


# ── @ 单点：busy 目标被跳过 ───────────────────────────────────────────────────


def test_at_mention_skips_busy_target_returns_none():
    """``@Bob`` 但 Bob busy → mention 返 None，落到打分（busy 也不打分）。"""
    orch = build_orch("Bob", "Yvonne")
    orch.active_guest_names = {"Bob"}
    _say(orch, "@Bob 你怎么看？")
    assert orch._find_user_mention() is None


def test_at_mention_falls_through_to_next_at_when_first_busy():
    """``@Bob @Yvonne``、Bob busy → 跳 Bob、命中 Yvonne。"""
    orch = build_orch("Bob", "Yvonne")
    orch.active_guest_names = {"Bob"}
    _say(orch, "@Bob 嘿你 @Yvonne 也说说")
    assert orch._find_user_mention() == "Yvonne"


def test_at_mention_returns_none_when_all_ats_are_busy():
    """``@Bob @Yvonne`` 两位都 busy → None（无可路由 @ 落到打分）。"""
    orch = build_orch("Bob", "Yvonne")
    orch.active_guest_names = {"Bob", "Yvonne"}
    _say(orch, "@Bob 你 @Yvonne 都说说")
    assert orch._find_user_mention() is None


def test_at_mention_unchanged_when_active_guest_names_none():
    """``active_guest_names is None``（裸构 orch）路径不受 P11 改动影响。"""
    orch = build_orch("Bob", "Yvonne")
    assert orch.active_guest_names is None
    _say(orch, "@Bob 你怎么看？")
    assert orch._find_user_mention() == "Bob"


# ── scoring：busy 既不进 scorables 也不进 cooled ─────────────────────────────


async def test_pick_next_speaker_excludes_busy_from_scorables():
    """Bob busy + 用户消息 → scoring 跳过 Bob，只对 Yvonne 打分。"""
    scorer = FixedScorer({"Yvonne": 0.9, "Bob": 0.9})
    orch = build_orch("Bob", "Yvonne", scorer=scorer)
    orch.active_guest_names = {"Bob"}
    _say(orch, "随便聊聊")

    winners, scores, _, debug_meta = await orch._scoring_ops.pick_next_speaker(
        respect_at_mention=True,
    )
    assert winners == ["Yvonne"]  # Bob 不参与 → 不在 winners
    # Bob 既不在 scorables 也不在 cooled —— 第三档（busy）整段跳过
    assert "Bob" not in debug_meta.scorables
    assert "Bob" not in debug_meta.cooled
    assert "Yvonne" in debug_meta.scorables
    # scores 里也不会有 Bob（不进打分 → 不生成 ScoreResult）
    assert all(s.guest_name != "Bob" for s in scores)


async def test_pick_next_speaker_all_busy_returns_empty_winners():
    """全员 busy → winners=[]、scorables=[]、cooled=[]，turn 走 no-winners 分支。"""
    scorer = FixedScorer({"Yvonne": 0.9, "Bob": 0.9})
    orch = build_orch("Bob", "Yvonne", scorer=scorer)
    orch.active_guest_names = {"Bob", "Yvonne"}
    _say(orch, "随便聊聊")

    winners, scores, _, debug_meta = await orch._scoring_ops.pick_next_speaker(
        respect_at_mention=True,
    )
    assert winners == []
    assert debug_meta.scorables == []
    assert debug_meta.cooled == []
    # 没人参与打分，scores 也是空 —— 上层会 flush 一行 "no winners" turn
    assert scores == []


async def test_pick_next_speaker_busy_releases_back_to_scorables():
    """Bob busy 时被跳过；从 active_guest_names 摘掉后下一轮回到 scorables。"""
    scorer = FixedScorer({"Bob": 0.9, "Yvonne": 0.1})
    orch = build_orch("Bob", "Yvonne", scorer=scorer)
    orch.active_guest_names = {"Bob"}
    _say(orch, "随便聊聊")

    winners1, _, _, meta1 = await orch._scoring_ops.pick_next_speaker(
        respect_at_mention=True,
    )
    assert winners1 == []  # Yvonne 低分、Bob 被 busy 排除
    assert "Bob" not in meta1.scorables

    # bg run 结束（active_guest_names 释放）；再触发一轮：
    orch.active_guest_names.discard("Bob")
    _say(orch, "再问一次")
    winners2, _, _, meta2 = await orch._scoring_ops.pick_next_speaker(
        respect_at_mention=True,
    )
    assert winners2 == ["Bob"]  # 回到打分桶、高分胜出
    assert "Bob" in meta2.scorables


# ── @broadcast：busy 从 winners 剔除 ──────────────────────────────────────────


async def test_broadcast_excludes_busy_from_winners():
    """``@all`` 时 Bob busy → winners 只含 Yvonne，且全是 MENTION kind。"""
    orch = build_orch("Bob", "Yvonne")
    orch.active_guest_names = {"Bob"}
    _say(orch, "@all 都说两句")

    winners, scores, _, debug_meta = await orch._scoring_ops.pick_next_speaker(
        respect_at_mention=True,
    )
    assert winners == ["Yvonne"]
    assert len(scores) == 1
    assert scores[0].guest_name == "Yvonne"
    assert scores[0].kind == ScoreKind.MENTION
    # broadcast 路径 scoring_path 不变
    from chahua.debug_recorder import SCORING_PATH_BROADCAST
    assert debug_meta.scoring_path == SCORING_PATH_BROADCAST


async def test_broadcast_all_busy_returns_empty_winners():
    """``@all`` 时全员 busy → winners=[]、scores=[]。"""
    orch = build_orch("Bob", "Yvonne")
    orch.active_guest_names = {"Bob", "Yvonne"}
    _say(orch, "@all 都说两句")

    winners, scores, _, _ = await orch._scoring_ops.pick_next_speaker(
        respect_at_mention=True,
    )
    assert winners == []
    assert scores == []
