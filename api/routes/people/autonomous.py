from fastapi import APIRouter, Depends, HTTPException
from api.deps import get_manager
from .models import AutonomousStatusResponse

router = APIRouter()

@router.get("/{persona_id}/autonomous/status", response_model=AutonomousStatusResponse)
def get_autonomous_status(persona_id: str, manager = Depends(get_manager)):
    """Get autonomous operation status for a persona.

    認知モデル新基盤では AUTONOMY_ENABLED が単独で自律発話を制御する。
    MetaLayer + AutonomyManager は SAIVerseManager 起動時に常駐するため、
    system_running は常に True 扱い。
    旧 ``global_auto_enabled`` は ConversationManager (旧自律会話ループ)
    の停止スイッチに限定され、ここでは判定に使わない。
    """
    persona = manager.personas.get(persona_id)
    if not persona:
        raise HTTPException(status_code=404, detail="Persona not found")

    autonomy_enabled = bool(getattr(persona, "autonomy_enabled", True))

    return AutonomousStatusResponse(
        persona_id=persona_id,
        autonomy_enabled=autonomy_enabled,
        system_running=True,
        is_active=autonomy_enabled,
    )
