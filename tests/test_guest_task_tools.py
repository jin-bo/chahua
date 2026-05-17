"""TeaGuest 装配 ``register_task_tools`` —— 端到端验证（docs §13 P5.3.5）。

验收：
① 装配后 ``agent.tools.tools`` 含三个 ``task_*``
② 三档权限下（read-only / workspace-write / full-access）三工具均存在且 is_read_only=True
③ propose 工具调用后 sink 里有 TASK_PROPOSAL envelope（proposer / kind / payload / task_id 都对）

不测 LLM 真调（``agentao.arun`` 要 LLM client）；只验工具注册 + transport.emit_chahua 路径。
``env_paths`` 走 conftest 共享版本。
"""

from __future__ import annotations

from typing import Any

import pytest

from chahua import admin
from chahua.events import ChahuaEventType
from chahua.session import build_room_session


def _build_session(env_paths, *, permission: str = "read-only"):
    rc = admin.create_room(
        paths=env_paths,
        room_id="t",
        name="t",
        guests=[{"persona": "chahua/personas/宝总.md", "name": "宝总", "permission": permission}],
    )
    return build_room_session(rc.room_dir, env_paths)


def test_teaguest_registers_three_task_tools(env_paths):
    session = _build_session(env_paths)
    try:
        guest = session.guests[0]
        tool_names = set(guest.agent.tools.tools.keys())
        for expected in ("task_list_artifacts", "task_propose_decision", "task_propose_open"):
            assert expected in tool_names, f"工具 {expected} 未注册"
    finally:
        session.close()


@pytest.mark.parametrize("permission", ["read-only", "workspace-write", "full-access"])
def test_task_tools_present_under_all_permissions(env_paths, permission):
    """三档权限模式下三工具均存在（is_read_only=True 让 read-only 也放行）。"""
    session = _build_session(env_paths, permission=permission)
    try:
        guest = session.guests[0]
        names = set(guest.agent.tools.tools.keys())
        assert "task_list_artifacts" in names
        assert "task_propose_decision" in names
        assert "task_propose_open" in names
        # 三工具的 is_read_only 仍是 True
        for n in ("task_list_artifacts", "task_propose_decision", "task_propose_open"):
            assert guest.agent.tools.tools[n].is_read_only is True
    finally:
        session.close()


def test_propose_tool_emits_envelope_through_transport(env_paths):
    """propose 工具的 execute 调 transport.emit_chahua —— bind 期内 sink 收到 TASK_PROPOSAL。"""
    session = _build_session(env_paths)
    try:
        guest = session.guests[0]
        captured: list[Any] = []
        # 模拟 speak() bind 期：手动绑 sink + turn_id + message_id + task_id
        with guest._transport.bind(  # type: ignore[attr-defined]
            sink=captured.append,
            turn_id="turn_x",
            message_id="msg_x",
            task_id="task_y",
        ):
            tool = guest.agent.tools.tools["task_propose_decision"]
            ack = tool.execute(summary="测试决策")
            assert "测试决策" in ack
        # 至少一帧 TASK_PROPOSAL
        proposals = [e for e in captured if e.type is ChahuaEventType.TASK_PROPOSAL]
        assert len(proposals) == 1
        env = proposals[0]
        assert env.data["proposer"] == "宝总"
        assert env.data["kind"] == "decision"
        assert env.data["payload"]["summary"] == "测试决策"
        # transport 把 bind 的 task_id 自动合进 envelope.data
        assert env.data.get("task_id") == "task_y"
    finally:
        session.close()
