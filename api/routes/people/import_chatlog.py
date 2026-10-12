from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks, UploadFile, File, Form
from api.deps import get_manager
from api.file_safety import safe_upload_suffix, write_upload_bounded
from .models import (
    ConversationSummary, PreviewResponse, ImportRequest,
    OfficialImportStatusResponse, ExtensionImportStatusResponse
)
from .utils import ensure_persona_exists
import tempfile
from pathlib import Path
import threading
import logging
import uuid
from typing import List, Optional

LOGGER = logging.getLogger(__name__)
router = APIRouter()
CHATLOG_IMPORT_MAX_BYTES = 2 * 1024 * 1024 * 1024

# Store parsed exports temporarily (in-memory cache for preview -> import flow)
_chatgpt_export_cache: dict = {}

# Track import status per persona
_extension_import_status: dict = {}
_extension_import_lock = threading.Lock()

_official_import_status: dict = {}
_official_import_lock = threading.Lock()

# Embeddings of imported messages are made after the import has finished
# (docs/intent/embedding_gemma2_migration.md §2). Each message is stored
# together with an empty vector row (``storage.DEFERRED_VECTOR``, written in
# the same transaction as the message), the import reports success, and a
# background thread then re-embeds the persona's stale messages, the empty
# rows among them (``SAIMemoryAdapter.reembed_stale_messages``).
#
# Nothing in this module is needed for that to end correctly: the work left
# is in the memory.db itself. If the thread fails or the process dies, the
# startup check warns about the empty rows and the banner's re-embed fills
# them. The per-persona chain of threads (each waits for the previous one)
# only avoids embedding one persona twice at the same time; the work is
# idempotent, so two at once would only waste time.
#
# The status kept here is for display only: one entry per persona, written
# only by the persona's latest thread (it redoes whatever an earlier one
# left). An import whose embedding was handed over carries "embedding": True
# in its own status, and its status endpoint shows this per-persona entry; a
# "skip" import does not, and shows none.
_import_embedding_lock = threading.Lock()
# persona -> {"embedding_running", "embedding_progress", "embedding_total",
#             "embedding_failed", "embedding_error"}
_import_embedding_status: dict = {}
# Latest embedding thread per persona (tests join it).
_import_embedding_threads: dict = {}


def _set_embedding_status(persona_id: str, fields: dict) -> None:
    """Write the persona's status, but only from the persona's latest thread."""
    with _import_embedding_lock:
        if _import_embedding_threads.get(persona_id) is threading.current_thread():
            _import_embedding_status[persona_id] = fields


def _embed_imported_messages(
    persona_id: str, adapter, previous: Optional[threading.Thread] = None,
) -> None:
    """Fill the persona's stale message vectors, then close the import's adapter.

    Waits first for ``previous`` (the persona's earlier embedding thread), so
    one persona is embedded by one thread at a time without making the import
    wait.
    """
    if previous is not None and previous.is_alive():
        LOGGER.info("Import embedding for %s waits for the previous one", persona_id)
        previous.join()

    def _progress(done: int, total: int) -> None:
        _set_embedding_status(persona_id, {
            "embedding_running": True, "embedding_progress": done, "embedding_total": total,
        })

    result: dict = {}
    error: Optional[str] = None
    try:
        result = adapter.reembed_stale_messages(progress_callback=_progress)
        changed = int(result.get("changed", 0) or 0)
        if changed:
            error = (
                f"{changed} message(s) were edited while being embedded and still "
                "wait for their embeddings; the re-embed fills them."
            )
    except Exception as exc:
        LOGGER.exception("Import embedding failed for %s", persona_id)
        error = f"{type(exc).__name__}: {exc}".splitlines()[0]
    finally:
        try:
            adapter.close()
        finally:
            final = {
                "embedding_running": False,
                "embedding_progress": result.get("embedded", 0),
                "embedding_total": result.get("total", 0),
                "embedding_failed": 0,
            }
            if error:
                final["embedding_error"] = error
            _set_embedding_status(persona_id, final)
    if error:
        LOGGER.warning("Import embedding for %s ended with a problem: %s", persona_id, error)
    LOGGER.info(
        "Import embedding finished for %s: embedded=%d changed=%d (of %d)",
        persona_id, result.get("embedded", 0), result.get("changed", 0), result.get("total", 0),
    )


