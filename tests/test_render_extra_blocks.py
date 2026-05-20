"""P7.2.4: ContextRenderer.build_context_for 的 extra_blocks 参数。

覆盖：
- onboarding / incremental 两条路径都把 extra_blocks 渲在 <speak_instruction> 之前。
- extra_blocks=None 时输出与改动前一致（不引入空块 / 多余换行）。
- 多元素 extra_blocks 按列表顺序注入。
"""

from __future__ import annotations

from pathlib import Path

from chahua.context_renderer import ContextRenderer
from chahua.orchestrator_config import OrchestratorConfig
from chahua.room import Room
from chahua.user_md import USER_SPEAKER_ID, UserConfig

from conftest import NoopSummarizer

_BLOCK = "<review_target>\n审这条\n</review_target>"


def _renderer(tmp_path: Path) -> tuple[ContextRenderer, Room]:
    room = Room(name="t")
    room.add_participant(USER_SPEAKER_ID)
    room.add_participant("魏理论")
    renderer = ContextRenderer(
        room=room,
        user_config=UserConfig(display_name="老金", full_md=None, source=None),
        summarizer=NoopSummarizer(),
        config=OrchestratorConfig(onboarding_threshold=20),
    )
    return renderer, room


def test_onboarding_extra_block_before_speak_instruction(tmp_path: Path) -> None:
    renderer, room = _renderer(tmp_path)
    room.append(USER_SPEAKER_ID, "你好")
    ctx = renderer.build_context_for("魏理论", 0, extra_blocks=[_BLOCK])
    assert _BLOCK in ctx
    assert ctx.index(_BLOCK) < ctx.index("<speak_instruction>")


def test_incremental_extra_block_before_speak_instruction(tmp_path: Path) -> None:
    renderer, room = _renderer(tmp_path)
    room.append(USER_SPEAKER_ID, "第一句")
    room.append(USER_SPEAKER_ID, "第二句")
    # last_seen=1 且增量小 → incremental 路径。
    ctx = renderer.build_context_for("魏理论", 1, extra_blocks=[_BLOCK])
    assert "<room_update " in ctx
    assert _BLOCK in ctx
    assert ctx.index(_BLOCK) < ctx.index("<speak_instruction>")


def test_extra_blocks_none_unchanged(tmp_path: Path) -> None:
    renderer, room = _renderer(tmp_path)
    room.append(USER_SPEAKER_ID, "你好")
    baseline = renderer.build_context_for("魏理论", 0)
    assert renderer.build_context_for("魏理论", 0, extra_blocks=None) == baseline
    assert "<review_target>" not in baseline


def test_extra_blocks_preserve_order(tmp_path: Path) -> None:
    renderer, room = _renderer(tmp_path)
    room.append(USER_SPEAKER_ID, "你好")
    b1, b2 = "<aaa>1</aaa>", "<bbb>2</bbb>"
    ctx = renderer.build_context_for("魏理论", 0, extra_blocks=[b1, b2])
    assert ctx.index(b1) < ctx.index(b2) < ctx.index("<speak_instruction>")
