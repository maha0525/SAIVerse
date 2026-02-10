"""Build visual context messages for LLM with Building/Persona images."""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

from tools.context import get_active_persona_id, get_active_manager
from tools.core import ToolSchema

LOGGER = logging.getLogger(__name__)

# Marker to identify visual context messages so they can be exempt from attachment limits
VISUAL_CONTEXT_MARKER = "__visual_context__"

# API media URL prefix
API_MEDIA_PREFIX = "/api/media/images/"


def _resolve_image_path(path_or_url: Optional[str]) -> Optional[str]:
    """
    Resolve an image path or API URL to an actual filesystem path.
    
    Handles:
    - API URLs like /api/media/images/filename.jpg -> ~/.saiverse/image/filename.jpg
    - Already absolute filesystem paths -> returned as-is
    - saiverse:// URIs -> resolved via media_utils
    """
    if not path_or_url:
        return None
    
    # Handle API URL format
    if path_or_url.startswith(API_MEDIA_PREFIX):
        filename = path_or_url[len(API_MEDIA_PREFIX):]
        from pathlib import Path
        return str(Path.home() / ".saiverse" / "image" / filename)
    
    # Handle saiverse:// URI format
    if path_or_url.startswith("saiverse://"):
        try:
            from saiverse.media_utils import resolve_media_uri
            resolved = resolve_media_uri(path_or_url)
            return str(resolved) if resolved else None
        except ImportError:
            pass
    
    # Already an absolute or relative filesystem path
    return path_or_url


def _resolve_item_file_path(manager, file_path_str: str) -> Optional[str]:
    """
    Resolve an item file path to an actual filesystem path.
    
    Handles:
    - Relative paths (e.g., "image/filename.png") -> saiverse_home / relative_path
    - Legacy WSL absolute paths (e.g., "/home/maha/.saiverse/image/...") -> extract and remap
    """
    from pathlib import Path
    
    if not file_path_str:
        return None
    
    path = Path(file_path_str)
    
    # If path exists as-is, return it
    if path.exists():
        return str(path)
    
    # Try recovery strategies using saiverse_home
    home = getattr(manager, 'saiverse_home', None)
    if not home:
        home = Path.home() / ".saiverse"
    
    # Strategy 0: Relative path (new format)
    if not path.is_absolute():
        candidate = home / file_path_str
        if candidate.exists():
            return str(candidate)
    
    # Strategy 1: Extract from legacy paths containing 'image' or 'documents'
    parts = path.parts
    for folder in ['image', 'documents']:
        if folder in parts:
            idx = parts.index(folder)
            rel = Path(*parts[idx:])
            candidate = home / rel
            if candidate.exists():
                return str(candidate)
    
    # Strategy 2: Just filename fallback
    for folder in ['image', 'documents']:
        candidate = home / folder / path.name
        if candidate.exists():
            return str(candidate)
    
    return None


