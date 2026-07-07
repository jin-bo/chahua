"""P18 M1：当前房历史搜索（只读、子串、无索引/分页/正则）。

复现优先：先钉死「命中/大小写/limit+truncated/严格入站/只读」六条行为，再有实现改动
不许破坏它们。走真 ``task_inbound_srv``（真 session + IOHandlers），直接调
``srv.io._inbound_search_room`` —— 与 wire dispatch 同一条 handler。
"""

from __future__ import annotations

from chahua.events import NOTICE_LEVEL_ERROR, ChahuaEventType
from chahua.user_md import USER_SPEAKER_ID


def _collect():
    out: list = []
    return out, out.append


def _results(envs):
    return [e for e in envs if e.type == ChahuaEventType.SEARCH_RESULTS]


def _notices(envs):
    return [e for e in envs if e.type == ChahuaEventType.NOTICE]


async def test_search_hit_basic(task_inbound_srv):
    session, srv = task_inbound_srv
    session.room.append(USER_SPEAKER_ID, "黄河路今天很热闹")
    session.room.append("宝总", "确实，我们去至真园吧")
    envs, sink = _collect()

    await srv.io._inbound_search_room({"query": "至真园"}, sink)

    res = _results(envs)
    assert len(res) == 1
    data = res[0].data
    assert data["query"] == "至真园"
    assert data["truncated"] is False
    assert len(data["results"]) == 1
    hit = data["results"][0]
    assert hit["speaker_id"] == "宝总"
    assert hit["seq"] == 2
    assert "至真园" in hit["snippet"]
    # 只带 speaker_id、不解析 display_name（与 room_history 同口径）。
    assert "display_name" not in hit


async def test_search_case_insensitive(task_inbound_srv):
    session, srv = task_inbound_srv
    session.room.append(USER_SPEAKER_ID, "Hello World")
    envs, sink = _collect()

    await srv.io._inbound_search_room({"query": "hello"}, sink)

    res = _results(envs)
    assert len(res[0].data["results"]) == 1


async def test_search_no_hit_empty_results(task_inbound_srv):
    session, srv = task_inbound_srv
    session.room.append(USER_SPEAKER_ID, "abc")
    envs, sink = _collect()

    await srv.io._inbound_search_room({"query": "zzz"}, sink)

    res = _results(envs)
    assert len(res) == 1
    assert res[0].data["results"] == []
    assert res[0].data["truncated"] is False


async def test_search_limit_truncates(task_inbound_srv):
    session, srv = task_inbound_srv
    for _ in range(5):
        session.room.append(USER_SPEAKER_ID, "match here")
    envs, sink = _collect()

    await srv.io._inbound_search_room({"query": "match", "limit": 3}, sink)

    data = _results(envs)[0].data
    assert len(data["results"]) == 3
    assert data["truncated"] is True


async def test_search_empty_query_no_frame(task_inbound_srv):
    session, srv = task_inbound_srv
    session.room.append(USER_SPEAKER_ID, "abc")
    envs, sink = _collect()

    # 空 query：require_str 只 WARN、静默早返，不发 SEARCH_RESULTS、不发 NOTICE。
    await srv.io._inbound_search_room({"query": ""}, sink)

    assert _results(envs) == []


async def test_search_non_str_query_no_frame(task_inbound_srv):
    session, srv = task_inbound_srv
    envs, sink = _collect()

    await srv.io._inbound_search_room({"query": 123}, sink)

    assert _results(envs) == []


async def test_search_caps_long_query(task_inbound_srv):
    """超长 query 兜底截断到上限（评审 #1）—— 证明截断生效：不截则 "A"*300 不是 "A"*250
    的子串（0 命中）；截到 200 后 "A"*200 ⊂ "A"*250 → 1 命中。"""
    session, srv = task_inbound_srv
    session.room.append(USER_SPEAKER_ID, "A" * 250)
    envs, sink = _collect()

    await srv.io._inbound_search_room({"query": "A" * 300}, sink)

    res = _results(envs)
    assert len(res) == 1
    assert len(res[0].data["results"]) == 1


async def test_search_unknown_key_rejected(task_inbound_srv):
    session, srv = task_inbound_srv
    session.room.append(USER_SPEAKER_ID, "abc")
    envs, sink = _collect()

    # 入站严格：未知键 → NOTICE error + 丢帧，无结果帧。
    await srv.io._inbound_search_room({"query": "abc", "bogus": 1}, sink)

    assert _results(envs) == []
    notices = _notices(envs)
    assert len(notices) == 1
    assert notices[0].data["level"] == NOTICE_LEVEL_ERROR


async def test_search_full_wire_frame_with_type(task_inbound_srv):
    """真实线帧含 ``type`` —— 走 _handle_inbound 完整分发，证明 ``type`` 不被入站白名单
    误拒（check_keys_whitelist 恒排除 ``type``）。回归守卫，防 ws 搜索被静默拒。"""
    session, srv = task_inbound_srv
    session.room.append(USER_SPEAKER_ID, "黄河路很热闹")
    envs, sink = _collect()

    await srv._handle_inbound({"type": "search_room", "query": "黄河路"}, sink)

    res = _results(envs)
    assert len(res) == 1
    assert len(res[0].data["results"]) == 1
    assert _notices(envs) == []


async def test_search_is_read_only(task_inbound_srv):
    session, srv = task_inbound_srv
    session.room.append(USER_SPEAKER_ID, "黄河路")
    session.room.append("宝总", "回声")
    before_count = len(session.room.messages_since(0))
    before_cursor = session.orchestrator.cursor.snapshot()
    envs, sink = _collect()

    await srv.io._inbound_search_room({"query": "黄河路"}, sink)

    # 搜索纯只读：transcript 与 cursor 都不该动。
    assert len(session.room.messages_since(0)) == before_count
    assert session.orchestrator.cursor.snapshot() == before_cursor
