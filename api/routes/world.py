from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form
from pydantic import BaseModel
from typing import List, Optional, Any
import shutil
from pathlib import Path

from api.deps import get_manager
from saiverse.saiverse_manager import SAIVerseManager

router = APIRouter()


def _check_result(result: str) -> dict:
    """Check manager method result string and raise HTTPException on error.

    All legacy manager CRUD methods return plain strings. Error results
    start with ``"Error: "``. This helper converts them to proper HTTP
    responses so the frontend always receives 4xx on failure.
    """
    if isinstance(result, str) and result.startswith("Error:"):
        detail = result[len("Error:"):].strip()
        raise HTTPException(status_code=400, detail=detail)
    # Return as structured JSON so the frontend can parse it consistently.
    msg = result if isinstance(result, str) else str(result)
    return {"message": msg}


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
    building_id: Optional[str] = None  # Custom ID (optional, auto-generated if not provided)

class BuildingUpdate(BaseModel):
    name: str
    description: str
    capacity: int
    system_instruction: str
    city_id: int
    tool_ids: List[int]
    auto_interval: int
    image_path: Optional[str] = None  # Building interior image for LLM visual context
    extra_prompt_files: Optional[List[str]] = None  # Additional prompt files for this building

class AICreate(BaseModel):
    name: str
    system_prompt: str
    home_city_id: int
    ai_id: Optional[str] = None  # Custom ID (optional, auto-generated if not provided)

class AIUpdate(BaseModel):
    name: str
    description: str
    system_prompt: str
    home_city_id: int
    default_model: Optional[str]
    lightweight_model: Optional[str]
    interaction_mode: str
    avatar_path: Optional[str]
    appearance_image_path: Optional[str] = None  # Persona appearance image for LLM visual context

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
    description: str = ""  # Optional - auto-generated if empty for picture/document
    owner_kind: str
    owner_id: Optional[str] = None
    state_json: str = "{}"
    file_path: Optional[str] = None  # Relative path to file (for picture/document items)
    creator_id: Optional[str] = None
    source_context: Optional[str] = None

class ItemUpdate(BaseModel):
    name: str
    item_type: str
    description: str = ""
    owner_kind: str = "world"
    owner_id: Optional[str] = None
    state_json: Optional[str] = None
    file_path: Optional[str] = None

# --- Routes ---

# City
@router.post("/cities")
def create_city(city: CityCreate, manager: SAIVerseManager = Depends(get_manager)):
    return _check_result(manager.create_city(city.name, city.description, city.ui_port, city.api_port, city.timezone))

@router.put("/cities/{city_id}")
def update_city(city_id: int, city: CityUpdate, manager: SAIVerseManager = Depends(get_manager)):
    return _check_result(manager.update_city(city_id, city.name, city.description, city.online_mode, city.ui_port, city.api_port, city.timezone, city.host_avatar_path, None))

@router.delete("/cities/{city_id}")
def delete_city(city_id: int, manager: SAIVerseManager = Depends(get_manager)):
    return _check_result(manager.delete_city(city_id))

# Building
@router.post("/buildings")
def create_building(b: BuildingCreate, manager: SAIVerseManager = Depends(get_manager)):
    return _check_result(manager.create_building(b.name, b.description, b.capacity, b.system_instruction, b.city_id, b.building_id))

@router.put("/buildings/{building_id}")
def update_building(building_id: str, b: BuildingUpdate, manager: SAIVerseManager = Depends(get_manager)):
    return _check_result(manager.update_building(building_id, b.name, b.capacity, b.description, b.system_instruction, b.city_id, b.tool_ids, b.auto_interval, b.image_path, b.extra_prompt_files))

@router.delete("/buildings/{building_id}")
def delete_building(building_id: str, manager: SAIVerseManager = Depends(get_manager)):
    return _check_result(manager.delete_building(building_id))

@router.get("/prompts/available")
def get_available_prompts():
    """Get list of available prompt files from prompts directories."""
    from saiverse.data_paths import iter_files, PROMPTS_DIR
    prompts = []
    for path in iter_files(PROMPTS_DIR, "*.txt"):
        prompts.append(path.name)
    return sorted(prompts)

