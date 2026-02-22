import logging
import os
import json
from pathlib import Path

_log = logging.getLogger(__name__)
from typing import List, Dict, Any, Optional
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from api.deps import get_manager
from saiverse.model_configs import (
    get_model_choices_with_display_names,
    get_model_config,
    get_model_parameters,
    get_model_parameter_defaults,
    get_cache_config,
    is_model_available,
)

router = APIRouter()

class ModelInfo(BaseModel):
    id: str
    name: str
    input_price: Optional[float] = None   # USD per 1M input tokens
    output_price: Optional[float] = None  # USD per 1M output tokens

class PlaybookParamInfo(BaseModel):
    """Parameter info for playbook input_schema."""
    name: str
    description: str
    param_type: str = "string"
    required: bool = True
    default: Optional[Any] = None
    enum_values: Optional[List[str]] = None
    enum_source: Optional[str] = None
    user_configurable: bool = False
    ui_widget: Optional[str] = None


class PlaybookParamResolved(PlaybookParamInfo):
    """Parameter info with resolved enum options."""
    resolved_options: Optional[List[Dict[str, Any]]] = None


class PlaybookInfo(BaseModel):
    id: str
    name: str
    description: Optional[str] = None
    input_schema: Optional[List[PlaybookParamInfo]] = None

class UpdateModelRequest(BaseModel):
    model: str
    parameters: Optional[Dict[str, Any]] = None

class UpdateParametersRequest(BaseModel):
    parameters: Dict[str, Any]

class ParameterSpec(BaseModel):
    label: str
    type: str # 'slider', 'number', 'dropdown'
    default: Any
    min: Optional[float] = None
    max: Optional[float] = None
    step: Optional[float] = None
    options: Optional[List[str]] = None
    description: Optional[str] = None

class ModelConfigResponse(BaseModel):
    current_model: Optional[str]
    parameters: Dict[str, ParameterSpec]
    current_values: Dict[str, Any]
    max_history_messages: Optional[int] = None
    max_history_messages_model_default: Optional[int] = None
    metabolism_enabled: bool = True
    metabolism_keep_messages: Optional[int] = None
    metabolism_keep_messages_model_default: Optional[int] = None

@router.get("/models", response_model=List[ModelInfo])
def get_models():
    """List available LLM models.

    Only models whose required API key is configured are returned.
    Includes pricing info (USD per 1M tokens) when available.
    """
    choices = get_model_choices_with_display_names()
    result = []
    for mid, name in choices:
        if not is_model_available(mid):
            continue
        pricing = get_model_config(mid).get("pricing", {})
        result.append({
            "id": mid,
            "name": name,
            "input_price": pricing.get("input_per_1m_tokens"),
            "output_price": pricing.get("output_per_1m_tokens"),
        })
    return result


@router.post("/reload-models")
def reload_models():
    """Reload model configurations from disk without restarting the server."""
    from saiverse.model_configs import reload_configs

    reload_configs()
    choices = get_model_choices_with_display_names()
    return {
        "reloaded": len(choices),
        "models": [{"id": mid, "name": name} for mid, name in choices],
    }

@router.get("/playbooks", response_model=List[PlaybookInfo])
def get_playbooks(manager=Depends(get_manager)):
    """List available user-selectable playbooks with input_schema."""
    from database.session import SessionLocal
    from database.models import Playbook

    developer_mode = manager.state.developer_mode
    db = SessionLocal()
    try:
        query = db.query(Playbook).filter(Playbook.user_selectable == True)
        if not developer_mode:
            query = query.filter(Playbook.dev_only == False)
        playbooks = query.all()
        result = []
        for pb in playbooks:
            # Parse schema_json to get input_schema
            input_schema = None
            try:
                schema = json.loads(pb.schema_json) if pb.schema_json else {}
                raw_input_schema = schema.get("input_schema", [])
                # Filter to user_configurable params only for UI
                input_schema = [
                    {
                        "name": p.get("name"),
                        "description": p.get("description", ""),
                        "param_type": p.get("param_type", "string"),
                        "required": p.get("required", True),
                        "default": p.get("default"),
                        "enum_values": p.get("enum_values"),
                        "enum_source": p.get("enum_source"),
                        "user_configurable": p.get("user_configurable", False),
                        "ui_widget": p.get("ui_widget"),
                    }
                    for p in raw_input_schema
                    if p.get("user_configurable", False)
                ]
            except (json.JSONDecodeError, TypeError):
                _log.warning("Failed to parse schema_json for playbook %s", pb.name, exc_info=True)

            result.append({
                "id": pb.name,
                "name": pb.display_name or pb.name,
                "description": pb.description,
                "input_schema": input_schema if input_schema else None,
            })
        return result
    finally:
        db.close()


