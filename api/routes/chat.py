from fastapi import APIRouter, Depends, HTTPException
from api.deps import get_manager, avatar_path_to_url
from pydantic import BaseModel
from typing import List, Optional, Dict, Any

router = APIRouter()

from fastapi.responses import FileResponse, StreamingResponse
import json
import os

from saiverse.data_paths import get_saiverse_home

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
    currency: str = "USD"

class ChatMessageLLMUsageTotal(BaseModel):
    """Accumulated LLM usage for entire pulse (all LLM calls leading to this message)."""
    total_input_tokens: int
    total_output_tokens: int
    total_cached_tokens: int = 0  # Total cached tokens across all calls
    total_cache_write_tokens: int = 0  # Total cache write tokens across all calls
    total_cost_usd: float
    call_count: int
    models_used: List[str] = []
    currency: str = "USD"

class ChatMessage(BaseModel):
    id: Optional[str] = None
    role: str
    content: str
    timestamp: Optional[str] = None
    sender: Optional[str] = None
    avatar: Optional[str] = None
    persona_id: Optional[str] = None  # assistant メッセージで発話ペルソナを識別 (アドオン bubble button の context 等で使用)
    images: Optional[List[ChatMessageImage]] = None
    # Same shape (url + mime_type) but separated by media type so the frontend
    # can pick the right player (img / audio / video) without misclassifying.
    audios: Optional[List[ChatMessageImage]] = None
    videos: Optional[List[ChatMessageImage]] = None
    reasoning: Optional[str] = None
    # 自動想起 (記憶アーキv2 §4.5): この Pulse で末尾注入された「ふと浮かんだ記憶」の
    # 本文 (<system> タグ除去済み)。reasoning と同じくアシスタント応答メッセージの
    # metadata から復元する (永続化は sea/runtime.py _lg_say_node 等、
    # metadata["auto_recall"])。
    auto_recall: Optional[str] = None
    activity_trace: Optional[List[dict]] = None
    llm_usage: Optional[ChatMessageLLMUsage] = None
    llm_usage_total: Optional[ChatMessageLLMUsageTotal] = None

class ChatHistoryResponse(BaseModel):
    history: List[ChatMessage]
    has_more: bool = False  # Whether there are older messages available
    quarantined: bool = False  # True if this building's log.json is corrupted/quarantined

@router.get("/persona/{persona_id}/avatar")
def get_persona_avatar(persona_id: str, manager = Depends(get_manager)):
    persona = manager.personas.get(persona_id)
    if not persona or not persona.avatar_image:
        # Return default or 404. For now default host
        return FileResponse("builtin_data/icons/host.png")
    
    from api.file_safety import ensure_allowed_path
    from saiverse.data_paths import PROJECT_ROOT

    path = Path(persona.avatar_image)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    
    if path.exists():
        return FileResponse(ensure_allowed_path(path, home=manager.saiverse_home))
    return FileResponse("builtin_data/icons/host.png")

import logging
import hashlib


