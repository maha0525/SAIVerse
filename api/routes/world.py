from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form
from pydantic import BaseModel
from typing import List, Optional, Any
import shutil
from pathlib import Path

from api.deps import get_manager
from saiverse_manager import SAIVerseManager

router = APIRouter()

# --- Pydantic Models ---

class CityCreate(BaseModel):
    name: str
    description: str
    ui_port: int
    api_port: int
    timezone: str

class CityUpdate(BaseModel):
    name: str
    description: str
    online_mode: bool
    ui_port: int
    api_port: int
    timezone: str
    host_avatar_path: Optional[str] = None

class BuildingCreate(BaseModel):
    name: str
    description: str
    capacity: int
    system_instruction: str
    city_id: int

class BuildingUpdate(BaseModel):
    name: str
    description: str
    capacity: int
    system_instruction: str
    city_id: int
    tool_ids: List[int]
    auto_interval: int

class AICreate(BaseModel):
    name: str
    system_prompt: str
    home_city_id: int

class AIUpdate(BaseModel):
    name: str
    description: str
    system_prompt: str
    home_city_id: int
    default_model: Optional[str]
    lightweight_model: Optional[str]
    interaction_mode: str
    avatar_path: Optional[str]

class AIMove(BaseModel):
    target_building_name: str

class BlueprintCreate(BaseModel):
    name: str
    description: str
    city_id: int
    system_prompt: str
    entity_type: str

class BlueprintSpawn(BaseModel):
    entity_name: str
    building_name: str

class ToolCreate(BaseModel):
    name: str
    description: str
    module_path: str
    function_name: str

class ItemCreate(BaseModel):
    name: str
    item_type: str
    description: str
    owner_kind: str
    owner_id: Optional[str]
    state_json: str

class ItemUpdate(BaseModel):
    name: str
    item_type: str
    description: str
    owner_kind: str
    owner_id: Optional[str]
    state_json: str
    file_path: str

# --- Routes ---

# City
@router.post("/cities")
def create_city(city: CityCreate, manager: SAIVerseManager = Depends(get_manager)):
    return manager.create_city(city.name, city.description, city.ui_port, city.api_port, city.timezone)

@router.put("/cities/{city_id}")
def update_city(city_id: int, city: CityUpdate, manager: SAIVerseManager = Depends(get_manager)):
    # Note: Avatar upload is handled separately or client sends path
    return manager.update_city(city_id, city.name, city.description, city.online_mode, city.ui_port, city.api_port, city.timezone, city.host_avatar_path, None)

@router.delete("/cities/{city_id}")
def delete_city(city_id: int, manager: SAIVerseManager = Depends(get_manager)):
    return manager.delete_city(city_id)

# Building
@router.post("/buildings")
def create_building(b: BuildingCreate, manager: SAIVerseManager = Depends(get_manager)):
    return manager.create_building(b.name, b.description, b.capacity, b.system_instruction, b.city_id)

@router.put("/buildings/{building_id}")
def update_building(building_id: str, b: BuildingUpdate, manager: SAIVerseManager = Depends(get_manager)):
    return manager.update_building(building_id, b.name, b.capacity, b.description, b.system_instruction, b.city_id, b.tool_ids, b.auto_interval)

@router.delete("/buildings/{building_id}")
def delete_building(building_id: str, manager: SAIVerseManager = Depends(get_manager)):
    return manager.delete_building(building_id)

# AI
@router.post("/ais")
def create_ai(ai: AICreate, manager: SAIVerseManager = Depends(get_manager)):
    return manager.create_ai(ai.name, ai.system_prompt, ai.home_city_id)

@router.put("/ais/{ai_id}")
def update_ai(ai_id: str, ai: AIUpdate, manager: SAIVerseManager = Depends(get_manager)):
    return manager.update_ai(ai_id, ai.name, ai.description, ai.system_prompt, ai.home_city_id, ai.default_model, ai.lightweight_model, ai.interaction_mode, ai.avatar_path, None)

@router.delete("/ais/{ai_id}")
def delete_ai(ai_id: str, manager: SAIVerseManager = Depends(get_manager)):
    return manager.delete_ai(ai_id)

@router.post("/ais/{ai_id}/move")
def move_ai(ai_id: str, move: AIMove, manager: SAIVerseManager = Depends(get_manager)):
    # Resolve building ID from name if necessary, or client passes ID? 
    # Logic in ui/world_editor.py uses names. But manager method `move_ai_from_editor` takes ID.
    # UI helper `move_ai_ui` converts name to ID.
    # Let's rely on client passing generic building lookup or we look it up here.
    # Ideally client passes ID. But legacy UI passed name.
    # Let's assume we can look it up in manager.building_map if we iterate?
    # Or client should pass ID. Let's stick to name for compat if needed, or find ID.
    target_id = None
    for b in manager.buildings:
        if b.name == move.target_building_name:
            target_id = b.building_id
            break
    
    # Fallback: maybe client passed ID as name?
    if not target_id:
        target_id = move.target_building_name

    return manager.move_ai_from_editor(ai_id, target_id)

# Blueprint
@router.post("/blueprints")
def create_blueprint(bp: BlueprintCreate, manager: SAIVerseManager = Depends(get_manager)):
    return manager.create_blueprint(bp.name, bp.description, bp.city_id, bp.system_prompt, bp.entity_type)

@router.put("/blueprints/{bp_id}")
def update_blueprint(bp_id: int, bp: BlueprintCreate, manager: SAIVerseManager = Depends(get_manager)):
    return manager.update_blueprint(bp_id, bp.name, bp.description, bp.city_id, bp.system_prompt, bp.entity_type)

@router.delete("/blueprints/{bp_id}")
def delete_blueprint(bp_id: int, manager: SAIVerseManager = Depends(get_manager)):
    return manager.delete_blueprint(bp_id)

@router.post("/blueprints/{bp_id}/spawn")
def spawn_blueprint(bp_id: int, spawn: BlueprintSpawn, manager: SAIVerseManager = Depends(get_manager)):
    success, msg = manager.spawn_entity_from_blueprint(bp_id, spawn.entity_name, spawn.building_name)
    if not success:
        raise HTTPException(status_code=400, detail=msg)
    return {"message": msg}

# Tool
@router.post("/tools")
def create_tool(t: ToolCreate, manager: SAIVerseManager = Depends(get_manager)):
    return manager.create_tool(t.name, t.description, t.module_path, t.function_name)

@router.put("/tools/{tool_id}")
def update_tool(tool_id: int, t: ToolCreate, manager: SAIVerseManager = Depends(get_manager)):
    return manager.update_tool(tool_id, t.name, t.description, t.module_path, t.function_name)

@router.delete("/tools/{tool_id}")
def delete_tool(tool_id: int, manager: SAIVerseManager = Depends(get_manager)):
    return manager.delete_tool(tool_id)

# Item
@router.post("/items")
def create_item(i: ItemCreate, manager: SAIVerseManager = Depends(get_manager)):
    return manager.create_item(i.name, i.item_type, i.description, i.owner_kind, i.owner_id, i.state_json)

@router.put("/items/{item_id}")
def update_item(item_id: str, i: ItemUpdate, manager: SAIVerseManager = Depends(get_manager)):
    return manager.update_item(item_id, i.name, i.item_type, i.description, i.owner_kind, i.owner_id, i.state_json, i.file_path)

@router.delete("/items/{item_id}")
def delete_item(item_id: str, manager: SAIVerseManager = Depends(get_manager)):
    return manager.delete_item(item_id)