# AI
@router.post("/ais")
def create_ai(ai: AICreate, manager: SAIVerseManager = Depends(get_manager)):
    success, msg = manager.create_ai(ai.name, ai.system_prompt, ai.home_city_id, ai.ai_id)
    if not success:
        raise HTTPException(status_code=400, detail=msg)
    return {"message": msg}

@router.put("/ais/{ai_id}")
def update_ai(ai_id: str, ai: AIUpdate, manager: SAIVerseManager = Depends(get_manager)):
    return _check_result(manager.update_ai(ai_id, ai.name, ai.description, ai.system_prompt, ai.home_city_id, ai.default_model, ai.lightweight_model, ai.interaction_mode, ai.avatar_path, None, ai.appearance_image_path))

@router.delete("/ais/{ai_id}")
def delete_ai(ai_id: str, manager: SAIVerseManager = Depends(get_manager)):
    return _check_result(manager.delete_ai(ai_id))

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

    return _check_result(manager.move_ai_from_editor(ai_id, target_id))

# Blueprint
@router.post("/blueprints")
def create_blueprint(bp: BlueprintCreate, manager: SAIVerseManager = Depends(get_manager)):
    return _check_result(manager.create_blueprint(bp.name, bp.description, bp.city_id, bp.system_prompt, bp.entity_type))

@router.put("/blueprints/{bp_id}")
def update_blueprint(bp_id: int, bp: BlueprintCreate, manager: SAIVerseManager = Depends(get_manager)):
    return _check_result(manager.update_blueprint(bp_id, bp.name, bp.description, bp.city_id, bp.system_prompt, bp.entity_type))

@router.delete("/blueprints/{bp_id}")
def delete_blueprint(bp_id: int, manager: SAIVerseManager = Depends(get_manager)):
    return _check_result(manager.delete_blueprint(bp_id))

@router.post("/blueprints/{bp_id}/spawn")
def spawn_blueprint(bp_id: int, spawn: BlueprintSpawn, manager: SAIVerseManager = Depends(get_manager)):
    success, msg = manager.spawn_entity_from_blueprint(bp_id, spawn.entity_name, spawn.building_name)
    if not success:
        raise HTTPException(status_code=400, detail=msg)
    return {"message": msg}

# Tool
@router.post("/tools")
def create_tool(t: ToolCreate, manager: SAIVerseManager = Depends(get_manager)):
    return _check_result(manager.create_tool(t.name, t.description, t.module_path, t.function_name))

@router.put("/tools/{tool_id}")
def update_tool(tool_id: int, t: ToolCreate, manager: SAIVerseManager = Depends(get_manager)):
    return _check_result(manager.update_tool(tool_id, t.name, t.description, t.module_path, t.function_name))

@router.delete("/tools/{tool_id}")
def delete_tool(tool_id: int, manager: SAIVerseManager = Depends(get_manager)):
    return _check_result(manager.delete_tool(tool_id))

# Item
@router.post("/items")
def create_item(i: ItemCreate, manager: SAIVerseManager = Depends(get_manager)):
    description = i.description
    file_path = i.file_path
    
    # Auto-generate description if empty and file_path is provided
    if not description.strip() and file_path:
        try:
            from pathlib import Path
            saiverse_home = getattr(manager, 'saiverse_home', None)
            if saiverse_home:
                full_path = saiverse_home / file_path
            else:
                full_path = Path.home() / ".saiverse" / file_path
            
            if full_path.exists():
                item_type = i.item_type.lower()
                if item_type == "picture":
                    from saiverse.media_summary import ensure_image_summary
                    import mimetypes
                    mime_type = mimetypes.guess_type(str(full_path))[0] or "image/png"
                    summary = ensure_image_summary(full_path, mime_type)
                    if summary:
                        description = summary
                elif item_type == "document":
                    from saiverse.media_summary import ensure_document_summary
                    summary = ensure_document_summary(full_path)
                    if summary:
                        description = summary
        except Exception as e:
            import logging
            logging.warning(f"Failed to auto-generate description for item: {e}")
    
    return _check_result(manager.create_item(i.name, i.item_type, description, i.owner_kind, i.owner_id, i.state_json, file_path, creator_id=i.creator_id, source_context=i.source_context))

