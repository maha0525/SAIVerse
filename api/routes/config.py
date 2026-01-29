import os
import json
from pathlib import Path
from typing import List, Dict, Any, Optional
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from api.deps import get_manager
from model_configs import (
    get_model_choices_with_display_names,
    get_model_parameters,
    get_model_parameter_defaults,
)

router = APIRouter()

class ModelInfo(BaseModel):
    id: str
    name: str

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

@router.get("/models", response_model=List[ModelInfo])
def get_models():
    """List available LLM models."""
    choices = get_model_choices_with_display_names()
    return [{"id": mid, "name": name} for mid, name in choices]

@router.get("/playbooks", response_model=List[PlaybookInfo])
def get_playbooks():
    """List available user-selectable playbooks with input_schema."""
    from database.session import SessionLocal
    from database.models import Playbook

    db = SessionLocal()
    try:
        playbooks = db.query(Playbook).filter(Playbook.user_selectable == True).all()
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
                pass

            result.append({
                "id": pb.name,
                "name": pb.name,
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
            raw_input_schema = []

        # Build context for enum resolution
        # Get current user's context from manager
        context = EnumResolverContext(
            city_id=getattr(manager, 'city_id', None),
            building_id=getattr(manager.state, 'current_building_id', None) if hasattr(manager, 'state') else None,
            persona_id=getattr(manager.state, 'current_persona_id', None) if hasattr(manager, 'state') else None,
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
                except ValueError as e:
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
    current_model = manager.model if manager.model != "None" else None
    
    # If no model selected, return empty
    if not current_model:
        return {
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

    return {
        "current_model": current_model,
        "parameters": specs,
        "current_values": current_values
    }

@router.post("/model")
def set_model(req: UpdateModelRequest, manager = Depends(get_manager)):
    """Set the global model override and return updated config."""
    manager.set_model(req.model, req.parameters)
    
    # Return full config inline to avoid a separate /config fetch
    current_model = manager.model if manager.model != "None" else None
    
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

    return {
        "success": True,
        "model": req.model,
        "current_model": current_model,
        "parameters": specs,
        "current_values": current_values
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