def serialize_history_message(manager, msg: Dict[str, Any], message_id: str) -> "ChatMessage":
    """building_messages の dict 1 件を ChatMessage (API レスポンス形式) へ変換する。

    get_chat_history のループ本体を抽出したもの。ゲームセッションログビュー
    (/api/world/regions/{id}/game/log) も同じ整形を共用する。
    """
    role = msg.get("role")
    content = msg.get("content")
    timestamp = msg.get("timestamp", "")

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

    # Extract media from metadata
    # - "images" (legacy, user upload): image-only entries
    # - "media" (unified, audio/video and possibly mixed): type-tagged entries
    images_list = None
    audios_list = None
    videos_list = None
    metadata = msg.get("metadata", {})
    if metadata and ("images" in metadata or "media" in metadata):
        images_buf: List[ChatMessageImage] = []
        audios_buf: List[ChatMessageImage] = []
        videos_buf: List[ChatMessageImage] = []

        def _classify_and_append(entry: Dict[str, Any], forced_type: Optional[str]) -> None:
            """Resolve path → URL and route to the right buffer by media type."""
            item_type = (forced_type or entry.get("type") or "").lower()
            item_mime = (entry.get("mime_type") or "").lower()

            # Resolve path from "path" or "uri"
            img_path = entry.get("path") or ""
            uri = entry.get("uri", "")
            if not img_path and uri:
                if uri.startswith("saiverse://image/"):
                    filename = uri.replace("saiverse://image/", "")
                    img_path = str(get_saiverse_home() / "image" / filename)
                elif uri.startswith("saiverse://audio/"):
                    filename = uri.replace("saiverse://audio/", "")
                    img_path = str(get_saiverse_home() / "audio" / filename)
                elif uri.startswith("saiverse://video/"):
                    filename = uri.replace("saiverse://video/", "")
                    img_path = str(get_saiverse_home() / "video" / filename)
            if not img_path:
                return

            name = Path(img_path).name
            # Pick serving endpoint per media type
            if item_type == "audio" or item_mime.startswith("audio/"):
                audios_buf.append(ChatMessageImage(
                    url=f"/api/media/audio/{name}",
                    mime_type=entry.get("mime_type") or "audio/ogg",
                ))
            elif item_type == "video" or item_mime.startswith("video/"):
                videos_buf.append(ChatMessageImage(
                    url=f"/api/media/video/{name}",
                    mime_type=entry.get("mime_type") or "video/mp4",
                ))
            else:
                # Default to image (legacy "images" entries and tool-generated images)
                images_buf.append(ChatMessageImage(
                    url=f"/api/static/uploads/{name}",
                    mime_type=entry.get("mime_type"),
                ))

        # Legacy "images" key: every entry is an image
        for img in metadata.get("images") or []:
            _classify_and_append(img, forced_type="image")
        # Unified "media" key: entries carry their own type
        for img in metadata.get("media") or []:
            _classify_and_append(img, forced_type=None)

        images_list = images_buf or None
        audios_list = audios_buf or None
        videos_list = videos_buf or None

    # Extract LLM usage from metadata
    llm_usage_data = None
    if metadata and "llm_usage" in metadata:
        usage_raw = metadata["llm_usage"]
        if isinstance(usage_raw, dict):
            usage_model = usage_raw.get("model", "unknown")
            usage_currency = usage_raw.get("currency", "USD")
            if usage_currency == "USD":
                from saiverse.model_configs import get_model_pricing
                _p = get_model_pricing(usage_model)
                if _p:
                    usage_currency = _p.get("currency", "USD")
            llm_usage_data = ChatMessageLLMUsage(
                model=usage_model,
                model_display_name=usage_raw.get("model_display_name"),
                input_tokens=usage_raw.get("input_tokens", 0),
                output_tokens=usage_raw.get("output_tokens", 0),
                cached_tokens=usage_raw.get("cached_tokens", 0),
                cache_write_tokens=usage_raw.get("cache_write_tokens", 0),
                cost_usd=usage_raw.get("cost_usd"),
                currency=usage_currency,
            )

    # Extract LLM usage total (accumulated across all LLM calls in pulse)
    llm_usage_total_data = None
    if metadata and "llm_usage_total" in metadata:
        total_raw = metadata["llm_usage_total"]
        if isinstance(total_raw, dict):
            total_models = total_raw.get("models_used", [])
            total_currency = total_raw.get("currency", "USD")
            if total_currency == "USD" and total_models:
                from saiverse.model_configs import get_model_pricing
                _p = get_model_pricing(total_models[0])
                if _p:
                    total_currency = _p.get("currency", "USD")
            llm_usage_total_data = ChatMessageLLMUsageTotal(
                total_input_tokens=total_raw.get("total_input_tokens", 0),
                total_output_tokens=total_raw.get("total_output_tokens", 0),
                total_cached_tokens=total_raw.get("total_cached_tokens", 0),
                total_cache_write_tokens=total_raw.get("total_cache_write_tokens", 0),
                total_cost_usd=total_raw.get("total_cost_usd", 0.0),
                call_count=total_raw.get("call_count", 0),
                models_used=total_models,
                currency=total_currency,
            )

    # Extract reasoning (thinking) from metadata
    reasoning_data = None
    if metadata and "reasoning" in metadata:
        reasoning_data = metadata["reasoning"]

    # Extract auto_recall (記憶アーキv2 §4.5「ふと浮かんだ記憶」) from metadata.
    # reasoning と全く同じパターン: 永続化された metadata から復元するだけで、
    # LLM コンテキストの再構築には一切使わない (sea/runtime_context.py 側で
    # 履歴末尾への一時注入と ChatMessage 復元は別経路)。
    auto_recall_data = None
    if metadata and "auto_recall" in metadata:
        auto_recall_data = metadata["auto_recall"]

    # Extract activity trace from metadata
    activity_trace_data = None
    if metadata and "activity_trace" in metadata:
        activity_trace_data = metadata["activity_trace"]

    return ChatMessage(
        id=message_id,
        role=role,
        content=content,
        timestamp=timestamp,
        sender=sender,
        avatar=avatar,
        persona_id=msg.get("persona_id") if role == "assistant" else None,
        images=images_list,
        audios=audios_list,
        videos=videos_list,
        reasoning=reasoning_data,
        auto_recall=auto_recall_data,
        activity_trace=activity_trace_data,
        llm_usage=llm_usage_data,
        llm_usage_total=llm_usage_total_data
    )


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
        return {"history": [], "has_more": False, "quarantined": False}

    # Quarantine: building's log.json is corrupted. Return empty history but
    # signal the UI so it can show the appropriate state instead of pretending
    # the building is empty.
    if hasattr(manager, "quarantined_buildings") and current_bid in manager.quarantined_buildings:
        logging.info("[CHAT_HISTORY] Building %s is quarantined; returning empty history with flag", current_bid)
        return {"history": [], "has_more": False, "quarantined": True}

    raw_history = manager.get_building_history(current_bid)

    # Filter out empty messages but KEEP note-box host events (移動 / item pickup
    # 等)。 intent §D-2: 「移動が乱発しなくなる新ルール (= C-1 閲覧モード) の
    # 下では、 移動メッセージはノイズではなく時系列の意味ある情報になる」
    # 控えめなスタイル (globals.css の .note-box) で会話メッセージと区別される。
    raw_history = [
        msg for msg in raw_history
        if msg.get("content")
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

    final_response = [
        serialize_history_message(manager, msg, msg["virtual_id"])
        for msg in slice_history
    ]

    return {"history": final_response, "has_more": has_more_old}

import shutil
import mimetypes
import uuid
import base64
from datetime import datetime
from pathlib import Path

class AttachmentData(BaseModel):
    """Attachment data from frontend.

    Two delivery modes are supported:
    - `data` (base64 data URL) for small files like images / documents / audio.
    - `uri` (saiverse:// reference to a file already uploaded via
      /api/media/upload-*) for large files like video, to avoid base64
      ballooning browser memory.
    Exactly one of `data` or `uri` must be set per attachment.
    """
    data: Optional[str] = None  # Base64 encoded data URL
    uri: Optional[str] = None   # saiverse://video/<filename> etc.
    filename: str
    type: str  # 'image' | 'document' | 'audio' | 'video' | 'unknown'
    mime_type: str

class SendMessageRequest(BaseModel):
    message: str
    building_id: Optional[str] = None  # Client-provided building context for multi-device safety
    attachment: Optional[str] = None  # Base64 encoded file (legacy, single attachment)
    attachments: Optional[List[AttachmentData]] = None  # New: multiple attachments
    meta_playbook: Optional[str] = None
    args: Optional[Dict[str, Any]] = None  # Arguments for meta playbook
    metadata: Optional[Dict[str, Any]] = None
    # UI-triggered pre-spells: list of Spell invocation strings executed before the
    # first LLM call (e.g. ['/run_playbook(name="memory_research")']). Replaces the
    # deprecated meta_user_manual route. See docs/intent/persona_cognition/
    # nested_subline_spell.md §13.
    pre_spells: Optional[List[str]] = None
    # B-2 idempotency: クライアント生成 UUID。 同じ値で複数回送信されても、
    # サーバは building_messages に 1 件しか insert しない (UNIQUE 制約 +
    # 既存行返却)。 ネットワーク再送 / ユーザーの二重押し対策。
    # See: docs/intent/building_memory_unified.md §B-2
    client_message_id: Optional[str] = None

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
        
        dest_dir = get_saiverse_home() / "image"
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
    building_id: str,
    user_message: str = "",
    prev_ai_message: str = "",
    sync_summary: bool = False,
) -> Dict[str, Any]:
    """Store image and create picture Item.

    ``sync_summary`` (グローバル設定「添付したメディアの内容を自動想起に使う」ON 時):
    contextual description の生成をバックグラウンドスレッドでなく同期実行し、
    返り値の ``summary`` に載せる (呼び出し側が metadata に反映して
    sea/auto_recall.py の build_query から拾えるようにするため)。OFF 時は従来
    どおりバックグラウンド生成のみで、summary は常に None。
    """
    dest_dir = get_saiverse_home() / "image"
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

    # Create picture Item with placeholder description
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

    summary: Optional[str] = None
    if item_id and (user_message or prev_ai_message):
        if sync_summary:
            try:
                from saiverse.media_summary import generate_contextual_image_description
                desc = generate_contextual_image_description(dest_path, att.mime_type, user_message, prev_ai_message)
                if desc:
                    manager.update_item_description(item_id, desc)
                    summary = desc
                    logging.info("Generated contextual description for item %s (sync, media recall)", item_id)
            except Exception as e:
                logging.warning("Synchronous description generation failed for item %s: %s", item_id, e)
        else:
            # Generate contextual description in background (default; unchanged behavior)
            import threading
            _path = dest_path
            _mime = att.mime_type
            _item_id = item_id

            def _generate_description():
                try:
                    from saiverse.media_summary import generate_contextual_image_description
                    desc = generate_contextual_image_description(_path, _mime, user_message, prev_ai_message)
                    if desc:
                        manager.update_item_description(_item_id, desc)
                        logging.info("Generated contextual description for item %s", _item_id)
                except Exception as e:
                    logging.warning("Background description generation failed for item %s: %s", _item_id, e)

            threading.Thread(target=_generate_description, daemon=True).start()

    return {
        "type": "image",
        "uri": f"saiverse://image/{dest_name}",
        "mime_type": att.mime_type,
        "source": "user_upload",
        "path": str(dest_path),
        "item_id": item_id,
        "summary": summary,
    }