def get_visual_context(
    building_id: Optional[str] = None,
    include_self: bool = True,
    include_building: bool = True,
    include_other_personas: bool = True,
) -> List[Dict[str, Any]]:
    """Build visual context messages containing Building and Persona images.

    Returns a list of messages (user/assistant pair) that provide visual context
    about the current environment. These messages should be inserted after the
    system prompt in the conversation history.

    The returned messages include a special marker in metadata so that LLM clients
    can identify them and exempt them from attachment limits.

    Args:
        building_id: Building ID. Defaults to current building.
        include_self: Include the active persona's appearance image.
        include_building: Include the current building's interior image.
        include_other_personas: Include appearance images of other personas in the building.

    Returns:
        List of message dicts with 'role', 'content', and optionally 'metadata' keys.
        Returns empty list if no visual context images are available.
    """
    persona_id = get_active_persona_id()
    if not persona_id:
        LOGGER.debug("get_visual_context: No active persona")
        return []

    manager = get_active_manager()
    if not manager:
        LOGGER.debug("get_visual_context: No manager available")
        return []

    persona = manager.all_personas.get(persona_id)
    if not persona:
        LOGGER.debug("get_visual_context: Persona %s not found", persona_id)
        return []

    # Use current building if not specified
    if not building_id:
        building_id = getattr(persona, "current_building_id", None)
    if not building_id:
        LOGGER.debug("get_visual_context: No building_id")
        return []

    # Collect image paths
    image_items: List[Dict[str, str]] = []  # [{"path": str, "label": str, "type": str}]

    # 1. Building interior image
    if include_building:
        building_image_url = _get_building_image_path(manager, building_id)
        building_image_path = _resolve_image_path(building_image_url)
        LOGGER.debug("get_visual_context: Building %s image_url from DB: %s -> resolved: %s", building_id, building_image_url, building_image_path)
        if building_image_path:
            exists = os.path.exists(building_image_path)
            LOGGER.debug("get_visual_context: Building image file exists: %s", exists)
            if exists:
                building_obj = getattr(persona, "buildings", {}).get(building_id)
                building_name = building_obj.name if building_obj else building_id
                image_items.append({
                    "path": building_image_path,
                    "label": f"現在地「{building_name}」の内装",
                    "filename": os.path.basename(building_image_path),
                    "type": "building",
                })
                LOGGER.debug("get_visual_context: Added building image: %s", building_image_path)

    # 2. Self appearance image
    if include_self:
        self_image_url = _get_persona_appearance_path(manager, persona_id)
        self_image_path = _resolve_image_path(self_image_url)
        LOGGER.debug("get_visual_context: Persona %s appearance_url from DB: %s -> resolved: %s", persona_id, self_image_url, self_image_path)
        if self_image_path:
            exists = os.path.exists(self_image_path)
            LOGGER.debug("get_visual_context: Self image file exists: %s", exists)
            if exists:
                persona_name = getattr(persona, "persona_name", persona_id)
                image_items.append({
                    "path": self_image_path,
                    "label": f"あなた自身（{persona_name}）の外見",
                    "filename": os.path.basename(self_image_path),
                    "type": "self",
                })
                LOGGER.debug("get_visual_context: Added self image: %s", self_image_path)

    # 3. Other personas in the building
    if include_other_personas:
        occupants = manager.occupants.get(building_id, [])
        for other_id in occupants:
            if other_id == persona_id:
                continue
            other_image_url = _get_persona_appearance_path(manager, other_id)
            other_image_path = _resolve_image_path(other_image_url)
            if other_image_path and os.path.exists(other_image_path):
                other_persona = manager.all_personas.get(other_id)
                other_name = getattr(other_persona, "persona_name", other_id) if other_persona else other_id
                image_items.append({
                    "path": other_image_path,
                    "label": f"{other_name}の外見",
                    "filename": os.path.basename(other_image_path),
                    "type": "other_persona",
                })
                LOGGER.debug("get_visual_context: Added other persona image: %s (%s)", other_id, other_image_path)

    # 4. Open items in the building (pictures and documents)
    document_items: List[Dict[str, str]] = []  # For document content
    if hasattr(manager, 'get_open_items_in_building'):
        open_items = manager.get_open_items_in_building(building_id)
        for item in open_items:
            item_type = (item.get("type") or "").lower()
            item_name = item.get("name", "不明なアイテム")
            file_path_str = item.get("file_path")
            
            if not file_path_str:
                LOGGER.debug("get_visual_context: Open item %s has no file_path", item.get("item_id"))
                continue
            
            # Resolve path (handle relative paths and legacy WSL paths)
            resolved_path = _resolve_item_file_path(manager, file_path_str)
            if not resolved_path or not os.path.exists(resolved_path):
                LOGGER.debug("get_visual_context: Open item %s file not found: %s", item.get("item_id"), file_path_str)
                continue
            
            if item_type == "picture":
                image_items.append({
                    "path": resolved_path,
                    "label": f"開かれたアイテム「{item_name}」",
                    "filename": os.path.basename(resolved_path),
                    "type": "open_item_picture",
                })
                LOGGER.debug("get_visual_context: Added open picture item: %s", item_name)
            
            elif item_type == "document":
                try:
                    from pathlib import Path
                    content = Path(resolved_path).read_text(encoding="utf-8")
                    # Truncate if too long
                    if len(content) > 8000:
                        content = content[:8000] + "\n... (以下省略)"
                    document_items.append({
                        "name": item_name,
                        "content": content,
                    })
                    LOGGER.debug("get_visual_context: Added open document item: %s (%d chars)", item_name, len(content))
                except Exception as exc:
                    LOGGER.warning("get_visual_context: Failed to read document %s: %s", item_name, exc)

    if not image_items and not document_items:
        LOGGER.debug("get_visual_context: No images or documents available")
        return []

    # Build message content and metadata
    import mimetypes
    text_parts = ["<system>", "[ビジュアルコンテキスト] 以下は現在の状況を視覚的に示す情報です。"]
    
    # Image descriptions
    if image_items:
        text_parts.append("")
        text_parts.append("【画像】")
        for item in image_items:
            text_parts.append(f"- {item['label']} (file: {item['filename']})")
    
    # Document contents
    if document_items:
        text_parts.append("")
        text_parts.append("【開かれた文書】")
        for doc in document_items:
            text_parts.append(f"\n[文書: {doc['name']}]")
            text_parts.append("```")
            text_parts.append(doc["content"])
            text_parts.append("```")
    
    text_parts.append("</system>")

    media_list = []
    for item in image_items:
        mime_type = mimetypes.guess_type(item["path"])[0] or "image/png"
        media_list.append({
            "path": item["path"],
            "mime_type": mime_type,
            "type": "image",
        })

    # Return single user message (no assistant response to avoid character break)
    messages: List[Dict[str, Any]] = [
        {
            "role": "user",
            "content": "\n".join(text_parts),
            "metadata": {
                "media": media_list,
                VISUAL_CONTEXT_MARKER: True,  # Marker for attachment limit exemption
            },
        },
    ]

    LOGGER.info("get_visual_context: Generated %d images, %d documents in visual context", len(image_items), len(document_items))
    return messages


