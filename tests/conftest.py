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
from chahua.orchestrator import Orchestrator, OrchestratorConfig
from chahua.room import Room
from chahua.scoring import IntentScorer
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


class NoopScorer(IntentScorer):
    """跳过 ``IntentScorer.__init__``——测试只调 ``_find_user_mention`` 等本地方法。"""

    def __init__(self) -> None:
        pass


class NoopSummarizer(Summarizer):
    """``maybe_summarize`` 不调 LLM；``summaries`` 返回空。"""

    def __init__(self) -> None:
        pass

    async def maybe_summarize(self, *args, **kwargs):
        return None

    def clear(self) -> None:
        pass

    @property
    def summaries(self):
        return ()


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
