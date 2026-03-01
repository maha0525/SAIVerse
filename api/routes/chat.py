from fastapi import APIRouter, Depends, HTTPException
from api.deps import get_manager, avatar_path_to_url
from pydantic import BaseModel
from typing import List, Optional, Dict, Any

router = APIRouter()

from fastapi.responses import FileResponse
import os

class ChatMessageImage(BaseModel):
    url: str  # URL to access the image
    mime_type: Optional[str] = None

class ChatMessageLLMUsage(BaseModel):
    """LLM usage information for a message."""
    model: str
    model_display_name: Optional[str] = None
    input_tokens: int
    output_tokens: int
    cached_tokens: int = 0  # Tokens served from cache (cache read)
    cache_write_tokens: int = 0  # Tokens written to cache (Anthropic: 1.25x cost)
    cost_usd: Optional[float] = None

class ChatMessageLLMUsageTotal(BaseModel):
    """Accumulated LLM usage for entire pulse (all LLM calls leading to this message)."""
    total_input_tokens: int
    total_output_tokens: int
    total_cached_tokens: int = 0  # Total cached tokens across all calls
    total_cache_write_tokens: int = 0  # Total cache write tokens across all calls
    total_cost_usd: float
    call_count: int
    models_used: List[str] = []

class ChatMessage(BaseModel):
    id: Optional[str] = None
    role: str
    content: str
    timestamp: Optional[str] = None
    sender: Optional[str] = None
    avatar: Optional[str] = None
    images: Optional[List[ChatMessageImage]] = None
    reasoning: Optional[str] = None
    activity_trace: Optional[List[dict]] = None
    llm_usage: Optional[ChatMessageLLMUsage] = None
    llm_usage_total: Optional[ChatMessageLLMUsageTotal] = None

class ChatHistoryResponse(BaseModel):
    history: List[ChatMessage]
    has_more: bool = False  # Whether there are older messages available

@router.get("/persona/{persona_id}/avatar")
def get_persona_avatar(persona_id: str, manager = Depends(get_manager)):
    persona = manager.personas.get(persona_id)
    if not persona or not persona.avatar_image:
        # Return default or 404. For now default host
        return FileResponse("builtin_data/icons/host.png")
    
    # Check if absolute path
    path = Path(persona.avatar_image)
    if not path.is_absolute():
        # Assume relative to workspace root or handled by manager
        # But commonly it might be in assets/avatars
        pass 
    
    if path.exists():
        return FileResponse(path)
    return FileResponse("builtin_data/icons/host.png")

import logging
import hashlib

