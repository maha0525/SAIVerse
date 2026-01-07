from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks, UploadFile, File, Form
from api.deps import get_manager
from .models import (
    ConversationSummary, PreviewResponse, ImportRequest,
    OfficialImportStatusResponse, ExtensionImportStatusResponse
)
import shutil
import tempfile
from pathlib import Path
import threading
import logging
import uuid
from typing import List

LOGGER = logging.getLogger(__name__)
router = APIRouter()

# Store parsed exports temporarily (in-memory cache for preview -> import flow)
_chatgpt_export_cache: dict = {}

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

@router.post("/{persona_id}/import/official/preview", response_model=PreviewResponse)
def preview_official_chatgpt(
    persona_id: str,
    file: UploadFile = File(...),
    manager = Depends(get_manager)
):
    """Preview ChatGPT export file and return conversation list for selection."""
    
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

@router.get("/{persona_id}/import/extension/status", response_model=ExtensionImportStatusResponse)
def get_extension_import_status(persona_id: str, manager = Depends(get_manager)):
    """Get the status of extension import task."""
    with _extension_import_lock:
        status = _extension_import_status.get(persona_id, {"running": False, "message": "No import task has been run."})
    return ExtensionImportStatusResponse(**status)
