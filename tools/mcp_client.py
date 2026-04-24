"""MCP client manager for dynamically registering external tools."""
from __future__ import annotations

import asyncio
import inspect
import logging
import threading
from contextlib import AsyncExitStack
from datetime import timedelta
from typing import Any, Dict, List, Optional

from tools.core import ToolSchema

LOGGER = logging.getLogger(__name__)

_DEFAULT_TOOL_TIMEOUT_SECONDS = 120
_loop: Optional[asyncio.AbstractEventLoop] = None
_loop_thread: Optional[threading.Thread] = None


def _normalize_spell_config(raw_value: Any) -> Dict[str, Dict[str, Any]]:
    """Normalize spell tool declarations into ``tool_name -> options``."""
    result: Dict[str, Dict[str, Any]] = {}
    if isinstance(raw_value, list):
        for entry in raw_value:
            if isinstance(entry, str):
                result[entry] = {}
            elif isinstance(entry, dict):
                tool_name = entry.get("name")
                if isinstance(tool_name, str) and tool_name:
                    result[tool_name] = {
                        key: value for key, value in entry.items()
                        if key != "name"
                    }
    elif isinstance(raw_value, dict):
        for tool_name, options in raw_value.items():
            if isinstance(tool_name, str) and tool_name:
                if options is True or options is None:
                    result[tool_name] = {}
                elif isinstance(options, str):
                    result[tool_name] = {"display_name": options}
                elif isinstance(options, dict):
                    result[tool_name] = dict(options)
    return result


def _tool_schema_from_mcp(
    namespaced_name: str,
    server_name: str,
    tool_name: str,
    tool_def: Any,
    spell_config: Dict[str, Dict[str, Any]],
) -> ToolSchema:
    description = getattr(tool_def, "description", "") or ""
    parameters = getattr(tool_def, "inputSchema", None)
    if not isinstance(parameters, dict):
        parameters = {"type": "object", "properties": {}}

    spell_options = spell_config.get(tool_name, {})
    display_name = spell_options.get("display_name") or spell_options.get("spell_display_name") or ""

    return ToolSchema(
        name=namespaced_name,
        description=f"[MCP:{server_name}] {description}".strip(),
        parameters=parameters,
        result_type="string",
        spell=tool_name in spell_config,
        spell_display_name=str(display_name) if display_name else "",
    )


def _format_tool_result(result: Any) -> str:
    content = getattr(result, "content", None) or []
    if not content:
        return "(no content)"

    rendered: List[str] = []
    for item in content:
        text = getattr(item, "text", None)
        if text:
            rendered.append(str(text))
            continue
        uri = getattr(item, "uri", None)
        if uri:
            rendered.append(f"[resource: {uri}]")
            continue
        data = getattr(item, "data", None)
        if data is not None:
            rendered.append(f"[binary: {len(data)} bytes]")
            continue
        rendered.append(str(item))
    return "\n".join(rendered)


