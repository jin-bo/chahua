from __future__ import annotations

import asyncio
import json

from chahua import mcp_thread
from chahua.guest import _merged_mcp_configs


class _Status:
    value = "connected"


class _FakeClient:
    def __init__(self, name, config):
        self.name = name
        self.config = config
        self.status = _Status()
        self.tools = []
        self.error_message = None
        self.connect_loop = None
        self.call_loop = None
        self.disconnected = False
        self.connect_task = None
        self.disconnect_task = None

    @property
    def transport_type(self):
        return "stdio"

    @property
    def is_trusted(self):
        return bool(self.config.get("trust"))

    async def connect(self):
        await asyncio.sleep(0)
        self.connect_loop = asyncio.get_running_loop()
        self.connect_task = asyncio.current_task()

    async def call_tool(self, tool_name, arguments):
        await asyncio.sleep(0)
        self.call_loop = asyncio.get_running_loop()
        return f"{tool_name}:{arguments['x']}"

    async def disconnect(self):
        await asyncio.sleep(0)
        self.disconnected = True
        self.disconnect_task = asyncio.current_task()


async def test_threaded_mcp_manager_works_inside_running_loop(monkeypatch):
    monkeypatch.setattr(mcp_thread, "McpClient", _FakeClient)

    main_loop = asyncio.get_running_loop()
    mgr = mcp_thread.ThreadedMcpClientManager(
        {"demo": {"command": "fake", "trust": True}}
    )
    try:
        mgr.connect_all()
        client = mgr.get_client("demo")

        assert client is not None
        assert client.connect_loop is not main_loop
        assert mgr.call_tool("demo", "echo", {"x": 3}) == "echo:3"
        assert client.call_loop is client.connect_loop
        assert mgr.get_server_status()[0]["trusted"] is True
    finally:
        mgr.disconnect_all()


async def test_owner_task_connects_and_disconnects_in_same_task(monkeypatch):
    """P17：一个 owner task 必须同时进（connect）出（disconnect）client 的
    ``AsyncExitStack``，否则 streamable-http / SSE 的 anyio task group 会在拆除时
    抛 "Attempted to exit cancel scope in a different task"。这里钉死「同一 task
    进出」不变量 —— 回落到「connect 一个 task / disconnect 另一个 task」的旧形状即失败。"""
    monkeypatch.setattr(mcp_thread, "McpClient", _FakeClient)

    mgr = mcp_thread.ThreadedMcpClientManager({"demo": {"command": "fake"}})
    mgr.connect_all()
    client = mgr.get_client("demo")
    mgr.disconnect_all()

    assert client is not None
    assert client.disconnected is True
    assert client.connect_task is not None
    assert client.connect_task is client.disconnect_task


def test_merged_mcp_configs_preserves_file_loaded_and_overlays_persona(
    tmp_path, monkeypatch
):
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path / "home")
    user_cfg_dir = tmp_path / "home" / ".agentao"
    project_cfg_dir = tmp_path / "wd" / ".agentao"
    user_cfg_dir.mkdir(parents=True)
    project_cfg_dir.mkdir(parents=True)

    (user_cfg_dir / "mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "user": {"command": "user-cmd"},
                    "shared": {"command": "user-shared"},
                }
            }
        ),
        encoding="utf-8",
    )
    (project_cfg_dir / "mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "project": {"command": "project-cmd"},
                    "shared": {"command": "project-shared"},
                }
            }
        ),
        encoding="utf-8",
    )

    merged = _merged_mcp_configs(
        tmp_path / "wd",
        {
            "persona": {"command": "persona-cmd"},
            "shared": {"command": "persona-shared", "trust": False},
        },
    )

    assert merged["user"]["command"] == "user-cmd"
    assert merged["project"]["command"] == "project-cmd"
    assert merged["persona"] == {"command": "persona-cmd", "trust": True}
    assert merged["shared"] == {"command": "persona-shared", "trust": False}


def test_merged_mcp_configs_room_level_overrides_persona_and_file(
    tmp_path, monkeypatch
):
    """P4.3：`[[guest.extra_mcp_servers]]` 是用户在自己 room.toml 里手写的 ——
    等价用户意图自动信任，与 persona / 文件加载层同名时**房间级覆盖**。"""
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path / "home")
    user_cfg_dir = tmp_path / "home" / ".agentao"
    user_cfg_dir.mkdir(parents=True)
    (user_cfg_dir / "mcp.json").write_text(
        json.dumps(
            {"mcpServers": {"shared": {"command": "user-shared"}}}
        ),
        encoding="utf-8",
    )

    merged = _merged_mcp_configs(
        tmp_path / "wd",
        {
            "persona-only": {"command": "persona-cmd"},
            "shared": {"command": "persona-shared", "trust": False},
        },
        room_level_servers={
            "room-only": {"command": "room-cmd", "args": ["--port", "9000"]},
            "shared": {"command": "room-shared"},  # 覆盖 persona / 文件加载层同名
        },
    )

    # 房间级 entry 自动信任，无需任何 trust 标记。
    assert merged["room-only"] == {
        "command": "room-cmd", "args": ["--port", "9000"], "trust": True,
    }
    # 同名时房间级覆盖 persona（含信任语义 —— 即便 persona trust=False）。
    assert merged["shared"] == {"command": "room-shared", "trust": True}
    # persona-only 仍走 persona trust 默认（cfg 无 trust → True）。
    assert merged["persona-only"] == {"command": "persona-cmd", "trust": True}


def test_merged_mcp_configs_no_room_level_falls_through(tmp_path, monkeypatch):
    """room_level_servers=None / [] → 与原 persona-only 路径行为等价。"""
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path / "home")
    (tmp_path / "home" / ".agentao").mkdir(parents=True)
    merged = _merged_mcp_configs(
        tmp_path / "wd",
        {"persona": {"command": "p"}},
        room_level_servers=None,
    )
    assert merged == {"persona": {"command": "p", "trust": True}}
