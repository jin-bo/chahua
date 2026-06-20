"""P16：发言权重（``[[guest]].talkativeness``）+ 手动档（``[room].schedule_mode``）。

复现优先，覆盖 docs/P16-发言权重与手动模式.md「测试计划」：

- 配置校验：schedule_mode 白名单（暂缓 round_robin/pooled 分诊）、talkativeness 数值/
  bool/越界、缺省透 None / 默认 scoring。
- talkativeness 偏置：effective=clamp(base×talk)、no-op、reclamp、base=0、0.0 不被吞、
  不作用 @/broadcast。
- manual：auto-pick 恒空 + IntentScorer 零调用 + @/broadcast 仍路由 + scoring_path=manual。
- schedule_mode 串接（专用字段非 overrides）+ explicit 入参优先钉点。
- 取证显式穿透：record_scoring 落盘 base_score/talkativeness；data.scores 仅 effective。
- round-trip toml：写 schedule_mode/talkativeness → reload 保住；默认不写盘。
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from chahua import admin
from chahua._persist import read_jsonl_skip_bad
from chahua.admin_room import _room_config_to_dict
from chahua.admin_toml import _render_room_toml
from chahua.config import RoomConfigError, load_room_config
from chahua.cursor import GuestCursor
from chahua.debug_recorder import (
    SCORING_PATH_BROADCAST,
    SCORING_PATH_MANUAL,
    SCORING_PATH_MENTION,
    SCORING_PATH_SCORING,
    TurnRecorder,
)
from chahua.events import ChahuaEnvelope, ChahuaEventType
from chahua.orchestrator import Orchestrator, OrchestratorConfig
from chahua.orchestrator_config import (
    SCHEDULE_MODE_MANUAL,
    SCHEDULE_MODE_SCORING,
)
from chahua.room import Room
from chahua.scoring import IntentScorer, ScoreKind
from chahua.session import _make_orchestrator_config
from chahua.summarizer import Summarizer
from chahua.user_md import USER_SPEAKER_ID, UserConfig

from conftest import (
    FixedScorer,
    NoopSummarizer,
    SpeakingStubGuest,
    StubGuest,
    build_orch,
)


# ── helpers ──────────────────────────────────────────────────────────────────


class CountingScorer(FixedScorer):
    """FixedScorer + 调用计数 —— manual 档断言 IntentScorer 零调用。"""

    def __init__(self, scores: dict[str, float]) -> None:
        super().__init__(scores)
        self.calls = 0

    async def score(self, **kw):
        self.calls += 1
        return await super().score(**kw)

    async def score_with_prompt(self, **kw):
        self.calls += 1
        return await super().score_with_prompt(**kw)


def _orch(scores, *, talk=None, mode=None, scorer=None):
    """build_orch + 可选 per-guest talkativeness + schedule_mode 热替。"""
    orch = build_orch(*scores.keys(), scorer=scorer or FixedScorer(scores))
    if talk:
        orch.update_talkativeness(talk)
    if mode:
        orch.set_config(dataclasses.replace(orch.config, schedule_mode=mode))
    return orch


async def _pick(orch, text="随便聊聊"):
    orch.room.append(USER_SPEAKER_ID, text)
    return await orch._pick_next_speaker(respect_at_mention=True)


def _seed_room(env_paths, room_id="p16"):
    return admin.create_room(
        paths=env_paths, room_id=room_id, name=room_id,
        guests=[{"persona": "chahua/personas/宝总/宝总.md", "name": "宝总"}],
    )


def _write_room(rc, *, room_extra="", guest_extra="") -> None:
    body = (
        '[room]\nname = "x"\n'
        + room_extra
        + '\n\n[[guest]]\nname = "宝总"\n'
        'persona = "chahua/personas/宝总/宝总.md"\npermission = "read-only"\n'
        + guest_extra
        + "\n"
    )
    (rc.room_dir / "room.toml").write_text(body, encoding="utf-8")


# ── 配置校验：schedule_mode ───────────────────────────────────────────────────


def test_schedule_mode_missing_defaults_scoring(env_paths):
    rc = _seed_room(env_paths)
    rc2 = load_room_config(rc.room_dir, paths=env_paths)
    assert rc2.schedule_mode == SCHEDULE_MODE_SCORING


def test_schedule_mode_manual_parses(env_paths):
    rc = _seed_room(env_paths)
    _write_room(rc, room_extra='schedule_mode = "manual"')
    rc2 = load_room_config(rc.room_dir, paths=env_paths)
    assert rc2.schedule_mode == SCHEDULE_MODE_MANUAL


def test_schedule_mode_not_in_orchestrator_overrides(env_paths):
    """专用字段——不能漏进 orchestrator_overrides 数值表（防"白名单允许但被忽略"）。"""
    rc = _seed_room(env_paths)
    _write_room(rc, room_extra='schedule_mode = "manual"')
    rc2 = load_room_config(rc.room_dir, paths=env_paths)
    assert "schedule_mode" not in rc2.orchestrator_overrides


def test_schedule_mode_deferred_round_robin_rejected(env_paths):
    """round_robin/pooled 暂缓 → "暂未实现"分诊（区别于拼错）。"""
    rc = _seed_room(env_paths)
    _write_room(rc, room_extra='schedule_mode = "round_robin"')
    with pytest.raises(RoomConfigError, match=r"暂未实现"):
        load_room_config(rc.room_dir, paths=env_paths)


def test_schedule_mode_typo_rejected(env_paths):
    rc = _seed_room(env_paths)
    _write_room(rc, room_extra='schedule_mode = "manaul"')
    with pytest.raises(RoomConfigError, match=r"非法"):
        load_room_config(rc.room_dir, paths=env_paths)


def test_schedule_mode_non_string_rejected(env_paths):
    rc = _seed_room(env_paths)
    _write_room(rc, room_extra="schedule_mode = 1")
    with pytest.raises(RoomConfigError, match=r"schedule_mode.*字符串"):
        load_room_config(rc.room_dir, paths=env_paths)


# ── 配置校验：talkativeness ───────────────────────────────────────────────────


def test_talkativeness_missing_is_none(env_paths):
    """缺字段 → None（未显式选；消费侧 coalesce 1.0）。"""
    rc = _seed_room(env_paths)
    rc2 = load_room_config(rc.room_dir, paths=env_paths)
    assert rc2.guests[0].talkativeness is None


def test_talkativeness_explicit_value(env_paths):
    rc = _seed_room(env_paths)
    _write_room(rc, guest_extra="talkativeness = 1.8")
    rc2 = load_room_config(rc.room_dir, paths=env_paths)
    assert rc2.guests[0].talkativeness == 1.8


def test_talkativeness_explicit_zero_kept(env_paths):
    """0.0 是合法显式值（哑茶客），不被当缺省。"""
    rc = _seed_room(env_paths)
    _write_room(rc, guest_extra="talkativeness = 0.0")
    rc2 = load_room_config(rc.room_dir, paths=env_paths)
    assert rc2.guests[0].talkativeness == 0.0


def test_talkativeness_int_promotes(env_paths):
    rc = _seed_room(env_paths)
    _write_room(rc, guest_extra="talkativeness = 2")
    rc2 = load_room_config(rc.room_dir, paths=env_paths)
    assert rc2.guests[0].talkativeness == 2.0


def test_talkativeness_bool_rejected(env_paths):
    rc = _seed_room(env_paths)
    _write_room(rc, guest_extra="talkativeness = true")
    with pytest.raises(RoomConfigError, match=r"talkativeness.*数值"):
        load_room_config(rc.room_dir, paths=env_paths)


def test_talkativeness_out_of_range_rejected(env_paths):
    rc = _seed_room(env_paths)
    _write_room(rc, guest_extra="talkativeness = 5.0")
    with pytest.raises(RoomConfigError, match=r"talkativeness.*越界"):
        load_room_config(rc.room_dir, paths=env_paths)


def test_talkativeness_negative_rejected(env_paths):
    rc = _seed_room(env_paths)
    _write_room(rc, guest_extra="talkativeness = -0.5")
    with pytest.raises(RoomConfigError, match=r"talkativeness.*越界"):
        load_room_config(rc.room_dir, paths=env_paths)


@pytest.mark.parametrize("literal", ["nan", "inf", "-inf"])
def test_talkativeness_non_finite_rejected(env_paths, literal):
    """TOML 允许 ``nan`` / ``inf`` 字面量；非有限值不是合法权重（``score×NaN=NaN`` 永久静默）。
    NaN 尤其要先于范围判（``nan<MIN`` / ``nan>MAX`` 都 False 会漏过区间检查）。"""
    rc = _seed_room(env_paths)
    _write_room(rc, guest_extra=f"talkativeness = {literal}")
    with pytest.raises(RoomConfigError, match=r"talkativeness"):
        load_room_config(rc.room_dir, paths=env_paths)


# ── talkativeness 偏置（scoring 档）───────────────────────────────────────────


async def test_talk_default_no_op():
    """talk=1.0 严格 no-op：0.7 ≥ 0.45 → winner，score 不变。"""
    orch = _orch({"宝总": 0.7, "陶陶": 0.3})
    winners, scores, _, meta = await _pick(orch)
    assert winners == ["宝总"]
    by = {s.guest_name: s for s in scores}
    assert by["宝总"].score == 0.7
    assert by["宝总"].base_score == 0.7
    assert by["宝总"].talkativeness == 1.0


async def test_talk_damp_drops_below_threshold():
    """damp：0.8 × 0.5 = 0.4 < 0.45 → 相关话题也忍住。"""
    orch = _orch({"宝总": 0.8}, talk={"宝总": 0.5})
    winners, scores, _, _ = await _pick(orch)
    assert winners == []
    s = scores[0]
    assert s.base_score == 0.8
    assert s.score == pytest.approx(0.4)
    assert s.talkativeness == 0.5


async def test_talk_boost_lifts_above_threshold():
    """boost：0.3 × 1.8 = 0.54 ≥ 0.45 → 话痨插进来。"""
    orch = _orch({"陶陶": 0.3}, talk={"陶陶": 1.8})
    winners, scores, _, _ = await _pick(orch)
    assert winners == ["陶陶"]
    assert scores[0].score == pytest.approx(0.54)
    assert scores[0].base_score == 0.3


async def test_talk_reclamp_caps_at_one():
    """reclamp：0.8 × 2.0 = 1.6 → 1.0。"""
    orch = _orch({"陶陶": 0.8}, talk={"陶陶": 2.0})
    _, scores, _, _ = await _pick(orch)
    assert scores[0].score == 1.0
    assert scores[0].base_score == 0.8


async def test_talk_base_zero_stays_zero():
    """乘性：base=0（话题无关）再高权重仍 0 —— "爱说话"≠"乱插话"。"""
    orch = _orch({"陶陶": 0.0}, talk={"陶陶": 4.0})
    winners, scores, _, _ = await _pick(orch)
    assert winners == []
    assert scores[0].score == 0.0


async def test_talk_explicit_zero_mutes():
    """显式 0.0：相关话题也哑（0.9 × 0.0 = 0）。"""
    orch = _orch({"宝总": 0.9}, talk={"宝总": 0.0})
    winners, scores, _, _ = await _pick(orch)
    assert winners == []
    assert scores[0].score == 0.0
    assert scores[0].talkativeness == 0.0


async def test_talk_not_applied_to_mention():
    """被 @ 的高冷茶客仍 score=1.0 入选 —— talkativeness 不缩放路由信号。"""
    orch = _orch({"宝总": 0.7, "陶陶": 0.3}, talk={"宝总": 0.0})
    orch.room.append(USER_SPEAKER_ID, "@宝总 你怎么看")
    winners, scores, _, meta = await orch._pick_next_speaker(respect_at_mention=True)
    assert winners == ["宝总"]
    assert scores[0].score == 1.0
    assert scores[0].kind == ScoreKind.MENTION
    assert meta.scoring_path == SCORING_PATH_MENTION


async def test_talk_not_applied_to_broadcast():
    """@all 全员各一次，哑茶客也带上（score=1.0），talkativeness 不缩放。"""
    orch = _orch({"宝总": 0.7, "陶陶": 0.3}, talk={"宝总": 0.0, "陶陶": 0.0})
    orch.room.append(USER_SPEAKER_ID, "@all 都说一句")
    winners, scores, _, meta = await orch._pick_next_speaker(respect_at_mention=True)
    assert set(winners) == {"宝总", "陶陶"}
    assert all(s.score == 1.0 for s in scores)
    assert meta.scoring_path == SCORING_PATH_BROADCAST


# ── manual 档 ─────────────────────────────────────────────────────────────────


async def test_manual_auto_pick_empty_and_zero_scoring():
    """manual：auto-pick 恒空 + IntentScorer 零调用。"""
    scorer = CountingScorer({"宝总": 0.9, "陶陶": 0.9})
    orch = _orch({"宝总": 0.9, "陶陶": 0.9}, mode=SCHEDULE_MODE_MANUAL, scorer=scorer)
    winners, scores, prompts, meta = await _pick(orch)
    assert winners == []
    assert scores == []
    assert prompts == {}
    assert meta.scoring_path == SCORING_PATH_MANUAL
    assert scorer.calls == 0


async def test_manual_ai_relay_also_empty():
    """manual 下连 AI 接力（respect_at_mention=False）都恒空。"""
    scorer = CountingScorer({"宝总": 0.9})
    orch = _orch({"宝总": 0.9}, mode=SCHEDULE_MODE_MANUAL, scorer=scorer)
    orch.room.append(USER_SPEAKER_ID, "hi")
    winners, _, _, meta = await orch._pick_next_speaker(respect_at_mention=False)
    assert winners == []
    assert meta.scoring_path == SCORING_PATH_MANUAL
    assert scorer.calls == 0


async def test_manual_mention_still_routes():
    """manual 下 @宝总 仍确定性路由一句。"""
    scorer = CountingScorer({"宝总": 0.0, "陶陶": 0.0})
    orch = _orch({"宝总": 0.0, "陶陶": 0.0}, mode=SCHEDULE_MODE_MANUAL, scorer=scorer)
    orch.room.append(USER_SPEAKER_ID, "@宝总 你怎么看")
    winners, _, _, meta = await orch._pick_next_speaker(respect_at_mention=True)
    assert winners == ["宝总"]
    assert meta.scoring_path == SCORING_PATH_MENTION
    assert scorer.calls == 0


async def test_manual_broadcast_still_routes():
    """manual 下 @all 仍全员一次。"""
    orch = _orch({"宝总": 0.0, "陶陶": 0.0}, mode=SCHEDULE_MODE_MANUAL)
    orch.room.append(USER_SPEAKER_ID, "@all 都说一句")
    winners, _, _, meta = await orch._pick_next_speaker(respect_at_mention=True)
    assert set(winners) == {"宝总", "陶陶"}
    assert meta.scoring_path == SCORING_PATH_BROADCAST


# ── schedule_mode 串接 + explicit 优先 ────────────────────────────────────────


def test_make_orch_config_threads_schedule_mode():
    cfg = _make_orchestrator_config({}, schedule_mode=SCHEDULE_MODE_MANUAL)
    assert cfg.schedule_mode == SCHEDULE_MODE_MANUAL


def test_make_orch_config_default_scoring():
    cfg = _make_orchestrator_config({})
    assert cfg.schedule_mode == SCHEDULE_MODE_SCORING


def test_make_orch_config_keeps_numeric_overrides():
    """schedule_mode 穿参不影响数值 override patch。"""
    cfg = _make_orchestrator_config(
        {"want_threshold": 0.6}, schedule_mode=SCHEDULE_MODE_MANUAL
    )
    assert cfg.want_threshold == 0.6
    assert cfg.schedule_mode == SCHEDULE_MODE_MANUAL


def test_make_orch_config_explicit_wins():
    """钉点：explicit SDK 入参完全优先，自带 schedule_mode 不被 schedule_mode= 覆盖。"""
    explicit = OrchestratorConfig(schedule_mode=SCHEDULE_MODE_SCORING)
    cfg = _make_orchestrator_config(
        {}, schedule_mode=SCHEDULE_MODE_MANUAL, explicit=explicit
    )
    assert cfg is explicit
    assert cfg.schedule_mode == SCHEDULE_MODE_SCORING


def test_build_session_manual_takes_effect(env_paths):
    """端到端：room.toml schedule_mode=manual → OrchestratorConfig.schedule_mode=manual。"""
    from chahua.session import build_room_session

    rc = _seed_room(env_paths, room_id="p16-manual")
    _write_room(rc, room_extra='schedule_mode = "manual"')
    session = build_room_session(rc.room_dir, env_paths)
    try:
        assert session.orchestrator.config.schedule_mode == SCHEDULE_MODE_MANUAL
    finally:
        session.close()


# ── 取证显式穿透 ──────────────────────────────────────────────────────────────


def _build_orch_with_recorder(room, scorer, rec):
    return Orchestrator(
        room=room,
        user_config=UserConfig(display_name="测试者", full_md=None, source=None),
        scorer=scorer,
        summarizer=NoopSummarizer(),
        cursor=GuestCursor(),
        config=OrchestratorConfig(
            max_consecutive_ai_turns=1, max_speakers_per_pick=1,
            summary_block_size=999,
        ),
        recorder=rec,
    )


async def test_forensics_punch_through(tmp_path: Path):
    """record_scoring 落盘补 base_score/talkativeness（仅 SCORED 且 talk≠1.0）；
    talk=1.0 省略；实时 data.scores 仅 effective、不含 base_score。"""
    room = Room(name="r-forensics")
    room.add_participant(USER_SPEAKER_ID)
    rec = TurnRecorder(tmp_path, enabled=True, capture_prompts=False)
    scorer = FixedScorer({"A": 0.9, "B": 0.8})
    orch = _build_orch_with_recorder(room, scorer, rec)
    # A talk=1.0（no-op，winner）；B talk=0.5（eff 0.4，loser）。
    orch.register(SpeakingStubGuest("A", room, rec), persona_md="A", talkativeness=1.0)
    orch.register(SpeakingStubGuest("B", room, rec), persona_md="B", talkativeness=0.5)

    captured: list[ChahuaEnvelope] = []
    await orch.submit_user_message("你好", sink=captured.append)

    rows = list(read_jsonl_skip_bad(tmp_path / "debug" / "turns.jsonl"))
    assert len(rows) == 1
    by_guest = {r["guest"]: r for r in rows[0]["scoring"]["results"]}
    # B 被偏置：落盘有 base_score / talkativeness，score 是 effective。
    assert by_guest["B"]["base_score"] == 0.8
    assert by_guest["B"]["talkativeness"] == 0.5
    assert by_guest["B"]["score"] == pytest.approx(0.4)
    # A talk=1.0：no-op，省略 base_score / talkativeness 两 key。
    assert "base_score" not in by_guest["A"]
    assert "talkativeness" not in by_guest["A"]

    # 实时 turn_start data.scores 仅 effective，绝不含 base_score。
    starts = [e for e in captured if e.type == ChahuaEventType.TURN_START]
    assert starts
    score_dicts = {d["guest_name"]: d for d in starts[0].data["scores"]}
    assert score_dicts["B"]["score"] == pytest.approx(0.4)
    assert "base_score" not in score_dicts["B"]
    assert "talkativeness" not in score_dicts["B"]


# ── round-trip toml ───────────────────────────────────────────────────────────


def test_round_trip_preserves_non_default(env_paths):
    """schedule_mode=manual + 显式 talkativeness（含 0.0）→ reload 保住。"""
    rc = _seed_room(env_paths, room_id="p16-rt")
    rc2 = load_room_config(rc.room_dir, paths=env_paths)
    # 手工拼一个非默认 RoomConfig 经 snapshot → render → reload。
    snap = _room_config_to_dict(rc2, env_paths)
    snap["schedule_mode"] = SCHEDULE_MODE_MANUAL
    snap["guests"][0]["talkativeness"] = 0.0
    (rc.room_dir / "room.toml").write_text(
        _render_room_toml(snap), encoding="utf-8"
    )
    reloaded = load_room_config(rc.room_dir, paths=env_paths)
    assert reloaded.schedule_mode == SCHEDULE_MODE_MANUAL
    assert reloaded.guests[0].talkativeness == 0.0


def test_round_trip_defaults_not_written(env_paths):
    """默认 scoring / 未填 talkativeness 不写盘（保持 toml 简洁）。"""
    rc = _seed_room(env_paths, room_id="p16-rt-def")
    rc2 = load_room_config(rc.room_dir, paths=env_paths)
    snap = _room_config_to_dict(rc2, env_paths)
    rendered = _render_room_toml(snap)
    assert "schedule_mode" not in rendered
    assert "talkativeness" not in rendered