class PlaybookParamsResponse(BaseModel):
    """Response for playbook params endpoint with resolved enum options."""
    name: str
    params: List[PlaybookParamResolved]


@router.get("/playbooks/{name}/params", response_model=PlaybookParamsResponse)
def get_playbook_params(name: str, manager=Depends(get_manager)):
    """
    Get playbook parameters with resolved enum options.

    For params with enum_source, resolves to actual options based on current context.
    """
    from database.session import SessionLocal
    from database.models import Playbook
    from api.utils.enum_resolver import resolve_enum_source, EnumResolverContext

    db = SessionLocal()
    try:
        playbook = db.query(Playbook).filter(Playbook.name == name).first()
        if not playbook:
            raise HTTPException(status_code=404, detail=f"Playbook '{name}' not found")

        # Parse schema_json
        try:
            schema = json.loads(playbook.schema_json) if playbook.schema_json else {}
            raw_input_schema = schema.get("input_schema", [])
        except (json.JSONDecodeError, TypeError):
            _log.warning("Failed to parse schema_json for playbook %s", name, exc_info=True)
            raw_input_schema = []

        # Build context for enum resolution
        # Get current user's context from manager
        context = EnumResolverContext(
            city_id=getattr(manager, 'city_id', None),
            building_id=getattr(manager.state, 'current_building_id', None) if hasattr(manager, 'state') else None,
            persona_id=getattr(manager.state, 'current_persona_id', None) if hasattr(manager, 'state') else None,
            developer_mode=getattr(manager.state, 'developer_mode', False) if hasattr(manager, 'state') else False,
        )

        # Filter to user_configurable and resolve enum_source
        params = []
        for p in raw_input_schema:
            if not p.get("user_configurable", False):
                continue

            param_info = {
                "name": p.get("name"),
                "description": p.get("description", ""),
                "param_type": p.get("param_type", "string"),
                "required": p.get("required", True),
                "default": p.get("default"),
                "enum_values": p.get("enum_values"),
                "enum_source": p.get("enum_source"),
                "user_configurable": True,
                "ui_widget": p.get("ui_widget"),
                "resolved_options": None,
            }

            # Resolve enum_source if present
            enum_source = p.get("enum_source")
            if enum_source:
                try:
                    param_info["resolved_options"] = resolve_enum_source(enum_source, context)
                except ValueError:
                    # Log but don't fail - just return empty options
                    param_info["resolved_options"] = []

            # If static enum_values, convert to resolved_options format
            elif p.get("enum_values"):
                param_info["resolved_options"] = [
                    {"value": v, "label": v} for v in p.get("enum_values", [])
                ]

            params.append(param_info)

        return {"name": name, "params": params}
    finally:
        db.close()


