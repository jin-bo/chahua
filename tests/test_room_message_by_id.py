"""P7.2.3: Room.message_by_id 线性查找 —— 命中 / 未命中 / 空房间。"""

from __future__ import annotations

from chahua.room import Room


def test_message_by_id_hit() -> None:
    room = Room(name="t")
    room.add_participant("user")
    room.add_participant("宝总")
    m1 = room.append("user", "你好")
    m2 = room.append("宝总", "侬好")
    assert room.message_by_id(m1.message_id) is m1
    assert room.message_by_id(m2.message_id) is m2


def test_message_by_id_miss() -> None:
    room = Room(name="t")
    room.add_participant("user")
    room.append("user", "你好")
    assert room.message_by_id("msg_does_not_exist") is None


def test_message_by_id_empty_room() -> None:
    assert Room(name="t").message_by_id("msg_anything") is None
