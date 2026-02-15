"""Build visual context messages for LLM with structured environment info."""
from __future__ import annotations

import logging
import mimetypes
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from tools.context import get_active_persona_id, get_active_manager
from tools.core import ToolSchema

LOGGER = logging.getLogger(__name__)

# Marker to identify visual context messages so they can be exempt from attachment limits
VISUAL_CONTEXT_MARKER = "__visual_context__"

# API media URL prefix
API_MEDIA_PREFIX = "/api/media/images/"

# Maximum characters for open document content
DOCUMENT_CONTENT_MAX_CHARS = 8000


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


def _add_to_media_list(file_path: str, media_list: List[Dict[str, str]]) -> None:
    """Add an image file to the media list with its MIME type."""
    mime_type = mimetypes.guess_type(file_path)[0] or "image/png"
    media_list.append({
        "path": file_path,
        "mime_type": mime_type,
        "type": "image",
    })


def _render_item(
    item: Dict[str, Any],
    text_parts: List[str],
    media_list: List[Dict[str, str]],
    manager: Any,
) -> None:
    """Render a single item into the visual context text and media list."""
    item_id = item.get("item_id", "")
    item_type = (item.get("type") or "").lower()
    item_name = item.get("name", "不明なアイテム")
    description = (item.get("description") or "").strip() or "(説明なし)"
    state = item.get("state", {})
    is_open = isinstance(state, dict) and state.get("is_open", False)
    file_path_str = item.get("file_path")

    type_label = {
        "picture": "Image",
        "document": "Document",
        "object": "Object",
    }.get(item_type, item_type.capitalize() or "Item")

    if item_type == "object":
        # Objects have no open/closed concept
        text_parts.append(f"[{type_label}] {item_name}")
        text_parts.append(f"id: {item_id}")
        text_parts.append(description)
        text_parts.append("")

    elif item_type == "picture":
        open_label = "(Open) " if is_open else "(Closed) "
        text_parts.append(f"[{type_label}] {item_name}")
        text_parts.append(f"{open_label}id: {item_id}")

        if is_open and file_path_str:
            resolved = _resolve_item_file_path(manager, file_path_str)
            if resolved and os.path.exists(resolved):
                text_parts.append(f"saiverse://item/{item_id}/image")
                _add_to_media_list(resolved, media_list)
                LOGGER.debug("get_visual_context: Added open picture item: %s", item_name)
            else:
                text_parts.append(description)
        else:
            text_parts.append(description)
        text_parts.append("")

    elif item_type == "document":
        open_label = "(Open) " if is_open else "(Closed) "
        text_parts.append(f"[{type_label}] {item_name}")
        text_parts.append(f"{open_label}id: {item_id}")

        if is_open and file_path_str:
            resolved = _resolve_item_file_path(manager, file_path_str)
            if resolved and os.path.exists(resolved):
                try:
                    content = Path(resolved).read_text(encoding="utf-8")
                    if len(content) > DOCUMENT_CONTENT_MAX_CHARS:
                        content = content[:DOCUMENT_CONTENT_MAX_CHARS] + "\n... (以下省略)"
                    text_parts.append("```")
                    text_parts.append(content)
                    text_parts.append("```")
                    LOGGER.debug("get_visual_context: Added open document: %s (%d chars)", item_name, len(content))
                except Exception as exc:
                    LOGGER.warning("get_visual_context: Failed to read document %s: %s", item_name, exc)
                    text_parts.append(description)
            else:
                text_parts.append(description)
        else:
            text_parts.append(description)
        text_parts.append("")

    else:
        # Unknown type — show as generic item
        text_parts.append(f"[{type_label}] {item_name}")
        text_parts.append(f"id: {item_id}")
        text_parts.append(description)
        text_parts.append("")


