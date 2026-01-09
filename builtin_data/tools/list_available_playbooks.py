from __future__ import annotations

import json
from typing import Optional

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from database.paths import default_db_path
from database.models import Base, Playbook
from tools.context import get_active_persona_id, get_active_manager
from tools.defs import ToolSchema


def list_available_playbooks(persona_id: Optional[str] = None, building_id: Optional[str] = None) -> str:
    """List playbooks available for router selection.

    Returns router_callable=True playbooks that the persona has access to based on scope as a JSON string.
    """
    # Get context if not provided
    if not persona_id:
        persona_id = get_active_persona_id()

    if not building_id:
        manager = get_active_manager()
        if manager:
            # Try to get current building from manager/persona
            # This is a best-effort; if unavailable, we only return public playbooks
            try:
                from database.operations import get_db_session
                session = get_db_session()
                # Get persona's current location
                from database.models import AI
                ai_record = session.query(AI).filter(AI.AIID == persona_id).first()
                if ai_record:
                    building_id = ai_record.CURRENT_LOCATION
                session.close()
            except Exception:
                pass

    db_path = default_db_path()
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)

    with Session() as session:
        # Get all router_callable playbooks
        query = session.query(Playbook).filter(Playbook.router_callable == True)
        all_playbooks = query.all()

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
