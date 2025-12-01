from __future__ import annotations

import json
from typing import Optional, Tuple

from pydantic import ValidationError

from sea.playbook_models import PlaybookSchema, PlaybookValidationError, validate_playbook_graph

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from database.paths import default_db_path
from database.models import Base, Playbook
from tools.context import get_active_persona_id
from tools.defs import ToolSchema


def save_playbook(
    name: str,
    description: str,
    scope: str = "public",
    created_by_persona_id: Optional[str] = None,
    building_id: Optional[str] = None,
    playbook_json: str = "",
    router_callable: Optional[bool] = None,
) -> Tuple[str, None, None]:
    """Persist a playbook definition to the database.

    - name: unique playbook identifier
    - scope: public / personal / building
    - created_by_persona_id: owner persona (required for personal scope)
    - building_id: building scope target (required for building scope)
    - playbook_json: full PlaybookSchema JSON string (must include nodes/start_node/input_schema)
    - router_callable: if True, playbook can be called from router (defaults to value in JSON or False)
    """

    scope = (scope or "public").lower()
    if scope not in {"public", "personal", "building"}:
        raise ValueError("scope must be public/personal/building")

    owner = created_by_persona_id or get_active_persona_id()
    if scope == "personal" and not owner:
        raise ValueError("personal scope requires created_by_persona_id")
    if scope == "building" and not building_id:
        raise ValueError("building scope requires building_id")

    try:
        raw_data = json.loads(playbook_json)
    except Exception as exc:
        raise ValueError(f"playbook_json must be valid JSON: {exc}")

    try:
        parsed = PlaybookSchema(**raw_data)
    except ValidationError as exc:
        raise ValueError(f"playbook_json does not match PlaybookSchema: {exc}")

    try:
        validate_playbook_graph(parsed)
    except PlaybookValidationError as exc:
        raise ValueError(f"playbook validation failed: {exc}")

    normalized_nodes = parsed.dict()
    schema_payload = {
        "name": normalized_nodes.get("name", name),
        "description": normalized_nodes.get("description", description),
        "input_schema": normalized_nodes.get("input_schema", []),
        "start_node": normalized_nodes.get("start_node"),
    }
    nodes_json = json.dumps(normalized_nodes, ensure_ascii=False)
    schema_json = json.dumps(schema_payload, ensure_ascii=False)

    # Determine router_callable: explicit param > JSON value > False
    if router_callable is None:
        router_callable = normalized_nodes.get("router_callable", False)

    db_path = default_db_path()
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)

    with Session() as session:
        existing = session.query(Playbook).filter(Playbook.name == name).first()
        if existing:
            existing.description = description
            existing.scope = scope
            existing.created_by_persona_id = owner
            existing.building_id = building_id
            existing.schema_json = schema_json
            existing.nodes_json = nodes_json
            existing.router_callable = router_callable
        else:
            record = Playbook(
                name=name,
                description=description,
                scope=scope,
                created_by_persona_id=owner,
                building_id=building_id,
                schema_json=schema_json,
                nodes_json=nodes_json,
                router_callable=router_callable,
            )
            session.add(record)
        session.commit()

    return f"Saved playbook '{name}' (scope={scope}).", None, None


def schema() -> ToolSchema:
    return ToolSchema(
        name="save_playbook",
        description="Save or update a playbook definition into the shared database.",
        parameters={
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "description": {"type": "string"},
                "scope": {"type": "string", "enum": ["public", "personal", "building"], "default": "public"},
                "created_by_persona_id": {"type": "string"},
                "building_id": {"type": "string"},
                "playbook_json": {"type": "string", "description": "Full PlaybookSchema JSON."},
                "router_callable": {"type": "boolean", "description": "If true, playbook can be called from router. Defaults to value in JSON or False."},
            },
            "required": ["name", "description", "playbook_json"],
        },
        result_type="string",
    )

