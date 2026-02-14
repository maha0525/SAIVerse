from fastapi import APIRouter, Depends, HTTPException
from typing import List, Optional
import logging
from api.deps import get_manager, avatar_path_to_url
from database.models import AI, UserAiLink
from .models import AIConfigResponse, UpdateAIConfigRequest

LOGGER = logging.getLogger(__name__)

router = APIRouter()

@router.get("/{persona_id}/config", response_model=AIConfigResponse)
def get_persona_config(persona_id: str, manager = Depends(get_manager)):
    """Get persona configuration."""
    details = manager.get_ai_details(persona_id)
    if not details:
        raise HTTPException(status_code=404, detail="Persona not found")

    # Get linked user ID (first linked user)
    session = manager.SessionLocal()
    try:
        link = session.query(UserAiLink).filter(UserAiLink.AIID == persona_id).first()
        linked_user_id = link.USERID if link else None
    finally:
        session.close()

    return AIConfigResponse(
        name=details["AINAME"],
        description=details["DESCRIPTION"] or "",
        system_prompt=details["SYSTEMPROMPT"] or "",
        default_model=details["DEFAULT_MODEL"],
        lightweight_model=details.get("LIGHTWEIGHT_MODEL"),
        interaction_mode=details["INTERACTION_MODE"],
        chronicle_enabled=details.get("CHRONICLE_ENABLED", True),
        memory_weave_context=details.get("MEMORY_WEAVE_CONTEXT", True),
        avatar_path=avatar_path_to_url(details.get("AVATAR_IMAGE")),
        appearance_image_path=avatar_path_to_url(details.get("APPEARANCE_IMAGE_PATH")),
        home_city_id=details["HOME_CITYID"],
        linked_user_id=linked_user_id,
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
        chronicle_enabled=req.chronicle_enabled,
        memory_weave_context=req.memory_weave_context,
    )

    if result.startswith("Error:"):
        raise HTTPException(status_code=400, detail=result)

    # Extract LLM warnings if present
    llm_warning = None
    if "[WARNING:LLM]" in result:
        parts = result.split("[WARNING:LLM]", 1)
        result = parts[0].strip()
        llm_warning = parts[1].strip()

    # Handle linked user update
    if req.linked_user_id is not None:
        session = manager.SessionLocal()
        try:
            # Remove existing links for this persona
            session.query(UserAiLink).filter(UserAiLink.AIID == persona_id).delete()

            # Add new link if not clearing (0 = clear)
            if req.linked_user_id > 0:
                new_link = UserAiLink(USERID=req.linked_user_id, AIID=persona_id)
                session.add(new_link)

            session.commit()

            # Update PersonaCore's linked_user_name if persona is loaded
            persona = manager.personas.get(persona_id)
            if persona:
                if req.linked_user_id > 0:
                    from database.models import User
                    user = session.query(User).filter(User.USERID == req.linked_user_id).first()
                    persona.linked_user_name = user.USERNAME if user else "the user"
                else:
                    persona.linked_user_name = "the user"
        except Exception as e:
            session.rollback()
            raise HTTPException(status_code=500, detail=f"Failed to update linked user: {e}")
        finally:
            session.close()

    response = {"success": True, "message": result}
    if llm_warning:
        response["warning"] = llm_warning
    return response


@router.post("/{persona_id}/organize-memory")
def organize_persona_memory(persona_id: str, manager=Depends(get_manager)):
    """Clear all metabolism anchors and trigger metabolism (Chronicle generation + anchor reset).

    This forces the persona to re-evaluate its conversation history,
    generating Chronicle entries for any unprocessed messages and
    resetting the metabolism anchor to a minimal window.
    """
    import os

    persona = manager.personas.get(persona_id)
    if not persona:
        raise HTTPException(status_code=404, detail="Persona not loaded")

    # 1. Clear all anchors from DB
    db = manager.SessionLocal()
    try:
        ai_row = db.query(AI).filter_by(AIID=persona_id).first()
        if ai_row:
            ai_row.METABOLISM_ANCHORS = None
            db.commit()
    except Exception as exc:
        LOGGER.warning("[organize-memory] Failed to clear anchors: %s", exc)
        db.rollback()
    finally:
        db.close()

    # 2. Clear in-memory anchor
    history_mgr = getattr(persona, "history_manager", None)
    if history_mgr:
        history_mgr.metabolism_anchor_message_id = None

    # 3. Generate Chronicle for unprocessed messages
    chronicle_generated = False
    memory_weave_enabled = os.getenv("ENABLE_MEMORY_WEAVE_CONTEXT", "").lower() in ("true", "1")
    # Check per-persona Chronicle toggle
    if memory_weave_enabled:
        db2 = manager.SessionLocal()
        try:
            ai_check = db2.query(AI).filter_by(AIID=persona_id).first()
            if ai_check and not ai_check.CHRONICLE_ENABLED:
                memory_weave_enabled = False
        finally:
            db2.close()
    if memory_weave_enabled and hasattr(manager, "runtime") and manager.runtime:
        try:
            manager.runtime._generate_chronicle(persona)
            chronicle_generated = True
        except Exception as exc:
            LOGGER.warning("[organize-memory] Chronicle generation failed: %s", exc)

    return {
        "success": True,
        "anchors_cleared": True,
        "chronicle_generated": chronicle_generated,
    }
