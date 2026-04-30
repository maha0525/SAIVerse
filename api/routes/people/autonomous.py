from fastapi import APIRouter, Depends, HTTPException
from api.deps import get_manager
from .models import AutonomousStatusResponse

router = APIRouter()

@router.get("/{persona_id}/autonomous/status", response_model=AutonomousStatusResponse)
def get_autonomous_status(persona_id: str, manager = Depends(get_manager)):
    """Get autonomous operation status for a persona.

    認知モデル新基盤では ACTIVITY_STATE が単独で自律発話を制御する
    (intent A v0.9 表)。MetaLayer + AutonomyManager は SAIVerseManager
    起動時に常駐するため、system_running は常に True 扱い。
    旧 ``global_auto_enabled`` は ConversationManager (旧自律会話ループ)
    の停止スイッチに限定され、ここでは判定に使わない。
    """
    persona = manager.personas.get(persona_id)
    if not persona:
        raise HTTPException(status_code=404, detail="Persona not found")

    activity_state = getattr(persona, "activity_state", "Idle")
    is_active = activity_state == "Active"

    return AutonomousStatusResponse(
        persona_id=persona_id,
        activity_state=activity_state,
        system_running=True,
        is_active=is_active,
    )
