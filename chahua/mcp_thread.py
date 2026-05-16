"""Thread-backed MCP manager for embedded async hosts.

``agentao`` currently exposes a synchronous ``McpClientManager`` whose bridge
calls ``loop.run_until_complete``.  That works from a plain CLI thread, but
fails when ``Agentao`` is constructed inside chahua's websocket event loop.

This adapter keeps the same small manager surface used by
``agentao.tooling.register_mcp_tools`` while running all MCP async work on a
dedicated background loop.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any, Dict, List, Optional, Tuple

from agentao.mcp.client import McpClient
from mcp.types import Tool as McpToolDef

_log = logging.getLogger(__name__)


class ThreadedMcpClientManager:
    """MCP client manager with a private event-loop thread."""

    def __init__(self, server_configs: Dict[str, Dict[str, Any]]):
        self._configs = server_configs
        self._clients: Dict[str, McpClient] = {}
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._started = threading.Event()

    @property
    def clients(self) -> Dict[str, McpClient]:
        return self._clients

    @property
    def server_configs(self) -> Dict[str, Dict[str, Any]]:
        return self._configs

    def connect_all(self) -> None:
        """Connect to all configured MCP servers."""
        if not self._configs:
            return
        self._ensure_loop()
        self._run(self._connect_all_async())

    async def _connect_all_async(self) -> None:
        async def _connect_one(name: str, config: Dict[str, Any]) -> None:
            client = McpClient(name, config)
            self._clients[name] = client
            try:
                await client.connect()
            except Exception as e:
                _log.error("Failed to start MCP server %r: %s", name, e)

        await asyncio.gather(
            *[_connect_one(name, cfg) for name, cfg in self._configs.items()],
            return_exceptions=True,
        )

    def get_client(self, name: str) -> Optional[McpClient]:
        return self._clients.get(name)

    def get_all_tools(self) -> List[Tuple[str, McpToolDef]]:
        tools: List[Tuple[str, McpToolDef]] = []
        for name, client in self._clients.items():
            if client.status.value == "connected":
                for tool in client.tools:
                    tools.append((name, tool))
        return tools

    def call_tool(self, server_name: str, tool_name: str, arguments: Dict[str, Any]) -> str:
        client = self._clients.get(server_name)
        if not client:
            raise RuntimeError(f"MCP server '{server_name}' not found")
        self._ensure_loop()
        return self._run(client.call_tool(tool_name, arguments))

    def disconnect_all(self) -> None:
        if self._loop is None:
            return
        try:
            if self._clients:
                self._run(self._disconnect_all_async())
        finally:
            loop = self._loop
            thread = self._thread
            loop.call_soon_threadsafe(loop.stop)
            if thread is not None and thread is not threading.current_thread():
                thread.join(timeout=5)
            self._loop = None
            self._thread = None
            self._started.clear()

    async def _disconnect_all_async(self) -> None:
        for client in self._clients.values():
            await client.disconnect()
        self._clients.clear()

    def get_server_status(self) -> List[Dict[str, Any]]:
        result: List[Dict[str, Any]] = []
        for name, client in self._clients.items():
            result.append(
                {
                    "name": name,
                    "status": client.status.value,
                    "transport": client.transport_type,
                    "tools": len(client.tools),
                    "trusted": client.is_trusted,
                    "error": client.error_message,
                }
            )
        return result

    def _ensure_loop(self) -> None:
        if self._loop is not None and self._thread is not None and self._thread.is_alive():
            return
        self._loop = asyncio.new_event_loop()
        self._started.clear()
        self._thread = threading.Thread(
            target=self._run_loop,
            name="chahua-mcp",
            daemon=True,
        )
        self._thread.start()
        self._started.wait(timeout=5)
        if not self._started.is_set():
            raise RuntimeError("MCP event loop thread did not start")

    def _run_loop(self) -> None:
        assert self._loop is not None
        asyncio.set_event_loop(self._loop)
        self._started.set()
        try:
            self._loop.run_forever()
        finally:
            self._loop.close()

    def _run(self, coro):
        if self._loop is None:
            raise RuntimeError("MCP event loop is not running")
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result()
