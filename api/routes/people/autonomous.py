from fastapi import APIRouter, Depends, HTTPException
from api.deps import get_manager
from .models import AutonomousStatusResponse

router = APIRouter()

@router.get("/{persona_id}/autonomous/status", response_model=AutonomousStatusResponse)
def get_autonomous_status(persona_id: str, manager = Depends(get_manager)):
    """Get autonomous operation status for a persona."""
    persona = manager.personas.get(persona_id)
    if not persona:
        raise HTTPException(status_code=404, detail="Persona not found")
    
    # autonomous_conversation_running is the system-wide flag
    system_running = manager.state.autonomous_conversation_running
    
    # interaction_mode determines if this persona will actually speak
    interaction_mode = getattr(persona, "interaction_mode", "auto")
    is_active = system_running and interaction_mode == "auto"
    
    return AutonomousStatusResponse(
        persona_id=persona_id,
        interaction_mode=interaction_mode,
        system_running=system_running,
        is_active=is_active
    )
