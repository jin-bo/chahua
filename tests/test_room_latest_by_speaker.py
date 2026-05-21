"""P7.4.1: Room.latest_message_by_speaker_id —— 命中取最近 / 未命中 / 空房间。"""

from __future__ import annotations

from chahua.room import Room


def test_latest_by_speaker_returns_most_recent() -> None:
    room = Room(name="t")
    room.add_participant("user")
    room.add_participant("范总")
    room.append("范总", "第一句")
    room.append("user", "插一句")
    m_last = room.append("范总", "第二句")
    got = room.latest_message_by_speaker_id("范总")
    assert got is m_last
    assert got.text == "第二句"


def test_latest_by_speaker_miss() -> None:
    room = Room(name="t")
    room.add_participant("user")
    room.append("user", "你好")
    assert room.latest_message_by_speaker_id("从没发言的人") is None


def test_latest_by_speaker_empty_room() -> None:
    assert Room(name="t").latest_message_by_speaker_id("user") is None
