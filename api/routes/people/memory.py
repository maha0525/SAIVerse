from fastapi import APIRouter, Depends, HTTPException
from typing import List
from api.deps import get_manager
from .models import (
    ThreadSummary, MessageItem, MessagesResponse, UpdateMessageRequest
)
from .utils import get_adapter
import math

router = APIRouter()

@router.get("/{persona_id}/threads", response_model=List[ThreadSummary])
def list_persona_threads(persona_id: str, manager = Depends(get_manager)):
    """List all conversation threads for a persona."""
    persona = manager.personas.get(persona_id)
    if not persona:
        raise HTTPException(status_code=404, detail=f"Persona {persona_id} not found")

    with get_adapter(persona_id, manager) as adapter:
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

@router.get("/{persona_id}/threads/{thread_id}/messages", response_model=MessagesResponse)
def list_thread_messages(
    persona_id: str, 
    thread_id: str, 
    page: int = 1, 
    page_size: int = 50, 
    manager = Depends(get_manager)
):
    """List messages in a thread with pagination."""
    with get_adapter(persona_id, manager) as adapter:
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
        
        items = [
            MessageItem(
                id=m["id"],
                thread_id=m["thread_id"],
                role=m["role"],
                content=m["content"],
                created_at=m["created_at"],
                metadata=m.get("metadata")
            )
            for m in msgs
        ]
        
        # Get first and last timestamps for the thread
        first_created_at = None
        last_created_at = None
        try:
            first_msgs = adapter.get_thread_messages(thread_id, page=0, page_size=1)
            if first_msgs:
                first_created_at = first_msgs[0].get("created_at")
            last_page = max(0, math.ceil(total / 1) - 1)
            last_msgs = adapter.get_thread_messages(thread_id, page=last_page, page_size=1)
            if last_msgs:
                last_created_at = last_msgs[0].get("created_at")
        except Exception:
            pass
            
        return MessagesResponse(
            items=items, 
            total=total, 
            page=page, 
            page_size=page_size,
            first_created_at=first_created_at,
            last_created_at=last_created_at,
        )

@router.patch("/{persona_id}/messages/{message_id}")
def update_message(
    persona_id: str, 
    message_id: str, 
    request: UpdateMessageRequest, 
    manager = Depends(get_manager)
):
    """Update message content and/or timestamp."""
    with get_adapter(persona_id, manager) as adapter:
        new_created_at = int(request.created_at) if request.created_at is not None else None
        success = adapter.update_message(
            message_id, 
            new_content=request.content, 
            new_created_at=new_created_at
        )
        if not success:
            raise HTTPException(status_code=404, detail="Message not found or update failed")
        return {"success": True}

@router.delete("/{persona_id}/messages/{message_id}")
def delete_message(
    persona_id: str, 
    message_id: str, 
    manager = Depends(get_manager)
):
    """Delete a message."""
    with get_adapter(persona_id, manager) as adapter:
        success = adapter.delete_message(message_id)
        if not success:
            raise HTTPException(status_code=404, detail="Message not found or delete failed")
        return {"success": True}

@router.delete("/{persona_id}/threads/{thread_id}")
def delete_thread(
    persona_id: str, 
    thread_id: str, 
    manager = Depends(get_manager)
):
    """Delete a thread."""
    with get_adapter(persona_id, manager) as adapter:
        success = adapter.delete_thread(thread_id)
        if not success:
             raise HTTPException(status_code=404, detail="Thread not found or delete failed")
        return {"success": True}

@router.put("/{persona_id}/threads/{thread_id}/activate")
def set_active_thread(
    persona_id: str,
    thread_id: str,
    manager = Depends(get_manager)
):
    """Set a thread as the active thread for the persona."""
    with get_adapter(persona_id, manager) as adapter:
        success = adapter.set_active_thread(thread_id)
        if not success:
            raise HTTPException(status_code=500, detail="Failed to set active thread")
        return {"success": True, "thread_id": thread_id}
