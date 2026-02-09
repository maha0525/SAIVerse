"""URI resolve API endpoint — saiverse:// URIを解決してコンテンツを返す。"""

from fastapi import APIRouter, Depends, HTTPException, Query
from typing import Optional

from api.deps import get_manager

router = APIRouter()


@router.get("/resolve")
def resolve_uri(
    uri: str = Query(..., description="saiverse:// URI to resolve"),
    persona_id: Optional[str] = Query(None, description="Persona ID for resolving 'self' URIs"),
    manager=Depends(get_manager),
):
    """Resolve a saiverse:// URI and return its content.

    Returns:
        JSON with uri, content, content_type, and metadata.
    """
    from uri_resolver import UriResolver

    if not uri.startswith("saiverse://"):
        raise HTTPException(status_code=400, detail="URI must start with saiverse://")

    # Fallback: if persona_id not provided and URI contains "self",
    # try to infer from current building's first persona
    if not persona_id and "//self/" in uri:
        building_id = getattr(manager, 'user_current_building_id', None)
        if building_id and hasattr(manager, 'occupancy_manager'):
            occupants = manager.occupancy_manager.occupants.get(building_id, set())
            for oid in sorted(occupants):
                if oid in manager.personas:
                    persona_id = oid
                    break

    resolver = UriResolver(manager=manager)

    try:
        result = resolver.resolve(uri, persona_id=persona_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    if result.content_type == "error":
        status = 403 if result.metadata.get("error") == "access_denied" else 404
        raise HTTPException(status_code=status, detail=result.content)

    return {
        "uri": result.uri,
        "content": result.content,
        "content_type": result.content_type,
        "metadata": result.metadata,
    }
