from fastapi import APIRouter, Depends, HTTPException
from typing import List, Optional
import json
import logging
from api.deps import get_manager, avatar_path_to_url
from database.models import UserAiLink
from .models import AIConfigResponse, MetaJudgmentConfig, UpdateAIConfigRequest

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

    # Phase 4-e: META_JUDGMENT_CONFIG (Text/JSON) → MetaJudgmentConfig
    meta_cfg_raw = details.get("META_JUDGMENT_CONFIG")
    meta_cfg_obj: Optional[MetaJudgmentConfig] = None
    if meta_cfg_raw:
        try:
            parsed = json.loads(meta_cfg_raw) if isinstance(meta_cfg_raw, str) else meta_cfg_raw
            if isinstance(parsed, dict):
                meta_cfg_obj = MetaJudgmentConfig(**parsed)
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            LOGGER.warning(
                "Invalid META_JUDGMENT_CONFIG JSON for %s: %s; returning None",
                persona_id, exc,
            )

    return AIConfigResponse(
        name=details["AINAME"],
        description=details["DESCRIPTION"] or "",
        system_prompt=details["SYSTEMPROMPT"] or "",
        default_model=details["DEFAULT_MODEL"],
        lightweight_model=details.get("LIGHTWEIGHT_MODEL"),
        vision_model=details.get("VISION_MODEL"),
        audio_model=details.get("AUDIO_MODEL"),
        video_model=details.get("VIDEO_MODEL"),
        memory_weave_model=details.get("MEMORY_WEAVE_MODEL"),
        autonomy_enabled=bool(details["AUTONOMY_ENABLED"]),
        chronicle_enabled=details.get("CHRONICLE_ENABLED", True),
        autonomous_chronicle_enabled=details.get("AUTONOMOUS_CHRONICLE_ENABLED", True),
        auto_recall_enabled=details.get("AUTO_RECALL_ENABLED", True),
        memory_weave_context=details.get("MEMORY_WEAVE_CONTEXT", True),
        memopedia_index_enabled=details.get("MEMOPEDIA_INDEX_ENABLED", True),
        core_memory_char_budget=details.get("CORE_MEMORY_CHAR_BUDGET"),
        spell_enabled=details.get("SPELL_ENABLED", True),
        realtime_info_enabled=details.get("REALTIME_INFO_ENABLED", True),
        avatar_path=avatar_path_to_url(details.get("AVATAR_IMAGE")),
        appearance_image_path=avatar_path_to_url(details.get("APPEARANCE_IMAGE_PATH")),
        home_city_id=details["HOME_CITYID"],
        linked_user_id=linked_user_id,
        meta_judgment_config=meta_cfg_obj,
        user_conv_timeout_minutes=details.get("USER_CONV_TIMEOUT_MINUTES"),
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
    new_vision_model = (req.vision_model or None) if req.vision_model is not None else current.get("VISION_MODEL")
    new_audio_model = (req.audio_model or None) if req.audio_model is not None else current.get("AUDIO_MODEL")
    new_video_model = (req.video_model or None) if req.video_model is not None else current.get("VIDEO_MODEL")
    new_memory_weave_model = (req.memory_weave_model or None) if req.memory_weave_model is not None else current.get("MEMORY_WEAVE_MODEL")
    new_autonomy_enabled = req.autonomy_enabled if req.autonomy_enabled is not None else current["AUTONOMY_ENABLED"]
    new_avatar = req.avatar_path if req.avatar_path is not None else current.get("AVATAR_IMAGE")
    new_appearance = req.appearance_image_path if req.appearance_image_path is not None else current.get("APPEARANCE_IMAGE_PATH")
    
    # Ensure strings
    new_desc = new_desc or ""
    new_prompt = new_prompt or ""
    
    # Phase 4-e: meta_judgment_config を dict 化して manager に渡す
    meta_cfg_dict = None
    if req.meta_judgment_config is not None:
        # exclude_none=True で未指定キーを落とし、META_JUDGMENT_CONFIG には
        # 明示的に与えられた項目だけを保存する。MetaLayer 側で既定値とマージされる。
        meta_cfg_dict = req.meta_judgment_config.model_dump(exclude_none=True)

    result = manager.update_ai(
        ai_id=persona_id,
        name=current["AINAME"], # Name update not supported here for safety/complexity
        description=new_desc,
        system_prompt=new_prompt,
        home_city_id=current["HOME_CITYID"],
        default_model=new_model,
        lightweight_model=new_lightweight_model,
        vision_model=new_vision_model,
        audio_model=new_audio_model,
        video_model=new_video_model,
        memory_weave_model=new_memory_weave_model,
        autonomy_enabled=new_autonomy_enabled,
        avatar_path=new_avatar,
        avatar_upload=None,
        appearance_image_path=new_appearance,
        chronicle_enabled=req.chronicle_enabled,
        autonomous_chronicle_enabled=req.autonomous_chronicle_enabled,
        auto_recall_enabled=req.auto_recall_enabled,
        memory_weave_context=req.memory_weave_context,
        memopedia_index_enabled=req.memopedia_index_enabled,
        core_memory_char_budget=req.core_memory_char_budget,
        spell_enabled=req.spell_enabled,
        realtime_info_enabled=req.realtime_info_enabled,
        meta_judgment_config=meta_cfg_dict,
        user_conv_timeout_minutes=req.user_conv_timeout_minutes,
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
    """手動の記憶整理 — 残す量より古い側を今すぐあらすじに畳む。

    arasuji_levels.md §13 裁定4 (2026-07-29): 範囲規則は自動 (応答後 Metabolism)
    と同一で、「発火 (予算超過) を待たずに今すぐ畳む」だけ。旧実装の「起点の
    全消し + 全未編纂の一括編纂」は撤去した — 起点は畳みで前進するだけで
    消えない。全量再編纂 (修復) は scripts/arasuji/ の領分。

    編纂の要否 (ENABLE_MEMORY_WEAVE_CONTEXT / persona の CHRONICLE_ENABLED) は
    畳み本体 (_run_metabolism_locked) が判定する。フロントの confirm() で同意
    済みのため、編纂の確認ダイアログは chronicle_force で回避される。
    """
    persona = manager.personas.get(persona_id)
    if not persona:
        raise HTTPException(status_code=404, detail="Persona not loaded")

    # NOTE: SEARuntime は manager.sea_runtime。manager.runtime は RuntimeService
    # (別物) で、かつて誤参照して AttributeError を握り潰し「完了しました」を
    # 返し続けていた (2026-07-04 修正)。
    lifecycle = getattr(getattr(manager, "sea_runtime", None), "session_lifecycle", None)
    if lifecycle is None:
        raise HTTPException(status_code=503, detail="SEA runtime not available")

    try:
        compaction_status = lifecycle.run_manual_compaction(persona)
    except Exception as exc:
        LOGGER.warning("[organize-memory] manual compaction failed: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Memory compaction failed: {exc}")

    # Recall embedding maintenance — 畳みの有無・成否と独立に未埋め込みの
    # Chronicle/ページ/Fragment を全件埋める (ローカル・無料)。
    try:
        lifecycle.ensure_recall_embeddings(persona)
    except Exception:
        LOGGER.warning("[organize-memory] embedding maintenance failed", exc_info=True)

    # head の再構築 (snapshot refresh) は run_manual_compaction が出口で持つ
    # (畳みが起きた "ok" は畳み本体が、それ以外は手動入口が発火する)。
    # かつてここにも無条件発火があり、"ok" のとき capture_all が二重に走って
    # いた (2026-09-01 に発火責務を session_lifecycle へ一本化して撤去)。

    # failed / deferred は「完了」ではない (Codex 2026-07-29 指摘: 失敗の成功偽装
    # の根治)。畳みは適用されておらず、再実行で再試行できる。
    return {
        "success": compaction_status in ("ok", "noop"),
        "compaction": compaction_status,
    }