def _store_document_attachment(
    data: bytes,
    att: AttachmentData,
    manager,
    building_id: str
) -> Dict[str, Any]:
    """Store document and create document Item."""
    dest_dir = get_saiverse_home() / "documents"
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

AUDIO_EXTENSIONS = {'wav', 'mp3', 'ogg', 'oga', 'opus', 'aac', 'flac', 'aiff', 'm4a'}
VIDEO_EXTENSIONS = {'mp4', 'webm', 'mov', 'avi', 'mpeg', 'mpg', '3gp', 'mkv', 'flv', 'wmv'}


def _store_audio_attachment(
    data: bytes,
    att: AttachmentData,
    manager,
    building_id: str,
    sync_summary: bool = False,
) -> Dict[str, Any]:
    """Normalize audio with ffmpeg and create an audio Item.

    Raises RuntimeError on ffmpeg unavailable or normalization failure (e.g. duration exceeded).

    ``sync_summary`` (グローバル設定「添付したメディアの内容を自動想起に使う」ON 時):
    ``ensure_audio_summary`` を同期実行し、返り値の ``summary`` に載せる。この
    関数は ``.summary.txt`` サイドカーにキャッシュするので、後で
    ``llm_clients/gemini.py`` 側 (モデルが音声非対応のとき) が同じ関数を呼んでも
    二重生成にはならない。OFF 時は呼ばない (従来どおり概要は生成されない)。
    """
    import tempfile
    from saiverse.ffmpeg_runner import is_ffmpeg_available, normalize_audio

    if not is_ffmpeg_available():
        raise RuntimeError("ffmpeg is not available; audio attachment cannot be processed.")

    suffix = Path(att.filename).suffix or ".bin"
    tmp_input: Optional[Path] = None
    try:
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp.write(data)
            tmp_input = Path(tmp.name)

        dest_dir = get_saiverse_home() / "audio"
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest_name = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex}.ogg"
        dest_path = dest_dir / dest_name

        success, error = normalize_audio(tmp_input, dest_path, max_duration=300.0)
        if not success:
            raise RuntimeError(f"Audio normalization failed: {error}")
    finally:
        if tmp_input is not None and tmp_input.exists():
            try:
                tmp_input.unlink()
            except OSError:
                logging.warning("Failed to remove temp audio file: %s", tmp_input)

    # Create audio Item (is_open=True so it appears in visual_context)
    item_id = None
    try:
        item_id = manager.create_audio_item_for_user(
            name=att.filename,
            description=f"User uploaded audio: {att.filename}",
            file_path=str(dest_path),
            building_id=building_id,
            is_open=True,
            creator_id="user",
            source_context='{"source": "upload"}',
        )
    except Exception as e:
        logging.warning("Failed to create audio item: %s", e, exc_info=True)

    summary: Optional[str] = None
    if sync_summary:
        try:
            from saiverse.media_summary import ensure_audio_summary
            summary = ensure_audio_summary(dest_path, "audio/ogg")
        except Exception as e:
            logging.warning("Synchronous audio summary generation failed for %s: %s", dest_path, e)

    return {
        "type": "audio",
        "uri": f"saiverse://audio/{dest_name}",
        "mime_type": "audio/ogg",
        "source": "user_upload",
        "path": str(dest_path),
        "item_id": item_id,
        "summary": summary,
    }


