from fastapi import APIRouter, Depends, HTTPException
from api.deps import get_manager
from pydantic import BaseModel
from typing import List, Optional, Dict, Any

router = APIRouter()

from fastapi.responses import FileResponse
import os

class ChatMessageImage(BaseModel):
    url: str  # URL to access the image
    mime_type: Optional[str] = None

class ChatMessage(BaseModel):
    id: Optional[str] = None
    role: str
    content: str
    timestamp: Optional[str] = None
    sender: Optional[str] = None
    avatar: Optional[str] = None
    images: Optional[List[ChatMessageImage]] = None

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

import logging
import hashlib

@router.get("/history", response_model=ChatHistoryResponse)
def get_chat_history(
    limit: int = 20, 
    before: Optional[str] = None,
    after: Optional[str] = None,
    manager = Depends(get_manager)
):
    # DEBUG LOGGING SETUP
    debug_log_path = r"c:\Users\shuhe\workspace\SAIVerse\debug_chat.log"
    def log_debug(msg):
        with open(debug_log_path, "a", encoding="utf-8") as f:
            f.write(f"{datetime.now()}: {msg}\n")

    current_bid = manager.user_current_building_id
    log_debug(f"Request: limit={limit}, before={before}, current_bid={current_bid}")
    
    if not current_bid:
        logging.warning("get_chat_history: No user_current_building_id")
        log_debug("ERROR: No user_current_building_id")
        return {"history": []}
        
    raw_history = manager.building_histories.get(current_bid, [])
    
    # Filter out note-box messages before pagination to ensure consistent counts
    raw_history = [msg for msg in raw_history if '<div class="note-box">' not in str(msg.get("content", ""))]
    
    log_debug(f"Found history items (after note-box filter): {len(raw_history)}")
    if len(raw_history) == 0:
        log_debug(f"Available building keys: {list(manager.building_histories.keys())}")
    
    # 1. Enrich/Normalize history with IDs
    # We must do this dynamically to support legacy messages without IDs
    # and ensure pagination works consistently.
    enriched_history_objects = []
    
    for idx, msg in enumerate(raw_history):
        # Determine ID
        msg_id = msg.get("message_id")
        if not msg_id:
            # Generate stable ID for legacy messages
            # Use content + timestamp + role + index to ensure uniqueness and stability
            # Index is risky if history changes (e.g. deletion), but better than random
            content_str = str(msg.get("content", ""))
            timestamp = str(msg.get("timestamp", ""))
            role = str(msg.get("role", ""))
            # Use timestamp+role+content for stable ID (no index dependency)
            unique_str = f"{current_bid}:{timestamp}:{role}:{content_str[:100]}" 
            msg_id = hashlib.md5(unique_str.encode()).hexdigest()
        
        # Create temp object for pagination logic
        enriched_history_objects.append({
            **msg,
            "virtual_id": str(msg_id)
        })

    # 2. Pagination Logic
    start_index = 0
    end_index = len(enriched_history_objects)

    if before:
        # Find the index of the message with ID 'before'
        found_index = -1
        # Search backwards
        for i in range(len(enriched_history_objects) - 1, -1, -1):
            if enriched_history_objects[i]["virtual_id"] == before:
                found_index = i
                break
        
        if found_index != -1:
            end_index = found_index
        else:
            # ID not found - ID mismatch due to history changes
            # Return empty; client interprets <20 results as "no more history"
            logging.warning(f"get_chat_history: 'before' ID {before} not found in history for {current_bid}")
            log_debug(f"WARN: 'before' ID {before} NOT FOUND (ID mismatch). IDs available (first 5): {[x['virtual_id'] for x in enriched_history_objects[:5]]}")
            return {"history": []}

    if after:
        # Find the index of the message with ID 'after' and return messages after it
        found_index = -1
        for i in range(len(enriched_history_objects)):
            if enriched_history_objects[i]["virtual_id"] == after:
                found_index = i
                break
        
        if found_index != -1:
            start_index = found_index + 1  # Start after the found message
            # For polling, we want newest messages (no need for limit typically, but cap at limit)
            end_index = min(start_index + limit, len(enriched_history_objects))
        else:
            # ID not found - maybe history was cleared or rolled over
            # Return empty for safety (client will need to refresh)
            logging.warning(f"get_chat_history: 'after' ID {after} not found in history for {current_bid}")
            log_debug(f"WARN: 'after' ID {after} NOT FOUND. Returning empty for polling.")
            return {"history": []}

    # Slice
    start_index = max(0, end_index - limit) if not after else start_index
    slice_history = enriched_history_objects[start_index:end_index]
    
    log_debug(f"Slice calc: start={start_index}, end={end_index}, limit={limit}. Returning {len(slice_history)} items.")
    logging.info(f"get_chat_history: bid={current_bid} total={len(raw_history)} limit={limit} before={before} returned={len(slice_history)}")

    final_response = []
    
    for msg in slice_history:
        role = msg.get("role")
        content = msg.get("content")
        
        # note-box messages already filtered out above
        if not content:
            continue
            
        timestamp = msg.get("timestamp", "")
        message_id = msg["virtual_id"] # Use the robust ID
        
        sender = "Unknown"
        avatar = "/api/static/icons/host.png" 
        
        if role == "user":
            sender = manager.user_display_name or "User"
            avatar = manager.state.user_avatar_data or "/api/static/icons/user.png"
        elif role == "assistant":
            pid = msg.get("persona_id")
            if pid:
                persona = manager.personas.get(pid)
                if persona:
                    sender = persona.persona_name
                    avatar = f"/api/chat/persona/{pid}/avatar"
            else:
                sender = "Assistant"
        elif role == "host":
            sender = "System"
            avatar = "/api/static/icons/host.png"
            
        # Extract images from metadata
        images_list = None
        metadata = msg.get("metadata", {})
        if metadata and "images" in metadata:
            images_list = []
            for img in metadata["images"]:
                # Convert path to URL
                img_path = img.get("path", "")
                if img_path:
                    # Serve via static endpoint
                    images_list.append(ChatMessageImage(
                        url=f"/api/static/uploads/{Path(img_path).name}",
                        mime_type=img.get("mime_type")
                    ))
        
        final_response.append(ChatMessage(
            id=message_id,
            role=role,
            content=content,
            timestamp=timestamp,
            sender=sender,
            avatar=avatar,
            images=images_list
        ))
        
    return {"history": final_response}

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
                {"uri": attachment_info["uri"], "path": attachment_info["path"], "mime_type": attachment_info["mime_type"]}
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