@router.get("/history", response_model=ChatHistoryResponse)
def get_chat_history(
    limit: int = 20,
    before: Optional[str] = None,
    after: Optional[str] = None,
    building_id: Optional[str] = None,
    manager = Depends(get_manager)
):
    current_bid = building_id or manager.user_current_building_id
    logging.debug("[CHAT_HISTORY] Request: limit=%s, before=%s, current_bid=%s", limit, before, current_bid)
    
    if not current_bid:
        logging.warning("get_chat_history: No user_current_building_id")
        return {"history": [], "has_more": False}

    raw_history = manager.building_histories.get(current_bid, [])
    
    # Filter out non-displayable messages before pagination to ensure consistent counts
    raw_history = [
        msg for msg in raw_history
        if msg.get("content") and '<div class="note-box">' not in str(msg.get("content", ""))
    ]

    logging.debug("[CHAT_HISTORY] Found history items (after filter): %d", len(raw_history))
    if len(raw_history) == 0:
        logging.debug("[CHAT_HISTORY] Available building keys: %s", list(manager.building_histories.keys()))
    
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
            logging.warning("get_chat_history: 'before' ID %s not found in history for %s", before, current_bid)
            logging.debug("[CHAT_HISTORY] WARN: 'before' ID %s NOT FOUND (ID mismatch). IDs available (first 5): %s",
                         before, [x['virtual_id'] for x in enriched_history_objects[:5]])
            return {"history": [], "has_more": False}

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
            logging.warning("get_chat_history: 'after' ID %s not found in history for %s", after, current_bid)
            logging.debug("[CHAT_HISTORY] WARN: 'after' ID %s NOT FOUND. Returning empty for polling.", after)
            return {"history": [], "has_more": False}

    # Slice
    start_index = max(0, end_index - limit) if not after else start_index
    slice_history = enriched_history_objects[start_index:end_index]
    
    # Determine if there are older messages (for pagination)
    has_more_old = start_index > 0

    logging.debug("[CHAT_HISTORY] Slice calc: start=%d, end=%d, limit=%d. Returning %d items. has_more=%s",
                 start_index, end_index, limit, len(slice_history), has_more_old)
    logging.info("get_chat_history: bid=%s total=%d limit=%d before=%s returned=%d has_more=%s",
                current_bid, len(raw_history), limit, before, len(slice_history), has_more_old)

    final_response = []
    
    for msg in slice_history:
        role = msg.get("role")
        content = msg.get("content")
        timestamp = msg.get("timestamp", "")
        message_id = msg["virtual_id"] # Use the robust ID
        
        sender = "Unknown"
        avatar = "/api/static/builtin_icons/host.png" 
        
        if role == "user":
            sender = manager.user_display_name or "User"
            avatar = manager.state.user_avatar_data or "/api/static/builtin_icons/user.png"
        elif role == "assistant":
            pid = msg.get("persona_id")
            if pid:
                persona = manager.personas.get(pid)
                if persona:
                    sender = persona.persona_name
                    avatar = avatar_path_to_url(persona.avatar_image) or "/api/static/builtin_icons/host.png"
            else:
                sender = "Assistant"
        elif role == "host":
            sender = "System"
            avatar = "/api/static/builtin_icons/host.png"
            
        # Extract images from metadata
        # Support both 'images' (user upload) and 'media' (tool-generated) keys
        images_list = None
        metadata = msg.get("metadata", {})
        if metadata and ("images" in metadata or "media" in metadata):
            images_list = []
            media_items = metadata.get("images") or metadata.get("media") or []
            for img in media_items:
                # Convert path to URL
                # Tool-generated images may use 'uri' instead of 'path'
                img_path = img.get("path") or ""
                if not img_path:
                    # Try to extract from uri (saiverse://image/filename.jpg)
                    uri = img.get("uri", "")
                    if uri.startswith("saiverse://image/"):
                        filename = uri.replace("saiverse://image/", "")
                        img_path = str(Path.home() / ".saiverse" / "image" / filename)
                if img_path:
                    # Serve via static endpoint
                    images_list.append(ChatMessageImage(
                        url=f"/api/static/uploads/{Path(img_path).name}",
                        mime_type=img.get("mime_type")
                    ))

        # Extract LLM usage from metadata
        llm_usage_data = None
        if metadata and "llm_usage" in metadata:
            usage_raw = metadata["llm_usage"]
            if isinstance(usage_raw, dict):
                llm_usage_data = ChatMessageLLMUsage(
                    model=usage_raw.get("model", "unknown"),
                    model_display_name=usage_raw.get("model_display_name"),
                    input_tokens=usage_raw.get("input_tokens", 0),
                    output_tokens=usage_raw.get("output_tokens", 0),
                    cached_tokens=usage_raw.get("cached_tokens", 0),
                    cache_write_tokens=usage_raw.get("cache_write_tokens", 0),
                    cost_usd=usage_raw.get("cost_usd"),
                )

        # Extract LLM usage total (accumulated across all LLM calls in pulse)
        llm_usage_total_data = None
        if metadata and "llm_usage_total" in metadata:
            total_raw = metadata["llm_usage_total"]
            if isinstance(total_raw, dict):
                llm_usage_total_data = ChatMessageLLMUsageTotal(
                    total_input_tokens=total_raw.get("total_input_tokens", 0),
                    total_output_tokens=total_raw.get("total_output_tokens", 0),
                    total_cached_tokens=total_raw.get("total_cached_tokens", 0),
                    total_cache_write_tokens=total_raw.get("total_cache_write_tokens", 0),
                    total_cost_usd=total_raw.get("total_cost_usd", 0.0),
                    call_count=total_raw.get("call_count", 0),
                    models_used=total_raw.get("models_used", []),
                )

        # Extract reasoning (thinking) from metadata
        reasoning_data = None
        if metadata and "reasoning" in metadata:
            reasoning_data = metadata["reasoning"]

        # Extract activity trace from metadata
        activity_trace_data = None
        if metadata and "activity_trace" in metadata:
            activity_trace_data = metadata["activity_trace"]

        final_response.append(ChatMessage(
            id=message_id,
            role=role,
            content=content,
            timestamp=timestamp,
            sender=sender,
            avatar=avatar,
            images=images_list,
            reasoning=reasoning_data,
            activity_trace=activity_trace_data,
            llm_usage=llm_usage_data,
            llm_usage_total=llm_usage_total_data
        ))

    return {"history": final_response, "has_more": has_more_old}