@router.put("/items/{item_id}")
def update_item(item_id: str, i: ItemUpdate, manager: SAIVerseManager = Depends(get_manager)):
    return _check_result(manager.update_item(item_id, i.name, i.item_type, i.description, i.owner_kind, i.owner_id, i.state_json, i.file_path))

@router.get("/items/{item_id}")
def get_item(item_id: str, manager: SAIVerseManager = Depends(get_manager)):
    """Get item details including owner information."""
    details = manager.get_item_details(item_id)
    if not details:
        raise HTTPException(status_code=404, detail="Item not found")
    return details

@router.delete("/items/{item_id}")
def delete_item(item_id: str, manager: SAIVerseManager = Depends(get_manager)):
    return _check_result(manager.delete_item(item_id))


# --- Playbook ---
from api.deps import get_db
from database.models import Playbook as PlaybookModel
from sea.playbook_models import PlaybookSchema, validate_playbook_graph, PlaybookValidationError
import json

class PlaybookCreate(BaseModel):
    name: str
    description: str
    scope: str = "public"
    router_callable: bool = False
    user_selectable: bool = False
    nodes_json: str  # JSON string
    schema_json: str  # JSON string (input_schema, start_node, etc.)

class PlaybookUpdate(BaseModel):
    name: str
    description: str
    scope: str
    router_callable: bool
    user_selectable: bool
    nodes_json: str
    schema_json: str

class PlaybookListItem(BaseModel):
    id: int
    name: str
    description: str
    scope: str
    router_callable: bool
    user_selectable: bool

class PlaybookDetail(PlaybookListItem):
    nodes_json: str
    schema_json: str

@router.get("/playbooks", response_model=List[PlaybookListItem])
def list_playbooks(db = Depends(get_db)):
    """List all playbooks."""
    playbooks = db.query(PlaybookModel).all()
    return [
        {
            "id": p.id,
            "name": p.name,
            "description": p.description,
            "scope": p.scope,
            "router_callable": p.router_callable,
            "user_selectable": p.user_selectable,
        }
        for p in playbooks
    ]

@router.get("/playbooks/{playbook_id}", response_model=PlaybookDetail)
def get_playbook(playbook_id: int, db = Depends(get_db)):
    """Get playbook details including nodes."""
    playbook = db.query(PlaybookModel).filter(PlaybookModel.id == playbook_id).first()
    if not playbook:
        raise HTTPException(status_code=404, detail="Playbook not found")
    return {
        "id": playbook.id,
        "name": playbook.name,
        "description": playbook.description,
        "scope": playbook.scope,
        "router_callable": playbook.router_callable,
        "user_selectable": playbook.user_selectable,
        "nodes_json": playbook.nodes_json,
        "schema_json": playbook.schema_json,
    }

def _validate_playbook_data(name: str, description: str, nodes_json: str, schema_json: str):
    """Validate playbook schema and graph."""
    try:
        nodes = json.loads(nodes_json)
        schema = json.loads(schema_json)
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=400, detail=f"Invalid JSON: {e}")
    
    # Build full playbook for validation
    full_playbook = {
        "name": name,
        "description": description,
        "nodes": nodes,
        **schema
    }
    
    try:
        playbook_schema = PlaybookSchema(**full_playbook)
        validate_playbook_graph(playbook_schema)
    except PlaybookValidationError as e:
        raise HTTPException(status_code=400, detail=f"Graph validation error: {e}")
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Schema validation error: {e}")

@router.post("/playbooks")
def create_playbook(pb: PlaybookCreate, db = Depends(get_db)):
    """Create a new playbook."""
    # Check name uniqueness
    existing = db.query(PlaybookModel).filter(PlaybookModel.name == pb.name).first()
    if existing:
        raise HTTPException(status_code=400, detail="Playbook with this name already exists")
    
    # Validate
    _validate_playbook_data(pb.name, pb.description, pb.nodes_json, pb.schema_json)
    
    playbook = PlaybookModel(
        name=pb.name,
        description=pb.description,
        scope=pb.scope,
        router_callable=pb.router_callable,
        user_selectable=pb.user_selectable,
        nodes_json=pb.nodes_json,
        schema_json=pb.schema_json,
    )
    db.add(playbook)
    db.commit()
    db.refresh(playbook)
    return {"success": True, "id": playbook.id}