def _store_video_attachment(
    data: bytes,
    att: AttachmentData,
    manager,
    building_id: str,
    sync_summary: bool = False,
) -> Dict[str, Any]:
    """Normalize video with ffmpeg and create a video Item.

    Raises RuntimeError on ffmpeg unavailable or normalization failure.

    ``sync_summary``: 音声と同様、ON 時のみ ``ensure_video_summary`` を同期実行
    する。``.summary.txt`` キャッシュを共用するため二重生成にはならない。
    """
    import tempfile
    from saiverse.ffmpeg_runner import is_ffmpeg_available, normalize_video

    if not is_ffmpeg_available():
        raise RuntimeError("ffmpeg is not available; video attachment cannot be processed.")

    suffix = Path(att.filename).suffix or ".bin"
    tmp_input: Optional[Path] = None
    try:
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp.write(data)
            tmp_input = Path(tmp.name)

        dest_dir = get_saiverse_home() / "video"
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest_name = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex}.mp4"
        dest_path = dest_dir / dest_name

        success, error = normalize_video(tmp_input, dest_path, max_duration=90.0)
        if not success:
            raise RuntimeError(f"Video normalization failed: {error}")
    finally:
        if tmp_input is not None and tmp_input.exists():
            try:
                tmp_input.unlink()
            except OSError:
                logging.warning("Failed to remove temp video file: %s", tmp_input)

    item_id = None
    try:
        item_id = manager.create_video_item_for_user(
            name=att.filename,
            description=f"User uploaded video: {att.filename}",
            file_path=str(dest_path),
            building_id=building_id,
            is_open=True,
            creator_id="user",
            source_context='{"source": "upload"}',
        )
    except Exception as e:
        logging.warning("Failed to create video item: %s", e, exc_info=True)

    summary: Optional[str] = None
    if sync_summary:
        try:
            from saiverse.media_summary import ensure_video_summary
            summary = ensure_video_summary(dest_path, "video/mp4")
        except Exception as e:
            logging.warning("Synchronous video summary generation failed for %s: %s", dest_path, e)

    return {
        "type": "video",
        "uri": f"saiverse://video/{dest_name}",
        "mime_type": "video/mp4",
        "source": "user_upload",
        "path": str(dest_path),
        "item_id": item_id,
        "summary": summary,
    }