import shutil
import mimetypes
import uuid
import base64
from datetime import datetime
from pathlib import Path

class AttachmentData(BaseModel):
    """Attachment data from frontend."""
    data: str  # Base64 encoded
    filename: str
    type: str  # 'image' | 'document' | 'unknown'
    mime_type: str

class SendMessageRequest(BaseModel):
    message: str
    building_id: Optional[str] = None  # Client-provided building context for multi-device safety
    attachment: Optional[str] = None  # Base64 encoded file (legacy, single attachment)
    attachments: Optional[List[AttachmentData]] = None  # New: multiple attachments
    meta_playbook: Optional[str] = None
    args: Optional[Dict[str, Any]] = None  # Arguments for meta playbook
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

# File type detection constants
TEXT_EXTENSIONS = {'txt', 'md', 'py', 'js', 'ts', 'tsx', 'json', 'yaml', 'yml', 'csv',
                   'html', 'css', 'xml', 'log', 'sh', 'bat', 'sql', 'java', 'c', 'cpp',
                   'h', 'hpp', 'go', 'rs', 'rb', 'swift', 'kt', 'scala', 'r', 'lua', 'pl',
                   'pdf'}
IMAGE_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp', 'bmp'}

def _store_image_attachment(
    data: bytes,
    att: AttachmentData,
    manager,
    building_id: str
) -> Dict[str, Any]:
    """Store image and create picture Item."""
    dest_dir = Path.home() / ".saiverse" / "image"
    dest_dir.mkdir(parents=True, exist_ok=True)

    # Determine extension from mime_type
    ext = ".bin"
    if "image/png" in att.mime_type: ext = ".png"
    elif "image/jpeg" in att.mime_type or "image/jpg" in att.mime_type: ext = ".jpg"
    elif "image/gif" in att.mime_type: ext = ".gif"
    elif "image/webp" in att.mime_type: ext = ".webp"
    elif "image/bmp" in att.mime_type: ext = ".bmp"

    dest_name = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex}{ext}"
    dest_path = dest_dir / dest_name
    dest_path.write_bytes(data)

    # Create picture Item
    item_id = None
    try:
        item_id = manager.create_picture_item_for_user(
            name=att.filename,
            description=f"User uploaded image: {att.filename}",
            file_path=str(dest_path),
            building_id=building_id,
            creator_id="user",
            source_context='{"source": "upload"}',
        )
    except Exception as e:
        logging.warning("Failed to create picture item: %s", e, exc_info=True)

    return {
        "type": "image",
        "uri": f"saiverse://image/{dest_name}",
        "mime_type": att.mime_type,
        "source": "user_upload",
        "path": str(dest_path),
        "item_id": item_id
    }

