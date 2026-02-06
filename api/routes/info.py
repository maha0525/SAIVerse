from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse, FileResponse
from pathlib import Path
from pydantic import BaseModel
from typing import List, Optional, Any
from api.deps import get_manager

router = APIRouter()

class OccupantInfo(BaseModel):
    id: str
    name: str
    avatar: Optional[str] = None

class ItemInfo(BaseModel):
    id: str
    name: str
    description: str
    type: str # 'object', 'document', 'picture'
    file_path: Optional[str] = None
    is_open: bool = False  # Whether item content is included in visual context

class BuildingDetailsResponse(BaseModel):
    id: str
    name: str
    description: str = ""
    image_path: Optional[str] = None  # Building interior image for visual context
    occupants: List[OccupantInfo]
    items: List[ItemInfo]

@router.get("/details", response_model=BuildingDetailsResponse)
def get_building_details(building_id: Optional[str] = None, manager = Depends(get_manager)):
    """Get detailed info about current building: occupants, items."""
    building_id = building_id or manager.user_current_building_id
    if not building_id or building_id not in manager.building_map:
        return {
            "id": "unknown", 
            "name": "Unknown", 
            "occupants": [], 
            "items": []
        }

    building = manager.building_map[building_id]
    
    # 1. Occupants
    occupants_list = []
    if building_id in manager.occupancy_manager.occupants:
        occupant_ids = manager.occupancy_manager.occupants[building_id]
        sorted_ids = sorted(occupant_ids) if occupant_ids else []
        for oid in sorted_ids:
            if oid in manager.personas:
                persona = manager.personas[oid]
                avatar = persona.avatar_image
                if avatar:
                    if avatar.startswith("user_data/icons/"):
                        # Convert user_data/icons path to API URL
                        avatar = "/api/static/user_icons/" + avatar[len("user_data/icons/"):]
                    elif avatar.startswith("builtin_data/icons/"):
                        # Convert builtin_data/icons path to API URL
                        avatar = "/api/static/builtin_icons/" + avatar[len("builtin_data/icons/"):]
                    elif avatar.startswith("assets/"):
                        # Convert local path "assets/..." to API URL "/api/static/..."
                        avatar = "/api/static/" + avatar[7:]
                
                occupants_list.append({
                    "id": oid,
                    "name": persona.persona_name,
                    "avatar": avatar
                })

    # 2. Items
    items_list = []
    if building_id in manager.items_by_building:
        item_ids = manager.items_by_building[building_id]
        sorted_item_ids = sorted(item_ids) if item_ids else []
        for item_id in sorted_item_ids:
            if item_id in manager.item_registry:
                item_data = manager.item_registry[item_id] # item_registry or items? manager.items seems to be the one used in app.py
                # Actually app.py uses manager.items for retrieval but manager.item_registry might be the source of truth for listing?
                # Let's check saiverse_manager.py if distinct. Usually they are same or items is runtime dict.
                # Assuming manager.items is safer based on app.py logic.
                
                # Check if manager.items is available or use registry
                data = item_data # Default to registry
                if hasattr(manager, 'items') and item_id in manager.items:
                    data = manager.items[item_id]
                
                raw_name = data.get("name", "") or ""
                display_name = raw_name.strip() if raw_name.strip() else "(名前なし)"
                
                items_list.append({
                    "id": item_id,
                    "name": display_name,
                    "description": data.get("description", ""),
                    "type": data.get("type", "object"),
                    "file_path": data.get("file_path"),
                    "is_open": data.get("state", {}).get("is_open", False) if isinstance(data.get("state"), dict) else False,
                })

    # Get Building image path from database
    building_image_path = None
    try:
        from database.session import SessionLocal
        from database.models import Building as BuildingModel
        session = SessionLocal()
        try:
            db_building = session.query(BuildingModel).filter(BuildingModel.BUILDINGID == building_id).first()
            if db_building and db_building.IMAGE_PATH:
                building_image_path = db_building.IMAGE_PATH
        finally:
            session.close()
    except Exception as e:
        print(f"Failed to get building image: {e}")

    return {
        "id": building_id,
        "name": building.name,
        "description": building.description or "",
        "image_path": building_image_path,
        "occupants": occupants_list,
        "items": items_list
    }