def _register_uploaded_video_by_uri(
    att: AttachmentData,
    manager,
    building_id: str,
    sync_summary: bool = False,
) -> Dict[str, Any]:
    """Register a video that was already uploaded + normalized via /api/media/upload-video.

    Frontend uploads large video files directly via multipart to avoid base64
    in-memory ballooning, then references the saved file by saiverse:// URI here.
    No re-decode / re-normalize; we just locate the existing file and create
    the video Item.

    ``sync_summary``: same as ``_store_video_attachment`` — synchronous
    ``ensure_video_summary`` only when the global option is ON.
    """
    if not att.uri or not att.uri.startswith("saiverse://video/"):
        raise RuntimeError(f"Invalid video URI: {att.uri!r}")
    filename = att.uri.replace("saiverse://video/", "", 1)
    if not filename or "/" in filename or "\\" in filename:
        raise RuntimeError(f"Invalid video filename in URI: {att.uri!r}")

    dest_path = get_saiverse_home() / "video" / filename
    if not dest_path.exists():
        raise RuntimeError(f"Video file not found for URI {att.uri!r}")

    item_id = None
    try:
        item_id = manager.create_video_item_for_user(
            name=att.filename,
            description=f"User uploaded video: {att.filename}",
            file_path=str(dest_path),
            building_id=building_id,
            is_open=True,
            creator_id="user",
            source_context='{"source": "upload"}',
        )
    except Exception as e:
        logging.warning("Failed to create video item: %s", e, exc_info=True)

    summary: Optional[str] = None
    if sync_summary:
        try:
            from saiverse.media_summary import ensure_video_summary
            summary = ensure_video_summary(dest_path, "video/mp4")
        except Exception as e:
            logging.warning("Synchronous video summary generation failed for %s: %s", dest_path, e)

    return {
        "type": "video",
        "uri": att.uri,
        "mime_type": "video/mp4",
        "source": "user_upload",
        "path": str(dest_path),
        "item_id": item_id,
        "summary": summary,
    }


def _store_uploaded_attachment_v2(
    att: AttachmentData,
    manager,
    building_id: str,
    user_message: str = "",
    prev_ai_message: str = "",
    sync_summary: bool = False,
) -> Optional[Dict[str, Any]]:
    """Process attachment and create appropriate Item type.

    ``sync_summary``: グローバル設定「添付したメディアの内容を自動想起に使う」
    (``manager.state.media_recall_enabled``) が ON のとき True。ON 時のみ
    画像/音声/動画の概要生成を同期実行し、返り値の ``summary`` に載せる。
    """
    try:
        # URI-mode: file already uploaded via /api/media/upload-video.
        # Skip base64 decode entirely. Only video uses this path today.
        if att.uri and not att.data:
            if att.type == "video":
                return _register_uploaded_video_by_uri(att, manager, building_id, sync_summary=sync_summary)
            raise RuntimeError(f"URI-mode attachments not supported for type {att.type!r}")

        if not att.data:
            raise RuntimeError("Attachment requires either 'data' or 'uri'")

        # Decode base64
        header, encoded = att.data.split(",", 1) if "," in att.data else ("", att.data)
        data = base64.b64decode(encoded)

        if att.type == 'image':
            return _store_image_attachment(data, att, manager, building_id, user_message, prev_ai_message, sync_summary=sync_summary)
        elif att.type == 'document':
            return _store_document_attachment(data, att, manager, building_id)
        elif att.type == 'audio':
            return _store_audio_attachment(data, att, manager, building_id, sync_summary=sync_summary)
        elif att.type == 'video':
            return _store_video_attachment(data, att, manager, building_id, sync_summary=sync_summary)
        else:
            # Unknown type: determine from extension
            ext = Path(att.filename).suffix.lower().lstrip('.')
            if ext in IMAGE_EXTENSIONS:
                return _store_image_attachment(data, att, manager, building_id, user_message, prev_ai_message, sync_summary=sync_summary)
            elif ext in AUDIO_EXTENSIONS:
                return _store_audio_attachment(data, att, manager, building_id, sync_summary=sync_summary)
            elif ext in VIDEO_EXTENSIONS:
                return _store_video_attachment(data, att, manager, building_id, sync_summary=sync_summary)
            elif ext in TEXT_EXTENSIONS:
                return _store_document_attachment(data, att, manager, building_id)
            else:
                # Default to image for compatibility
                return _store_image_attachment(data, att, manager, building_id, user_message, prev_ai_message, sync_summary=sync_summary)
    except RuntimeError as e:
        # User-facing error (e.g. duration exceeded, ffmpeg failed) — surface message
        logging.warning("Attachment rejected: %s", e)
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logging.error(f"Failed to process attachment: {e}")
        return None

@router.post("/stop")
def stop_generation(manager = Depends(get_manager)):
    """Stop the active LLM generation for the user's current building."""
    cancelled = manager.cancel_active_generation()
    return {"cancelled": cancelled}


