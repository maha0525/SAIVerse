"""MCP status, reconnect, and instance control API.

Extended for instance_key-based management (see
``docs/intent/mcp_addon_integration.md``): endpoints now expose individual
MCP server instances (potentially multiple per qualified_server_name when
scope=per_persona), and support manual stop / retry in addition to
the existing reconnect-by-name.
"""
from __future__ import annotations

from typing import Any, Dict, List

from fastapi import APIRouter, Query

router = APIRouter()


@router.get("/servers")
def list_servers() -> List[Dict[str, Any]]:
    """Return status of every known server instance.

    Each entry includes ``instance_key``, ``qualified_server_name``,
    ``scope``, ``persona_id`` (for per_persona instances), refcount and
    referrers, plus connection/tool info. Configured-but-not-yet-started
    per_persona servers are also included (with ``instance_key: null``).
    """
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


@router.get("/failures")
def list_failures() -> List[Dict[str, Any]]:
    """Return all instances currently in backoff after a startup failure."""
    from tools.mcp_client import get_mcp_manager

    manager = get_mcp_manager()
    if manager is None:
        return []
    return manager.get_failed_instances()


@router.post("/servers/{server_name}/reconnect")
async def reconnect_server(server_name: str) -> Dict[str, Any]:
    """Reconnect all instances of the given qualified server name."""
    from tools.mcp_client import get_mcp_manager, reconnect_mcp_server

    manager = get_mcp_manager()
    if manager is None:
        return {"success": False, "error": "MCP is not initialized"}

    success = await reconnect_mcp_server(server_name)
    if success:
        return {"success": True, "server_name": server_name}
    return {"success": False, "error": f"Failed to reconnect '{server_name}'"}


@router.post("/instances/stop")
async def stop_instance(
    instance_key: str = Query(..., description="Full instance_key to stop"),
) -> Dict[str, Any]:
    """Force-stop a specific instance, ignoring refcount.

    The instance may be restarted on the next tool call (per_persona
    scope) or remain stopped until a referrer is re-added (global scope).
    """
    from tools.mcp_client import get_mcp_manager, run_on_mcp_loop

    manager = get_mcp_manager()
    if manager is None:
        return {"success": False, "error": "MCP is not initialized"}

    success = await run_on_mcp_loop(manager.manual_stop_instance(instance_key))
    if success:
        return {"success": True, "instance_key": instance_key}
    return {
        "success": False,
        "error": f"No active instance for '{instance_key}'",
    }


@router.post("/instances/retry")
async def retry_failed_instance(
    instance_key: str = Query(..., description="Full instance_key to retry"),
) -> Dict[str, Any]:
    """Force-retry an instance that is currently in backoff.

    Clears the failure record so the next tool call (per_persona) or
    refcount operation (global) can attempt a fresh start immediately.
    """
    from tools.mcp_client import get_mcp_manager

    manager = get_mcp_manager()
    if manager is None:
        return {"success": False, "error": "MCP is not initialized"}

    if instance_key not in manager._failed_instances:
        return {
            "success": False,
            "error": f"No failure record for '{instance_key}'",
        }
    manager._clear_failure(instance_key)
    return {"success": True, "instance_key": instance_key}