class MCPServerConnection:
    """Manage one MCP server connection."""

    def __init__(self, server_name: str, config: Dict[str, Any]) -> None:
        self.server_name = server_name
        self.config = config
        self.session: Any = None
        self.tools: List[Any] = []
        self._connected = False
        self._exit_stack: Optional[AsyncExitStack] = None

    @property
    def connected(self) -> bool:
        return self._connected and self.session is not None

    @property
    def transport_type(self) -> str:
        if "command" in self.config:
            return "stdio"
        return str(self.config.get("transport", "streamable_http"))

    async def connect(self) -> None:
        if self.connected:
            return

        self._exit_stack = AsyncExitStack()
        try:
            if self.transport_type == "stdio":
                await self._connect_stdio()
            elif self.transport_type == "sse":
                await self._connect_sse()
            else:
                await self._connect_streamable_http()

            if self.session is None:
                raise RuntimeError("MCP session was not created")

            init_result = await self.session.initialize()
            self._connected = True
            LOGGER.info(
                "MCP server '%s' initialized (protocol=%s, server=%s)",
                self.server_name,
                getattr(init_result, "protocolVersion", "unknown"),
                getattr(getattr(init_result, "serverInfo", None), "name", "unknown"),
            )
            await self._discover_tools()
        except Exception:
            await self.disconnect()
            raise

    async def _connect_stdio(self) -> None:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        server_params = StdioServerParameters(
            command=self.config["command"],
            args=self.config.get("args", []),
            env=self.config.get("env"),
        )
        read_stream, write_stream = await self._exit_stack.enter_async_context(  # type: ignore[union-attr]
            stdio_client(server_params)
        )
        self.session = await self._exit_stack.enter_async_context(  # type: ignore[union-attr]
            ClientSession(read_stream, write_stream)
        )

    async def _connect_sse(self) -> None:
        from mcp import ClientSession
        from mcp.client.sse import sse_client

        read_stream, write_stream = await self._exit_stack.enter_async_context(  # type: ignore[union-attr]
            sse_client(self.config["url"])
        )
        self.session = await self._exit_stack.enter_async_context(  # type: ignore[union-attr]
            ClientSession(read_stream, write_stream)
        )

    async def _connect_streamable_http(self) -> None:
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        transport = await self._exit_stack.enter_async_context(  # type: ignore[union-attr]
            streamablehttp_client(self.config["url"])
        )
        read_stream, write_stream = transport[0], transport[1]
        self.session = await self._exit_stack.enter_async_context(  # type: ignore[union-attr]
            ClientSession(read_stream, write_stream)
        )

    async def _discover_tools(self) -> None:
        result = await self.session.list_tools()
        self.tools = list(getattr(result, "tools", []) or [])
        LOGGER.info(
            "MCP server '%s': discovered %d tool(s): %s",
            self.server_name,
            len(self.tools),
            [getattr(tool, "name", "?") for tool in self.tools],
        )

    async def call_tool(self, tool_name: str, arguments: Dict[str, Any]) -> str:
        if not self.connected:
            raise ConnectionError(f"MCP server '{self.server_name}' is not connected")

        timeout_seconds = int(self.config.get("timeout", _DEFAULT_TOOL_TIMEOUT_SECONDS))
        try:
            result = await self.session.call_tool(
                name=tool_name,
                arguments=arguments,
                read_timeout_seconds=timedelta(seconds=timeout_seconds),
            )
        except Exception as exc:
            LOGGER.warning(
                "MCP tool call '%s__%s' failed (%s). Reconnecting once...",
                self.server_name,
                tool_name,
                exc,
            )
            await self.disconnect()
            await self.connect()
            result = await self.session.call_tool(
                name=tool_name,
                arguments=arguments,
                read_timeout_seconds=timedelta(seconds=timeout_seconds),
            )

        if getattr(result, "isError", False):
            LOGGER.warning("MCP tool '%s__%s' returned an error", self.server_name, tool_name)
        return _format_tool_result(result)

    async def disconnect(self) -> None:
        self._connected = False
        self.tools = []
        self.session = None
        if self._exit_stack is not None:
            try:
                await self._exit_stack.aclose()
            except Exception as exc:
                LOGGER.debug("MCP server '%s' cleanup error: %s", self.server_name, exc)
            self._exit_stack = None


class MCPClientManager:
    """Manage all configured MCP servers."""

    def __init__(self) -> None:
        self._connections: Dict[str, MCPServerConnection] = {}
        self._registered_tools: Dict[str, Dict[str, Any]] = {}

    async def start_all(self) -> None:
        from tools.mcp_config import load_mcp_configs

        configs = load_mcp_configs()
        if not configs:
            LOGGER.info("MCP: no servers configured")
            return

        try:
            import mcp  # noqa: F401
        except ImportError:
            LOGGER.warning("MCP: 'mcp' package is not installed; skipping MCP initialization")
            return

        for server_name, config in configs.items():
            connection = MCPServerConnection(server_name, config)
            try:
                await connection.connect()
            except Exception as exc:
                LOGGER.warning("MCP: server '%s' failed to connect: %s", server_name, exc)
                continue
            self._connections[server_name] = connection
            self._register_tools(connection)

    def _register_tools(self, connection: MCPServerConnection) -> None:
        from tools import register_external_tool

        spell_config = _normalize_spell_config(connection.config.get("spell_tools"))
        for tool_def in connection.tools:
            tool_name = getattr(tool_def, "name", None)
            if not tool_name:
                continue
            namespaced_name = f"{connection.server_name}__{tool_name}"
            schema = _tool_schema_from_mcp(
                namespaced_name,
                connection.server_name,
                tool_name,
                tool_def,
                spell_config,
            )
            wrapper = _make_mcp_tool_wrapper(connection, tool_name)
            if register_external_tool(namespaced_name, schema, wrapper):
                self._registered_tools[namespaced_name] = {
                    "server_name": connection.server_name,
                    "tool_name": tool_name,
                    "description": schema.description,
                    "spell": schema.spell,
                    "spell_display_name": schema.spell_display_name,
                    "source_path": connection.config.get("_source_path"),
                }

    async def shutdown_all(self) -> None:
        from tools import unregister_external_tool

        for name in list(self._registered_tools):
            unregister_external_tool(name)
            self._registered_tools.pop(name, None)

        for server_name, connection in list(self._connections.items()):
            try:
                await connection.disconnect()
            except Exception as exc:
                LOGGER.debug("MCP: failed to disconnect '%s': %s", server_name, exc)
        self._connections.clear()

    async def reconnect_server(self, server_name: str) -> bool:
        connection = self._connections.get(server_name)
        if connection is None:
            return False

        await self._unregister_server_tools(server_name)
        try:
            await connection.disconnect()
            await connection.connect()
            self._register_tools(connection)
        except Exception as exc:
            LOGGER.warning("MCP: reconnect failed for '%s': %s", server_name, exc)
            return False
        return True

    async def _unregister_server_tools(self, server_name: str) -> None:
        from tools import unregister_external_tool

        for tool_name, meta in list(self._registered_tools.items()):
            if meta.get("server_name") != server_name:
                continue
            unregister_external_tool(tool_name)
            self._registered_tools.pop(tool_name, None)

    def get_registered_tool_names(self) -> List[str]:
        return list(self._registered_tools.keys())

    def get_registered_tool_info(self) -> List[Dict[str, Any]]:
        info: List[Dict[str, Any]] = []
        for namespaced_name, meta in sorted(self._registered_tools.items()):
            info.append({
                "name": namespaced_name,
                "server_name": meta.get("server_name"),
                "tool_name": meta.get("tool_name"),
                "description": meta.get("description"),
                "spell": bool(meta.get("spell")),
                "spell_display_name": meta.get("spell_display_name") or "",
                "source_path": meta.get("source_path"),
            })
        return info

    def get_server_status(self) -> List[Dict[str, Any]]:
        status: List[Dict[str, Any]] = []
        for server_name, connection in sorted(self._connections.items()):
            tool_infos = [
                meta for meta in self.get_registered_tool_info()
                if meta["server_name"] == server_name
            ]
            status.append({
                "name": server_name,
                "transport": connection.transport_type,
                "connected": connection.connected,
                "tool_count": len(connection.tools),
                "tools": [getattr(tool, "name", "?") for tool in connection.tools],
                "spell_tools": [info["tool_name"] for info in tool_infos if info["spell"]],
                "source_path": connection.config.get("_source_path"),
            })
        return status