def _cleanup_attachment_items(
    manager, items: List[tuple], building_id: str, context: str
) -> None:
    """位置競合で拒否した発言の添付 Item を片付ける (best-effort + 失敗の記録)。

    `delete_item` は失敗を例外でなく "Error: ..." 文字列で返す契約のため、
    戻り値検査が必須 (2026-07-21 Codex 第七巡 P2)。Item 作成時に書かれた
    「User uploaded ...」の host 履歴は削除せず、撤去の**補記**を同じ機構で
    追記する (第八巡 P1 — 履歴が削除済み Item を指したまま残らないように。
    削除口を新設せず追記で正直に補償する)。保存済みファイルは削除しない
    (URI モードの動画は再送が同じファイルを参照するため)。

    ``items`` は ``[(item_id, filename), ...]``。
    """
    removed_names: List[str] = []
    for item_id, filename in items:
        try:
            result = manager.delete_item(item_id)
        except Exception:
            logging.warning(
                "Failed to clean up attachment item %s (%s)",
                item_id, context, exc_info=True,
            )
            continue
        if isinstance(result, str) and result.startswith("Error"):
            logging.warning(
                "Attachment item %s cleanup reported failure (%s): %s",
                item_id, context, result,
            )
            continue
        removed_names.append(filename or item_id)
    if removed_names:
        try:
            names = ", ".join(f'"{name}"' for name in removed_names)
            note = (
                '<div class="note-box">🗑 System:<br>'
                f'<b>Upload of {names} was withdrawn (the utterance was '
                'refused due to a location change).</b></div>'
            )
            manager._append_building_history_note(building_id, note)
        except Exception:
            logging.warning(
                "Failed to append withdrawal note for building %s (%s)",
                building_id, context, exc_info=True,
            )


