"""MCP (Model Context Protocol) client manager for SAIVerse.

Manages connections to external MCP servers, discovers their tools,
and registers them into the SAIVerse tool system so that personas
can use them transparently through playbooks.

Usage::

    from tools.mcp_client import initialize_mcp, shutdown_mcp, get_mcp_manager

    # At application startup
    manager = await initialize_mcp()

    # At application shutdown
    await shutdown_mcp()
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import AsyncExitStack
from datetime import timedelta
from typing import Any, Dict, List, Optional

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from tools.core import ToolSchema

LOGGER = logging.getLogger(__name__)

# Default timeout for MCP tool calls (seconds)
_DEFAULT_TOOL_TIMEOUT_SECONDS = 120


class MCPServerConnection:
    """Manages a single MCP server connection."""

    def __init__(self, server_name: str, config: Dict[str, Any]) -> None:
        self.server_name = server_name
        self.config = config
        self.session: Optional[ClientSession] = None
        self.tools: List[Any] = []  # mcp.types.Tool objects
        self._exit_stack: Optional[AsyncExitStack] = None
        self._connected = False

    @property
    def connected(self) -> bool:
        return self._connected and self.session is not None

    @property
    def transport_type(self) -> str:
        if "command" in self.config:
            return "stdio"
        transport = self.config.get("transport", "streamable_http")
        return transport

    async def connect(self) -> None:
        """Connect to the MCP server and initialize the session."""
        if self._connected:
            return

        self._exit_stack = AsyncExitStack()

        try:
            transport_type = self.transport_type

            if transport_type == "stdio":
                await self._connect_stdio()
            elif transport_type == "sse":
                await self._connect_sse()
            else:  # streamable_http (default for url-based)
                await self._connect_streamable_http()

            # Initialize the session
            if self.session:
                init_result = await self.session.initialize()
                LOGGER.info(
                    "MCP server '%s' initialized (protocol=%s, server=%s)",
                    self.server_name,
                    getattr(init_result, "protocolVersion", "unknown"),
                    getattr(getattr(init_result, "serverInfo", None), "name", "unknown"),
                )
                self._connected = True

                # Discover tools
                await self._discover_tools()

        except Exception as exc:
            LOGGER.warning("MCP server '%s' failed to connect: %s", self.server_name, exc)
            await self._cleanup()
            raise

    async def _connect_stdio(self) -> None:
        """Connect via stdio transport (local process)."""
        command = self.config["command"]
        args = self.config.get("args", [])
        env = self.config.get("env")

        server_params = StdioServerParameters(
            command=command,
            args=args,
            env=env,
        )

        stdio_transport = await self._exit_stack.enter_async_context(
            stdio_client(server_params)
        )
        read_stream, write_stream = stdio_transport
        self.session = await self._exit_stack.enter_async_context(
            ClientSession(read_stream, write_stream)
        )

    async def _connect_sse(self) -> None:
        """Connect via SSE transport (remote server)."""
        from mcp.client.sse import sse_client

        url = self.config["url"]
        sse_transport = await self._exit_stack.enter_async_context(
            sse_client(url)
        )
        read_stream, write_stream = sse_transport
        self.session = await self._exit_stack.enter_async_context(
            ClientSession(read_stream, write_stream)
        )

    async def _connect_streamable_http(self) -> None:
        """Connect via streamable HTTP transport (remote server)."""
        from mcp.client.streamable_http import streamablehttp_client

        url = self.config["url"]
        http_transport = await self._exit_stack.enter_async_context(
            streamablehttp_client(url)
        )
        read_stream, write_stream = http_transport[0], http_transport[1]
        self.session = await self._exit_stack.enter_async_context(
            ClientSession(read_stream, write_stream)
        )

    async def _discover_tools(self) -> None:
        """Discover tools from the connected MCP server."""
        if not self.session:
            return

        result = await self.session.list_tools()
        self.tools = result.tools
        LOGGER.info(
            "MCP server '%s': discovered %d tool(s): %s",
            self.server_name,
            len(self.tools),
            [t.name for t in self.tools],
        )

    async def call_tool(self, tool_name: str, arguments: Dict[str, Any]) -> str:
        """Call a tool on the MCP server and return the result as a string.

        Args:
            tool_name: The original MCP tool name (without server prefix).
            arguments: Tool arguments dict.

        Returns:
            Tool result as a string.
        """
        if not self.session or not self._connected:
            raise ConnectionError(f"MCP server '{self.server_name}' is not connected")

        timeout_seconds = self.config.get("timeout", _DEFAULT_TOOL_TIMEOUT_SECONDS)

        try:
            result = await self.session.call_tool(
                name=tool_name,
                arguments=arguments,
                read_timeout_seconds=timedelta(seconds=timeout_seconds),
            )
        except Exception as exc:
            # Try one reconnect
            LOGGER.warning(
                "MCP tool call '%s' on server '%s' failed: %s. Attempting reconnect...",
                tool_name, self.server_name, exc,
            )
            try:
                await self._cleanup()
                await self.connect()
                result = await self.session.call_tool(
                    name=tool_name,
                    arguments=arguments,
                    read_timeout_seconds=timedelta(seconds=timeout_seconds),
                )
            except Exception as retry_exc:
                error_msg = (
                    f"MCP tool '{self.server_name}__{tool_name}' failed after reconnect: {retry_exc}"
                )
                LOGGER.error(error_msg)
                return error_msg

        # Check if the tool reported an error
        if result.isError:
            LOGGER.warning(
                "MCP tool '%s__%s' returned an error", self.server_name, tool_name
            )

        # Convert content to string
        return _format_tool_result(result)

    async def disconnect(self) -> None:
        """Disconnect from the MCP server."""
        await self._cleanup()

    async def _cleanup(self) -> None:
        """Clean up connection resources."""
        self._connected = False
        self.session = None
        self.tools = []
        if self._exit_stack:
            try:
                await self._exit_stack.aclose()
            except Exception as exc:
                LOGGER.debug("MCP server '%s' cleanup error: %s", self.server_name, exc)
            self._exit_stack = None


def _format_tool_result(result: Any) -> str:
    """Convert a CallToolResult to a plain text string."""
    texts: List[str] = []
    for item in result.content:
        if hasattr(item, "text"):
            texts.append(item.text)
        elif hasattr(item, "data"):
            item_type = getattr(item, "type", "binary")
            data_len = len(item.data) if item.data else 0
            texts.append(f"[{item_type}: {data_len} bytes]")
        elif hasattr(item, "uri"):
            texts.append(f"[resource: {item.uri}]")
        else:
            texts.append(str(item))
    return "\n".join(texts) if texts else "(no content)"


class MCPClientManager:
    """Manages all MCP server connections and tool registrations."""

    def __init__(self) -> None:
        self._connections: Dict[str, MCPServerConnection] = {}
        self._registered_tools: List[str] = []  # namespaced tool names

    async def start_all(self) -> None:
        """Load configs, connect to all enabled servers, and register tools."""
        from tools.mcp_config import load_mcp_configs

        configs = load_mcp_configs()
        if not configs:
            LOGGER.info("MCP: no servers configured")
            return

        for server_name, config in configs.items():
            conn = MCPServerConnection(server_name, config)
            try:
                await conn.connect()
                self._connections[server_name] = conn
                self._register_tools(conn)
            except Exception as exc:
                LOGGER.warning(
                    "MCP: server '%s' failed to start, skipping: %s",
                    server_name, exc,
                )

    def _register_tools(self, conn: MCPServerConnection) -> None:
        """Register tools from an MCP server into the SAIVerse tool registry."""
        from tools import register_external_tool

        for mcp_tool in conn.tools:
            namespaced_name = f"{conn.server_name}__{mcp_tool.name}"

            description = mcp_tool.description or ""
            prefixed_description = f"[MCP:{conn.server_name}] {description}"

            # MCP inputSchema is JSON Schema, directly maps to ToolSchema.parameters
            parameters = mcp_tool.inputSchema
            if not isinstance(parameters, dict):
                parameters = {"type": "object", "properties": {}}

            schema = ToolSchema(
                name=namespaced_name,
                description=prefixed_description,
                parameters=parameters,
                result_type="string",
            )

            # Create async wrapper that delegates to the MCP server
            wrapper = _make_mcp_tool_wrapper(conn, mcp_tool.name)

            register_external_tool(namespaced_name, schema, wrapper)
            self._registered_tools.append(namespaced_name)

    async def shutdown_all(self) -> None:
        """Disconnect all MCP servers and unregister their tools."""
        from tools import unregister_external_tool

        # Unregister all MCP tools
        for tool_name in self._registered_tools:
            unregister_external_tool(tool_name)
        self._registered_tools.clear()

        # Disconnect all servers
        for name, conn in self._connections.items():
            try:
                await conn.disconnect()
                LOGGER.info("MCP server '%s' disconnected", name)
            except Exception as exc:
                LOGGER.debug("MCP server '%s' disconnect error: %s", name, exc)
        self._connections.clear()

    async def reconnect_server(self, server_name: str) -> bool:
        """Reconnect a specific MCP server.

        Returns True if reconnection succeeded.
        """
        conn = self._connections.get(server_name)
        if not conn:
            LOGGER.warning("MCP: server '%s' not found for reconnect", server_name)
            return False

        # Unregister existing tools from this server
        from tools import unregister_external_tool
        prefix = f"{server_name}__"
        to_remove = [t for t in self._registered_tools if t.startswith(prefix)]
        for tool_name in to_remove:
            unregister_external_tool(tool_name)
            self._registered_tools.remove(tool_name)

        # Disconnect and reconnect
        try:
            await conn.disconnect()
            await conn.connect()
            self._register_tools(conn)
            LOGGER.info("MCP server '%s' reconnected successfully", server_name)
            return True
        except Exception as exc:
            LOGGER.warning("MCP server '%s' reconnect failed: %s", server_name, exc)
            return False

    def get_registered_tool_names(self) -> List[str]:
        """Return list of all registered MCP tool names."""
        return list(self._registered_tools)

    def get_server_status(self) -> List[Dict[str, Any]]:
        """Return status information for all servers."""
        statuses = []
        for name, conn in self._connections.items():
            statuses.append({
                "name": name,
                "transport": conn.transport_type,
                "connected": conn.connected,
                "tool_count": len(conn.tools),
                "tools": [t.name for t in conn.tools],
            })
        return statuses


def _make_mcp_tool_wrapper(connection: MCPServerConnection, tool_name: str):
    """Create an async wrapper function for an MCP tool.

    The returned function is a coroutine that can be ``await``-ed in the
    SEA runtime's async LangGraph nodes.
    """

    async def _mcp_tool_wrapper(**kwargs: Any) -> str:
        LOGGER.info(
            "MCP tool call: %s__%s args=%s",
            connection.server_name, tool_name, kwargs,
        )
        result = await connection.call_tool(tool_name, kwargs)
        LOGGER.info(
            "MCP tool result: %s__%s -> %s",
            connection.server_name, tool_name,
            result[:200] + "..." if len(result) > 200 else result,
        )
        return result

    # Set a useful function name for debugging
    _mcp_tool_wrapper.__name__ = f"{connection.server_name}__{tool_name}"
    _mcp_tool_wrapper.__qualname__ = f"mcp.{connection.server_name}.{tool_name}"

    return _mcp_tool_wrapper


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_manager: Optional[MCPClientManager] = None


def get_mcp_manager() -> Optional[MCPClientManager]:
    """Return the global MCPClientManager instance, or None if not initialized."""
    return _manager


async def initialize_mcp() -> Optional[MCPClientManager]:
    """Initialize MCP client connections.

    Loads configuration, connects to all enabled MCP servers,
    and registers their tools into the SAIVerse tool system.

    Returns:
        The MCPClientManager instance, or None if no servers are configured.
    """
    global _manager

    if _manager is not None:
        LOGGER.warning("MCP: already initialized, skipping")
        return _manager

    mgr = MCPClientManager()
    await mgr.start_all()

    if not mgr._connections:
        LOGGER.info("MCP: no servers connected")
        return None

    _manager = mgr
    return _manager


async def shutdown_mcp() -> None:
    """Shut down all MCP connections and unregister tools."""
    global _manager

    if _manager is None:
        return

    await _manager.shutdown_all()
    _manager = None
    LOGGER.info("MCP: shutdown complete")