def _make_mcp_tool_wrapper(connection: MCPServerConnection, tool_name: str):
    async def _mcp_tool_wrapper(**kwargs: Any) -> str:
        LOGGER.info("MCP tool call: %s__%s args=%s", connection.server_name, tool_name, kwargs)
        result = await run_on_mcp_loop(connection.call_tool(tool_name, kwargs))
        preview = result[:200] + "..." if len(result) > 200 else result
        LOGGER.info("MCP tool result: %s__%s -> %s", connection.server_name, tool_name, preview)
        return result

    _mcp_tool_wrapper.__name__ = f"{connection.server_name}__{tool_name}"
    _mcp_tool_wrapper.__qualname__ = f"mcp.{connection.server_name}.{tool_name}"
    return _mcp_tool_wrapper


async def maybe_await_tool_result(tool_func: Any, *args: Any, **kwargs: Any) -> Any:
    """Execute a tool and await its result when needed."""
    if tool_func is None or not callable(tool_func):
        return None

    if inspect.iscoroutinefunction(tool_func):
        return await tool_func(*args, **kwargs)

    result = tool_func(*args, **kwargs)
    if inspect.isawaitable(result):
        return await result
    return result


_manager: Optional[MCPClientManager] = None


def get_mcp_manager() -> Optional[MCPClientManager]:
    return _manager


def _ensure_loop_thread() -> asyncio.AbstractEventLoop:
    global _loop, _loop_thread
    if _loop is not None and _loop_thread is not None and _loop_thread.is_alive():
        return _loop

    ready = threading.Event()
    holder: Dict[str, asyncio.AbstractEventLoop] = {}

    def _runner() -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        holder["loop"] = loop
        ready.set()
        loop.run_forever()
        pending = asyncio.all_tasks(loop)
        for task in pending:
            task.cancel()
        if pending:
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        loop.close()

    thread = threading.Thread(target=_runner, name="SAIVerse-MCP", daemon=True)
    thread.start()
    ready.wait()
    _loop = holder["loop"]
    _loop_thread = thread
    return _loop


async def run_on_mcp_loop(coro: Any) -> Any:
    """Await a coroutine on the dedicated MCP event loop."""
    if _loop is None:
        return await coro
    current_loop = asyncio.get_running_loop()
    if current_loop is _loop:
        return await coro
    future = asyncio.run_coroutine_threadsafe(coro, _loop)
    return await asyncio.wrap_future(future)


async def initialize_mcp() -> Optional[MCPClientManager]:
    global _manager
    if _manager is not None:
        return _manager

    manager = MCPClientManager()
    await manager.start_all()
    if not manager.get_registered_tool_names():
        return None

    _manager = manager
    return _manager


async def shutdown_mcp() -> None:
    global _manager
    if _manager is None:
        return
    await _manager.shutdown_all()
    _manager = None


async def reconnect_mcp_server(server_name: str) -> bool:
    manager = get_mcp_manager()
    if manager is None:
        return False
    return await run_on_mcp_loop(manager.reconnect_server(server_name))


def initialize_mcp_sync() -> Optional[MCPClientManager]:
    loop = _ensure_loop_thread()
    future = asyncio.run_coroutine_threadsafe(initialize_mcp(), loop)
    return future.result()


def shutdown_mcp_sync() -> None:
    global _loop, _loop_thread
    if _loop is None:
        return
    future = asyncio.run_coroutine_threadsafe(shutdown_mcp(), _loop)
    future.result()
    _loop.call_soon_threadsafe(_loop.stop)
    if _loop_thread is not None:
        _loop_thread.join(timeout=5)
    _loop = None
    _loop_thread = None
