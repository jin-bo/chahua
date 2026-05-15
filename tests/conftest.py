"""测试共享 stub —— ``IntentScorer`` / ``Summarizer`` / ``TeaGuest`` 的无 LLM 替身。

只放新测试文件实际共用的轻量替身。``test_orchestrator_cancel_during_scoring.py`` 用的
``_StubGuest`` 还要往 transcript 里写消息（``speak()`` 实现），跟纯路由测里只用 ``name``
属性的 stub 不一样，保留在该测试里，不强求收编进来。
"""

from __future__ import annotations

from chahua.cursor import GuestCursor
from chahua.orchestrator import Orchestrator, OrchestratorConfig
from chahua.room import Room
from chahua.scoring import IntentScorer
from chahua.summarizer import Summarizer
from chahua.user_md import USER_SPEAKER_ID, UserConfig


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