def _store_document_attachment(
    data: bytes,
    att: AttachmentData,
    manager,
    building_id: str
) -> Dict[str, Any]:
    """Store document and create document Item."""
    dest_dir = Path.home() / ".saiverse" / "documents"
    dest_dir.mkdir(parents=True, exist_ok=True)

    dest_name = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex}_{att.filename}"
    dest_path = dest_dir / dest_name
    dest_path.write_bytes(data)

    # Read content for summary
    is_pdf = att.filename.lower().endswith('.pdf') or att.mime_type == 'application/pdf'
    if is_pdf:
        try:
            import io
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(data))
            text_parts = [page.extract_text() or "" for page in reader.pages[:5]]  # first 5 pages for summary
            content = "\n".join(text_parts)
        except Exception:
            logging.warning("PDF text extraction failed for %s", att.filename, exc_info=True)
            content = "(PDF text extraction failed)"
    else:
        try:
            content = data.decode('utf-8')
        except UnicodeDecodeError:
            content = data.decode('utf-8', errors='replace')

    # Generate summary (first 200 chars)
    summary = content[:200].strip()
    if len(content) > 200:
        summary += "..."

    # Create document Item
    item_id = None
    try:
        item_id = manager.create_document_item_for_user(
            name=att.filename,
            description=summary,
            file_path=str(dest_path),
            building_id=building_id,
            is_open=True,  # Auto-open so it appears in visual context
            creator_id="user",
            source_context='{"source": "upload"}',
        )
    except Exception as e:
        logging.warning("Failed to create document item: %s", e, exc_info=True)

    return {
        "type": "document",
        "uri": f"saiverse://document/{dest_name}",
        "mime_type": att.mime_type,
        "source": "user_upload",
        "path": str(dest_path),
        "item_id": item_id,
        "content_preview": content[:500] if len(content) > 500 else content
    }

def _store_uploaded_attachment_v2(
    att: AttachmentData,
    manager,
    building_id: str
) -> Optional[Dict[str, Any]]:
    """Process attachment and create appropriate Item type."""
    try:
        # Decode base64
        header, encoded = att.data.split(",", 1) if "," in att.data else ("", att.data)
        data = base64.b64decode(encoded)

        if att.type == 'image':
            return _store_image_attachment(data, att, manager, building_id)
        elif att.type == 'document':
            return _store_document_attachment(data, att, manager, building_id)
        else:
            # Unknown type: determine from extension
            ext = Path(att.filename).suffix.lower().lstrip('.')
            if ext in IMAGE_EXTENSIONS:
                return _store_image_attachment(data, att, manager, building_id)
            elif ext in TEXT_EXTENSIONS:
                return _store_document_attachment(data, att, manager, building_id)
            else:
                # Default to image for compatibility
                return _store_image_attachment(data, att, manager, building_id)
    except Exception as e:
        logging.error(f"Failed to process attachment: {e}")
        return None

@router.post("/stop")
def stop_generation(manager = Depends(get_manager)):
    """Stop the active LLM generation for the user's current building."""
    cancelled = manager.cancel_active_generation()
    return {"cancelled": cancelled}


