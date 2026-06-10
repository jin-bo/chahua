"""P13：``_inbound_user_message`` 从 ``files`` 筛出 ``images_rel`` 子集，文本标记照旧
追加全部 ``files``。图片 rel 流入 turn，非图 rel 只进文本标记不进像素通道。
"""

from __future__ import annotations

import asyncio

from chahua.server import INBOUND_USER_MESSAGE
from chahua.user_md import USER_SPEAKER_ID


async def _send_and_capture(srv, monkeypatch, files):
    """发一条带 files 的 user_message，捕获透传到 submit_user_message 的 images_rel。"""
    captured: dict = {}

    orig = srv._session.orchestrator.submit_user_message

    async def _capture(text, *, sink=None, task_id=..., images_rel=()):
        captured["text"] = text
        captured["images_rel"] = images_rel
        # 不真跑 AI 链：只 append 用户消息以模拟既有行为。
        return None

    monkeypatch.setattr(
        srv._session.orchestrator, "submit_user_message", _capture,
    )
    await srv._handle_inbound(
        {"type": INBOUND_USER_MESSAGE, "text": "看这张图", "files": files},
        lambda e: None,
    )
    task = srv._inflight_turn_task
    if task is not None:
        await task
    return captured


async def test_filters_images_from_files(task_inbound_srv, monkeypatch):
    _, srv = task_inbound_srv
    cap = await _send_and_capture(
        srv, monkeypatch, ["share/a.png", "share/b.txt"],
    )
    # 文本含两个标记（文本标记是普适视图）；与 agentao 降级标签同格式。
    assert '<attachment uri="share/a.png" mimetype="image/png"/>' in cap["text"]
    assert '<attachment uri="share/b.txt"/>' in cap["text"]
    # images_rel 只含图片。
    assert cap["images_rel"] == ("share/a.png",)


async def test_no_files_empty_images_rel(task_inbound_srv, monkeypatch):
    _, srv = task_inbound_srv
    cap = await _send_and_capture(srv, monkeypatch, None)
    assert cap["images_rel"] == ()


async def test_multiple_images_preserved(task_inbound_srv, monkeypatch):
    _, srv = task_inbound_srv
    cap = await _send_and_capture(
        srv, monkeypatch, ["share/a.png", "share/c.jpg", "share/d.gif"],
    )
    assert cap["images_rel"] == ("share/a.png", "share/c.jpg", "share/d.gif")


async def test_illegal_rel_excluded_from_images(task_inbound_srv, monkeypatch):
    """``..`` 穿越 rel 既不进文本标记，也不进 images_rel。"""
    _, srv = task_inbound_srv
    cap = await _send_and_capture(
        srv, monkeypatch, ["share/../boom.png", "share/ok.png"],
    )
    assert cap["images_rel"] == ("share/ok.png",)
    assert "boom" not in cap["text"]