def get_visual_context(
    building_id: Optional[str] = None,
    include_self: bool = True,
    include_building: bool = True,
    include_other_personas: bool = True,
) -> List[Dict[str, Any]]:
    """Build structured visual context message for LLM.

    Returns a single-element list containing a user message with structured
    environment info: persona presence, building details, and all items.

    Args:
        building_id: Building ID. Defaults to current building.
        include_self: Include the active persona's appearance image.
        include_building: Include the current building's interior image.
        include_other_personas: Include appearance images of other personas in the building.

    Returns:
        List of message dicts with 'role', 'content', and 'metadata' keys.
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

    text_parts: List[str] = []
    media_list: List[Dict[str, str]] = []

    text_parts.append("<system>")
    text_parts.append("# ビジュアルコンテキスト")
    text_parts.append("以下は現在の状況を視覚的に示す情報です。")
    text_parts.append("")
    text_parts.append("---")
    text_parts.append("")

    # ========== Section 1: ペルソナ ==========
    text_parts.append("## ペルソナ")
    occupants = manager.occupants.get(building_id, [])
    persona_count = len(occupants)
    if persona_count <= 1:
        text_parts.append("現在、このBuildingにはあなただけがいます。")
    else:
        text_parts.append(f"現在、このBuildingにはあなた含め{persona_count}人のペルソナがいます。")
    text_parts.append("")

    # Self appearance
    if include_self:
        persona_name = getattr(persona, "persona_name", persona_id)
        text_parts.append(f"[あなた自身（{persona_name}）の外見]")
        text_parts.append(f"saiverse://persona/{persona_id}/image")

        self_image_url = _get_persona_appearance_path(manager, persona_id)
        self_image_path = _resolve_image_path(self_image_url)
        if self_image_path and os.path.exists(self_image_path):
            _add_to_media_list(self_image_path, media_list)
            LOGGER.debug("get_visual_context: Added self image: %s", self_image_path)
        text_parts.append("")

    # Other personas
    if include_other_personas:
        for other_id in occupants:
            if other_id == persona_id:
                continue
            other_persona = manager.all_personas.get(other_id)
            other_name = getattr(other_persona, "persona_name", other_id) if other_persona else other_id
            text_parts.append(f"[{other_name}の外見]")
            text_parts.append(f"saiverse://persona/{other_id}/image")

            other_image_url = _get_persona_appearance_path(manager, other_id)
            other_image_path = _resolve_image_path(other_image_url)
            if other_image_path and os.path.exists(other_image_path):
                _add_to_media_list(other_image_path, media_list)
                LOGGER.debug("get_visual_context: Added other persona image: %s (%s)", other_id, other_image_path)
            text_parts.append("")

    # ========== Section 2: Building ==========
    text_parts.append("---")
    text_parts.append("")
    text_parts.append("## Building")

    building_obj = getattr(persona, "buildings", {}).get(building_id)
    building_name = building_obj.name if building_obj else building_id
    text_parts.append(f"現在、「{building_name}」にいます。")
    text_parts.append("")

    # Building interior image
    if include_building:
        building_image_url = _get_building_image_path(manager, building_id)
        building_image_path = _resolve_image_path(building_image_url)
        if building_image_path and os.path.exists(building_image_path):
            text_parts.append("[内装]")
            text_parts.append(f"saiverse://building/{building_id}/image")
            _add_to_media_list(building_image_path, media_list)
            LOGGER.debug("get_visual_context: Added building image: %s", building_image_path)
            text_parts.append("")

    # Building system instruction
    if building_obj:
        base_sys = getattr(building_obj, "base_system_instruction", "") or ""
        if base_sys.strip():
            text_parts.append("[システムプロンプト]")
            text_parts.append(base_sys.strip())
            text_parts.append("")

    # ========== Section 3: Item ==========
    text_parts.append("---")
    text_parts.append("")
    text_parts.append("## Item")
    text_parts.append("")

    persona_name = getattr(persona, "persona_name", persona_id)

    # 3a. Persona inventory items
    inventory_items = (
        manager.get_all_items_for_persona(persona_id)
        if hasattr(manager, 'get_all_items_for_persona') else []
    )
    if inventory_items:
        text_parts.append(f"### あなた自身（{persona_name}）のインベントリ内")
        text_parts.append("")
        for item in inventory_items:
            _render_item(item, text_parts, media_list, manager)

    # 3b. Building items
    building_items = (
        manager.get_all_items_in_building(building_id)
        if hasattr(manager, 'get_all_items_in_building') else []
    )
    if building_items:
        text_parts.append("### Building内")
        text_parts.append("")
        for item in building_items:
            _render_item(item, text_parts, media_list, manager)

    if not inventory_items and not building_items:
        text_parts.append("アイテムはありません。")
        text_parts.append("")

    text_parts.append("</system>")

    # Build message
    messages: List[Dict[str, Any]] = [
        {
            "role": "user",
            "content": "\n".join(text_parts),
            "metadata": {
                "media": media_list,
                VISUAL_CONTEXT_MARKER: True,
            },
        },
    ]

    LOGGER.info(
        "get_visual_context: Generated visual context (%d images, %d inventory items, %d building items)",
        len(media_list), len(inventory_items), len(building_items),
    )
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
        description="Build visual context messages containing structured environment info for LLM context.",
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