@router.post("/send")
def send_message(req: SendMessageRequest, manager = Depends(get_manager)):
    building_id = req.building_id or manager.user_current_building_id
    if not building_id:
        raise HTTPException(status_code=400, detail="User is not in any building")

    if not req.message and not req.attachment and not req.attachments:
        raise HTTPException(status_code=400, detail="Message or attachment required")

    # Combine metadata
    metadata = req.metadata or {}

    # Handle new multi-attachment format
    if req.attachments:
        images = []
        documents = []
        for att in req.attachments:
            result = _store_uploaded_attachment_v2(att, manager, building_id)
            if result:
                if result["type"] == "image":
                    images.append({
                        "uri": result["uri"],
                        "path": result["path"],
                        "mime_type": result["mime_type"],
                        "item_id": result.get("item_id"),
                        "item_name": att.filename  # For history context
                    })
                elif result["type"] == "document":
                    documents.append({
                        "uri": result["uri"],
                        "path": result["path"],
                        "mime_type": result["mime_type"],
                        "item_id": result.get("item_id"),
                        "item_name": att.filename,  # For history context
                        "content_preview": result.get("content_preview")
                    })
        if images:
            metadata["images"] = images
        if documents:
            metadata["documents"] = documents

    # Handle legacy single attachment format (backwards compatibility)
    elif req.attachment:
        attachment_info = _store_uploaded_attachment(req.attachment)
        if attachment_info:
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
                meta_playbook=req.meta_playbook,
                args=req.args,
                building_id=building_id,
            )
            
            for chunk in stream:
                yield chunk

        return StreamingResponse(response_generator(), media_type="application/x-ndjson")

    except Exception as e:
        import logging
        logging.error(f"Error sending message: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


# ---- Context Preview ----

class PreviewRequest(BaseModel):
    message: str
    building_id: Optional[str] = None
    meta_playbook: Optional[str] = None
    attachment_count: int = 0
    attachment_types: List[str] = []  # ["image", "document"]


@router.post("/preview")
def preview_context(req: PreviewRequest, manager=Depends(get_manager)):
    """Preview the context that would be sent to the LLM, without executing."""
    import logging

    if not req.message:
        raise HTTPException(status_code=400, detail="Message is required")

    image_count = sum(1 for t in req.attachment_types if t == "image")
    document_count = sum(1 for t in req.attachment_types if t == "document")
    # Also count untyped attachments as documents
    if req.attachment_count > len(req.attachment_types):
        document_count += req.attachment_count - len(req.attachment_types)

    try:
        results = manager.preview_context(
            req.message,
            building_id=req.building_id,
            meta_playbook=req.meta_playbook,
            image_count=image_count,
            document_count=document_count,
        )
        return {"personas": results}
    except Exception as e:
        logging.error("Error previewing context: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


# ── Playbook permission response ──────────────────────────────────

class PermissionResponseRequest(BaseModel):
    request_id: str
    decision: str  # allow | deny | always_allow | never_use


@router.post("/permission-response")
def respond_to_permission(req: PermissionResponseRequest, manager=Depends(get_manager)):
    """Respond to a playbook execution permission request."""
    valid_decisions = ("allow", "deny", "always_allow", "never_use")
    if req.decision not in valid_decisions:
        raise HTTPException(status_code=400, detail=f"Invalid decision. Must be one of: {valid_decisions}")

    event = manager._pending_permission_requests.get(req.request_id)
    if not event:
        raise HTTPException(status_code=404, detail="Permission request not found or expired")

    manager._permission_responses[req.request_id] = req.decision
    event.set()  # Wake up the waiting worker thread
    return {"success": True}


# ---------------------------------------------------------------------------
# Tweet confirmation
# ---------------------------------------------------------------------------

class TweetConfirmationRequest(BaseModel):
    request_id: str
    decision: str  # approve | reject | edit
    edited_text: Optional[str] = None


@router.post("/tweet-confirmation-response")
def respond_to_tweet_confirmation(req: TweetConfirmationRequest, manager=Depends(get_manager)):
    """Respond to a tweet posting confirmation request."""
    valid_decisions = ("approve", "reject", "edit")
    if req.decision not in valid_decisions:
        raise HTTPException(status_code=400, detail=f"Invalid decision. Must be one of: {valid_decisions}")

    event = manager._pending_tweet_confirmations.get(req.request_id)
    if not event:
        raise HTTPException(status_code=404, detail="Tweet confirmation request not found or expired")

    response_value = req.decision
    if req.decision == "edit" and req.edited_text:
        response_value = f"edit:{req.edited_text}"

    manager._tweet_confirmation_responses[req.request_id] = response_value
    event.set()
    return {"success": True}