@router.put("/playbooks/{playbook_id}")
def update_playbook(playbook_id: int, pb: PlaybookUpdate, db = Depends(get_db)):
    """Update an existing playbook."""
    playbook = db.query(PlaybookModel).filter(PlaybookModel.id == playbook_id).first()
    if not playbook:
        raise HTTPException(status_code=404, detail="Playbook not found")
    
    # Check name uniqueness if changed
    if pb.name != playbook.name:
        existing = db.query(PlaybookModel).filter(PlaybookModel.name == pb.name).first()
        if existing:
            raise HTTPException(status_code=400, detail="Playbook with this name already exists")
    
    # Validate
    _validate_playbook_data(pb.name, pb.description, pb.nodes_json, pb.schema_json)
    
    playbook.name = pb.name
    playbook.description = pb.description
    playbook.scope = pb.scope
    playbook.router_callable = pb.router_callable
    playbook.user_selectable = pb.user_selectable
    playbook.nodes_json = pb.nodes_json
    playbook.schema_json = pb.schema_json
    db.commit()
    return {"success": True}

@router.delete("/playbooks/{playbook_id}")
def delete_playbook(playbook_id: int, db = Depends(get_db)):
    """Delete a playbook."""
    playbook = db.query(PlaybookModel).filter(PlaybookModel.id == playbook_id).first()
    if not playbook:
        raise HTTPException(status_code=404, detail="Playbook not found")
    
    db.delete(playbook)
    db.commit()
    return {"success": True}


class PlaybookImportRequest(BaseModel):
    """Request body for importing a playbook from JSON content."""
    playbook_json: str  # Full playbook JSON as string


@router.post("/playbooks/import")
def import_playbook(req: PlaybookImportRequest, db = Depends(get_db)):
    """Import a playbook from JSON content. Creates new or updates existing based on name."""
    try:
        data = json.loads(req.playbook_json)
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=400, detail=f"Invalid JSON: {e}")
    
    name = data.get("name")
    if not name:
        raise HTTPException(status_code=400, detail="Playbook JSON must contain a 'name' field")
    
    description = data.get("description", "")
    scope = data.get("scope", "public")
    router_callable = data.get("router_callable", False)
    user_selectable = data.get("user_selectable", False)
    
    # Validate playbook using full structure
    try:
        parsed = PlaybookSchema(**data)
        validate_playbook_graph(parsed)
    except PlaybookValidationError as e:
        raise HTTPException(status_code=400, detail=f"Graph validation error: {e}")
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Schema validation error: {e}")
    
    # Convert to normalized dict and serialize
    normalized_data = parsed.dict()
    
    # nodes_json stores the FULL playbook structure (for runtime loading)
    nodes_json = json.dumps(normalized_data, ensure_ascii=False)
    
    # schema_json stores metadata for display/editing
    schema_payload = {
        "name": name,
        "description": description,
        "input_schema": normalized_data.get("input_schema", []),
        "start_node": normalized_data.get("start_node"),
    }
    schema_json = json.dumps(schema_payload, ensure_ascii=False)
    
    # Check if playbook exists
    existing = db.query(PlaybookModel).filter(PlaybookModel.name == name).first()
    
    if existing:
        # Update existing playbook
        existing.description = description
        existing.scope = scope
        existing.router_callable = router_callable
        existing.user_selectable = user_selectable
        existing.nodes_json = nodes_json
        existing.schema_json = schema_json
        db.commit()
        return {"success": True, "action": "updated", "id": existing.id, "name": name}
    else:
        # Create new playbook
        playbook = PlaybookModel(
            name=name,
            description=description,
            scope=scope,
            router_callable=router_callable,
            user_selectable=user_selectable,
            nodes_json=nodes_json,
            schema_json=schema_json,
        )
        db.add(playbook)
        db.commit()
        db.refresh(playbook)
        return {"success": True, "action": "created", "id": playbook.id, "name": name}

