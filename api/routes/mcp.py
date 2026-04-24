"""MCP status and reconnect API."""
from __future__ import annotations

from typing import Any, Dict, List

from fastapi import APIRouter

router = APIRouter()


@router.get("/servers")
def list_servers() -> List[Dict[str, Any]]:
    from tools.mcp_client import get_mcp_manager

    manager = get_mcp_manager()
    if manager is None:
        return []
    return manager.get_server_status()


@router.get("/tools")
def list_tools() -> List[Dict[str, Any]]:
    from tools.mcp_client import get_mcp_manager

    manager = get_mcp_manager()
    if manager is None:
        return []
    return manager.get_registered_tool_info()


@router.post("/servers/{server_name}/reconnect")
async def reconnect_server(server_name: str) -> Dict[str, Any]:
    from tools.mcp_client import get_mcp_manager, reconnect_mcp_server

    manager = get_mcp_manager()
    if manager is None:
        return {"success": False, "error": "MCP is not initialized"}

    success = await reconnect_mcp_server(server_name)
    if success:
        return {"success": True, "server_name": server_name}
    return {"success": False, "error": f"Failed to reconnect '{server_name}'"}
