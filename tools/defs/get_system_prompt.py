"""Build system prompt for the active persona."""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from tools.context import get_active_persona_id, get_active_manager
from tools.defs import ToolSchema

LOGGER = logging.getLogger(__name__)


def _safe_format(template: str, variables: Dict[str, Any]) -> str:
    """Safely format template with variables, returning original on error."""
    try:
        return template.format(**variables)
    except Exception:
        return template


def get_system_prompt(
    building_id: Optional[str] = None,
    include_inventory: bool = True,
    include_building_items: bool = True,
    include_available_playbooks: bool = False,
) -> str:
    """Build and return the system prompt for the active persona.

    Combines:
    - Common prompt (world setting)
    - Persona system instruction
    - Persona inventory (optional)
    - Building system instruction
    - Building items (optional)
    - Available playbooks (optional)

    Returns the complete system prompt text.
    """
    persona_id = get_active_persona_id()
    if not persona_id:
        raise RuntimeError("Active persona is not set")

    manager = get_active_manager()
    if not manager:
        raise RuntimeError("Manager reference is not available")

    persona = manager.all_personas.get(persona_id)
    if not persona:
        raise RuntimeError(f"Persona {persona_id} not found in manager")

    # Use current building if not specified
    if not building_id:
        building_id = getattr(persona, "current_building_id", None)
    if not building_id:
        raise RuntimeError("Building ID not specified and persona has no current building")

    system_sections: List[str] = []

    # 1. Common prompt (world setting, framework explanation)
    common_prompt_template = getattr(persona, "common_prompt", None)
    if common_prompt_template:
        try:
            building_obj = getattr(persona, "buildings", {}).get(building_id)
            building_name = building_obj.name if building_obj else building_id
            city_name = getattr(persona, "current_city_id", "unknown_city")

            common_text = common_prompt_template.format(
                current_persona_name=getattr(persona, "persona_name", "Unknown"),
                current_persona_id=getattr(persona, "persona_id", "unknown_id"),
                current_building_name=building_name,
                current_city_name=city_name,
                current_persona_system_instruction=getattr(persona, "persona_system_instruction", ""),
                current_building_system_instruction=getattr(building_obj, "system_instruction", "") if building_obj else "",
            )
            system_sections.append(common_text.strip())
        except Exception as exc:
            LOGGER.debug("Failed to format common prompt: %s", exc)

    # 2. "## あなたについて" section
    persona_section_parts: List[str] = []
    persona_sys = getattr(persona, "persona_system_instruction", "") or ""
    if persona_sys:
        persona_section_parts.append(persona_sys.strip())

    # Persona inventory
    if include_inventory:
        try:
            inv_builder = getattr(persona, "_inventory_summary_lines", None)
            inv_lines: List[str] = inv_builder() if callable(inv_builder) else []
        except Exception:
            inv_lines = []
        if inv_lines:
            persona_section_parts.append("### インベントリ\n" + "\n".join(inv_lines))

    if persona_section_parts:
        system_sections.append("## あなたについて\n" + "\n\n".join(persona_section_parts))

    # 3. "## {building_name}" section (current location)
    try:
        building_obj = getattr(persona, "buildings", {}).get(building_id)
        if building_obj:
            building_section_parts: List[str] = []

            # Building system instruction (with variable expansion)
            building_sys = getattr(building_obj, "system_instruction", None)
            if building_sys:
                timezone = getattr(persona, "timezone", None)
                if timezone:
                    now = datetime.now(timezone)
                else:
                    now = datetime.now()
                time_vars = {
                    "current_time": now.strftime("%H:%M"),
                    "current_date": now.strftime("%Y年%m月%d日"),
                    "current_datetime": now.strftime("%Y年%m月%d日 %H:%M"),
                    "current_weekday": ["月", "火", "水", "木", "金", "土", "日"][now.weekday()],
                }
                expanded_sys = _safe_format(str(building_sys), time_vars)
                building_section_parts.append(expanded_sys.strip())

            # Building items
            if include_building_items:
                try:
                    items_by_building = getattr(manager, "items_by_building", {}) or {}
                    item_registry = getattr(manager, "item_registry", {}) or {}
                    b_items = items_by_building.get(building_id, [])
                    lines = []
                    for iid in b_items:
                        data = item_registry.get(iid, {})
                        raw_name = data.get("name", "") or ""
                        name = raw_name.strip() if raw_name.strip() else "(名前なし)"
                        desc = (data.get("description") or "").strip() or "(説明なし)"
                        lines.append(f"- [{iid}] {name}: {desc}")
                    if lines:
                        building_section_parts.append("### 建物内のアイテム\n" + "\n".join(lines))
                except Exception:
                    pass

            if building_section_parts:
                building_name = getattr(building_obj, "name", building_id)
                system_sections.append(f"## {building_name}\n" + "\n\n".join(building_section_parts))
    except Exception:
        pass

    # 4. "## 利用可能な能力" section (available playbooks)
    if include_available_playbooks:
        try:
            from tools import TOOL_REGISTRY
            list_playbooks_func = TOOL_REGISTRY.get("list_available_playbooks")
            if list_playbooks_func:
                playbooks_raw = list_playbooks_func(
                    persona_id=persona_id,
                    building_id=building_id
                )
                playbooks_json = playbooks_raw[0] if isinstance(playbooks_raw, tuple) else playbooks_raw
                if playbooks_json:
                    playbooks_list = json.loads(playbooks_json)
                    if playbooks_list:
                        playbooks_formatted = json.dumps(playbooks_list, ensure_ascii=False, indent=2)
                        system_sections.append(f"## 利用可能な能力\n以下のPlaybookを実行できます：\n```json\n{playbooks_formatted}\n```")
        except Exception as exc:
            LOGGER.debug("Failed to add available playbooks section: %s", exc)

    return "\n\n---\n\n".join([s for s in system_sections if s])


def schema() -> ToolSchema:
    return ToolSchema(
        name="get_system_prompt",
        description="Build and return the system prompt for the active persona, including world setting, persona info, and building context.",
        parameters={
            "type": "object",
            "properties": {
                "building_id": {
                    "type": "string",
                    "description": "Building ID. Defaults to current building."
                },
                "include_inventory": {
                    "type": "boolean",
                    "description": "Include persona inventory. Default: true.",
                    "default": True
                },
                "include_building_items": {
                    "type": "boolean",
                    "description": "Include building items. Default: true.",
                    "default": True
                },
                "include_available_playbooks": {
                    "type": "boolean",
                    "description": "Include available playbooks list. Default: false.",
                    "default": False
                }
            },
            "required": [],
        },
        result_type="string",
    )
