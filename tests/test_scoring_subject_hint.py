"""回归：打分 prompt 的"话题就在讨论你"信号。

两层修复（针对 LLM 把"话题在围绕你做安排"误判低分的情况）：

L1 —— ``_SCORING_PROMPT_TEMPLATE`` 加打分参考档位，把"话题就在讨论你"列为 0.6-0.8。
L2 —— orchestrator 预扫最近窗口，统计"别人提到 guest_name 的次数"，注入
       ``<context_hint>`` 段给模型一个 deterministic 信号。

本文件覆盖：
- L1：标尺档位字面出现在 prompt 里
- L2：``_count_self_mentions`` 计数正确（排除自我、计入他人）
- L2：mention_count > 0 时 prompt 含 ``<context_hint>`` 段，= 0 时不含
- 端到端：orchestrator 跑打分时把正确的 count 传给 scorer
"""

from __future__ import annotations

from chahua.events import ChahuaEnvelope
from chahua.room import Message
from chahua.scoring import (
    IntentScorer,
    ScoreKind,
    ScoreResult,
    _render_prompt,
)
from chahua.user_md import USER_SPEAKER_ID, UserConfig

from conftest import build_orch


# ── L1：标尺档位 ───────────────────────────────────────────────────────────


def test_prompt_contains_rubric_for_topic_about_you():
    """prompt 里必须有"话题就在讨论你"的 0.6-0.8 档位说明，否则 LLM 易把"讨论你的行程"
    判低分。"""
    prompt = _render_prompt(
        guest_name="Elon Musk",
        persona="founder",
        transcript_text="老金 说：我们送 Elon Musk 去机场",
        user_config=UserConfig(display_name="老金", full_md=None, source=None),
    )
    assert "话题就在讨论你" in prompt
    assert "0.6-0.8" in prompt


# ── L2：subject hint 注入 ─────────────────────────────────────────────────


def test_prompt_omits_hint_when_count_zero():
    prompt = _render_prompt(
        guest_name="Elon Musk",
        persona="founder",
        transcript_text="老金 说：泡饭真好吃",
        user_config=UserConfig(display_name="老金", full_md=None, source=None),
        subject_mention_count=0,
    )
    assert "<context_hint>" not in prompt


def test_prompt_includes_hint_when_count_positive():
    prompt = _render_prompt(
        guest_name="Elon Musk",
        persona="founder",
        transcript_text="老金 说：周日送 Elon Musk 去机场",
        user_config=UserConfig(display_name="老金", full_md=None, source=None),
        subject_mention_count=3,
    )
    assert "<context_hint>" in prompt
    assert "Elon Musk" in prompt
    assert "3" in prompt
    # hint 显式提到了"≥ 0.6"，确保模型有数字锚点而不是只能靠抽象语义
    assert "0.6" in prompt


# ── L2：_count_self_mentions ───────────────────────────────────────────────


def _msgs(*pairs: tuple[str, str]) -> list[Message]:
    out: list[Message] = []
    for i, (speaker, text) in enumerate(pairs, start=1):
        out.append(
            Message(
                seq=i, speaker_id=speaker, text=text, ts_ms=i, message_id=f"m{i}"
            )
        )
    return out


def test_count_self_mentions_excludes_own_messages():
    orch = build_orch("Elon Musk", "Yvonne")
    recent = _msgs(
        ("Elon Musk", "Elon Musk is here"),  # 自言 → 不计
        ("Yvonne", "Elon Musk 已经说过"),    # 计 1
        (USER_SPEAKER_ID, "Elon Musk 你怎么看"),  # 计 1
    )
    assert orch._count_self_mentions("Elon Musk", recent) == 2


def test_count_self_mentions_zero_when_unrelated():
    orch = build_orch("Elon Musk", "Yvonne")
    recent = _msgs(
        (USER_SPEAKER_ID, "泡饭真好吃"),
        ("Yvonne", "好的，我去拿"),
    )
    assert orch._count_self_mentions("Elon Musk", recent) == 0


def test_count_self_mentions_counts_at_mention_too():
    """``@Elon Musk`` 也算（@ 是更早 user 消息的余热）。"""
    orch = build_orch("Elon Musk")
    recent = _msgs(
        (USER_SPEAKER_ID, "@Elon Musk 跟着 Trump 访华有什么收获"),
        ("Elon Musk", "...制造效率..."),
        (USER_SPEAKER_ID, "还有哪些老板跟你一起来的"),  # 不含名字
    )
    # 只有第一条含 "Elon Musk"，Elon 自己第二条不算
    assert orch._count_self_mentions("Elon Musk", recent) == 1


# ── 端到端：orchestrator → scorer 透传 ────────────────────────────────────


async def test_orchestrator_passes_mention_count_to_scorer():
    """orchestrator 调 scorer.score 时应该带上 subject_mention_count。

    自定义 scorer 截留 call kwargs 验证；全部返回 0 分让 orchestrator 走"无人接话"
    路径，``_let_speak`` 不会被调，StubGuest 不需要 ``speak()`` 实现。
    """
    captured: dict[str, int] = {}

    class _CapturingScorer(IntentScorer):
        def __init__(self) -> None:
            pass

        async def score(
            self,
            *,
            guest_name,
            persona,
            transcript_text,
            user_config,
            subject_mention_count=0,
        ):
            captured[guest_name] = subject_mention_count
            return ScoreResult(
                guest_name=guest_name, score=0.0, kind=ScoreKind.SCORED
            )

    orch = build_orch("Elon Musk", "Yvonne", scorer=_CapturingScorer())

    # "老金 + Yvonne 在讨论 Elon 的送机"，user 那条提一次 Elon Musk、不提 Yvonne。
    # 避开 ``@Yvonne`` —— @ 字面含 "Yvonne" 子串会让 Yvonne 计数 +1，掩盖 Elon 的差异。
    orch.room.append("Elon Musk", "我多留两天，最早周日晚回")
    orch.room.append(USER_SPEAKER_ID, "周日我们送 Elon Musk 去机场")
    orch.room.append("Yvonne", "几点的飞机？")

    sink: list[ChahuaEnvelope] = []
    await orch.submit_user_message("具体几点", sink=sink.append)

    assert captured == {"Elon Musk": 1, "Yvonne": 0}
