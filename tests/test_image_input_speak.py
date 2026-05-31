"""P13：``TeaGuest.speak(images_rel=...)`` 把本轮用户图懒读成 agentao ``images=``。

非图 rel 被过滤；空 images_rel → arun 收 ``images=None``（行为与 P13 前一致）。
"""

from __future__ import annotations

import base64
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

from chahua.events import ChahuaEventType, STATUS_OK
from chahua.guest import TeaGuest
from chahua.room import Room
from chahua.user_md import USER_SPEAKER_ID


class _FakeTransport:
    @contextmanager
    def bind(self, **kwargs: Any):
        yield self

    def emit_chahua(self, *a: Any, **k: Any) -> None:
        pass

    @property
    def partial_text(self) -> str:
        return ""


def _make_guest(room: Room, share_dir: Path | None) -> TeaGuest:
    g = TeaGuest.__new__(TeaGuest)
    g.name = "A"
    g.room = room
    from chahua.debug_recorder import NOOP_RECORDER
    g._recorder = NOOP_RECORDER
    g._share_dir = share_dir
    g._transport = _FakeTransport()  # type: ignore[assignment]
    agent = type("FakeAgent", (), {})()
    agent.arun = AsyncMock(return_value="ok")
    g.agent = agent  # type: ignore[attr-defined]
    room.add_participant("A")
    return g


async def test_speak_passes_resolved_images_to_arun(tmp_path: Path) -> None:
    share = tmp_path / "share"
    share.mkdir()
    (share / "x.png").write_bytes(b"pix")
    room = Room(name="r-img")
    room.add_participant(USER_SPEAKER_ID)
    g = _make_guest(room, share)

    await g.speak(
        "ctx", turn_id="t1", sink=lambda _e: None,
        images_rel=("share/x.png",),
    )

    _, kwargs = g.agent.arun.call_args  # type: ignore[attr-defined]
    assert kwargs["images"] == [{
        "data": base64.b64encode(b"pix").decode("ascii"),
        "mimeType": "image/png",
        "_source": "share/x.png",
    }]


async def test_speak_filters_non_image_rels(tmp_path: Path) -> None:
    share = tmp_path / "share"
    share.mkdir()
    (share / "a.png").write_bytes(b"pix")
    room = Room(name="r-filter")
    room.add_participant(USER_SPEAKER_ID)
    g = _make_guest(room, share)

    await g.speak(
        "ctx", turn_id="t1", sink=lambda _e: None,
        images_rel=("share/a.png", "share/doc.txt", "bogus"),
    )

    _, kwargs = g.agent.arun.call_args  # type: ignore[attr-defined]
    assert [i["_source"] for i in kwargs["images"]] == ["share/a.png"]


async def test_speak_no_images_passes_none(tmp_path: Path) -> None:
    """无图轮：arun 收 images=None（``resolved or None`` 短路），与 P13 前等价。"""
    room = Room(name="r-noimg")
    room.add_participant(USER_SPEAKER_ID)
    g = _make_guest(room, tmp_path / "share")

    await g.speak("ctx", turn_id="t1", sink=lambda _e: None)

    _, kwargs = g.agent.arun.call_args  # type: ignore[attr-defined]
    assert kwargs["images"] is None