@router.post("/send")
def send_message(req: SendMessageRequest, manager = Depends(get_manager)):
    # manager.user_current_building_id は _refresh_user_state_cache が作る遅延
    # mirror で、移動確定 (state 更新) から wrapper 戻りまでの間 stale になる。
    # 境界照合は canonical な state を読む (2026-07-21 Codex 第三巡 P2)。
    current_bid = manager.state.user_current_building_id
    building_id = req.building_id or current_bid
    if not building_id:
        raise HTTPException(status_code=400, detail="User is not in any building")

    # 分離監査 P1-3 (W7 柱5): raw /send はサーバ現在地専用。別 Building への
    # 発言は単一位置モデルの迂回 (不在の部屋に「居た」履歴が残る) なので拒否し、
    # 発言契機入室 (/chat/utter) へ誘導する。/chat/utter は移動を確定させてから
    # 本関数を呼ぶため、正規経路では常に一致する。
    if req.building_id and req.building_id != current_bid:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "not_in_building",
                "message": (
                    "現在地ではない建物への発言はできません。"
                    "入室を伴う発言は /chat/utter を使ってください。"
                ),
                "current_building_id": current_bid,
            },
        )

    if not req.message and not req.attachment and not req.attachments:
        raise HTTPException(status_code=400, detail="Message or attachment required")

    # Combine metadata
    metadata = req.metadata or {}

    # Get last AI message for contextual image description
    prev_ai_message = ""
    try:
        history = manager.get_building_history(building_id)
        for msg in reversed(history):
            if msg.get("role") in ("assistant", "model"):
                content = msg.get("content", "")
                if content and not content.startswith('<div class="note-box"'):
                    prev_ai_message = content[:500]
                    break
    except Exception:
        pass

    # グローバル設定「添付したメディアの内容を自動想起に使う」(既定 OFF)。ON 時のみ
    # 画像/音声/動画の概要生成を同期実行し、この送信リクエストの遅延と引き換えに
    # sea/auto_recall.py の build_query が拾える summary を metadata に載せる。
    media_recall_enabled = bool(getattr(manager.state, "media_recall_enabled", False))

    # 添付は Item として building へ永続化されるため、処理後に現在地を再照合して
    # 「別デバイスの移動と競合した添付だけが旧 Building に残る」経路を塞ぐ
    # (2026-07-21 Codex 第四巡 P2)。作成 Item を (id, filename) で控えて
    # 競合時に片付ける。
    created_items: List[tuple] = []

    # Handle new multi-attachment format
    if req.attachments:
        images = []
        documents = []
        media_entries = []  # for audio/video, consumed by iter_audio_media / iter_video_media
        for att in req.attachments:
            result = _store_uploaded_attachment_v2(
                att, manager, building_id,
                user_message=req.message or "",
                prev_ai_message=prev_ai_message,
                sync_summary=media_recall_enabled,
            )
            if result:
                if result.get("item_id"):
                    created_items.append((result["item_id"], att.filename))
                if result["type"] == "image":
                    entry = {
                        "uri": result["uri"],
                        "path": result["path"],
                        "mime_type": result["mime_type"],
                        "item_id": result.get("item_id"),
                        "item_name": att.filename  # For history context
                    }
                    if result.get("summary"):
                        entry["summary"] = result["summary"]
                    images.append(entry)
                elif result["type"] == "document":
                    documents.append({
                        "uri": result["uri"],
                        "path": result["path"],
                        "mime_type": result["mime_type"],
                        "item_id": result.get("item_id"),
                        "item_name": att.filename,  # For history context
                        "content_preview": result.get("content_preview")
                    })
                elif result["type"] in ("audio", "video"):
                    entry = {
                        "type": result["type"],
                        "uri": result["uri"],
                        "path": result["path"],
                        "mime_type": result["mime_type"],
                        "item_id": result.get("item_id"),
                        "item_name": att.filename,
                    }
                    if result.get("summary"):
                        entry["summary"] = result["summary"]
                    media_entries.append(entry)
        if images:
            metadata["images"] = images
        if documents:
            metadata["documents"] = documents
        if media_entries:
            metadata["media"] = media_entries

    # Handle legacy single attachment format (backwards compatibility)
    elif req.attachment:
        attachment_info = _store_uploaded_attachment(req.attachment)
        if attachment_info:
            metadata["images"] = [
                {"uri": attachment_info["uri"], "path": attachment_info["path"], "mime_type": attachment_info["mime_type"]}
            ]

    # 添付処理 (概要生成の同期実行で数秒かかりうる) の間に別デバイスが移動して
    # いないか再照合。競合していたら作成済み Item を片付けて 409 (runtime 層の
    # 最終照合はこの後も残る — そちらは発言を拒否するだけで Item は消せない)。
    recheck_bid = manager.state.user_current_building_id
    if recheck_bid != building_id:
        _cleanup_attachment_items(
            manager, created_items, building_id, "route recheck"
        )
        raise HTTPException(
            status_code=409,
            detail={
                "code": "not_in_building",
                "message": (
                    "送信処理中に現在地が変わったため、発言は受け付けられ"
                    "ませんでした。最新状態に同期します。"
                ),
                "current_building_id": recheck_bid,
            },
        )

    # For V1, we will consume the stream and return the full response.
    # Future improvement: Use StreamingResponse
    # NOTE: 関数内で `import logging` / `import json` すると名前が関数全体で
    # ローカル扱いになり、それより前の分岐 (添付競合 cleanup 等) で
    # UnboundLocalError になる (2026-07-21 Codex 第五巡 P2) — module import を使う。
    def response_generator():
        # ここで起きる失敗は、下の `return StreamingResponse(...)` を抜けた**後**に
        # 走る。外側に try/except を置いても届かない (ジェネレータの本体は、返した
        # 時点ではまだ一行も実行されていない)。ヘッダ送出後の例外はストリームが
        # 途中で切れるだけになり、画面には何も出ないので、本体をここで包む。
        # 設計: docs/issues/user_utterance_path_failure_inventory.md
        try:
            # Yield an initial status event to flush headers (with padding for buffering)
            yield json.dumps({"type": "status", "content": "processing"}, ensure_ascii=False) + " " * 2048 + "\n"

            # 遅延 generator 開始までにも競合窓がある (Codex 第五巡 P2):
            # ここを通過した後の移動は runtime 層が発言を拒否するが、Item の
            # 片付けは route 側にしかできないため、runtime 呼び出し直前にも
            # 最終照合 + cleanup を行う。
            live_bid = manager.state.user_current_building_id
            if live_bid != building_id:
                _cleanup_attachment_items(
                    manager, created_items, building_id, "stream start recheck"
                )
                yield json.dumps({
                    "type": "error",
                    "error_code": "not_in_building",
                    "content": (
                        "送信処理中に現在地が変わったため、発言は受け付けられ"
                        "ませんでした。最新状態に同期します。"
                    ),
                    "current_building_id": live_bid,
                }, ensure_ascii=False) + "\n"
                return

            stream = manager.handle_user_input_stream(
                req.message,
                metadata=metadata,
                meta_playbook=req.meta_playbook,
                args=req.args,
                building_id=building_id,
                pre_spells=req.pre_spells,
                client_message_id=req.client_message_id,
            )

            for chunk in stream:
                # runtime 層 (境界照合 / 永続化 tx 内検証) が位置競合で発言を
                # 拒否した場合、Item の片付けは route にしかできない
                # (2026-07-21 Codex 第六巡 P2)。誤爆防止に JSON parse で確認。
                if (
                    created_items
                    and isinstance(chunk, str)
                    and "not_in_building" in chunk
                ):
                    try:
                        event = json.loads(chunk)
                    except ValueError:
                        event = None
                    if (
                        isinstance(event, dict)
                        and event.get("error_code") == "not_in_building"
                    ):
                        _cleanup_attachment_items(
                            manager, created_items, building_id,
                            "runtime refusal",
                        )
                        created_items.clear()
                yield chunk
        except Exception as e:
            logging.error("Error while streaming the reply", exc_info=True)
            yield json.dumps({
                "type": "error",
                "error_code": "unknown",
                "content": "応答の配信中にエラーが発生しました。",
                "technical_detail": str(e),
            }, ensure_ascii=False) + "\n"

    return StreamingResponse(response_generator(), media_type="application/x-ndjson")


# ---- Utter: 発言契機入室 (C-2) ----

