"""MCP status, reconnect, and instance control API.

Extended for instance_key-based management (see
``docs/intent/mcp_addon_integration.md``): endpoints now expose individual
MCP server instances (potentially multiple per qualified_server_name when
scope=per_persona), and support manual stop / retry in addition to
the existing reconnect-by-name.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Query
from pydantic import BaseModel

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


class ToolCallRequest(BaseModel):
    server: str
    tool_name: str
    arguments: Dict[str, Any] = {}
    persona_id: Optional[str] = None


@router.post("/tool-call")
async def call_mcp_tool(body: ToolCallRequest) -> Dict[str, Any]:
    """Invoke an MCP tool directly (admin / debug).

    Bypasses the persona-spell pipeline: looks up the running MCP server
    instance by qualified server name, forwards the tool call, and returns
    the raw result (parsed as JSON when the server returns a JSON string,
    otherwise the string as-is).

    Use for verifying tools that are not exposed as spells (e.g.
    ``visible: false`` admin / diagnostic tools like
    ``stackchan / gpio_test``, ``stackchan / self.i2c.scan``) without
    routing through a persona.

    Body:
        server: qualified MCP server name (e.g. ``stackchan``).
        tool_name: MCP tool name as registered by the server
            (e.g. ``self.i2c.scan``).
        arguments: tool argument object. Default: ``{}``.
        persona_id: required only for ``per_persona``-scope servers;
            omit for global-scope servers.

    Response (success):
        ``{"ok": true, "result": <parsed JSON or string>}``
    Response (failure):
        ``{"ok": false, "error": "..."}``
    """
    from tools.mcp_client import (
        _make_instance_key,
        get_mcp_manager,
        run_on_mcp_loop,
    )

    manager = get_mcp_manager()
    if manager is None:
        return {"ok": False, "error": "MCP is not initialized"}

    instance_key = _make_instance_key(body.server, body.persona_id)
    connection = manager._connections.get(instance_key)
    if connection is None:
        return {
            "ok": False,
            "error": (
                f"No active MCP server '{body.server}' "
                f"(instance_key={instance_key}). "
                f"Use GET /api/mcp/servers to list available instances."
            ),
        }

    try:
        raw = await run_on_mcp_loop(connection.call_tool(body.tool_name, body.arguments))
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    # MCP responses are strings; try to surface as JSON when possible so
    # downstream curl / scripts get structured fields directly.
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else raw
        return {"ok": True, "result": parsed}
    except (json.JSONDecodeError, TypeError):
        return {"ok": True, "result": raw}
