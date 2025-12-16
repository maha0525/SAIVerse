from fastapi import APIRouter, Depends, HTTPException
from api.deps import get_manager
from pydantic import BaseModel
from typing import List, Optional, Dict, Any

router = APIRouter()

from fastapi.responses import FileResponse
import os

class ChatMessage(BaseModel):
    role: str
    content: str
    timestamp: Optional[str] = None
    sender: Optional[str] = None
    avatar: Optional[str] = None

class ChatHistoryResponse(BaseModel):
    history: List[ChatMessage]

@router.get("/persona/{persona_id}/avatar")
def get_persona_avatar(persona_id: str, manager = Depends(get_manager)):
    persona = manager.personas.get(persona_id)
    if not persona or not persona.avatar_image:
        # Return default or 404. For now default host
        return FileResponse("assets/icons/host.png")
    
    # Check if absolute path
    path = Path(persona.avatar_image)
    if not path.is_absolute():
        # Assume relative to workspace root or handled by manager
        # But commonly it might be in assets/avatars
        pass 
    
    if path.exists():
        return FileResponse(path)
    return FileResponse("assets/icons/host.png")

@router.get("/history", response_model=ChatHistoryResponse)
def get_chat_history(manager = Depends(get_manager)):
    if not manager.user_current_building_id:
        return {"history": []}
        
    raw_history = manager.building_histories.get(manager.user_current_building_id, [])
    enriched_history = []
    
    for msg in raw_history:
        role = msg.get("role")
        content = msg.get("content")
        
        # Filter out "User Action" logs (Gradio legacy)
        if content and '<div class="note-box">' in content:
            continue
            
        timestamp = msg.get("timestamp", "")
        
        sender = "Unknown"
        avatar = "/api/static/icons/host.png" # Default
        
        if role == "user":
            sender = manager.user_display_name or "User"
            avatar = "/api/static/icons/user.png" # Frontend public asset
        elif role == "assistant":
            pid = msg.get("persona_id")
            if pid:
                persona = manager.personas.get(pid)
                if persona:
                    sender = persona.persona_name
                    # Use our new endpoint
                    avatar = f"/api/chat/persona/{pid}/avatar"
            else:
                sender = "Assistant"
        elif role == "host":
            sender = "System"
            avatar = "/api/static/icons/host.png"
            
        enriched_history.append(ChatMessage(
            role=role,
            content=content,
            timestamp=timestamp,
            sender=sender,
            avatar=avatar
        ))
        
    return {"history": enriched_history}

import shutil
import mimetypes
import uuid
import base64
from datetime import datetime
from pathlib import Path

class SendMessageRequest(BaseModel):
    message: str
    attachment: Optional[str] = None # Base64 encoded file
    meta_playbook: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None

def _store_uploaded_attachment(base64_data: str) -> Optional[Dict[str, str]]:
    """Decode and save base64 attachment."""
    if not base64_data:
        return None
    
    try:
        # Simple data URI parsing
        header, encoded = base64_data.split(",", 1) if "," in base64_data else ("", base64_data)
        
        # Determine extension from header
        ext = ".bin"
        if "image/png" in header: ext = ".png"
        elif "image/jpeg" in header: ext = ".jpg"
        elif "image/gif" in header: ext = ".gif"
        elif "image/webp" in header: ext = ".webp"
        
        data = base64.b64decode(encoded)
        
        dest_dir = Path.home() / ".saiverse" / "image"
        dest_dir.mkdir(parents=True, exist_ok=True)
        
        dest_name = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex}{ext}"
        dest_path = dest_dir / dest_name
        
        dest_path.write_bytes(data)
        
        mime_type = mimetypes.guess_type(dest_path)[0] or "application/octet-stream"
        
        return {
            "type": "image" if "image" in mime_type else "file",
            "uri": f"saiverse://image/{dest_name}",
            "mime_type": mime_type,
            "source": "user_upload",
            "path": str(dest_path) # Absolute path for internal use
        }
    except Exception as e:
        import logging
        logging.error(f"Failed to process attachment: {e}")
        return None

@router.post("/send")
def send_message(req: SendMessageRequest, manager = Depends(get_manager)):
    if not manager.user_current_building_id:
        raise HTTPException(status_code=400, detail="User is not in any building")

    if not req.message and not req.attachment:
        raise HTTPException(status_code=400, detail="Message or attachment required")

    # Combine metadata
    metadata = req.metadata or {}
    
    # Handle attachment
    if req.attachment:
        attachment_info = _store_uploaded_attachment(req.attachment)
        if attachment_info:
            # Add to metadata in format SAIVerse expects (usually list of attachments or single)
            # Checking ui/chat.py: input_files -> metadata={"images": [...]} or similar
            # ui/chat.py logic is: metadata = {"images": [path, ...]}
            # But here we have full info. Let's provide a list of media objects.
            # Standardizing on "media" list or "images" list for legacy support.
            
            # Legacy support: SAIVerse often looks for 'images': [{'path': ...}] ?
            # Let's check manager.runtime logic. It mostly passes metadata through.
            # ui/chat.py passes: {"path": path, "mime_type": mime}
            metadata["images"] = [
                {"path": attachment_info["path"], "mime_type": attachment_info["mime_type"]}
            ]
    
    # For V1, we will consume the stream and return the full response.
    # Future improvement: Use StreamingResponse
    try:
        from fastapi.responses import StreamingResponse
        import json
        import logging

        def response_generator():
            # Yield an initial status event to flush headers (with padding for buffering)
            yield json.dumps({"type": "status", "content": "processing"}, ensure_ascii=False) + " " * 2048 + "\n"
            
            stream = manager.handle_user_input_stream(
                req.message, 
                metadata=metadata, 
                meta_playbook=req.meta_playbook
            )
            
            for chunk in stream:
                yield chunk

        return StreamingResponse(response_generator(), media_type="application/x-ndjson")

    except Exception as e:
        import logging
        logging.error(f"Error sending message: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))