def _get_building_image_path(manager, building_id: str) -> Optional[str]:
    """Get the IMAGE_PATH for a building from the database."""
    try:
        from database.session import SessionLocal
        from database.models import Building
        session = SessionLocal()
        try:
            building = session.query(Building).filter(Building.BUILDINGID == building_id).first()
            if building and building.IMAGE_PATH:
                return building.IMAGE_PATH
        finally:
            session.close()
    except Exception as exc:
        LOGGER.debug("Failed to get building image path: %s", exc)
    return None


def _get_persona_appearance_path(manager, persona_id: str) -> Optional[str]:
    """Get the APPEARANCE_IMAGE_PATH for a persona from the database."""
    try:
        from database.session import SessionLocal
        from database.models import AI
        session = SessionLocal()
        try:
            ai = session.query(AI).filter(AI.AIID == persona_id).first()
            if ai and ai.APPEARANCE_IMAGE_PATH:
                return ai.APPEARANCE_IMAGE_PATH
        finally:
            session.close()
    except Exception as exc:
        LOGGER.debug("Failed to get persona appearance path: %s", exc)
    return None


def schema() -> ToolSchema:
    return ToolSchema(
        name="get_visual_context",
        description="Build visual context messages containing Building and Persona images for LLM context.",
        parameters={
            "type": "object",
            "properties": {
                "building_id": {
                    "type": "string",
                    "description": "Building ID. Defaults to current building."
                },
                "include_self": {
                    "type": "boolean",
                    "description": "Include active persona's appearance image. Default: true.",
                    "default": True
                },
                "include_building": {
                    "type": "boolean",
                    "description": "Include building interior image. Default: true.",
                    "default": True
                },
                "include_other_personas": {
                    "type": "boolean",
                    "description": "Include other personas' appearance images. Default: true.",
                    "default": True
                }
            },
            "required": [],
        },
        result_type="array",
    )
