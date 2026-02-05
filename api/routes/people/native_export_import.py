"""Native SAIMemory export/import API endpoints.

Provides full-fidelity thread export and import with all metadata preserved.
"""
from __future__ import annotations

import json
import logging
import tempfile
import threading
from pathlib import Path
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form
from fastapi.responses import Response

from api.deps import get_manager
from .models import NativeImportStatusResponse

LOGGER = logging.getLogger(__name__)
router = APIRouter()

# Track import status per persona
_native_import_status: Dict[str, Dict[str, Any]] = {}
_native_import_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

@router.get("/{persona_id}/threads/{thread_id}/export-native")
def export_thread_native(
    persona_id: str,
    thread_id: str,
    manager=Depends(get_manager),
):
    """Export a single thread as native SAIVerse JSON.

    Returns a downloadable JSON file with all metadata preserved.
    """
    from saiverse_memory.native_export import export_thread_by_id

    try:
        data = export_thread_by_id(persona_id, thread_id)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        LOGGER.exception("Failed to export thread %s for persona %s", thread_id, persona_id)
        raise HTTPException(status_code=500, detail=f"Export failed: {e}")

    # Build filename from thread suffix
    suffix = thread_id.split(":", 1)[1] if ":" in thread_id else thread_id
    safe_suffix = suffix.replace("/", "_").replace("\\", "_")
    filename = f"{persona_id}_{safe_suffix}.json"

    content = json.dumps(data, ensure_ascii=False, indent=2)
    return Response(
        content=content,
        media_type="application/json",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
        },
    )


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------

def _run_native_import_task(
    persona_id: str,
    data: Dict[str, Any],
    skip_embedding: bool,
) -> None:
    """Background task to import native JSON."""
    from saiverse_memory.native_export import import_threads_native

    def progress_callback(current: int, total: int, message: str) -> None:
        with _native_import_lock:
            _native_import_status[persona_id] = {
                "running": True,
                "progress": current,
                "total": total,
                "message": message,
            }

    with _native_import_lock:
        _native_import_status[persona_id] = {
            "running": True,
            "progress": 0,
            "total": 0,
            "message": "Starting import...",
        }

    try:
        result = import_threads_native(
            persona_id,
            data,
            replace=True,
            skip_embed=skip_embedding,
            progress_callback=progress_callback,
        )
        with _native_import_lock:
            _native_import_status[persona_id] = {
                "running": False,
                "progress": result["messages_imported"],
                "total": result["messages_imported"],
                "message": (
                    f"Imported {result['threads_imported']} thread(s), "
                    f"{result['messages_imported']} message(s)"
                ),
                "success": True,
                "threads_imported": result["threads_imported"],
                "messages_imported": result["messages_imported"],
            }
    except Exception as e:
        LOGGER.exception("Native import failed for persona %s", persona_id)
        with _native_import_lock:
            _native_import_status[persona_id] = {
                "running": False,
                "progress": 0,
                "total": 0,
                "message": f"Import failed: {e}",
                "success": False,
            }


@router.post("/{persona_id}/import/native")
async def import_native(
    persona_id: str,
    file: UploadFile = File(...),
    skip_embedding: bool = Form(False),
    manager=Depends(get_manager),
):
    """Import native SAIVerse JSON.

    Replaces existing threads with the same thread_id.
    Runs as a background task with progress tracking.
    """
    # Check if an import is already running
    with _native_import_lock:
        status = _native_import_status.get(persona_id, {})
        if status.get("running"):
            raise HTTPException(status_code=409, detail="An import is already running for this persona")

    # Read and parse the uploaded file
    try:
        raw = await file.read()
        data = json.loads(raw.decode("utf-8-sig"))
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise HTTPException(status_code=400, detail=f"Invalid JSON file: {e}")

    # Basic validation
    fmt = data.get("format")
    if fmt != "saiverse_saimemory_v1":
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported format: {fmt!r} (expected 'saiverse_saimemory_v1')",
        )

    threads = data.get("threads", [])
    total_msgs = sum(len(t.get("messages", [])) for t in threads)

    # Start background task
    thread = threading.Thread(
        target=_run_native_import_task,
        args=(persona_id, data, skip_embedding),
        daemon=True,
    )
    thread.start()

    return {
        "status": "started",
        "threads": len(threads),
        "total_messages": total_msgs,
    }


@router.get("/{persona_id}/import/native/status", response_model=NativeImportStatusResponse)
def get_native_import_status(
    persona_id: str,
    manager=Depends(get_manager),
):
    """Poll native import progress."""
    with _native_import_lock:
        status = _native_import_status.get(persona_id, {
            "running": False,
            "progress": None,
            "total": None,
            "message": None,
        })
    return NativeImportStatusResponse(**status)


@router.post("/{persona_id}/import/native/preview")
async def preview_native_import(
    persona_id: str,
    file: UploadFile = File(...),
    manager=Depends(get_manager),
):
    """Preview a native JSON file before importing.

    Returns thread summaries and message counts without modifying the database.
    """
    try:
        raw = await file.read()
        data = json.loads(raw.decode("utf-8-sig"))
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise HTTPException(status_code=400, detail=f"Invalid JSON file: {e}")

    fmt = data.get("format")
    if fmt != "saiverse_saimemory_v1":
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported format: {fmt!r} (expected 'saiverse_saimemory_v1')",
        )

    source_persona = data.get("persona_id", "unknown")
    threads = data.get("threads", [])

    preview_threads = []
    for t in threads:
        messages = t.get("messages", [])
        first_msg = messages[0] if messages else None
        preview_threads.append({
            "thread_id": t.get("thread_id", ""),
            "message_count": len(messages),
            "has_stelis": t.get("stelis") is not None,
            "preview": (first_msg.get("content", "")[:100] + "...") if first_msg and len(first_msg.get("content", "")) > 100 else (first_msg.get("content", "") if first_msg else ""),
        })

    return {
        "format": fmt,
        "source_persona": source_persona,
        "exported_at": data.get("exported_at"),
        "thread_count": len(threads),
        "total_messages": sum(len(t.get("messages", [])) for t in threads),
        "threads": preview_threads,
    }
