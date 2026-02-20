"""MCP (Model Context Protocol) status and management API endpoints."""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List

from fastapi import APIRouter

LOGGER = logging.getLogger(__name__)

router = APIRouter()


@router.get("/servers")
def list_servers() -> List[Dict[str, Any]]:
    """List all configured MCP servers with connection status."""
    from tools.mcp_client import get_mcp_manager

    mgr = get_mcp_manager()
    if mgr is None:
        return []
    return mgr.get_server_status()


@router.get("/tools")
def list_mcp_tools() -> List[Dict[str, str]]:
    """List all registered MCP tools."""
    from tools.mcp_client import get_mcp_manager

    mgr = get_mcp_manager()
    if mgr is None:
        return []

    from tools import TOOL_SCHEMAS
    mcp_tools = []
    registered_names = set(mgr.get_registered_tool_names())
    for schema in TOOL_SCHEMAS:
        if schema.name in registered_names:
            mcp_tools.append({
                "name": schema.name,
                "description": schema.description,
                "parameters": schema.parameters,
            })
    return mcp_tools


@router.post("/servers/{server_name}/reconnect")
async def reconnect_server(server_name: str) -> Dict[str, Any]:
    """Reconnect a specific MCP server."""
    from tools.mcp_client import get_mcp_manager

    mgr = get_mcp_manager()
    if mgr is None:
        return {"success": False, "error": "MCP not initialized"}

    success = await mgr.reconnect_server(server_name)
    if success:
        return {"success": True, "message": f"Server '{server_name}' reconnected"}
    return {"success": False, "error": f"Failed to reconnect server '{server_name}'"}
