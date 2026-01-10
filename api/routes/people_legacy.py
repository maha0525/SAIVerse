from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from pydantic import BaseModel
from typing import List, Optional
from api.deps import get_manager
import json
import sqlite3
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from database.models import PersonaSchedule, Playbook as PlaybookModel, AI as AIModel, City as CityModel
import logging

LOGGER = logging.getLogger(__name__)

router = APIRouter()

class PersonaInfo(BaseModel):
    id: str
    name: str
    avatar: Optional[str] = None
    status: str # "available", "conversing", "dispatched"

class SummonRequest(BaseModel):
    persona_id: str

@router.get("/summonable", response_model=List[PersonaInfo])
def get_summonable_personas(manager = Depends(get_manager)):
    """List personas that can be summoned (not in current room, not dispatched)."""
    if not manager.user_current_building_id:
        return []

    here = manager.user_current_building_id
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

# -----------------------------------------------------------------------------
# Memory Management (Chat Logs)
# -----------------------------------------------------------------------------

class ThreadSummary(BaseModel):
    thread_id: str
    suffix: str
    preview: str
    active: bool

@router.get("/{persona_id}/threads", response_model=List[ThreadSummary])
def list_persona_threads(persona_id: str, manager = Depends(get_manager)):
    """List all conversation threads for a persona."""
    persona = manager.personas.get(persona_id)
    if not persona:
        raise HTTPException(status_code=404, detail=f"Persona {persona_id} not found")

    adapter = getattr(persona, "sai_memory", None)
    if not adapter or not adapter.is_ready():
        # Try to initialize a temporary adapter if not loaded (e.g. passive persona)
        # However, for now, we assume active personas or we might need a helper to load it.
        # But per `persona_settings.py`, we can create one on the fly.
        # Let's try to see if we can use the one from persona first.
        # If persona is just a "passive" object in manager.personas, it might be initialized.
        pass

    # If adapter is missing or not ready, we try to instantiate one temporarily
    # This mirrors `_acquire_adapter` logic in memory_settings_ui.py
    should_close = False
    if not adapter or not adapter.is_ready():
        from saiverse_memory import SAIMemoryAdapter
        try:
            adapter = SAIMemoryAdapter(persona_id)
            should_close = True
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to access memory: {e}")

    try:
        summaries = adapter.list_thread_summaries()
        return [
            ThreadSummary(
                thread_id=s["thread_id"],
                suffix=s["suffix"],
                preview=s["preview"] or "",
                active=s["active"]
            )
            for s in summaries
        ]
    finally:
        if should_close and adapter:
            adapter.close()

class MessageItem(BaseModel):
    id: str
    thread_id: str
    role: str
    content: str
    created_at: Optional[float] = None
    metadata: Optional[dict] = None

class MessagesResponse(BaseModel):
    items: List[MessageItem]
    total: int
    page: int
    page_size: int
    first_created_at: Optional[float] = None
    last_created_at: Optional[float] = None

@router.get("/{persona_id}/threads/{thread_id}/messages", response_model=MessagesResponse)
def list_thread_messages(
    persona_id: str, 
    thread_id: str, 
    page: int = 1, 
    page_size: int = 50, 
    manager = Depends(get_manager)
):
    """List messages in a thread with pagination."""
    
    # Logic to acquire adapter (deduplicate later if needed)
    persona = manager.personas.get(persona_id)
    adapter = getattr(persona, "sai_memory", None) if persona else None
    should_close = False
    
    if not adapter or not adapter.is_ready():
        from saiverse_memory import SAIMemoryAdapter
        try:
            adapter = SAIMemoryAdapter(persona_id)
            should_close = True
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to access memory: {e}")

    try:
        # Check total count first
        total = adapter.count_thread_messages(thread_id)
        if total == 0:
            return MessagesResponse(items=[], total=0, page=1, page_size=page_size)

        # Handle page=-1 (Last Page)
        if page == -1:
            import math
            page = math.ceil(total / page_size)
        
        if page < 1: page = 1

        # 0-indexed page for adapter
        offset_page = page - 1
        msgs = adapter.get_thread_messages(thread_id, page=offset_page, page_size=page_size)
        
        items = []
        for m in msgs:
            items.append(MessageItem(
                id=m["id"],
                thread_id=m["thread_id"],
                role=m["role"],
                content=m["content"],
                created_at=m["created_at"],
                metadata=m.get("metadata")
            ))
        
        # Get first and last timestamps for the thread
        first_created_at: Optional[float] = None
        last_created_at: Optional[float] = None
        try:
            # Get first message (oldest)
            first_msgs = adapter.get_thread_messages(thread_id, page=0, page_size=1)
            if first_msgs:
                first_created_at = first_msgs[0].get("created_at")
            # Get last message (newest) - use total to calculate last page
            import math
            last_page = max(0, math.ceil(total / 1) - 1)  # page_size=1 for last msg
            last_msgs = adapter.get_thread_messages(thread_id, page=last_page, page_size=1)
            if last_msgs:
                last_created_at = last_msgs[0].get("created_at")
        except Exception:
            pass  # Timestamps are optional, don't fail the request
            
        return MessagesResponse(
            items=items, 
            total=total, 
            page=page, 
            page_size=page_size,
            first_created_at=first_created_at,
            last_created_at=last_created_at,
        )
    finally:
        if should_close and adapter:
            adapter.close()

class UpdateMessageRequest(BaseModel):
    content: str

@router.patch("/{persona_id}/messages/{message_id}")
def update_message(
    persona_id: str, 
    message_id: str, 
    request: UpdateMessageRequest, 
    manager = Depends(get_manager)
):
    """Update message content."""
    persona = manager.personas.get(persona_id)
    adapter = getattr(persona, "sai_memory", None) if persona else None
    should_close = False
    
    if not adapter or not adapter.is_ready():
        from saiverse_memory import SAIMemoryAdapter
        try:
            adapter = SAIMemoryAdapter(persona_id)
            should_close = True
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to access memory: {e}")

    try:
        success = adapter.update_message_content(message_id, request.content)
        if not success:
            raise HTTPException(status_code=404, detail="Message not found or update failed")
        return {"success": True}
    finally:
        if should_close and adapter:
            adapter.close()

@router.delete("/{persona_id}/messages/{message_id}")
def delete_message(
    persona_id: str, 
    message_id: str, 
    manager = Depends(get_manager)
):
    """Delete a message."""
    persona = manager.personas.get(persona_id)
    adapter = getattr(persona, "sai_memory", None) if persona else None
    should_close = False
    
    if not adapter or not adapter.is_ready():
        from saiverse_memory import SAIMemoryAdapter
        try:
            adapter = SAIMemoryAdapter(persona_id)
            should_close = True
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to access memory: {e}")

    try:
        success = adapter.delete_message(message_id)
        if not success:
            raise HTTPException(status_code=404, detail="Message not found or delete failed")
        return {"success": True}
    finally:
        if should_close and adapter:
            adapter.close()


@router.delete("/{persona_id}/threads/{thread_id}")
def delete_thread(
    persona_id: str, 
    thread_id: str, 
    manager = Depends(get_manager)
):
    """Delete a thread."""
    persona = manager.personas.get(persona_id)
    adapter = getattr(persona, "sai_memory", None) if persona else None
    should_close = False
    
    if not adapter or not adapter.is_ready():
        from saiverse_memory import SAIMemoryAdapter
        try:
            adapter = SAIMemoryAdapter(persona_id)
            should_close = True
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to access memory: {e}")

    try:
        success = adapter.delete_thread(thread_id)
        if not success:
             raise HTTPException(status_code=404, detail="Thread not found or delete failed")
        
        return {"success": True}
    finally:
        if should_close and adapter:
            adapter.close()

