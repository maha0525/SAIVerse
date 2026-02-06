from fastapi import APIRouter, Depends, HTTPException
from typing import List, Optional
from api.deps import get_manager
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
        avatar_url = f"/api/chat/persona/{pid}/avatar"
        
        results.append(PersonaInfo(
            id=pid,
            name=persona.persona_name,
            avatar=avatar_url,
            status="available"
        ))
        
    return sorted(results, key=lambda x: x.name)

@router.get("/meta_playbooks", response_model=List[str])
def list_meta_playbooks(manager = Depends(get_manager)):
    """List user-selectable meta playbooks."""
    session = manager.SessionLocal()
    try:
        playbooks = (
            session.query(PlaybookModel)
            .filter(
                PlaybookModel.user_selectable == True,
                PlaybookModel.name.like("meta_%"),
            )
            .all()
        )
        return sorted([pb.name for pb in playbooks])
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        session.close()

@router.post("/summon/{persona_id}")
def summon_persona(persona_id: str, manager = Depends(get_manager)):
    """Summon a persona to the current location."""
    if not manager.user_current_building_id:
        raise HTTPException(status_code=400, detail="User location unknown")
        
    success, message = manager.summon_persona(persona_id)
    if not success:
        raise HTTPException(status_code=400, detail=message or "Summon failed")
        
    return {"success": True, "message": f"Summoned {persona_id}"}

@router.post("/dismiss/{persona_id}")
def dismiss_persona(persona_id: str, manager = Depends(get_manager)):
    """Dismiss a persona (send back to private room)."""
    # RuntimeService.end_conversation returns a string message or starts with "Error:"
    msg = manager.end_conversation(persona_id)
    
    if msg.startswith("Error"):
        raise HTTPException(status_code=400, detail=msg)
    
    return {"success": True, "message": msg}
