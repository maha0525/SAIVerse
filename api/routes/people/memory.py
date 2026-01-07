from fastapi import APIRouter, Depends, HTTPException
from typing import List
from api.deps import get_manager
from .models import (
    ThreadSummary, MessageItem, MessagesResponse, UpdateMessageRequest
)
import math

router = APIRouter()

@router.get("/{persona_id}/threads", response_model=List[ThreadSummary])
def list_persona_threads(persona_id: str, manager = Depends(get_manager)):
    """List all conversation threads for a persona."""
    persona = manager.personas.get(persona_id)
    if not persona:
        raise HTTPException(status_code=404, detail=f"Persona {persona_id} not found")

    adapter = getattr(persona, "sai_memory", None)
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

@router.get("/{persona_id}/threads/{thread_id}/messages", response_model=MessagesResponse)
def list_thread_messages(
    persona_id: str, 
    thread_id: str, 
    page: int = 1, 
    page_size: int = 50, 
    manager = Depends(get_manager)
):
    """List messages in a thread with pagination."""
    
    # Logic to acquire adapter
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
