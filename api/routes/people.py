from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import List, Optional
from api.deps import get_manager
import json
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from database.models import PersonaSchedule, Playbook as PlaybookModel, AI as AIModel, City as CityModel

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
            
        return MessagesResponse(items=items, total=total, page=page, page_size=page_size)
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
    home_city_id: int

class UpdateAIConfigRequest(BaseModel):
    description: Optional[str] = None
    system_prompt: Optional[str] = None
    default_model: Optional[str] = None
    lightweight_model: Optional[str] = None
    interaction_mode: Optional[str] = None
    avatar_path: Optional[str] = None

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
    new_model = req.default_model if req.default_model is not None else current["DEFAULT_MODEL"]
    
    new_lightweight_model = req.lightweight_model if req.lightweight_model is not None else current.get("LIGHTWEIGHT_MODEL")
    new_mode = req.interaction_mode if req.interaction_mode is not None else current["INTERACTION_MODE"]
    new_avatar = req.avatar_path if req.avatar_path is not None else current.get("AVATAR_IMAGE")
    
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
        avatar_upload=None
    )
    
    if result.startswith("Error:"):
        raise HTTPException(status_code=400, detail=result)
        
    return {"success": True, "message": result}


# -----------------------------------------------------------------------------
# Import APIs
# -----------------------------------------------------------------------------

import shutil
import tempfile
from pathlib import Path
from fastapi import UploadFile, File

@router.post("/{persona_id}/import/official")
def import_official_chatgpt(
    persona_id: str,
    file: UploadFile = File(...),
    manager = Depends(get_manager)
):
    """Import official ChatGPT conversations.json or ZIP export."""
    # 1. Save upload to temp file
    suffix = Path(file.filename).suffix if file.filename else ".tmp"
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        shutil.copyfileobj(file.file, tmp)
        tmp_path = Path(tmp.name)

    try:
        # 2. Parse using ChatGPTExport
        from tools.utilities.chatgpt_importer import ChatGPTExport
        # This will raise if file is invalid
        export = ChatGPTExport(tmp_path)
        records = export.conversations
        if not records:
            return {"count": 0, "message": "No conversations found in file."}

        # 3. acquire adapter
        persona = manager.personas.get(persona_id)
        adapter = getattr(persona, "sai_memory", None) if persona else None
        should_close = False
        adapter_ready = adapter and adapter.is_ready()
        
        if not adapter_ready:
            from saiverse_memory import SAIMemoryAdapter
            try:
                adapter = SAIMemoryAdapter(persona_id)
                should_close = True
            except Exception as e:
                raise HTTPException(status_code=500, detail=f"Failed to access memory: {e}")

        # 4. Import All
        imported_count = 0
        msg_count = 0
        
        try:
            for record in records:
                # Default logic from import_chatgpt_conversations.py
                payloads = list(record.iter_memory_payloads(include_roles=["user", "assistant"]))
                # Use conversation_id as suffix mostly
                thread_suffix = record.conversation_id or record.identifier
                
                # Insert header? (Skipping for API simplicity or default to True)
                # Let's simple append messages
                for payload in payloads:
                    # Fix tags/metadata as per script
                    meta = payload.get("metadata", {})
                    tags = meta.get("tags", [])
                    if "conversation" not in tags:
                        tags.append("conversation")
                    meta["tags"] = tags
                    payload["metadata"] = meta
                    
                    adapter.append_persona_message(payload, thread_suffix=thread_suffix)
                    msg_count += 1
                imported_count += 1
        finally:
            if should_close and adapter:
                adapter.close()
                
        return {
            "success": True, 
            "conversations": imported_count, 
            "messages": msg_count,
            "message": f"Imported {imported_count} conversations ({msg_count} messages)."
        }

    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Import failed: {str(e)}")
    finally:
        # Cleanup temp file
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except:
                pass

@router.post("/{persona_id}/import/extension")
def import_extension_export(
    persona_id: str,
    file: UploadFile = File(...),
    manager = Depends(get_manager)
):
    """Import Chrome extension export (JSON or Markdown)."""
    # 1. Save upload to temp file
    suffix = Path(file.filename).suffix if file.filename else ".tmp"
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        shutil.copyfileobj(file.file, tmp)
        tmp_path = Path(tmp.name)

    try:
        # 2. Parse using parse_exporter_file
        from tools.utilities.chatlog_exporter_importer import parse_exporter_file
        conversation = parse_exporter_file(tmp_path)
        
        # 3. Import
        persona = manager.personas.get(persona_id)
        adapter = getattr(persona, "sai_memory", None) if persona else None
        should_close = False
        adapter_ready = adapter and adapter.is_ready()
        
        if not adapter_ready:
            from saiverse_memory import SAIMemoryAdapter
            try:
                adapter = SAIMemoryAdapter(persona_id)
                should_close = True
            except Exception as e:
                raise HTTPException(status_code=500, detail=f"Failed to access memory: {e}")

        msg_count = 0
        try:
            # Extension export usually contains just ONE conversation
            payloads = list(conversation.iter_memory_payloads())
            thread_suffix = conversation.identifier
            
            for payload in payloads:
                adapter.append_persona_message(payload, thread_suffix=thread_suffix)
                msg_count += 1
        finally:
            if should_close and adapter:
                adapter.close()
                
        return {
            "success": True, 
            "title": conversation.title,
            "messages": msg_count,
            "message": f"Imported '{conversation.title}' ({msg_count} messages)."
        }

    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Import failed: {str(e)}")
    finally:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except:
                pass

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
