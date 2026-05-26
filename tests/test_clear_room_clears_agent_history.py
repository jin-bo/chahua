"""``clear_room`` 必须同步清每位茶客 agentao 进程内会话窗口（2026-05-19 回归）。

口径（CLAUDE.md 不变量"``clear_room`` 同步清茶客 agentao 进程内会话窗口"）：

- 进程内 ``agent.messages`` 跨 ``arun`` 累加 → 跟 transcript 同生命周期，clear 必清。
- ``agent.clear_history()`` 顺带清 active skills / todos / token counter，语义一致。
- 异常按茶客隔离 WARN 吞掉，一个 agent 坏掉不阻断其它茶客的 reset。
"""

from __future__ import annotations

import pytest

from chahua import admin
from chahua.session import build_room_session


def test_reset_room_clears_each_guest_agent_messages(env_paths):
    rc = admin.create_room(
        paths=env_paths, room_id="t1", name="t1",
        guests=[
            {"persona": "chahua/personas/宝总/宝总.md", "name": "宝总"},
            {"persona": "chahua/personas/汪小姐/汪小姐.md", "name": "汪小姐"},
        ],
    )
    session = build_room_session(rc.room_dir, env_paths)
    try:
        guests = list(session.orchestrator._guests.values())
        assert len(guests) == 2
        # 模拟"clear 前已累了若干轮对话"——直接往 agent.messages 塞。
        for g in guests:
            g.guest.agent.messages.append({"role": "user", "content": "前一轮的话"})
            g.guest.agent.messages.append({"role": "assistant", "content": "前一轮回的话"})
            assert g.guest.agent.messages  # sanity

        session.orchestrator.reset_room()

        for g in guests:
            # 这是承重墙：transcript 清空后，茶客脑里也不该残留 clear 前的对话。
            assert g.guest.agent.messages == [], (
                f"agent.messages for {g.guest.name!r} should be empty after reset_room"
            )
    finally:
        session.close()


def test_reset_room_isolates_clear_history_exception(env_paths, caplog):
    """单个茶客的 ``clear_history()`` 抛错时，其它茶客仍要清干净 + WARN 落日志。"""
    rc = admin.create_room(
        paths=env_paths, room_id="t1", name="t1",
        guests=[
            {"persona": "chahua/personas/宝总/宝总.md", "name": "宝总"},
            {"persona": "chahua/personas/汪小姐/汪小姐.md", "name": "汪小姐"},
        ],
    )
    session = build_room_session(rc.room_dir, env_paths)
    try:
        entries = list(session.orchestrator._guests.values())
        bao = next(e for e in entries if e.guest.name == "宝总")
        wang = next(e for e in entries if e.guest.name == "汪小姐")

        # 让宝总的 clear_history 抛，看汪小姐能不能照常清。
        def _boom() -> None:
            raise RuntimeError("simulated agent broken")

        bao.guest.agent.clear_history = _boom  # type: ignore[assignment]
        wang.guest.agent.messages.append({"role": "user", "content": "活着的话"})

        with caplog.at_level("WARNING", logger="chahua.orchestrator"):
            session.orchestrator.reset_room()

        # 汪小姐被清；宝总 raise 但被吞掉，整个 reset_room 不抛。
        assert wang.guest.agent.messages == []
        warns = [r for r in caplog.records if r.levelname == "WARNING"]
        assert any("clear_history failed" in r.getMessage() for r in warns), warns
    finally:
        session.close()


def test_reset_room_does_not_touch_memory_db(env_paths, tmp_path):
    """``clear_history()`` 是进程内操作，不能动 ``<workdir>/.agentao/memory.db``。

    跨重启长期记忆（人设印象 / 自有笔记）必须保留——这是"清空聊天 ≠ 重置茶客认知"
    的承重墙。
    """
    rc = admin.create_room(
        paths=env_paths, room_id="t1", name="t1",
        guests=[{"persona": "chahua/personas/宝总/宝总.md", "name": "宝总"}],
    )
    session = build_room_session(rc.room_dir, env_paths)
    try:
        guest = next(iter(session.orchestrator._guests.values())).guest
        memory_dir = guest.working_directory / ".agentao"
        memory_dir.mkdir(parents=True, exist_ok=True)
        memory_db = memory_dir / "memory.db"
        memory_db.write_bytes(b"sentinel-memory-blob")
        mtime_before = memory_db.stat().st_mtime_ns

        session.orchestrator.reset_room()

        assert memory_db.exists()
        assert memory_db.read_bytes() == b"sentinel-memory-blob"
        assert memory_db.stat().st_mtime_ns == mtime_before
    finally:
        session.close()