@router.get("/config", response_model=ModelConfigResponse)
def get_current_config(manager = Depends(get_manager)):
    """Get current model and parameter configuration."""
    current_model = manager.model or None
    
    # If no model selected, return empty (but still include overrides)
    if not current_model:
        override = getattr(manager, "max_history_messages_override", None)
        return {
            "current_model": None,
            "parameters": {},
            "current_values": {},
            "max_history_messages": override,
            "max_history_messages_model_default": None,
            "metabolism_enabled": getattr(manager, "metabolism_enabled", True),
            "metabolism_keep_messages": getattr(manager, "metabolism_keep_messages_override", None),
            "metabolism_keep_messages_model_default": None,
        }

    # Get param specs
    raw_specs = get_model_parameters(current_model)
    specs = {}
    
    for key, val in raw_specs.items():
        if not isinstance(val, dict):
            continue
        # Check client support (simplified check)
        scopes = val.get("client_support")
        if scopes and "chat" not in (scopes if isinstance(scopes, list) else [scopes]):
            continue
            
        spec_type = "text"
        if "options" in val:
            spec_type = "dropdown"
        elif "min" in val and "max" in val:
            spec_type = "slider"
        elif val.get("type", "") in ["int", "float"]:
            spec_type = "number"
            
        specs[key] = {
            "label": val.get("label", key),
            "type": spec_type,
            "default": val.get("default"),
            "min": val.get("min"),
            "max": val.get("max"),
            "step": val.get("step"),
            "options": val.get("options"),
            "description": val.get("description")
        }

    # Get current values
    current_values = dict(get_model_parameter_defaults(current_model))
    if manager.model_parameter_overrides:
        current_values.update(manager.model_parameter_overrides)

    # Max history messages
    from saiverse.model_configs import get_default_max_history_messages, get_metabolism_keep_messages
    override = getattr(manager, "max_history_messages_override", None)
    model_default = get_default_max_history_messages(current_model)

    # Metabolism settings
    metab_override = getattr(manager, "metabolism_keep_messages_override", None)
    metab_model_default = get_metabolism_keep_messages(current_model)

    return {
        "current_model": current_model,
        "parameters": specs,
        "current_values": current_values,
        "max_history_messages": override if override is not None else model_default,
        "max_history_messages_model_default": model_default,
        "metabolism_enabled": getattr(manager, "metabolism_enabled", True),
        "metabolism_keep_messages": metab_override if metab_override is not None else metab_model_default,
        "metabolism_keep_messages_model_default": metab_model_default,
    }

@router.post("/model")
def set_model(req: UpdateModelRequest, manager = Depends(get_manager)):
    """Set the global model override and return updated config."""
    manager.set_model(req.model, req.parameters)
    
    # Return full config inline to avoid a separate /config fetch
    current_model = manager.model or None
    
    if not current_model:
        return {
            "success": True,
            "model": req.model,
            "current_model": None,
            "parameters": {},
            "current_values": {}
        }
    
    # Get param specs
    raw_specs = get_model_parameters(current_model)
    specs = {}
    
    for key, val in raw_specs.items():
        if not isinstance(val, dict):
            continue
        scopes = val.get("client_support")
        if scopes and "chat" not in (scopes if isinstance(scopes, list) else [scopes]):
            continue
            
        spec_type = "text"
        if "options" in val:
            spec_type = "dropdown"
        elif "min" in val and "max" in val:
            spec_type = "slider"
        elif val.get("type", "") in ["int", "float"]:
            spec_type = "number"
            
        specs[key] = {
            "label": val.get("label", key),
            "type": spec_type,
            "default": val.get("default"),
            "min": val.get("min"),
            "max": val.get("max"),
            "step": val.get("step"),
            "options": val.get("options"),
            "description": val.get("description")
        }

    # Get current values
    current_values = dict(get_model_parameter_defaults(current_model))
    if manager.model_parameter_overrides:
        current_values.update(manager.model_parameter_overrides)

    # Max history messages (reset override on model change)
    from saiverse.model_configs import get_default_max_history_messages, get_metabolism_keep_messages
    manager.max_history_messages_override = None
    model_default = get_default_max_history_messages(current_model)

    # Metabolism (reset override on model change)
    manager.metabolism_keep_messages_override = None
    metab_model_default = get_metabolism_keep_messages(current_model)

    return {
        "success": True,
        "model": req.model,
        "current_model": current_model,
        "parameters": specs,
        "current_values": current_values,
        "max_history_messages": model_default,
        "max_history_messages_model_default": model_default,
        "metabolism_enabled": getattr(manager, "metabolism_enabled", True),
        "metabolism_keep_messages": metab_model_default,
        "metabolism_keep_messages_model_default": metab_model_default,
    }

