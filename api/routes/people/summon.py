from fastapi import APIRouter, Depends, HTTPException
from typing import List, Optional
from api.deps import get_manager, avatar_path_to_url
from database.models import Playbook as PlaybookModel
from .models import PersonaInfo, SummonRequest

router = APIRouter()

@router.get("/summonable", response_model=List[PersonaInfo])
def get_summonable_personas(building_id: Optional[str] = None, manager = Depends(get_manager)):
    """List personas that can be summoned (not in current room, not dispatched)."""
    here = building_id or manager.user_current_building_id
    if not here:
        return []
    results = []
    
    # Access personas directly from manager (RuntimeService)
    # Ensure we look at all personas
    for pid, persona in manager.personas.items():
        # Check if dispatched
        if getattr(persona, "is_dispatched", False):
            continue
            
        # Check if already here
        if persona.current_building_id == here:
            continue
            
        # Get avatar url
        avatar_url = avatar_path_to_url(persona.avatar_image)
        
        results.append(PersonaInfo(
            id=pid,
            name=persona.persona_name,
            avatar=avatar_url,
            status="available"
        ))
        
    return sorted(results, key=lambda x: x.name)

@router.get("/spells")
def list_available_spells(persona_id: Optional[str] = None, manager = Depends(get_manager)):
    """List Spells available for schedule / pre_spells UI selection.

    Returns a list of ``{name, display_name, description}`` for every Spell
    registered in ``SPELL_TOOL_SCHEMAS`` with ``spell_visible=True``. When
    ``persona_id`` is provided, applies per-persona availability filters
    (``availability_check`` callable + MCP per-persona gating) so only Spells
    actually invokable for that persona are returned.
    """
    from tools import SPELL_TOOL_SCHEMAS

    try:
        from tools.mcp_client import get_mcp_manager
        mcp_mgr = get_mcp_manager()
    except Exception:
        mcp_mgr = None

    results = []
    for name, schema in SPELL_TOOL_SCHEMAS.items():
        if not getattr(schema, "spell_visible", True):
            continue
        if mcp_mgr is not None and persona_id is not None:
            if not mcp_mgr.is_tool_available_for_persona(name, persona_id):
                continue
        availability_check = getattr(schema, "availability_check", None)
        if availability_check is not None:
            try:
                if not availability_check(persona_id):
                    continue
            except Exception:
                continue
        results.append({
            "name": name,
            "display_name": getattr(schema, "spell_display_name", "") or name,
            "description": schema.description or "",
        })
    results.sort(key=lambda x: x["name"])
    return results


@router.get("/meta_playbooks", response_model=List[str])
def list_meta_playbooks(manager = Depends(get_manager)):
    """List user-selectable meta playbooks for schedule / summon dialogs.

    Phase 3 移行で Pulse-root として走る Playbook の主流が ``meta_*`` (旧
    ``meta_user`` / ``meta_user_manual``) から ``track_*`` (``track_user_conversation``
    等) に移ったため、name prefix での絞り込みは廃止。``user_selectable=true``
    フラグのみを判定軸にする。
    """
    session = manager.SessionLocal()
    try:
        playbooks = (
            session.query(PlaybookModel)
            .filter(PlaybookModel.user_selectable == True)
            .all()
        )
        return sorted([pb.name for pb in playbooks])
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        session.close()

@router.post("/summon/{persona_id}")
def summon_persona(
    persona_id: str,
    building_id: Optional[str] = None,
    manager = Depends(get_manager),
):
    """Summon a persona to the target building.

    C-1 閲覧モード以降、 frontend は viewing 中の building を ``building_id``
    に渡す。 省略時はサーバ側の実滞在地 ``user_current_building_id`` にフォール
    バックする。
    """
    target = building_id or manager.user_current_building_id
    if not target:
        raise HTTPException(status_code=400, detail="Target building unknown")

    success, message = manager.summon_persona(persona_id, target)
    if not success:
        raise HTTPException(status_code=400, detail=message or "Summon failed")

    return {"success": True, "message": f"Summoned {persona_id}"}

@router.post("/dismiss/{persona_id}")
def dismiss_persona(
    persona_id: str,
    building_id: Optional[str] = None,
    manager = Depends(get_manager),
):
    """Dismiss a persona (send back to private room).

    C-1 閲覧モード以降、 frontend は viewing 中の building を ``building_id``
    に渡す。 省略時はサーバ側の実滞在地にフォールバックする。
    """
    msg = manager.end_conversation(persona_id, building_id)

    if msg.startswith("Error"):
        raise HTTPException(status_code=400, detail=msg)

    return {"success": True, "message": msg}