class UtterRequest(BaseModel):
    """発言契機入室エンドポイント /chat/utter のリクエスト。

    target_building_id がサーバ側の current_building_id と異なれば、 まず move を
    実行してから通常の chat 経路 (= send_message 内部処理) に流す。 「閲覧モード
    から別建物へ発言したら自動入室」 という UX を backend 側で保証する。

    コマンドの意味論 (分離監査 P1-3 / W7 柱5 で正直化):
    - **入室**は `move.entity` 台帳実行として原子的に確定する (W5)。
    - **発言**は durable insert が認知開始の前提条件 (insert 失敗 = Pulse 不起動、
      エラーイベントで返す)。
    - 「入室成功 → 発言 insert 失敗」では入室は残る (発言契機の入室は物理事実)。
      再送は current == target になるため move をスキップし、`client_message_id`
      の冪等キーで発言は一度だけ載る。
    - 並行デバイスの競合は expected_from_building_id の CAS (409) が検出する。

    See: docs/intent/building_memory_unified.md §C-2
    """
    message: str
    target_building_id: str  # 発言先 (= 必須、 現在地と違えば自動 move する)
    # B-1 CAS: クライアントが知っている現在地。 サーバの current_building_id と
    # 一致しなければ 409 (他クライアントが先に移動済み)。 後方互換のため Optional。
    expected_from_building_id: Optional[str] = None
    attachment: Optional[str] = None
    attachments: Optional[List[AttachmentData]] = None
    meta_playbook: Optional[str] = None
    args: Optional[Dict[str, Any]] = None
    metadata: Optional[Dict[str, Any]] = None
    pre_spells: Optional[List[str]] = None
    client_message_id: Optional[str] = None  # B-2 idempotency


@router.post("/utter")
def utter_message(req: UtterRequest, manager = Depends(get_manager)):
    """発言契機入室。 必要なら自動 move を伴って chat を実行する。"""
    current_bid = manager.state.user_current_building_id

    # 1. 必要なら自動 move (atomic leave + enter)
    if current_bid != req.target_building_id:
        # B-1 CAS: クライアントが思っている現在地とサーバが違うなら 409
        if (
            req.expected_from_building_id is not None
            and req.expected_from_building_id != current_bid
        ):
            logging.info(
                "[USER_UTTER] CAS conflict: expected_from=%s server_current=%s — refusing",
                req.expected_from_building_id, current_bid,
            )
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "cas_conflict",
                    "message": "他のクライアントが先に移動したため、 発言は受け付けられませんでした。 最新状態に同期します。",
                    "current_building_id": current_bid,
                },
            )

        # auto-move
        logging.info(
            "[USER_UTTER] Auto-move %s -> %s before utter",
            current_bid, req.target_building_id,
        )
        success, msg = manager.move_user(req.target_building_id)
        if not success:
            # サーバ側 CAS (move_entity の条件付き UPDATE) の競合は、クライアント
            # CAS と同じ 409 で再同期を起動する (W7 柱5 / 2026-07-21 Codex P2)
            if getattr(msg, "code", None) == "cas_conflict":
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "cas_conflict",
                        "message": str(msg),
                        # 拒否メッセージが運ぶ DB 確定現在地を優先 (第三巡 P2)
                        "current_building_id": (
                            getattr(msg, "current_building_id", None)
                            or manager.state.user_current_building_id
                        ),
                    },
                )
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "move_failed",
                    "message": f"発言先への移動に失敗: {msg}",
                    "current_building_id": manager.state.user_current_building_id,
                },
            )

    # 2. 通常の chat 処理に委譲 (= send_message 関数を直接呼ぶ)
    send_req = SendMessageRequest(
        message=req.message,
        building_id=req.target_building_id,
        attachment=req.attachment,
        attachments=req.attachments,
        meta_playbook=req.meta_playbook,
        args=req.args,
        metadata=req.metadata,
        pre_spells=req.pre_spells,
        client_message_id=req.client_message_id,
    )
    return send_message(send_req, manager)


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
# Generic spell confirmation (X, SwitchBot, future addons)
# ---------------------------------------------------------------------------

class SpellConfirmationRequest(BaseModel):
    request_id: str
    decision: str  # approve | reject | edit
    edited_text: Optional[str] = None


@router.post("/spell-confirmation-response")
def respond_to_spell_confirmation(req: SpellConfirmationRequest, manager=Depends(get_manager)):
    """Respond to a generic spell confirmation request."""
    valid_decisions = ("approve", "reject", "edit")
    if req.decision not in valid_decisions:
        raise HTTPException(status_code=400, detail=f"Invalid decision. Must be one of: {valid_decisions}")

    event = manager._pending_spell_confirmations.get(req.request_id)
    if not event:
        raise HTTPException(status_code=404, detail="Spell confirmation request not found or expired")

    response_value = req.decision
    if req.decision == "edit" and req.edited_text:
        response_value = f"edit:{req.edited_text}"

    manager._spell_confirmation_responses[req.request_id] = response_value
    event.set()
    return {"success": True}