@router.post("/parameters")
def set_parameters(req: UpdateParametersRequest, manager = Depends(get_manager)):
    """Update global model parameter overrides."""
    manager.set_model_parameters(req.parameters)
    return {"success": True}


class GlobalAutoRequest(BaseModel):
    enabled: bool


@router.get("/global-auto")
def get_global_auto(manager = Depends(get_manager)):
    """Get global autonomous mode status."""
    return {"enabled": manager.state.global_auto_enabled}


@router.post("/global-auto")
def set_global_auto(req: GlobalAutoRequest, manager = Depends(get_manager)):
    """Set global autonomous mode status."""
    manager.state.global_auto_enabled = req.enabled
    return {"success": True, "enabled": req.enabled}


class DeveloperModeRequest(BaseModel):
    enabled: bool


@router.get("/developer-mode")
def get_developer_mode(manager=Depends(get_manager)):
    """Get developer mode status."""
    return {"enabled": manager.state.developer_mode}


@router.post("/developer-mode")
def set_developer_mode(req: DeveloperModeRequest, manager=Depends(get_manager)):
    """Set developer mode status.

    When turning OFF, also disables global auto mode and
    sets all personas' interaction_mode to 'manual'.
    """
    manager.state.developer_mode = req.enabled

    if not req.enabled:
        # Disable global auto mode
        manager.state.global_auto_enabled = False

        # Set all personas to manual mode
        from database.session import SessionLocal
        from database.models import AI
        db = SessionLocal()
        try:
            db.query(AI).update({AI.INTERACTION_MODE: "manual"})
            db.commit()
        except Exception:
            _log.warning("Failed to reset interaction modes", exc_info=True)
            db.rollback()
        finally:
            db.close()

        # Update in-memory persona objects
        for persona in manager.state.personas.values():
            if hasattr(persona, "interaction_mode"):
                persona.interaction_mode = "manual"

    return {"success": True, "enabled": req.enabled}


class PlaybookOverrideRequest(BaseModel):
    playbook: Optional[str] = None
    playbook_params: Optional[Dict[str, Any]] = None


@router.get("/playbook")
def get_current_playbook(manager = Depends(get_manager)):
    """Get current playbook override and parameters."""
    return {
        "playbook": manager.state.current_playbook,
        "playbook_params": manager.state.playbook_params,
    }


@router.post("/playbook")
def set_playbook(req: PlaybookOverrideRequest, manager = Depends(get_manager)):
    """Set playbook override and parameters."""
    manager.state.current_playbook = req.playbook if req.playbook else None
    # Update playbook_params if provided, reset if playbook changed to None
    if req.playbook_params is not None:
        manager.state.playbook_params = req.playbook_params
    elif req.playbook is None:
        # Reset params when playbook is cleared
        manager.state.playbook_params = {}
    return {
        "success": True,
        "playbook": manager.state.current_playbook,
        "playbook_params": manager.state.playbook_params,
    }


class CacheConfigResponse(BaseModel):
    """Cache configuration response."""
    enabled: bool
    ttl: str
    supported: bool  # Whether current model supports explicit caching
    ttl_options: List[str]  # Available TTL options for current model
    cache_type: Optional[str] = None  # "explicit" or "implicit"


class CacheConfigRequest(BaseModel):
    """Cache configuration update request."""
    enabled: Optional[bool] = None
    ttl: Optional[str] = None


