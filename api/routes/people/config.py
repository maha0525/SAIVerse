from fastapi import APIRouter, Depends, HTTPException
from typing import List, Optional
from api.deps import get_manager
from .models import AIConfigResponse, UpdateAIConfigRequest

router = APIRouter()

@router.get("/{persona_id}/config", response_model=AIConfigResponse)
def get_persona_config(persona_id: str, manager = Depends(get_manager)):
    """Get persona configuration."""
    details = manager.get_ai_details(persona_id)
    if not details:
        raise HTTPException(status_code=404, detail="Persona not found")
    
    return AIConfigResponse(
        name=details["AINAME"],
        description=details["DESCRIPTION"] or "",
        system_prompt=details["SYSTEMPROMPT"] or "",
        default_model=details["DEFAULT_MODEL"],
        lightweight_model=details.get("LIGHTWEIGHT_MODEL"),
        interaction_mode=details["INTERACTION_MODE"],
        avatar_path=details.get("AVATAR_IMAGE"),
        appearance_image_path=details.get("APPEARANCE_IMAGE_PATH"),
        home_city_id=details["HOME_CITYID"]
    )

@router.patch("/{persona_id}/config")
def update_persona_config(
    persona_id: str, 
    req: UpdateAIConfigRequest, 
    manager = Depends(get_manager)
):
    """Update persona configuration."""
    # We need current details to fill in missing fields for update_ai
    current = manager.get_ai_details(persona_id)
    if not current:
         raise HTTPException(status_code=404, detail="Persona not found")
    
    # Merge updates
    new_desc = req.description if req.description is not None else current["DESCRIPTION"]
    new_prompt = req.system_prompt if req.system_prompt is not None else current["SYSTEMPROMPT"]
    # For model fields: empty string means "clear to None", None means "no change"
    new_model = (req.default_model or None) if req.default_model is not None else current["DEFAULT_MODEL"]
    
    new_lightweight_model = (req.lightweight_model or None) if req.lightweight_model is not None else current.get("LIGHTWEIGHT_MODEL")
    new_mode = req.interaction_mode if req.interaction_mode is not None else current["INTERACTION_MODE"]
    new_avatar = req.avatar_path if req.avatar_path is not None else current.get("AVATAR_IMAGE")
    new_appearance = req.appearance_image_path if req.appearance_image_path is not None else current.get("APPEARANCE_IMAGE_PATH")
    
    # Ensure strings
    new_desc = new_desc or ""
    new_prompt = new_prompt or ""
    
    result = manager.update_ai(
        ai_id=persona_id,
        name=current["AINAME"], # Name update not supported here for safety/complexity
        description=new_desc,
        system_prompt=new_prompt,
        home_city_id=current["HOME_CITYID"],
        default_model=new_model,
        lightweight_model=new_lightweight_model,
        interaction_mode=new_mode,
        avatar_path=new_avatar, 
        avatar_upload=None,
        appearance_image_path=new_appearance,
    )
    
    if result.startswith("Error:"):
        raise HTTPException(status_code=400, detail=result)
        
    return {"success": True, "message": result}