def _launch_import_embedding(persona_id: str, adapter, count: int) -> bool:
    """Start the persona's embedding thread; it takes ``adapter`` over and closes it.

    The thread is created, registered as the persona's latest and started in
    one step under the lock, so two imports finishing at once cannot both
    miss each other's thread. Returns False (and shows the failure) when the
    thread could not be started; the caller then closes the adapter. The
    empty vector rows stay either way.
    """
    with _import_embedding_lock:
        previous = _import_embedding_threads.get(persona_id)
        thread = threading.Thread(
            target=_embed_imported_messages,
            args=(persona_id, adapter, previous),
            name=f"import-embedding-{persona_id}",
            daemon=True,
        )
        _import_embedding_threads[persona_id] = thread
        _import_embedding_status[persona_id] = {
            "embedding_running": True, "embedding_progress": 0, "embedding_total": count,
        }
        try:
            thread.start()
        except Exception as exc:
            LOGGER.exception("Could not start the import embedding for %s", persona_id)
            _import_embedding_status[persona_id] = {
                "embedding_running": False, "embedding_progress": 0,
                "embedding_total": count, "embedding_failed": 0,
                "embedding_error": f"{type(exc).__name__}: {exc}".splitlines()[0],
            }
            return False
        return True


def _with_embedding_status(persona_id: str, status: dict) -> dict:
    """Import status plus the persona's background embedding, if this import handed one over."""
    shown = {k: v for k, v in status.items() if k != "embedding"}
    if not status.get("embedding"):
        return shown
    with _import_embedding_lock:
        embedding = dict(_import_embedding_status.get(persona_id, {}))
    return {**shown, **embedding}

def _is_placeholder_title(title: Optional[str]) -> bool:
    """取り込み元のパーサーが「題名なし」の印として入れる既定値か。

    chatgpt_importer は ``"(untitled)"``、chatlog_exporter_importer は
    ``"Untitled"`` を入れる。大文字小文字と括弧の有無は問わない — どちらも
    人が付けた題名ではないので、スレッド名として保存しない。
    """
    if not title:
        return True
    normalized = str(title).strip().lower().strip("()")
    return normalized in ("", "untitled")


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
        # 1. Parse file (never waits for an earlier import's embedding: that
        #    queue lives in the background threads)
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
        from saiverse_memory.adapter import EMBEDDING_DEFERRED
        adapter = SAIMemoryAdapter(persona_id)
        handed_off = False
        embedding_chunks = 0 if skip_embedding else EMBEDDING_DEFERRED

        try:
            msg_count = 0
            imported_ids: List[str] = []
            for i, payload in enumerate(payloads):
                # Never embed while importing: with skip_embedding the
                # messages stay unembedded, otherwise each is stored with an
                # empty vector row and embedded in the background after the
                # import has finished.
                payload["embedding_chunks"] = embedding_chunks
                mid = adapter.append_persona_message(payload, thread_suffix=thread_suffix)
                if mid:
                    imported_ids.append(mid)
                msg_count += 1

                # Update progress every 10 messages
                if msg_count % 10 == 0:
                    with _extension_import_lock:
                        _extension_import_status[persona_id] = {
                            "running": True, "progress": msg_count, "total": total,
                            "message": f"Importing {msg_count}/{total} messages..."
                        }

            # 会話タイトルをスレッド名として保存 (スレッド選択 UI が suffix の
            # UUID ではなく題名で選べるように)。パーサーの既定値は題名が無かった
            # 印なので書かない。
            if not _is_placeholder_title(conversation.title):
                adapter.set_thread_title(thread_suffix, conversation.title)

            embedding = not skip_embedding and bool(imported_ids)
            if embedding:
                handed_off = _launch_import_embedding(persona_id, adapter, len(imported_ids))
            with _extension_import_lock:
                _extension_import_status[persona_id] = {
                    "running": False, "progress": msg_count, "total": total,
                    "message": f"Imported '{conversation.title}' ({msg_count} messages).",
                    "success": True, "title": conversation.title,
                    "embedding": embedding,
                }
        finally:
            if not handed_off:
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
            except Exception:
                LOGGER.warning("Failed to delete temp file", exc_info=True)

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
        # 1. Retrieve cached export (never waits for an earlier import's
        #    embedding: that queue lives in the background threads)
        cached = _chatgpt_export_cache.get(cache_key)
        if not cached:
            with _official_import_lock:
                _official_import_status[persona_id] = {
                    "running": False, "message": "Preview expired. Please upload again.",
                    "success": False
                }
            return
        
        export = cached["export"]
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
        from saiverse_memory.adapter import EMBEDDING_DEFERRED
        adapter = SAIMemoryAdapter(persona_id)
        handed_off = False
        embedding_chunks = 0 if skip_embedding else EMBEDDING_DEFERRED

        try:
            total_conversations = len(selected_records)
            imported_count = 0
            msg_count = 0
            imported_ids: List[str] = []
            
            for conv_idx, record in enumerate(selected_records):
                payloads = list(record.iter_memory_payloads(include_roles=["user", "assistant"]))
                thread_suffix = record.conversation_id or record.identifier
                
                for payload in payloads:
                    meta = payload.get("metadata", {})
                    tags = meta.get("tags", [])
                    if "conversation" not in tags:
                        tags.append("conversation")
                    meta["tags"] = tags
                    # Never embed while importing (see the extension task).
                    payload["embedding_chunks"] = embedding_chunks
                    payload["metadata"] = meta

                    mid = adapter.append_persona_message(payload, thread_suffix=thread_suffix)
                    if mid:
                        imported_ids.append(mid)
                    msg_count += 1

                # 会話タイトルをスレッド名として保存。パーサーの既定値 (題名の
                # 無い会話の印) は書かない。
                if not _is_placeholder_title(record.title):
                    adapter.set_thread_title(thread_suffix, record.title)

                imported_count += 1
                
                with _official_import_lock:
                    _official_import_status[persona_id] = {
                        "running": True, "progress": imported_count, "total": total_conversations,
                        "message": f"Imported {imported_count}/{total_conversations} conversations ({msg_count} messages)..."
                    }
            
            embedding = not skip_embedding and bool(imported_ids)
            if embedding:
                handed_off = _launch_import_embedding(persona_id, adapter, len(imported_ids))
            with _official_import_lock:
                _official_import_status[persona_id] = {
                    "running": False, "progress": imported_count, "total": total_conversations,
                    "message": f"Imported {imported_count} conversations ({msg_count} messages).",
                    "success": True, "conversations": imported_count, "messages": msg_count,
                    "embedding": embedding,
                }
        finally:
            if not handed_off:
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
async def preview_official_chatgpt(
    persona_id: str,
    file: UploadFile = File(...),
    manager = Depends(get_manager)
):
    """Preview ChatGPT export file and return conversation list for selection."""
    
    # 1. Save upload to temp file
    suffix = safe_upload_suffix(file.filename)
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp_path = Path(tmp.name)
    tmp_path.unlink()
    await write_upload_bounded(file, tmp_path, CHATLOG_IMPORT_MAX_BYTES)

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
            except Exception:
                LOGGER.warning("Failed to delete temp file", exc_info=True)
        raise HTTPException(status_code=400, detail=f"Preview failed: {str(e)}")