@router.get("/cache", response_model=CacheConfigResponse)
def get_cache_settings(manager = Depends(get_manager)):
    """Get current cache settings and model cache support info."""
    current_model = manager.model or None

    # Get cache config for current model
    cache_config = {}
    if current_model:
        cache_config = get_cache_config(current_model) or {}

    supported = cache_config.get("supported", False)
    cache_type = cache_config.get("type", None)
    ttl_options = cache_config.get("ttl_options", ["5m"])

    # Only show explicit cache controls for explicit caching models (Anthropic)
    if cache_type != "explicit":
        supported = False
        ttl_options = []

    return {
        "enabled": manager.state.cache_enabled,
        "ttl": manager.state.cache_ttl,
        "supported": supported,
        "ttl_options": ttl_options,
        "cache_type": cache_type,
    }


@router.post("/cache")
def set_cache_settings(req: CacheConfigRequest, manager = Depends(get_manager)):
    """Update cache settings."""
    if req.enabled is not None:
        manager.state.cache_enabled = req.enabled
    if req.ttl is not None:
        # Validate TTL value
        if req.ttl not in ["5m", "1h"]:
            raise HTTPException(status_code=400, detail="Invalid TTL value. Must be '5m' or '1h'")
        manager.state.cache_ttl = req.ttl

    return {
        "success": True,
        "enabled": manager.state.cache_enabled,
        "ttl": manager.state.cache_ttl,
    }


class MaxHistoryMessagesRequest(BaseModel):
    value: Optional[int] = None


@router.get("/max-history-messages")
def get_max_history_messages(manager=Depends(get_manager)):
    """Get current max history messages setting.

    Returns the session override if set, otherwise the model default.
    """
    from saiverse.model_configs import get_default_max_history_messages

    override = getattr(manager, "max_history_messages_override", None)
    current_model = manager.model or None

    model_default = None
    if current_model:
        model_default = get_default_max_history_messages(current_model)

    return {
        "value": override if override is not None else model_default,
        "override": override,
        "model_default": model_default,
    }


@router.post("/max-history-messages")
def set_max_history_messages(req: MaxHistoryMessagesRequest, manager=Depends(get_manager)):
    """Set session override for max history messages.

    Send {"value": null} to clear the override and use the model default.
    """
    if req.value is not None and req.value < 1:
        raise HTTPException(status_code=400, detail="value must be >= 1 or null")
    manager.max_history_messages_override = req.value
    return {"success": True, "value": req.value}


class MetabolismConfigRequest(BaseModel):
    enabled: Optional[bool] = None
    keep_messages: Optional[int] = None


@router.get("/metabolism")
def get_metabolism_settings(manager=Depends(get_manager)):
    """Get current metabolism settings."""
    from saiverse.model_configs import get_metabolism_keep_messages, get_default_max_history_messages

    current_model = manager.model or None
    metab_override = getattr(manager, "metabolism_keep_messages_override", None)
    metab_model_default = None
    high_wm = None
    if current_model:
        metab_model_default = get_metabolism_keep_messages(current_model)
        high_wm = get_default_max_history_messages(current_model)

    return {
        "enabled": getattr(manager, "metabolism_enabled", True),
        "keep_messages": metab_override if metab_override is not None else metab_model_default,
        "keep_messages_override": metab_override,
        "keep_messages_model_default": metab_model_default,
        "high_watermark": high_wm,
    }


@router.post("/metabolism")
def set_metabolism_settings(req: MetabolismConfigRequest, manager=Depends(get_manager)):
    """Set metabolism settings."""
    if req.enabled is not None:
        manager.metabolism_enabled = req.enabled

    if req.keep_messages is not None:
        if req.keep_messages < 1:
            raise HTTPException(status_code=400, detail="keep_messages must be >= 1")
        # Validate: high_wm - keep_messages >= 20
        from saiverse.model_configs import get_default_max_history_messages
        current_model = manager.model or None
        high_wm_override = getattr(manager, "max_history_messages_override", None)
        high_wm = high_wm_override
        if high_wm is None and current_model:
            high_wm = get_default_max_history_messages(current_model)
        if high_wm is not None and high_wm - req.keep_messages < 20:
            raise HTTPException(
                status_code=400,
                detail=f"keep_messages must be at most {high_wm - 20} (high watermark {high_wm} - 20)",
            )
        manager.metabolism_keep_messages_override = req.keep_messages
    elif req.keep_messages is None and req.enabled is None:
        # Clear override
        manager.metabolism_keep_messages_override = None

    return {
        "success": True,
        "enabled": getattr(manager, "metabolism_enabled", True),
        "keep_messages": getattr(manager, "metabolism_keep_messages_override", None),
    }


