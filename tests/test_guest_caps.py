"""list_guest_caps 后端投影：TeaGuest.describe_capabilities + Orchestrator.get_guest。

验收：
① describe_capabilities 返 {guest, permission, tools, skills}，tools 含茶客侧工具
② tool 项形状严格 {name, description, is_read_only}
③ get_guest 命中返实例 / 未命中返 None
"""

from __future__ import annotations

from chahua import admin
from chahua.session import build_room_session


def _session(env_paths):
    rc = admin.create_room(
        paths=env_paths,
        room_id="t1",
        name="t1",
        guests=[{"persona": "chahua/personas/宝总/宝总.md", "name": "宝总"}],
    )
    return build_room_session(rc.room_dir, env_paths)


def test_describe_capabilities_shape(env_paths):
    session = _session(env_paths)
    try:
        guest = session.orchestrator.get_guest("宝总")
        assert guest is not None
        caps = guest.describe_capabilities()
        assert caps["guest"] == "宝总"
        assert isinstance(caps["permission"], str) and caps["permission"]
        assert isinstance(caps["skills"], list)

        tool_names = {t["name"] for t in caps["tools"]}
        # P5.3.4 task 工具 + P7.4 handoff propose 工具都该注册到位
        assert "task_list_artifacts" in tool_names
        assert "propose_delegate" in tool_names
        for t in caps["tools"]:
            assert set(t) == {"name", "description", "is_read_only"}
            assert isinstance(t["is_read_only"], bool)
    finally:
        session.close()


def test_get_guest_hit_and_miss(env_paths):
    session = _session(env_paths)
    try:
        assert session.orchestrator.get_guest("宝总") is not None
        assert session.orchestrator.get_guest("查无此人") is None
    finally:
        session.close()
