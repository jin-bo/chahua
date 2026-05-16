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

    @property
    def transport_type(self):
        return "stdio"

    @property
    def is_trusted(self):
        return bool(self.config.get("trust"))

    async def connect(self):
        await asyncio.sleep(0)
        self.connect_loop = asyncio.get_running_loop()

    async def call_tool(self, tool_name, arguments):
        await asyncio.sleep(0)
        self.call_loop = asyncio.get_running_loop()
        return f"{tool_name}:{arguments['x']}"

    async def disconnect(self):
        await asyncio.sleep(0)
        self.disconnected = True


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