@router.get("/startup-warnings")
def get_startup_warnings(manager=Depends(get_manager)):
    """Return warnings collected during startup (e.g. failed persona loads)."""
    warnings = getattr(manager, "startup_warnings", [])
    return {"warnings": warnings}


@router.get("/reembed-check")
def check_reembed_needed(manager=Depends(get_manager)):
    """Return list of personas that need re-embedding due to model changes."""
    warnings = getattr(manager, "startup_warnings", [])
    for w in warnings:
        if isinstance(w, dict) and w.get("source") == "embed_model_mismatch":
            return {
                "needed": True,
                "persona_ids": w.get("persona_ids", []),
                "message": w.get("message", ""),
            }
    return {"needed": False, "persona_ids": [], "message": ""}


# ── Playbook permissions CRUD ─────────────────────────────────────

class PlaybookPermissionInfo(BaseModel):
    playbook_name: str
    display_name: str
    description: str
    permission_level: str  # blocked | user_only | ask_every_time | auto_allow


class SetPlaybookPermissionRequest(BaseModel):
    playbook_name: str
    permission_level: str  # user_only | ask_every_time | auto_allow  (blocked not settable from UI)


@router.get("/playbook-permissions")
def get_playbook_permissions(manager=Depends(get_manager)):
    """Return all router_callable playbooks with their current permission level for this city."""
    from database.models import Playbook as PlaybookModel, PlaybookPermission

    city_id = getattr(manager, "city_id", None)
    if city_id is None:
        raise HTTPException(status_code=500, detail="City ID not available")

    db = manager.SessionLocal()
    try:
        playbooks = db.query(PlaybookModel).filter(PlaybookModel.router_callable == True).all()

        permissions: dict[str, str] = {}
        perm_rows = (
            db.query(PlaybookPermission)
            .filter(PlaybookPermission.CITYID == city_id)
            .all()
        )
        permissions = {r.playbook_name: r.permission_level for r in perm_rows}

        result = []
        for pb in playbooks:
            result.append(PlaybookPermissionInfo(
                playbook_name=pb.name,
                display_name=pb.display_name or pb.name,
                description=pb.description or "",
                permission_level=permissions.get(pb.name, "ask_every_time"),
            ))

        result.sort(key=lambda x: x.playbook_name)
        return result
    finally:
        db.close()


@router.post("/playbook-permissions")
def set_playbook_permission(req: SetPlaybookPermissionRequest, manager=Depends(get_manager)):
    """Set the permission level for a playbook in this city."""
    from database.models import PlaybookPermission

    valid_levels = ("user_only", "ask_every_time", "auto_allow")
    if req.permission_level not in valid_levels:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid permission level. Must be one of: {valid_levels}",
        )

    city_id = getattr(manager, "city_id", None)
    if city_id is None:
        raise HTTPException(status_code=500, detail="City ID not available")

    db = manager.SessionLocal()
    try:
        row = (
            db.query(PlaybookPermission)
            .filter(
                PlaybookPermission.CITYID == city_id,
                PlaybookPermission.playbook_name == req.playbook_name,
            )
            .first()
        )
        if row:
            row.permission_level = req.permission_level
        else:
            db.add(PlaybookPermission(
                CITYID=city_id,
                playbook_name=req.playbook_name,
                permission_level=req.permission_level,
            ))
        db.commit()
        return {"success": True, "playbook_name": req.playbook_name, "permission_level": req.permission_level}
    finally:
        db.close()
