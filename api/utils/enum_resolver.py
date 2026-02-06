"""
Resolve dynamic enum sources for playbook parameters.

enum_source format: "collection:scope"
Examples:
  - "playbooks:router_callable"
  - "buildings:current_city"
  - "personas:current_city"
  - "items:current_building"
  - "tools:available"
"""

from typing import Any, Dict, List, Optional
from database.session import SessionLocal
from database.models import Playbook, Building, AI, Item, ItemLocation, Tool, BuildingToolLink


class EnumResolverContext:
    """Context for resolving dynamic enums."""
    def __init__(
        self,
        city_id: Optional[int] = None,
        building_id: Optional[str] = None,
        persona_id: Optional[str] = None,
    ):
        self.city_id = city_id
        self.building_id = building_id
        self.persona_id = persona_id


def resolve_enum_source(
    enum_source: str,
    context: Optional[EnumResolverContext] = None
) -> List[Dict[str, Any]]:
    """
    Resolve enum_source string to a list of {value, label} options.

    Args:
        enum_source: Format "collection:scope" (e.g., "playbooks:router_callable")
        context: Optional context with city_id, building_id, persona_id

    Returns:
        List of {"value": str, "label": str} dicts
    """
    if not enum_source:
        return []

    parts = enum_source.split(":", 1)
    if len(parts) != 2:
        raise ValueError(f"Invalid enum_source format: {enum_source}. Expected 'collection:scope'")

    collection, scope = parts
    context = context or EnumResolverContext()

    resolvers = {
        "playbooks": _resolve_playbooks,
        "buildings": _resolve_buildings,
        "personas": _resolve_personas,
        "items": _resolve_items,
        "tools": _resolve_tools,
    }

    resolver = resolvers.get(collection)
    if not resolver:
        raise ValueError(f"Unknown collection: {collection}")

    return resolver(scope, context)


def _resolve_playbooks(scope: str, context: EnumResolverContext) -> List[Dict[str, Any]]:
    """Resolve playbook options."""
    db = SessionLocal()
    try:
        query = db.query(Playbook)

        if scope == "router_callable":
            query = query.filter(Playbook.router_callable == True)
        elif scope == "user_selectable":
            query = query.filter(Playbook.user_selectable == True)
        elif scope == "all":
            pass  # No filter
        else:
            raise ValueError(f"Unknown playbooks scope: {scope}")

        playbooks = query.all()
        return [
            {"value": pb.name, "label": pb.name}
            for pb in playbooks
        ]
    finally:
        db.close()


def _resolve_buildings(scope: str, context: EnumResolverContext) -> List[Dict[str, Any]]:
    """Resolve building options."""
    db = SessionLocal()
    try:
        query = db.query(Building)

        if scope == "current_city":
            if not context.city_id:
                return []
            query = query.filter(Building.CITYID == context.city_id)
        elif scope == "all":
            pass  # No filter
        else:
            raise ValueError(f"Unknown buildings scope: {scope}")

        buildings = query.all()
        return [
            {"value": b.BUILDINGID, "label": b.NAME or b.BUILDINGID}
            for b in buildings
        ]
    finally:
        db.close()


def _resolve_personas(scope: str, context: EnumResolverContext) -> List[Dict[str, Any]]:
    """Resolve persona options."""
    db = SessionLocal()
    try:
        query = db.query(AI)

        if scope == "current_city":
            if not context.city_id:
                return []
            query = query.filter(AI.CITYID == context.city_id)
        elif scope == "current_building":
            if not context.building_id:
                return []
            query = query.filter(AI.CURRENT_BUILDINGID == context.building_id)
        elif scope == "all":
            pass  # No filter
        else:
            raise ValueError(f"Unknown personas scope: {scope}")

        personas = query.all()
        return [
            {"value": p.AIID, "label": p.NAME or p.AIID}
            for p in personas
        ]
    finally:
        db.close()


def _resolve_items(scope: str, context: EnumResolverContext) -> List[Dict[str, Any]]:
    """Resolve item options."""
    db = SessionLocal()
    try:
        if scope == "current_building":
            if not context.building_id:
                return []
            # Items in building
            locations = db.query(ItemLocation).filter(
                ItemLocation.OWNER_KIND == "building",
                ItemLocation.OWNER_ID == context.building_id
            ).all()
            item_ids = [loc.ITEM_ID for loc in locations]
        elif scope == "persona_inventory":
            if not context.persona_id:
                return []
            # Items owned by persona
            locations = db.query(ItemLocation).filter(
                ItemLocation.OWNER_KIND == "persona",
                ItemLocation.OWNER_ID == context.persona_id
            ).all()
            item_ids = [loc.ITEM_ID for loc in locations]
        elif scope == "all":
            items = db.query(Item).all()
            return [
                {"value": i.ITEM_ID, "label": i.NAME or i.ITEM_ID}
                for i in items
            ]
        else:
            raise ValueError(f"Unknown items scope: {scope}")

        if not item_ids:
            return []

        items = db.query(Item).filter(Item.ITEM_ID.in_(item_ids)).all()
        return [
            {"value": i.ITEM_ID, "label": i.NAME or i.ITEM_ID}
            for i in items
        ]
    finally:
        db.close()


def _resolve_tools(scope: str, context: EnumResolverContext) -> List[Dict[str, Any]]:
    """Resolve tool options."""
    db = SessionLocal()
    try:
        if scope == "available":
            # Tools linked to current building
            if not context.building_id:
                return []

            links = db.query(BuildingToolLink).filter(
                BuildingToolLink.BUILDINGID == context.building_id
            ).all()
            tool_ids = [link.TOOLID for link in links]

            if not tool_ids:
                return []

            tools = db.query(Tool).filter(Tool.TOOLID.in_(tool_ids)).all()
        elif scope == "all":
            tools = db.query(Tool).all()
        else:
            raise ValueError(f"Unknown tools scope: {scope}")

        return [
            {"value": t.NAME, "label": t.DESCRIPTION or t.NAME}
            for t in tools
        ]
    finally:
        db.close()