@router.post("/{persona_id}/import/official")
def import_official_chatgpt(
    persona_id: str,
    request: ImportRequest,
    background_tasks: BackgroundTasks,
    manager = Depends(get_manager)
):
    """Import selected ChatGPT conversations from a previously previewed export (background)."""
    # The background task builds SAIMemoryAdapter(persona_id) directly, so gate
    # unknown/malformed IDs here (no orphan personas/<id>/memory.db creation).
    ensure_persona_exists(persona_id, manager)
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
    return OfficialImportStatusResponse(**_with_embedding_status(persona_id, status))

@router.post("/{persona_id}/import/extension")
async def import_extension_export(
    persona_id: str,
    file: UploadFile = File(...),
    skip_embedding: bool = Form(False),
    background_tasks: BackgroundTasks = None,
    manager = Depends(get_manager)
):
    """Import Chrome extension export (JSON or Markdown) in background."""
    from fastapi import BackgroundTasks as BT

    # The background task builds SAIMemoryAdapter(persona_id) directly, so gate
    # unknown/malformed IDs here (no orphan personas/<id>/memory.db creation).
    ensure_persona_exists(persona_id, manager)

    # Check if already running
    with _extension_import_lock:
        status = _extension_import_status.get(persona_id, {})
        if status.get("running"):
            raise HTTPException(status_code=409, detail="Import already in progress.")
    
    # 1. Save upload to temp file (will be deleted by background task)
    suffix = safe_upload_suffix(file.filename)
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp_path = Path(tmp.name)
    tmp_path.unlink()
    await write_upload_bounded(file, tmp_path, CHATLOG_IMPORT_MAX_BYTES)

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
    return ExtensionImportStatusResponse(**_with_embedding_status(persona_id, status))
