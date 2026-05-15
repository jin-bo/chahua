"""``chahua.exporter.format_room_markdown`` —— 房间导出 markdown 的格式回归。"""

from __future__ import annotations

import re
from pathlib import Path

from chahua.config import GuestConfig, RoomConfig
from chahua.exporter import format_room_markdown
from chahua.room import Message
from chahua.user_md import USER_SPEAKER_ID


def _rc(name: str, topic: str = "", room_dir: Path | None = None) -> RoomConfig:
    return RoomConfig(
        name=name,
        topic=topic,
        rules="",
        guests=(),
        room_dir=room_dir or Path("/tmp/fake-room"),
    )


def _msg(seq: int, speaker_id: str, text: str, ts_ms: int = 1715760000000) -> Message:
    return Message(
        seq=seq,
        speaker_id=speaker_id,
        text=text,
        ts_ms=ts_ms,
        message_id=f"msg_{seq:04x}",
    )


def test_filename_includes_room_name_and_timestamp():
    rc = _rc("深夜茶话室")
    fn, _ = format_room_markdown(rc, [], "老金")
    # `茶话室-深夜茶话室-YYYYMMDD-HHMMSS.md`。
    assert fn.startswith("茶话室-深夜茶话室-")
    assert fn.endswith(".md")
    assert re.search(r"-\d{8}-\d{6}\.md$", fn)


def test_filename_sanitizes_unsafe_chars():
    """房间名含路径 / 引号 / 反斜杠 → 替为下划线，不会逃出文件名安全边界。"""
    rc = _rc("../etc/passwd")
    fn, _ = format_room_markdown(rc, [], "老金")
    assert ".." not in fn
    assert "/" not in fn
    # 中文 + 拉丁 + 下划线 + 横杠之外应被洗掉；首尾下划线再 strip。
    assert "etc_passwd" in fn


def test_filename_empty_name_fallback():
    """房间名全是非法字符 → fallback 到 'untitled'，不报错。"""
    rc = _rc("///")
    fn, _ = format_room_markdown(rc, [], "老金")
    assert "untitled" in fn


def test_content_header_carries_name_topic_count():
    rc = _rc("深夜茶话室", topic="夜聊代码")
    _, content = format_room_markdown(rc, [_msg(1, "user", "嗨")], "老金")
    assert content.startswith("# 房间「深夜茶话室」")
    assert "> 主题：夜聊代码" in content
    assert "> 消息数：1" in content


def test_content_no_topic_line_when_topic_empty():
    """空 topic 不出"主题："行 —— 避免空字段污染头部。"""
    rc = _rc("无主题房")
    _, content = format_room_markdown(rc, [], "老金")
    assert "> 主题：" not in content
    assert "> 消息数：0" in content


def test_user_message_uses_display_name():
    """speaker_id=='user' 的消息渲染显示名（user_display_name），不漏内部 ID。"""
    rc = _rc("test")
    msgs = [
        _msg(1, USER_SPEAKER_ID, "我说一句"),
        _msg(2, "宝总", "你说啥"),
    ]
    _, content = format_room_markdown(rc, msgs, "老金")
    assert "## 老金 ·" in content
    assert "## 宝总 ·" in content
    # speaker_id 字面量不该泄露。
    assert "user ·" not in content


def test_message_text_is_paste_through():
    """茶客的代码块 / 列表 / markdown 源直接 paste，不重新转义 —— 否则导出的 .md 渲染走样。"""
    rc = _rc("test")
    msgs = [_msg(1, "宝总", "```python\nprint('x')\n```\n\n- 一\n- 二")]
    _, content = format_room_markdown(rc, msgs, "老金")
    assert "```python" in content
    assert "print('x')" in content
    assert "- 一" in content


def test_messages_preserve_order():
    rc = _rc("test")
    msgs = [_msg(i, "宝总" if i % 2 else USER_SPEAKER_ID, f"m{i}") for i in range(5)]
    _, content = format_room_markdown(rc, msgs, "老金")
    # 取所有 m<i> 出现位置，应严格递增。
    positions = [content.index(f"m{i}") for i in range(5)]
    assert positions == sorted(positions)


