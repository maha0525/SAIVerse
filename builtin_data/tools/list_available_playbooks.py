from __future__ import annotations

import json
import logging
from typing import Optional

_log = logging.getLogger(__name__)

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from database.paths import default_db_path
from database.models import Base, Playbook, PlaybookPermission
from tools.context import get_active_persona_id, get_active_manager, get_auto_mode
from tools.core import ToolSchema


def list_available_playbooks(persona_id: Optional[str] = None, building_id: Optional[str] = None) -> str:
    """List playbooks available for router selection.

    Returns router_callable=True playbooks that the persona has access to based on scope as a JSON string.
    Filters out playbooks whose city-scoped permission is ``blocked`` or ``user_only``.
    In auto_mode, also filters out ``ask_every_time`` playbooks (no user present to confirm).
    """
    # Get context if not provided
    if not persona_id:
        persona_id = get_active_persona_id()

    manager = get_active_manager()

    if not building_id:
        if manager:
            # Get current building from in-memory PersonaCore
            try:
                persona_obj = manager.personas.get(persona_id)
                if persona_obj:
                    building_id = persona_obj.current_building_id
            except Exception:
                _log.warning("Failed to get building_id for persona %s", persona_id, exc_info=True)

    db_path = default_db_path()
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)

    # Check developer mode
    developer_mode = False
    if manager and hasattr(manager, "state"):
        developer_mode = getattr(manager.state, "developer_mode", False)

    # City-scoped permission overrides
    city_id = getattr(manager, "city_id", None) if manager else None
    auto_mode = get_auto_mode()

    with Session() as session:
        # Get all router_callable playbooks
        query = session.query(Playbook).filter(Playbook.router_callable == True)
        if not developer_mode:
            query = query.filter(Playbook.dev_only == False)
        all_playbooks = query.all()

        # Load permission overrides for this city in one query
        permissions: dict[str, str] = {}
        if city_id is not None:
            try:
                perm_rows = (
                    session.query(PlaybookPermission)
                    .filter(PlaybookPermission.CITYID == city_id)
                    .all()
                )
                permissions = {r.playbook_name: r.permission_level for r in perm_rows}
            except Exception:
                _log.warning("Failed to load playbook permissions for city %s", city_id, exc_info=True)

        available = []
        for pb in all_playbooks:
            scope = (pb.scope or "public").lower()

            # Check visibility
            if scope == "public":
                include = True
            elif scope == "personal":
                include = (pb.created_by_persona_id == persona_id)
            elif scope == "building":
                include = (pb.building_id == building_id)
            else:
                include = False

            # Check city-scoped permission
            if include:
                perm = permissions.get(pb.name, "ask_every_time")
                if perm in ("blocked", "user_only"):
                    include = False
                elif perm == "ask_every_time" and auto_mode:
                    include = False  # No user present to confirm in auto mode

            if include:
                available.append({
                    "name": pb.name,
                    "description": pb.description or ""
                })

        # Sort by name
        available.sort(key=lambda x: x["name"])

        result = json.dumps(available, ensure_ascii=False)
        return result


def schema() -> ToolSchema:
    return ToolSchema(
        name="list_available_playbooks",
        description="List playbooks available for router selection based on persona and building context.",
        parameters={
            "type": "object",
            "properties": {
                "persona_id": {
                    "type": "string",
                    "description": "Persona ID (optional, defaults to current context)"
                },
                "building_id": {
                    "type": "string",
                    "description": "Building ID (optional, defaults to current context)"
                },
            },
            "required": [],
        },
        result_type="string",
    )
