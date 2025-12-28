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

class PlaybookInfo(BaseModel):
    id: str
    name: str

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
    """List available playbooks from sea/playbooks/public."""
    playbooks_dir = Path("sea/playbooks/public")
    if not playbooks_dir.exists():
        return []
    
    playbooks = []
    for f in playbooks_dir.glob("*.json"):
        # Use filename stem as ID (e.g. 'meta_user')
        playbook_id = f.stem
        # Try to read name from JSON, fallback to ID
        try:
            content = json.loads(f.read_text(encoding="utf-8"))
            if not content.get("user_selectable", False):
                continue
            name = content.get("name", playbook_id)
        except Exception:
            # If JSON invalid or read fails, skip or fallback
            # Here we skip to be safe if we can't verify selectable
            continue
        
        playbooks.append({"id": playbook_id, "name": name})
    
    # Sort by name
    playbooks.sort(key=lambda x: x["name"])
    return playbooks

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

