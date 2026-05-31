"""测试共享 stub —— ``IntentScorer`` / ``Summarizer`` / ``TeaGuest`` 的无 LLM 替身。

只放新测试文件实际共用的轻量替身。``test_orchestrator_cancel_during_scoring.py`` 用的
``_StubGuest`` 还要往 transcript 里写消息（``speak()`` 实现），跟纯路由测里只用 ``name``
属性的 stub 不一样，保留在该测试里，不强求收编进来。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from chahua._paths import ENV_APP_ROOT, ENV_USER_DATA_ROOT, Paths
from chahua.cursor import GuestCursor
from chahua.events import (
    STATUS_OK,
    ChahuaEnvelope,
    ChahuaEventType,
    new_message_id,
)
from chahua.orchestrator import Orchestrator, OrchestratorConfig
from chahua.room import Room
from chahua.scoring import IntentScorer, ScoreKind, ScoreResult
from chahua.summarizer import Summarizer
from chahua.user_md import USER_SPEAKER_ID, UserConfig

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def room():
    """裸 :class:`Room`（USER_SPEAKER_ID 已加），不挂 transcript_path。

    需要落盘 transcript 的测试别用这条，自己 ``Room(name=..., transcript_path=...)``
    在 test 里现造（持久化路径与 ``tmp_path`` 强耦合，fixture 复用收益不大）。"""
    r = Room(name="t")
    r.add_participant(USER_SPEAKER_ID)
    return r


@pytest.fixture
def env_paths(tmp_path, monkeypatch):
    """与 dev/prod 二根隔离的 Paths 夹具 —— ``user_data_root`` 指向 tmp，
    ``app_root`` 指向仓库根（persona / templates 找得到）。LLM 凭据全打假值，
    任何 ``build_room_session`` 都能跑到尾不真请 LLM。

    server_inbound / room_info / task_link / upload 等"装真房间"的测全靠这条。
    """
    user_data = tmp_path / "userdata"
    user_data.mkdir()
    monkeypatch.setenv(ENV_APP_ROOT, str(REPO_ROOT))
    monkeypatch.setenv(ENV_USER_DATA_ROOT, str(user_data))
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("OPENAI_MODEL", "gpt-5.4")
    return Paths.from_env()


class StubGuest:
    """只暴露 ``.name``，给路由 / 打分 / 计数类测试用——这些路径不调 ``speak()``。"""

    def __init__(self, name: str) -> None:
        self.name = name


class SpeakingStubGuest:
    """duck-typed TeaGuest：发 message_start/end + 写 transcript + （可选）调 recorder。

    给 ``test_orchestrator_cancel_during_scoring`` / ``test_orchestrator_debug_capture``
    这类需要真跑过 ``_let_speak`` 的测试用。``recorder=None`` 时跳过 recorder 调用
    （cancel-during-scoring 测不挂 recorder）；``recorder=NOOP_RECORDER`` 或真实例时
    各自按 enabled / capture_prompts 决定是否落盘。
    """

    def __init__(self, name: str, room: Room, recorder=None) -> None:
        self.name = name
        self._room = room
        self._recorder = recorder

    async def speak(
        self,
        context_message,
        *,
        turn_id,
        sink,
        cancellation_token=None,
        task_id=None,
        images_rel=(),
    ):
        message_id = new_message_id()
        if self._recorder is not None:
            self._recorder.record_message_start(
                message_id=message_id, guest=self.name,
                speak_prompt=context_message,
            )
        sink(
            ChahuaEnvelope(
                room_id=self._room.name,
                turn_id=turn_id,
                guest_name=self.name,
                message_id=message_id,
                type=ChahuaEventType.MESSAGE_START,
            )
        )
        msg = self._room.append(
            self.name, "(stub reply)", message_id=message_id, task_id=task_id,
        )
        sink(
            ChahuaEnvelope(
                room_id=self._room.name,
                turn_id=turn_id,
                guest_name=self.name,
                message_id=message_id,
                type=ChahuaEventType.MESSAGE_END,
                status=STATUS_OK,
                seq=msg.seq,
                data={"text": "(stub reply)"},
            )
        )
        if self._recorder is not None:
            self._recorder.record_message_end(
                message_id=message_id, status=STATUS_OK, seq=msg.seq,
            )
        return msg


class NoopScorer(IntentScorer):
    """跳过 ``IntentScorer.__init__``——测试只调 ``_find_user_mention`` 等本地方法。"""

    def __init__(self) -> None:
        pass


class FixedScorer(IntentScorer):
    """按名字返固定分数的 :class:`IntentScorer` 替身；不调 LLM。

    ``capture_prompts=True`` 路径走 :meth:`score_with_prompt`，返回 ``(result,
    "PROMPT for <name>")`` —— 测试用 ``"PROMPT for "`` 前缀断言 piggyback 落点。
    """

    def __init__(self, scores: dict[str, float], *, include_raw: bool = True) -> None:
        self._scores = scores
        self._include_raw = include_raw

    async def score(
        self, *, guest_name, persona, transcript_text, user_config,
        subject_mention_count=0, task_block="",
    ):
        s = self._scores.get(guest_name, 0.0)
        return ScoreResult(
            guest_name=guest_name,
            score=s,
            kind=ScoreKind.SCORED,
            raw=(f'{{"score":{s}}}' if self._include_raw else ""),
        )

    async def score_with_prompt(
        self, *, guest_name, persona, transcript_text, user_config,
        subject_mention_count=0, task_block="",
    ):
        result = await self.score(
            guest_name=guest_name, persona=persona,
            transcript_text=transcript_text, user_config=user_config,
            subject_mention_count=subject_mention_count, task_block=task_block,
        )
        return result, f"PROMPT for {guest_name}"


class NoopSummarizer(Summarizer):
    """``maybe_summarize`` 不调 LLM；``summaries`` 默认返回空。

    ``summaries=`` 可注入静态 SummarySpan 列表给"测 onboarding 含近期梗概"类用例。
    """

    def __init__(self, summaries=None) -> None:
        self._fixed = tuple(summaries or ())

    async def maybe_summarize(self, *args, **kwargs):
        return None

    def clear(self) -> None:
        pass

    @property
    def summaries(self):
        return self._fixed


@pytest.fixture
def task_inbound_srv(env_paths, monkeypatch):
    """挂真 ``RoomSession`` + ``ChahuaServer`` 的回归夹具，monkeypatch ``_run_ai_chain``
    成 no-op 让 ``submit_user_message`` 跑到 ``room.append`` 后立刻返回 —— 避免在测试
    event loop 上真打分 / 调 LLM。yield ``(session, srv)``。

    任何需要走真 ``_handle_inbound`` 链路又不能真跑 AI 链的测都拿这条；fixture 之外
    自己 ``build_room_session`` + ``object.__new__`` 这套样板会跟设计走样（
    ``_install_handler_slots`` 与 ``_bind_inbound_handlers`` 顺序敏感）。
    """
    from chahua import admin
    from chahua.orchestrator import Orchestrator
    from chahua.server import (
        ChahuaServer,
        _bind_inbound_handlers,
        _install_handler_slots,
    )
    from chahua.session import build_room_session

    async def _noop_ai_chain(self, *, sink, active_task_id=None, images_rel=()):
        return None

    monkeypatch.setattr(Orchestrator, "_run_ai_chain", _noop_ai_chain)

    rc = admin.create_room(
        paths=env_paths, room_id="t1", name="t1",
        guests=[{"persona": "chahua/personas/宝总/宝总.md", "name": "宝总"}],
    )
    session = build_room_session(rc.room_dir, env_paths)
    srv = object.__new__(ChahuaServer)
    srv._session = session
    srv._paths = env_paths
    srv._inflight_turn_task = None
    srv._inflight_kind = None
    _install_handler_slots(srv)
    srv._inbound_handlers = _bind_inbound_handlers(srv)
    yield session, srv
    session.close()


async def drain_inflight(srv) -> None:
    """等当前 ``_inflight_turn_task``（用户消息 turn）跑完再返回控制权。配合
    :func:`task_inbound_srv` 的 no-op AI 链 —— await 即完成。"""
    task = srv._inflight_turn_task
    if task is not None and not task.done():
        await task


def build_orch(*names: str, scorer: IntentScorer | None = None) -> Orchestrator:
    """组一只装好 ``names`` 的 Orchestrator；默认走 NoopScorer + NoopSummarizer。

    ``scorer=`` 给端到端测试传自定义 capturing scorer 用。
    """
    room = Room(name="t")
    room.add_participant(USER_SPEAKER_ID)
    orch = Orchestrator(
        room=room,
        user_config=UserConfig(display_name="老金", full_md=None, source=None),
        scorer=scorer if scorer is not None else NoopScorer(),
        summarizer=NoopSummarizer(),
        cursor=GuestCursor(),
        config=OrchestratorConfig(summary_block_size=999),
    )
    for n in names:
        orch.register(StubGuest(n), persona_md=f"{n} persona")  # type: ignore[arg-type]
    return orch