@router.put("/{persona_id}/threads/{thread_id}/activate")
def set_active_thread(
    persona_id: str,
    thread_id: str,
    manager = Depends(get_manager)
):
    """Set a thread as the active thread for the persona."""
    persona = manager.personas.get(persona_id)
    adapter = getattr(persona, "sai_memory", None) if persona else None
    should_close = False
    
    if not adapter or not adapter.is_ready():
        from saiverse_memory import SAIMemoryAdapter
        try:
            adapter = SAIMemoryAdapter(persona_id)
            should_close = True
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to access memory: {e}")

    try:
        success = adapter.set_active_thread(thread_id)
        if not success:
            raise HTTPException(status_code=500, detail="Failed to set active thread")
        
        return {"success": True, "thread_id": thread_id}
    finally:
        if should_close and adapter:
            adapter.close()

# -----------------------------------------------------------------------------
# Memory Recall API
# -----------------------------------------------------------------------------

class MemoryRecallRequest(BaseModel):
    query: str
    topk: int = 4
    max_chars: int = 1200

class MemoryRecallResponse(BaseModel):
    query: str
    result: str
    topk: int
    max_chars: int

@router.post("/{persona_id}/recall", response_model=MemoryRecallResponse)
def memory_recall(
    persona_id: str,
    request: MemoryRecallRequest,
    manager = Depends(get_manager)
):
    """Execute memory recall, similar to the memory_recall tool."""
    query = request.query.strip()
    if not query:
        raise HTTPException(status_code=400, detail="Query is required")
    
    # Acquire adapter
    persona = manager.personas.get(persona_id)
    adapter = getattr(persona, "sai_memory", None) if persona else None
    should_close = False
    
    if not adapter or not adapter.is_ready():
        from saiverse_memory import SAIMemoryAdapter
        try:
            adapter = SAIMemoryAdapter(persona_id)
            should_close = True
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to access memory: {e}")

    try:
        result = adapter.recall_snippet(
            None,
            query_text=query,
            max_chars=request.max_chars,
            topk=request.topk,
        )
        return MemoryRecallResponse(
            query=query,
            result=result or "(no relevant memory)",
            topk=request.topk,
            max_chars=request.max_chars,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Memory recall failed: {e}")
    finally:
        if should_close and adapter:
            adapter.close()

# -----------------------------------------------------------------------------
# Configuration APIs
# -----------------------------------------------------------------------------

class AIConfigResponse(BaseModel):
    name: str
    description: str
    system_prompt: str
    default_model: Optional[str]
    lightweight_model: Optional[str] = None
    interaction_mode: str
    avatar_path: Optional[str] = None
    appearance_image_path: Optional[str] = None  # Visual context appearance image
    home_city_id: int

class UpdateAIConfigRequest(BaseModel):
    description: Optional[str] = None
    system_prompt: Optional[str] = None
    default_model: Optional[str] = None
    lightweight_model: Optional[str] = None
    interaction_mode: Optional[str] = None
    avatar_path: Optional[str] = None
    appearance_image_path: Optional[str] = None  # Visual context appearance image

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


# -----------------------------------------------------------------------------
# Autonomous Status API
# -----------------------------------------------------------------------------

class AutonomousStatusResponse(BaseModel):
    persona_id: str
    interaction_mode: str
    system_running: bool
    is_active: bool  # True if actually doing autonomous conversation


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


# -----------------------------------------------------------------------------

import shutil
import tempfile
from pathlib import Path
from fastapi import UploadFile, File, Form
from typing import Optional, List as TypingList

# Store parsed exports temporarily (in-memory cache for preview -> import flow)
_chatgpt_export_cache: dict = {}

class ConversationSummary(BaseModel):
    idx: int
    id: str
    conversation_id: Optional[str]
    title: str
    create_time: Optional[str]
    update_time: Optional[str]
    message_count: int
    preview: Optional[str]

class PreviewResponse(BaseModel):
    conversations: TypingList[ConversationSummary]
    cache_key: str
    total_count: int

@router.post("/{persona_id}/import/official/preview", response_model=PreviewResponse)
def preview_official_chatgpt(
    persona_id: str,
    file: UploadFile = File(...),
    manager = Depends(get_manager)
):
    """Preview ChatGPT export file and return conversation list for selection."""
    import uuid
    
    # 1. Save upload to temp file
    suffix = Path(file.filename).suffix if file.filename else ".tmp"
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        shutil.copyfileobj(file.file, tmp)
        tmp_path = Path(tmp.name)

    try:
        # 2. Parse using ChatGPTExport
        from tools.utilities.chatgpt_importer import ChatGPTExport
        export = ChatGPTExport(tmp_path)
        records = export.conversations
        
        if not records:
            return PreviewResponse(conversations=[], cache_key="", total_count=0)

        # 3. Build summaries
        summaries = []
        for idx, record in enumerate(records):
            summary_dict = record.to_summary_dict(preview_chars=120)
            summaries.append(ConversationSummary(
                idx=idx,
                id=summary_dict.get("id", "")[:12],
                conversation_id=summary_dict.get("conversation_id"),
                title=summary_dict.get("title", "")[:50],
                create_time=summary_dict.get("create_time"),
                update_time=summary_dict.get("update_time"),
                message_count=summary_dict.get("message_count", 0),
                preview=summary_dict.get("first_user_preview", "")[:100],
            ))
        
        # 4. Cache the export for later import
        cache_key = str(uuid.uuid4())
        _chatgpt_export_cache[cache_key] = {
            "export": export,
            "tmp_path": tmp_path,
            "persona_id": persona_id,
        }
        
        return PreviewResponse(
            conversations=summaries,
            cache_key=cache_key,
            total_count=len(records),
        )

    except Exception as e:
        # Cleanup on error
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except:
                pass
        raise HTTPException(status_code=400, detail=f"Preview failed: {str(e)}")


class ImportRequest(BaseModel):
    cache_key: str
    conversation_ids: TypingList[str]  # List of conversation_id or idx as string
    skip_embedding: bool = False


# -----------------------------------------------------------------------------
# Import Background Tasks
# -----------------------------------------------------------------------------

import threading

# Track import status per persona
_extension_import_status: dict = {}
_extension_import_lock = threading.Lock()

_official_import_status: dict = {}
_official_import_lock = threading.Lock()


def _run_extension_import_task(
    persona_id: str,
    tmp_path_str: str,
    skip_embedding: bool,
):
    """Background task to import extension export."""
    import logging
    from pathlib import Path
    
    tmp_path = Path(tmp_path_str)
    
    with _extension_import_lock:
        _extension_import_status[persona_id] = {
            "running": True, "progress": 0, "total": 0, "message": "Parsing file..."
        }
    
    try:
        # 1. Parse file
        from tools.utilities.chatlog_exporter_importer import parse_exporter_file
        conversation = parse_exporter_file(tmp_path)
        
        payloads = list(conversation.iter_memory_payloads())
        total = len(payloads)
        thread_suffix = conversation.identifier
        
        with _extension_import_lock:
            _extension_import_status[persona_id] = {
                "running": True, "progress": 0, "total": total,
                "message": f"Importing 0/{total} messages..."
            }
        
        # 2. Acquire adapter
        from saiverse_memory import SAIMemoryAdapter
        adapter = SAIMemoryAdapter(persona_id)
        
        try:
            msg_count = 0
            for i, payload in enumerate(payloads):
                if skip_embedding:
                    payload["embedding_chunks"] = 0
                adapter.append_persona_message(payload, thread_suffix=thread_suffix)
                msg_count += 1
                
                # Update progress every 10 messages
                if msg_count % 10 == 0:
                    with _extension_import_lock:
                        _extension_import_status[persona_id] = {
                            "running": True, "progress": msg_count, "total": total,
                            "message": f"Importing {msg_count}/{total} messages..."
                        }
            
            with _extension_import_lock:
                _extension_import_status[persona_id] = {
                    "running": False, "progress": msg_count, "total": total,
                    "message": f"Imported '{conversation.title}' ({msg_count} messages).",
                    "success": True, "title": conversation.title
                }
        finally:
            adapter.close()
            
    except Exception as e:
        logging.exception("Extension import failed for %s", persona_id)
        with _extension_import_lock:
            _extension_import_status[persona_id] = {
                "running": False, "message": f"Error: {str(e)}", "success": False
            }
    finally:
        # Cleanup temp file
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except:
                pass


def _run_official_import_task(
    persona_id: str,
    cache_key: str,
    conversation_ids: list,
    skip_embedding: bool,
):
    """Background task to import official ChatGPT export."""
    import logging
    
    with _official_import_lock:
        _official_import_status[persona_id] = {
            "running": True, "progress": 0, "total": 0, "message": "Starting import..."
        }
    
    try:
        # 1. Retrieve cached export
        cached = _chatgpt_export_cache.get(cache_key)
        if not cached:
            with _official_import_lock:
                _official_import_status[persona_id] = {
                    "running": False, "message": "Preview expired. Please upload again.",
                    "success": False
                }
            return
        
        export = cached["export"]
        tmp_path = cached["tmp_path"]
        records = export.conversations
        
        # 2. Resolve selected records
        selected_records = []
        for selector in conversation_ids:
            try:
                idx = int(selector)
                if 0 <= idx < len(records):
                    selected_records.append(records[idx])
                    continue
            except ValueError:
                pass
            for record in records:
                if record.conversation_id == selector or record.identifier == selector:
                    selected_records.append(record)
                    break
        
        if not selected_records:
            with _official_import_lock:
                _official_import_status[persona_id] = {
                    "running": False, "message": "No valid conversations found.",
                    "success": False
                }
            return
        
        # 3. Acquire adapter
        from saiverse_memory import SAIMemoryAdapter
        adapter = SAIMemoryAdapter(persona_id)
        
        try:
            total_conversations = len(selected_records)
            imported_count = 0
            msg_count = 0
            
            for conv_idx, record in enumerate(selected_records):
                payloads = list(record.iter_memory_payloads(include_roles=["user", "assistant"]))
                thread_suffix = record.conversation_id or record.identifier
                
                for payload in payloads:
                    meta = payload.get("metadata", {})
                    tags = meta.get("tags", [])
                    if "conversation" not in tags:
                        tags.append("conversation")
                    meta["tags"] = tags
                    if skip_embedding:
                        payload["embedding_chunks"] = 0
                    payload["metadata"] = meta
                    
                    adapter.append_persona_message(payload, thread_suffix=thread_suffix)
                    msg_count += 1
                
                imported_count += 1
                
                with _official_import_lock:
                    _official_import_status[persona_id] = {
                        "running": True, "progress": imported_count, "total": total_conversations,
                        "message": f"Imported {imported_count}/{total_conversations} conversations ({msg_count} messages)..."
                    }
            
            with _official_import_lock:
                _official_import_status[persona_id] = {
                    "running": False, "progress": imported_count, "total": total_conversations,
                    "message": f"Imported {imported_count} conversations ({msg_count} messages).",
                    "success": True, "conversations": imported_count, "messages": msg_count
                }
        finally:
            adapter.close()
            
    except Exception as e:
        import logging
        logging.exception("Official import failed for %s", persona_id)
        with _official_import_lock:
            _official_import_status[persona_id] = {
                "running": False, "message": f"Error: {str(e)}", "success": False
            }
    finally:
        # Clean up cache and temp file
        if cache_key in _chatgpt_export_cache:
            cached = _chatgpt_export_cache.pop(cache_key, None)
            if cached and cached.get("tmp_path"):
                try:
                    cached["tmp_path"].unlink()
                except:
                    pass


@router.post("/{persona_id}/import/official")
def import_official_chatgpt(
    persona_id: str,
    request: ImportRequest,
    background_tasks: BackgroundTasks,
    manager = Depends(get_manager)
):
    """Import selected ChatGPT conversations from a previously previewed export (background)."""
    cache_key = request.cache_key
    conversation_ids = request.conversation_ids
    skip_embedding = request.skip_embedding
    
    # Check if already running
    with _official_import_lock:
        status = _official_import_status.get(persona_id, {})
        if status.get("running"):
            raise HTTPException(status_code=409, detail="Import already in progress.")
    
    # 1. Retrieve cached export
    cached = _chatgpt_export_cache.get(cache_key)
    if not cached:
        raise HTTPException(status_code=400, detail="Preview expired or invalid. Please upload the file again.")
    
    export = cached["export"]
    
    # Verify persona matches
    if cached["persona_id"] != persona_id:
        raise HTTPException(status_code=400, detail="Persona ID mismatch")
    
    # 2. Validate selection
    if not conversation_ids:
        raise HTTPException(status_code=400, detail="No conversations selected for import.")
    
    records = export.conversations
    
    # 3. Count selected records for response
    selected_count = 0
    for selector in conversation_ids:
        try:
            idx = int(selector)
            if 0 <= idx < len(records):
                selected_count += 1
                continue
        except ValueError:
            pass
        for record in records:
            if record.conversation_id == selector or record.identifier == selector:
                selected_count += 1
                break
    
    if selected_count == 0:
        raise HTTPException(status_code=400, detail="No valid conversations found for the given selection.")
    
    # 4. Start background task
    background_tasks.add_task(
        _run_official_import_task,
        persona_id,
        cache_key,
        list(conversation_ids),
        skip_embedding,
    )
    
    return {
        "success": True,
        "message": f"Import started for {selected_count} conversations. Check status endpoint for progress.",
        "status": {"running": True, "progress": 0, "total": selected_count, "message": "Starting..."}
    }


class OfficialImportStatusResponse(BaseModel):
    running: bool
    progress: Optional[int] = None
    total: Optional[int] = None
    message: Optional[str] = None
    success: Optional[bool] = None
    conversations: Optional[int] = None
    messages: Optional[int] = None


@router.get("/{persona_id}/import/official/status", response_model=OfficialImportStatusResponse)
def get_official_import_status(persona_id: str, manager = Depends(get_manager)):
    """Get the status of official import task."""
    with _official_import_lock:
        status = _official_import_status.get(persona_id, {"running": False, "message": "No import task has been run."})
    return OfficialImportStatusResponse(**status)

@router.post("/{persona_id}/import/extension")
def import_extension_export(
    persona_id: str,
    file: UploadFile = File(...),
    skip_embedding: bool = Form(False),
    background_tasks: BackgroundTasks = None,
    manager = Depends(get_manager)
):
    """Import Chrome extension export (JSON or Markdown) in background."""
    from fastapi import BackgroundTasks as BT
    
    # Check if already running
    with _extension_import_lock:
        status = _extension_import_status.get(persona_id, {})
        if status.get("running"):
            raise HTTPException(status_code=409, detail="Import already in progress.")
    
    # 1. Save upload to temp file (will be deleted by background task)
    suffix = Path(file.filename).suffix if file.filename else ".tmp"
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        shutil.copyfileobj(file.file, tmp)
        tmp_path = Path(tmp.name)

    # 2. Validate file can be parsed (quick check)
    try:
        from tools.utilities.chatlog_exporter_importer import parse_exporter_file
        conversation = parse_exporter_file(tmp_path)
        title = conversation.title
        msg_count = len(list(conversation.iter_memory_payloads()))
    except Exception as e:
        if tmp_path.exists():
            tmp_path.unlink()
        raise HTTPException(status_code=400, detail=f"Invalid file: {str(e)}")
    
    # 3. Start background task
    background_tasks.add_task(
        _run_extension_import_task,
        persona_id,
        str(tmp_path),
        skip_embedding,
    )
    
    return {
        "success": True,
        "message": f"Import started for '{title}' ({msg_count} messages). Check status endpoint for progress.",
        "status": {"running": True, "progress": 0, "total": msg_count, "message": "Starting..."}
    }


class ExtensionImportStatusResponse(BaseModel):
    running: bool
    progress: Optional[int] = None
    total: Optional[int] = None
    message: Optional[str] = None
    success: Optional[bool] = None
    title: Optional[str] = None


@router.get("/{persona_id}/import/extension/status", response_model=ExtensionImportStatusResponse)
def get_extension_import_status(persona_id: str, manager = Depends(get_manager)):
    """Get the status of extension import task."""
    with _extension_import_lock:
        status = _extension_import_status.get(persona_id, {"running": False, "message": "No import task has been run."})
    return ExtensionImportStatusResponse(**status)


# -----------------------------------------------------------------------------
# Re-embed API
# -----------------------------------------------------------------------------

# Track re-embed status per persona
_reembed_status: dict = {}
_reembed_lock = threading.Lock()

class ReembedRequest(BaseModel):
    force: bool = False  # If true, re-embed all messages regardless of current status

class ReembedStatusResponse(BaseModel):
    running: bool
    progress: Optional[int] = None
    total: Optional[int] = None
    message: Optional[str] = None

def _run_reembed_task(persona_id: str, force: bool):
    """Background task to run re-embedding."""
    from pathlib import Path
    from sai_memory.config import load_settings
    from sai_memory.memory.chunking import chunk_text
    from sai_memory.memory.recall import Embedder
    from sai_memory.memory.storage import get_message, init_db, replace_message_embeddings
    import json
    import logging
    
    with _reembed_lock:
        _reembed_status[persona_id] = {"running": True, "progress": 0, "total": 0, "message": "Starting..."}
    
    try:
        db_path = Path.home() / ".saiverse" / "personas" / persona_id / "memory.db"
        if not db_path.exists():
            with _reembed_lock:
                _reembed_status[persona_id] = {"running": False, "message": "Database not found"}
            return
        
        settings = load_settings()
        embedder = Embedder(
            model=settings.embed_model or "",
            local_model_path=str(Path(settings.embed_model_path).expanduser().resolve()) if settings.embed_model_path else None,
            model_dim=settings.embed_model_dim,
        )
        expected_dim = embedder.model.embedding_size
        
        conn = init_db(str(db_path), check_same_thread=False)
        
        try:
            if force:
                target_ids = set()
                for (mid,) in conn.execute("SELECT DISTINCT id FROM messages"):
                    target_ids.add(mid)
            else:
                all_message_ids = set()
                for (mid,) in conn.execute("SELECT DISTINCT id FROM messages"):
                    all_message_ids.add(mid)
                
                embedded_ids = set()
                bad_ids = set()
                for mid, _, vec_json in conn.execute(
                    "SELECT message_id, chunk_index, vector FROM message_embeddings"
                ):
                    embedded_ids.add(mid)
                    try:
                        vec = json.loads(vec_json)
                        if len(vec) != expected_dim:
                            bad_ids.add(mid)
                    except json.JSONDecodeError:
                        bad_ids.add(mid)
                
                missing_ids = all_message_ids - embedded_ids
                target_ids = missing_ids | bad_ids
            
            if not target_ids:
                with _reembed_lock:
                    _reembed_status[persona_id] = {"running": False, "progress": 0, "total": 0, "message": "No messages need re-embedding."}
                return
            
            target_list = list(target_ids)
            total = len(target_list)
            with _reembed_lock:
                _reembed_status[persona_id] = {"running": True, "progress": 0, "total": total, "message": f"Processing 0/{total}..."}
            
            fixed = 0
            for i, mid in enumerate(target_list):
                msg = get_message(conn, mid)
                if msg is None or not msg.content:
                    continue
                chunks = chunk_text(
                    msg.content,
                    min_chars=settings.chunk_min_chars,
                    max_chars=settings.chunk_max_chars,
                )
                payload = [c.strip() for c in chunks if c and c.strip()]
                if not payload:
                    payload = [msg.content.strip()]
                if not payload:
                    continue
                vectors = embedder.embed(payload, is_query=False)
                replace_message_embeddings(conn, mid, vectors)
                fixed += 1
                
                # Update progress every 10 messages
                if fixed % 10 == 0:
                    with _reembed_lock:
                        _reembed_status[persona_id] = {"running": True, "progress": fixed, "total": total, "message": f"Processing {fixed}/{total}..."}
            
            with _reembed_lock:
                _reembed_status[persona_id] = {"running": False, "progress": fixed, "total": total, "message": f"Re-embedded {fixed} messages."}
        finally:
            conn.close()
            
    except Exception as e:
        logging.exception("Re-embed task failed for %s", persona_id)
        with _reembed_lock:
            _reembed_status[persona_id] = {"running": False, "message": f"Error: {str(e)}"}

@router.post("/{persona_id}/reembed")
def reembed_persona_memory(
    persona_id: str,
    request: ReembedRequest,
    background_tasks: BackgroundTasks,
    manager = Depends(get_manager)
):
    """Start re-embedding messages in the background."""
    from pathlib import Path
    
    # Check if already running
    with _reembed_lock:
        status = _reembed_status.get(persona_id, {})
        if status.get("running"):
            return {"success": False, "message": "Re-embed already in progress.", "status": status}
    
    # Verify database exists
    db_path = Path.home() / ".saiverse" / "personas" / persona_id / "memory.db"
    if not db_path.exists():
        raise HTTPException(status_code=404, detail=f"Memory database not found for {persona_id}")
    
    # Start background task
    background_tasks.add_task(_run_reembed_task, persona_id, request.force)
    
    return {
        "success": True, 
        "message": "Re-embed task started. Check status endpoint for progress.",
        "status": {"running": True, "progress": 0, "total": 0, "message": "Starting..."}
    }

@router.get("/{persona_id}/reembed/status", response_model=ReembedStatusResponse)
def get_reembed_status(persona_id: str, manager = Depends(get_manager)):
    """Get the status of the re-embed task."""
    with _reembed_lock:
        status = _reembed_status.get(persona_id, {"running": False, "message": "No task has been run."})
    return ReembedStatusResponse(**status)

# -----------------------------------------------------------------------------
# Memopedia APIs
# -----------------------------------------------------------------------------

@router.get("/{persona_id}/memopedia/tree")
def get_memopedia_tree(persona_id: str, manager = Depends(get_manager)):
    """Get the Memopedia knowledge tree."""
    persona = manager.personas.get(persona_id)
    adapter = getattr(persona, "sai_memory", None) if persona else None
    should_close = False
    
    if not adapter or not adapter.is_ready():
        from saiverse_memory import SAIMemoryAdapter
        try:
            adapter = SAIMemoryAdapter(persona_id)
            should_close = True
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to access memory: {e}")

    try:
        from sai_memory.memopedia import Memopedia
        memopedia = Memopedia(adapter.conn)
        # Verify tables exist (memopedia init does this, but just in case)
        return memopedia.get_tree()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Memopedia error: {e}")
    finally:
        if should_close and adapter:
            adapter.close()

@router.get("/{persona_id}/memopedia/pages/{page_id}")
def get_memopedia_page(persona_id: str, page_id: str, manager = Depends(get_manager)):
    """Get a Memopedia page content as Markdown."""
    persona = manager.personas.get(persona_id)
    adapter = getattr(persona, "sai_memory", None) if persona else None
    should_close = False
    
    if not adapter or not adapter.is_ready():
        from saiverse_memory import SAIMemoryAdapter
        try:
            adapter = SAIMemoryAdapter(persona_id)
            should_close = True
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to access memory: {e}")

    try:
        from sai_memory.memopedia import Memopedia
        memopedia = Memopedia(adapter.conn)
        md = memopedia.get_page_markdown(page_id)
        return {"content": md}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Memopedia error: {e}")
    finally:
        if should_close and adapter:
            adapter.close()

@router.get("/{persona_id}/memopedia/pages/{page_id}/history")
def get_memopedia_page_history(persona_id: str, page_id: str, limit: int = 50, manager = Depends(get_manager)):
    """Get the edit history for a Memopedia page."""
    persona = manager.personas.get(persona_id)
    adapter = getattr(persona, "sai_memory", None) if persona else None
    should_close = False
    
    if not adapter or not adapter.is_ready():
        from saiverse_memory import SAIMemoryAdapter
        try:
            adapter = SAIMemoryAdapter(persona_id)
            should_close = True
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to access memory: {e}")

    try:
        from sai_memory.memopedia import Memopedia
        memopedia = Memopedia(adapter.conn)
        history = memopedia.get_page_edit_history(page_id, limit=limit)
        return {
            "history": [
                {
                    "id": h.id,
                    "page_id": h.page_id,
                    "edited_at": h.edited_at,
                    "diff_text": h.diff_text,
                    "ref_start_message_id": h.ref_start_message_id,
                    "ref_end_message_id": h.ref_end_message_id,
                    "edit_type": h.edit_type,
                    "edit_source": h.edit_source,
                }
                for h in history
            ]
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Memopedia error: {e}")
    finally:
        if should_close and adapter:
            adapter.close()


class UpdateMemopediaPageRequest(BaseModel):
    title: Optional[str] = None
    summary: Optional[str] = None
    content: Optional[str] = None
    keywords: Optional[List[str]] = None


@router.put("/{persona_id}/memopedia/pages/{page_id}")
def update_memopedia_page(
    persona_id: str,
    page_id: str,
    request: UpdateMemopediaPageRequest,
    manager = Depends(get_manager)
):
    """Update a Memopedia page (title, summary, content, keywords)."""
    # Prevent editing root pages
    if page_id.startswith("root_"):
        raise HTTPException(status_code=400, detail="Cannot edit root pages")
    
    persona = manager.personas.get(persona_id)
    adapter = getattr(persona, "sai_memory", None) if persona else None
    should_close = False
    
    if not adapter or not adapter.is_ready():
        from saiverse_memory import SAIMemoryAdapter
        try:
            adapter = SAIMemoryAdapter(persona_id)
            should_close = True
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to access memory: {e}")

    try:
        from sai_memory.memopedia import Memopedia
        memopedia = Memopedia(adapter.conn)
        updated = memopedia.update_page(
            page_id,
            title=request.title,
            summary=request.summary,
            content=request.content,
            keywords=request.keywords,
            edit_source="manual_ui",
        )
        if not updated:
            raise HTTPException(status_code=404, detail="Page not found")
        return {
            "success": True,
            "page": {
                "id": updated.id,
                "title": updated.title,
                "summary": updated.summary,
                "content": updated.content,
                "keywords": updated.keywords,
            }
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Memopedia error: {e}")
    finally:
        if should_close and adapter:
            adapter.close()


@router.delete("/{persona_id}/memopedia/pages/{page_id}")
def delete_memopedia_page(persona_id: str, page_id: str, manager = Depends(get_manager)):
    """Delete a Memopedia page (soft delete)."""
    # Prevent deleting root pages
    if page_id.startswith("root_"):
        raise HTTPException(status_code=400, detail="Cannot delete root pages")
    
    persona = manager.personas.get(persona_id)
    adapter = getattr(persona, "sai_memory", None) if persona else None
    should_close = False
    
    if not adapter or not adapter.is_ready():
        from saiverse_memory import SAIMemoryAdapter
        try:
            adapter = SAIMemoryAdapter(persona_id)
            should_close = True
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to access memory: {e}")

    try:
        from sai_memory.memopedia import Memopedia
        memopedia = Memopedia(adapter.conn)
        success = memopedia.delete_page(page_id, edit_source="manual_ui")
        if not success:
            raise HTTPException(status_code=404, detail="Page not found or could not be deleted")
        return {"success": True}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Memopedia error: {e}")
    finally:
        if should_close and adapter:
            adapter.close()

# -----------------------------------------------------------------------------
# Schedule APIs
# -----------------------------------------------------------------------------

class ScheduleItem(BaseModel):
    schedule_id: int
    schedule_type: str
    meta_playbook: str
    description: Optional[str]
    priority: int
    enabled: bool
    days_of_week: Optional[List[int]] = None
    time_of_day: Optional[str] = None
    scheduled_datetime: Optional[datetime] = None
    interval_seconds: Optional[int] = None
    last_executed_at: Optional[datetime] = None
    completed: bool

class CreateScheduleRequest(BaseModel):
    schedule_type: str # periodic, oneshot, interval
    meta_playbook: str
    description: str = ""
    priority: int = 0
    enabled: bool = True
    # periodic
    days_of_week: Optional[List[int]] = None # 0=Mon, 6=Sun
    time_of_day: Optional[str] = None # HH:MM
    # oneshot
    scheduled_datetime: Optional[str] = None # "YYYY-MM-DD HH:MM" (in persona TZ)
    # interval
    interval_seconds: Optional[int] = None

def _get_persona_timezone(manager, persona_id: str) -> ZoneInfo:
    session = manager.SessionLocal()
    try:
        persona_model = session.query(AIModel).filter(AIModel.AIID == persona_id).first()
        if not persona_model:
            return ZoneInfo("UTC")
        city_model = session.query(CityModel).filter(CityModel.CITYID == persona_model.HOME_CITYID).first()
        if not city_model or not city_model.TIMEZONE:
            return ZoneInfo("UTC")
        return ZoneInfo(city_model.TIMEZONE)
    except:
        return ZoneInfo("UTC")
    finally:
        session.close()

@router.get("/{persona_id}/schedules", response_model=List[ScheduleItem])
def list_schedules(persona_id: str, manager = Depends(get_manager)):
    """List schedules for a persona."""
    session = manager.SessionLocal()
    try:
        schedules = (
            session.query(PersonaSchedule)
            .filter(PersonaSchedule.PERSONA_ID == persona_id)
            .order_by(PersonaSchedule.PRIORITY.desc(), PersonaSchedule.SCHEDULE_ID.desc())
            .all()
        )
        results = []
        for s in schedules:
            days = None
            if s.DAYS_OF_WEEK:
                try:
                    days = json.loads(s.DAYS_OF_WEEK)
                except: pass
            
            results.append(ScheduleItem(
                schedule_id=s.SCHEDULE_ID,
                schedule_type=s.SCHEDULE_TYPE,
                meta_playbook=s.META_PLAYBOOK,
                description=s.DESCRIPTION,
                priority=s.PRIORITY,
                enabled=s.ENABLED,
                days_of_week=days,
                time_of_day=s.TIME_OF_DAY,
                scheduled_datetime=s.SCHEDULED_DATETIME,
                interval_seconds=s.INTERVAL_SECONDS,
                last_executed_at=s.LAST_EXECUTED_AT,
                completed=s.COMPLETED
            ))
        return results
    finally:
        session.close()

@router.post("/{persona_id}/schedules")
def create_schedule(
    persona_id: str,
    req: CreateScheduleRequest,
    manager = Depends(get_manager)
):
    """Create a new schedule."""
    session = manager.SessionLocal()
    try:
        new_schedule = PersonaSchedule(
            PERSONA_ID=persona_id,
            SCHEDULE_TYPE=req.schedule_type,
            META_PLAYBOOK=req.meta_playbook,
            DESCRIPTION=req.description,
            PRIORITY=req.priority,
            ENABLED=req.enabled,
        )

        if req.schedule_type == "periodic":
            if req.days_of_week:
                new_schedule.DAYS_OF_WEEK = json.dumps(req.days_of_week)
            new_schedule.TIME_OF_DAY = req.time_of_day

        elif req.schedule_type == "oneshot":
            if req.scheduled_datetime:
                try:
                    tz = _get_persona_timezone(manager, persona_id)
                    dt_naive = datetime.strptime(req.scheduled_datetime, "%Y-%m-%d %H:%M")
                    dt_local = dt_naive.replace(tzinfo=tz)
                    dt_utc = dt_local.astimezone(timezone.utc)
                    new_schedule.SCHEDULED_DATETIME = dt_utc
                except ValueError as e:
                    raise HTTPException(status_code=400, detail=f"Invalid datetime format: YYYY-MM-DD HH:MM")

        elif req.schedule_type == "interval":
            new_schedule.INTERVAL_SECONDS = req.interval_seconds

        session.add(new_schedule)
        session.commit()
        return {"success": True, "schedule_id": new_schedule.SCHEDULE_ID}
    except Exception as e:
        session.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        session.close()

@router.post("/{persona_id}/schedules/{schedule_id}/toggle")
def toggle_schedule(
    persona_id: str,
    schedule_id: int,
    manager = Depends(get_manager)
):
    """Toggle schedule enabled status."""
    session = manager.SessionLocal()
    try:
        schedule = session.query(PersonaSchedule).filter(
            PersonaSchedule.SCHEDULE_ID == schedule_id,
            PersonaSchedule.PERSONA_ID == persona_id
        ).first()
        if not schedule:
            raise HTTPException(status_code=404, detail="Schedule not found")
        
        schedule.ENABLED = not schedule.ENABLED
        session.commit()
        return {"success": True, "enabled": schedule.ENABLED}
    finally:
        session.close()

class UpdateScheduleRequest(BaseModel):
    schedule_type: Optional[str] = None
    meta_playbook: Optional[str] = None
    description: Optional[str] = None
    priority: Optional[int] = None
    enabled: Optional[bool] = None
    days_of_week: Optional[List[int]] = None
    time_of_day: Optional[str] = None
    scheduled_datetime: Optional[str] = None  # "YYYY-MM-DD HH:MM" (in persona TZ)
    interval_seconds: Optional[int] = None

@router.put("/{persona_id}/schedules/{schedule_id}")
def update_schedule(
    persona_id: str,
    schedule_id: int,
    req: UpdateScheduleRequest,
    manager = Depends(get_manager)
):
    """Update an existing schedule."""
    session = manager.SessionLocal()
    try:
        schedule = session.query(PersonaSchedule).filter(
            PersonaSchedule.SCHEDULE_ID == schedule_id,
            PersonaSchedule.PERSONA_ID == persona_id
        ).first()
        if not schedule:
            raise HTTPException(status_code=404, detail="Schedule not found")

        # Update basic fields if provided
        if req.schedule_type is not None:
            schedule.SCHEDULE_TYPE = req.schedule_type
        if req.meta_playbook is not None:
            schedule.META_PLAYBOOK = req.meta_playbook
        if req.description is not None:
            schedule.DESCRIPTION = req.description
        if req.priority is not None:
            schedule.PRIORITY = req.priority
        if req.enabled is not None:
            schedule.ENABLED = req.enabled

        # Update type-specific fields based on schedule type
        schedule_type = req.schedule_type if req.schedule_type is not None else schedule.SCHEDULE_TYPE

        if schedule_type == "periodic":
            if req.days_of_week is not None:
                schedule.DAYS_OF_WEEK = json.dumps(req.days_of_week) if req.days_of_week else None
            if req.time_of_day is not None:
                schedule.TIME_OF_DAY = req.time_of_day
            # Clear non-periodic fields
            schedule.SCHEDULED_DATETIME = None
            schedule.INTERVAL_SECONDS = None
            schedule.COMPLETED = False

        elif schedule_type == "oneshot":
            if req.scheduled_datetime is not None:
                try:
                    tz = _get_persona_timezone(manager, persona_id)
                    dt_naive = datetime.strptime(req.scheduled_datetime, "%Y-%m-%d %H:%M")
                    dt_local = dt_naive.replace(tzinfo=tz)
                    dt_utc = dt_local.astimezone(timezone.utc)
                    schedule.SCHEDULED_DATETIME = dt_utc
                except ValueError:
                    raise HTTPException(status_code=400, detail="Invalid datetime format: YYYY-MM-DD HH:MM")
            # Clear non-oneshot fields
            schedule.DAYS_OF_WEEK = None
            schedule.TIME_OF_DAY = None
            schedule.INTERVAL_SECONDS = None

        elif schedule_type == "interval":
            if req.interval_seconds is not None:
                schedule.INTERVAL_SECONDS = req.interval_seconds
            # Clear non-interval fields
            schedule.DAYS_OF_WEEK = None
            schedule.TIME_OF_DAY = None
            schedule.SCHEDULED_DATETIME = None
            schedule.COMPLETED = False

        session.commit()
        return {"success": True, "schedule_id": schedule.SCHEDULE_ID}
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        session.close()

@router.delete("/{persona_id}/schedules/{schedule_id}")
def delete_schedule(
    persona_id: str,
    schedule_id: int,
    manager = Depends(get_manager)
):
    """Delete a schedule."""
    session = manager.SessionLocal()
    try:
        schedule = session.query(PersonaSchedule).filter(
            PersonaSchedule.SCHEDULE_ID == schedule_id,
            PersonaSchedule.PERSONA_ID == persona_id
        ).first()
        if not schedule:
            raise HTTPException(status_code=404, detail="Schedule not found")

        session.delete(schedule)
        session.commit()
        return {"success": True}
    finally:
        session.close()

# -----------------------------------------------------------------------------
# Task Management APIs
# -----------------------------------------------------------------------------

class TaskStep(BaseModel):
    id: str
    position: int
    title: str
    description: Optional[str]
    status: str
    notes: Optional[str]
    updated_at: str

class TaskRecordModel(BaseModel):
    id: str
    title: str
    goal: str
    summary: str
    status: str
    priority: str
    active_step_id: Optional[str]
    updated_at: str
    steps: List[TaskStep]

class CreateTaskRequest(BaseModel):
    title: str
    goal: str
    summary: str
    notes: Optional[str] = None
    priority: str = "normal"
    steps: List[dict] # {title, description, ...}

class UpdateTaskStatusRequest(BaseModel):
    status: str
    reason: Optional[str] = None

@router.get("/{persona_id}/tasks", response_model=List[TaskRecordModel])
def list_tasks(persona_id: str, manager = Depends(get_manager)):
    """List all tasks for a persona."""
    from persona.tasks.storage import TaskStorage
    base_dir = manager.saiverse_home
    storage = TaskStorage(persona_id, base_dir=base_dir)
    try:
        tasks = storage.list_tasks(include_steps=True)
        return [
            TaskRecordModel(
                id=t.id,
                title=t.title,
                goal=t.goal,
                summary=t.summary,
                status=t.status,
                priority=t.priority,
                active_step_id=t.active_step_id,
                updated_at=t.updated_at,
                steps=[
                    TaskStep(
                        id=s.id,
                        position=s.position,
                        title=s.title,
                        description=s.description,
                        status=s.status,
                        notes=s.notes,
                        updated_at=s.updated_at
                    )
                    for s in t.steps
                ]
            )
            for t in tasks
        ]
    finally:
        storage.close()

@router.post("/{persona_id}/tasks")
def create_task(persona_id: str, req: CreateTaskRequest, manager = Depends(get_manager)):
    """Create a new task."""
    from persona.tasks.storage import TaskStorage
    base_dir = manager.saiverse_home
    storage = TaskStorage(persona_id, base_dir=base_dir)
    try:
        task = storage.create_task(
            title=req.title,
            goal=req.goal,
            summary=req.summary,
            notes=req.notes,
            steps=req.steps,
            priority=req.priority,
            origin="manual"
        )
        return {"success": True, "task_id": task.id}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        storage.close()

@router.patch("/{persona_id}/tasks/{task_id}")
def update_task_status(
    persona_id: str, 
    task_id: str, 
    req: UpdateTaskStatusRequest, 
    manager = Depends(get_manager)
):
    """Update task status."""
    from persona.tasks.storage import TaskStorage, TaskNotFoundError
    base_dir = manager.saiverse_home
    storage = TaskStorage(persona_id, base_dir=base_dir)
    try:
        storage.update_task_status(task_id, status=req.status, actor="user", reason=req.reason)
        return {"success": True}
    except TaskNotFoundError:
        raise HTTPException(status_code=404, detail="Task not found")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        storage.close()

@router.get("/{persona_id}/tasks/{task_id}/history")
def get_task_history(persona_id: str, task_id: str, manager = Depends(get_manager)):
    """Get history for a specific task."""
    from persona.tasks.storage import TaskStorage
    base_dir = manager.saiverse_home
    storage = TaskStorage(persona_id, base_dir=base_dir)
    try:
        history = storage.fetch_history(task_id, limit=50) # Limit to 50 for now
        return [
            {
                "id": h.id,
                "event_type": h.event_type,
                "payload": h.payload,
                "actor": h.actor,
                "created_at": h.created_at
            }
            for h in history
        ]
    finally:
        storage.close()



# -----------------------------------------------------------------------------
# Inventory APIs
# -----------------------------------------------------------------------------

class InventoryItem(BaseModel):
    id: str
    name: str
    type: str # document, picture, object, etc.
    description: str
    file_path: Optional[str] = None
    created_at: datetime

@router.get("/{persona_id}/items", response_model=List[InventoryItem])
def list_persona_items(persona_id: str, manager = Depends(get_manager)):
    """List items held by a persona."""
    from database.models import Item as ItemModel, ItemLocation

    session = manager.SessionLocal()
    try:
        # Query items where location owner is this persona
        items = (
            session.query(ItemModel)
            .join(ItemLocation, ItemModel.ITEM_ID == ItemLocation.ITEM_ID)
            .filter(
                ItemLocation.OWNER_KIND == "persona",
                ItemLocation.OWNER_ID == persona_id
            )
            .order_by(ItemModel.NAME)
            .all()
        )

        return [
            InventoryItem(
                id=i.ITEM_ID,
                name=i.NAME,
                type=i.TYPE,
                description=i.DESCRIPTION,
                file_path=i.FILE_PATH,
                created_at=i.CREATED_AT
            )
            for i in items
        ]
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        session.close()

# -----------------------------------------------------------------------------
# Arasuji (Episode Summary) APIs
# -----------------------------------------------------------------------------

class ArasujiStatsResponse(BaseModel):
    max_level: int
    counts_by_level: dict  # {level: count}
    total_count: int

class ArasujiEntryItem(BaseModel):
    id: str
    level: int
    content: str
    start_time: Optional[int] = None
    end_time: Optional[int] = None
    message_count: int
    is_consolidated: bool
    created_at: Optional[int] = None
    source_ids: List[str] = []
    # For level 1: message number range (1-indexed, for build_arasuji.py --offset)
    source_start_num: Optional[int] = None  # first message number
    source_end_num: Optional[int] = None    # last message number

class ArasujiListResponse(BaseModel):
    entries: List[ArasujiEntryItem]
    total: int
    level_filter: Optional[int] = None

def _get_arasuji_db(persona_id: str):
    """Get database connection for arasuji tables."""
    from pathlib import Path
    import sqlite3
    from sai_memory.arasuji.storage import init_arasuji_tables

    db_path = Path.home() / ".saiverse" / "personas" / persona_id / "memory.db"
    if not db_path.exists():
        return None
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    init_arasuji_tables(conn)
    return conn

@router.get("/{persona_id}/arasuji/stats", response_model=ArasujiStatsResponse)
def get_arasuji_stats(persona_id: str, manager = Depends(get_manager)):
    """Get arasuji statistics for a persona."""
    from sai_memory.arasuji.storage import count_entries_by_level, get_max_level

    conn = _get_arasuji_db(persona_id)
    if not conn:
        raise HTTPException(status_code=404, detail=f"Memory database not found for {persona_id}")

    try:
        counts = count_entries_by_level(conn)
        max_level = get_max_level(conn)
        total = sum(counts.values())
        return ArasujiStatsResponse(
            max_level=max_level,
            counts_by_level=counts,
            total_count=total
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get arasuji stats: {e}")
    finally:
        conn.close()

def _get_message_number_map(conn: sqlite3.Connection) -> dict:
    """Build a mapping of message_id -> row number (1-indexed) matching build_arasuji.py order.

    Messages are ordered globally by created_at ASC across all threads.
    This ensures consistent chronological ordering where message #1 is always the oldest.
    """
    cur = conn.execute("""
        SELECT id FROM messages ORDER BY created_at ASC
    """)

    msg_num_map = {}
    for msg_num, (msg_id,) in enumerate(cur.fetchall(), start=1):
        msg_num_map[msg_id] = msg_num

    return msg_num_map

@router.get("/{persona_id}/arasuji", response_model=ArasujiListResponse, tags=["Chronicle"])
def list_arasuji_entries(
    persona_id: str,
    level: Optional[int] = None,
    limit: int = 500,
    manager = Depends(get_manager)
):
    """List Chronicle entries for a persona (part of Memory Weave)."""
    from sai_memory.arasuji.storage import get_entries_by_level

    conn = _get_arasuji_db(persona_id)
    if not conn:
        raise HTTPException(status_code=404, detail=f"Memory database not found for {persona_id}")

    try:
        if level is not None:
            entries = get_entries_by_level(conn, level, order_by_time=True)
        else:
            # Get all entries ordered by level DESC, then start_time ASC
            cur = conn.execute("""
                SELECT id, level, content, source_ids_json, start_time, end_time,
                       source_count, message_count, parent_id, is_consolidated, created_at
                FROM arasuji_entries
                ORDER BY level DESC, start_time ASC
                LIMIT ?
            """, (limit,))
            from sai_memory.arasuji.storage import _row_to_entry
            entries = [_row_to_entry(row) for row in cur.fetchall()]

        # Build message number map for level 1 entries
        msg_num_map = None
        has_level1 = any(e.level == 1 for e in entries)
        if has_level1:
            try:
                msg_num_map = _get_message_number_map(conn)
            except Exception:
                pass  # Table might not exist or be empty

        items = []
        for e in entries:
            source_start_num = None
            source_end_num = None

            if e.level == 1 and e.source_ids and msg_num_map:
                # Calculate message number range
                nums = [msg_num_map.get(sid) for sid in e.source_ids if sid in msg_num_map]
                if nums:
                    source_start_num = min(nums)
                    source_end_num = max(nums)

            items.append(ArasujiEntryItem(
                id=e.id,
                level=e.level,
                content=e.content,
                start_time=e.start_time,
                end_time=e.end_time,
                message_count=e.message_count,
                is_consolidated=e.is_consolidated,
                created_at=e.created_at,
                source_ids=e.source_ids,
                source_start_num=source_start_num,
                source_end_num=source_end_num,
            ))

        return ArasujiListResponse(
            entries=items,
            total=len(items),
            level_filter=level
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to list Chronicle entries: {e}")
    finally:
        conn.close()

@router.get("/{persona_id}/arasuji/{entry_id}", response_model=ArasujiEntryItem, tags=["Chronicle"])
def get_arasuji_entry(
    persona_id: str,
    entry_id: str,
    manager = Depends(get_manager)
):
    """Get a detailed Chronicle entry by ID."""
    from sai_memory.arasuji.storage import get_entry

    conn = _get_arasuji_db(persona_id)
    if not conn:
        raise HTTPException(status_code=404, detail=f"Memory database not found for {persona_id}")

    try:
        entry = get_entry(conn, entry_id)
        if not entry:
            raise HTTPException(status_code=404, detail=f"Chronicle entry {entry_id} not found")

        # Calculate message number range for level 1
        source_start_num = None
        source_end_num = None
        if entry.level == 1 and entry.source_ids:
            try:
                msg_num_map = _get_message_number_map(conn)
                nums = [msg_num_map.get(sid) for sid in entry.source_ids if sid in msg_num_map]
                if nums:
                    source_start_num = min(nums)
                    source_end_num = max(nums)
            except Exception:
                pass

        return ArasujiEntryItem(
            id=entry.id,
            level=entry.level,
            content=entry.content,
            start_time=entry.start_time,
            end_time=entry.end_time,
            message_count=entry.message_count,
            is_consolidated=entry.is_consolidated,
            created_at=entry.created_at,
            source_ids=entry.source_ids,
            source_start_num=source_start_num,
            source_end_num=source_end_num,
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get Chronicle entry: {e}")
    finally:
        conn.close()

@router.delete("/{persona_id}/arasuji/{entry_id}")
def delete_arasuji_entry(persona_id: str, entry_id: str, manager = Depends(get_manager)):
    """Delete a Chronicle entry and unmark child entries as consolidated."""
    from sai_memory.arasuji.storage import delete_entry, get_entry

    conn = _get_arasuji_db(persona_id)
    if not conn:
        raise HTTPException(status_code=404, detail=f"Memory database not found for {persona_id}")

    try:
        # Get entry first to find child entries
        entry = get_entry(conn, entry_id)
        if not entry:
            raise HTTPException(status_code=404, detail=f"Chronicle entry {entry_id} not found")

        # If this is a consolidated entry (level 2+), unmark children as consolidated
        if entry.level >= 2 and entry.source_ids:
            placeholders = ",".join("?" for _ in entry.source_ids)
            conn.execute(
                f"""
                UPDATE arasuji_entries
                SET is_consolidated = 0, parent_id = NULL
                WHERE id IN ({placeholders})
                """,
                entry.source_ids
            )
            conn.commit()

        # Now delete the entry
        success = delete_entry(conn, entry_id)
        if not success:
            raise HTTPException(status_code=404, detail=f"Chronicle entry {entry_id} not found")

        return {
            "success": True,
            "message": f"Deleted Chronicle entry {entry_id}",
            "children_unmarked": len(entry.source_ids) if entry.level >= 2 else 0
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to delete Chronicle entry: {e}")
    finally:
        conn.close()


class SourceMessageItem(BaseModel):
    id: str
    role: str
    content: str
    created_at: int


@router.get("/{persona_id}/arasuji/{entry_id}/messages", response_model=List[SourceMessageItem], tags=["Chronicle"])
def get_arasuji_messages(
    persona_id: str,
    entry_id: str,
    manager = Depends(get_manager)
):
    """Get the source raw messages for a Level 1 Chronicle entry."""
    from sai_memory.arasuji.storage import get_entry
    from sai_memory.memory.storage import get_message

    conn = _get_arasuji_db(persona_id)
    if not conn:
        raise HTTPException(status_code=404, detail=f"Memory database not found for {persona_id}")

    try:
        entry = get_entry(conn, entry_id)
        if not entry:
            raise HTTPException(status_code=404, detail=f"Arasuji entry {entry_id} not found")

        if entry.level != 1:
            raise HTTPException(status_code=400, detail="This endpoint only works for level-1 arasuji entries")

        # Fetch messages by IDs
        messages = []
        for msg_id in entry.source_ids:
            msg = get_message(conn, msg_id)
            if msg:
                messages.append(SourceMessageItem(
                    id=msg.id,
                    role=msg.role,
                    content=msg.content or "",
                    created_at=msg.created_at,
                ))

        # Sort by created_at
        messages.sort(key=lambda m: m.created_at)
        return messages

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get source messages: {e}")
    finally:
        conn.close()

@router.post("/{persona_id}/arasuji/{entry_id}/regenerate", tags=["Chronicle"])
async def regenerate_arasuji_entry(
    persona_id: str,
    entry_id: str,
    manager = Depends(get_manager)
):
    """Regenerate a specific Chronicle entry while preserving parent relationship.
    
    This endpoint delegates to the storage layer which handles:
    1. Saving parent relationship
    2. Deleting and updating parent
    3. Regenerating with LLM
    4. Restoring parent relationship
    """
    from sai_memory.arasuji.storage import regenerate_entry
    
    conn = _get_arasuji_db(persona_id)
    
    try:
        new_entry = regenerate_entry(conn, entry_id)
        
        if not new_entry:
            raise HTTPException(
                status_code=500,
                detail="Failed to regenerate entry"
            )
        
        return {
            "success": True,
            "old_entry_id": entry_id,
            "new_entry_id": new_entry.id,
            "content": new_entry.content
        }
    
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        LOGGER.exception(f"[regenerate] Exception during regeneration: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to regenerate entry: {e}"
        )
    finally:
        conn.close()