@router.get("/item/{item_id}")
def get_item_content(item_id: str, manager = Depends(get_manager)):
    # Use manager.state.items if available, or manager.items
    items_map = {}
    if hasattr(manager, 'state') and hasattr(manager.state, 'items'):
        items_map = manager.state.items
    elif hasattr(manager, 'items'):
        items_map = manager.items
    
    # Debug logging
    print(f"DEBUG: Requesting item_id: {item_id}")
    print(f"DEBUG: Available item keys: {list(items_map.keys())}")
    
    if item_id not in items_map:
        # Fallback to registry if needed (for admin/seed items not yet in memory?)
        if hasattr(manager, 'item_registry') and item_id in manager.item_registry:
            items_map = manager.item_registry
            print("DEBUG: Found in registry")
        else:
            print("DEBUG: Not found in any map")
            raise HTTPException(status_code=404, detail=f"Item not found: {item_id}")

    item_data = items_map[item_id]

    item_type = item_data.get("type", "object")
    file_path = item_data.get("file_path")
    
    print(f"DEBUG: Item type: {item_type}")
    print(f"DEBUG: Raw file_path: {file_path}")

    if not file_path:
        raise HTTPException(status_code=400, detail="No file path for this item")
        
    path = Path(file_path)
    # Debug info
    print(f"DEBUG: Checking path: {path}")
    
    if not path.exists():
        # Attempt recovery for legacy/WSL paths or relative paths
        # The DB might contain:
        # - New format: relative paths like "image/filename.png" or "documents/filename.txt"
        # - Legacy format: /home/maha/.saiverse/... paths from WSL
        # We try to find the file relative to current manager.saiverse_home
        if hasattr(manager, 'saiverse_home'):
            home = manager.saiverse_home
            parts = path.parts
            
            # Strategy 0: Handle relative paths (new format)
            # If path is relative (e.g., "image/filename.png"), join with saiverse_home
            if not path.is_absolute():
                candidate = home / file_path
                if candidate.exists():
                    print(f"DEBUG: Recovered path (strategy 0 - relative path): {candidate}")
                    path = candidate
            
            # Strategy 1a: strict 'documents' match (legacy WSL paths)
            if not path.exists() and 'documents' in parts:
                idx = parts.index('documents')
                rel = Path(*parts[idx:])
                candidate = home / rel
                if candidate.exists():
                    print(f"DEBUG: Recovered path (strategy 1a - documents): {candidate}")
                    path = candidate
            
            # Strategy 1b: strict 'image' match (legacy WSL paths for picture items)
            if not path.exists() and 'image' in parts:
                idx = parts.index('image')
                rel = Path(*parts[idx:])
                candidate = home / rel
                if candidate.exists():
                    print(f"DEBUG: Recovered path (strategy 1b - image): {candidate}")
                    path = candidate
            
            # Strategy 2a: just filename in documents (fallback)
            if not path.exists():
                candidate = home / "documents" / path.name
                if candidate.exists():
                    print(f"DEBUG: Recovered path (strategy 2a - documents filename): {candidate}")
                    path = candidate
            
            # Strategy 2b: just filename in image (fallback for picture items)
            if not path.exists():
                candidate = home / "image" / path.name
                if candidate.exists():
                    print(f"DEBUG: Recovered path (strategy 2b - image filename): {candidate}")
                    path = candidate
    
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"File not found on server: {path}")

    if item_type == "document":
        try:
            content = path.read_text(encoding="utf-8")
            return {"type": "document", "content": content}
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))
            
    elif item_type == "picture":
        # For pictures, we can serve the file directly OR return the path for a static mount
        # If we return FileResponse, the frontend can display it.
        # But wait, frontend <img src> needs a URL.
        # If we return the file content here, it might be heavy.
        # Better: return a URL that the frontend can use. 
        # But we don't have a dynamic route for arbitrary file paths unless we mount them.
        # Previous app used /gradio_api/file=... which Gradio handled.
        
        # SOLUTION: We can verify the path is within valid areas (assets?) or serve it via a stream endpoint.
        # For now, let's return the content as FileResponse so the browser displays it if visited?
        # NO, frontend needs to Embed it.
        # API: GET /api/info/item/{id}/image -> returns image bytes
        return FileResponse(path)

    else:
        return {"type": item_type, "message": "No content to display"}

@router.get("/models")
def list_available_models():
    """Get list of available models for persona configuration."""
    from model_configs import get_model_choices_with_display_names
    choices = get_model_choices_with_display_names()
    return [{"id": mid, "name": name} for mid, name in choices]


@router.post("/item/{item_id}/toggle-open")
def toggle_item_open(item_id: str, manager = Depends(get_manager)):
    """Toggle the open/close state of an item.

    When an item is open, its content is included in the AI's visual context.
    - Picture items: Added as images
    - Document items: Added as text in prompt
    """
    try:
        new_state = manager.toggle_item_open_state(item_id)
        return {"success": True, "is_open": new_state, "item_id": item_id}
    except RuntimeError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


class DocumentContentUpdate(BaseModel):
    content: str


@router.put("/item/{item_id}/content")
def update_item_content(item_id: str, body: DocumentContentUpdate, manager = Depends(get_manager)):
    """Update the content of a document item."""
    # Get item data
    items_map = {}
    if hasattr(manager, 'state') and hasattr(manager.state, 'items'):
        items_map = manager.state.items
    elif hasattr(manager, 'items'):
        items_map = manager.items

    if item_id not in items_map:
        if hasattr(manager, 'item_registry') and item_id in manager.item_registry:
            items_map = manager.item_registry
        else:
            raise HTTPException(status_code=404, detail=f"Item not found: {item_id}")

    item_data = items_map[item_id]
    item_type = item_data.get("type", "object")

    if item_type != "document":
        raise HTTPException(status_code=400, detail="Only document items can be edited")

    file_path = item_data.get("file_path")
    if not file_path:
        raise HTTPException(status_code=400, detail="No file path for this item")

    path = Path(file_path)

    # Handle relative paths
    if not path.is_absolute() and hasattr(manager, 'saiverse_home'):
        path = manager.saiverse_home / file_path

    # Legacy path recovery (same logic as get_item_content)
    if not path.exists() and hasattr(manager, 'saiverse_home'):
        home = manager.saiverse_home
        parts = Path(file_path).parts

        if 'documents' in parts:
            idx = parts.index('documents')
            rel = Path(*parts[idx:])
            candidate = home / rel
            if candidate.exists():
                path = candidate

        if not path.exists():
            candidate = home / "documents" / Path(file_path).name
            if candidate.exists():
                path = candidate

    if not path.exists():
        raise HTTPException(status_code=404, detail=f"File not found: {path}")

    try:
        path.write_text(body.content, encoding="utf-8")
        return {"success": True, "message": "Content updated successfully"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
