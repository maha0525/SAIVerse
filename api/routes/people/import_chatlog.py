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
# (docs/intent/embedding_gemma2_migration.md §2): the text is stored first and
# the import reports success, then a background thread embeds what it stored.
#
# Each import that hands messages over gets a job number. The import's own
# status carries that number ("embedding_job"), and the status endpoints show
# the embedding state of that job only, so an import never shows the numbers
# of another import's embedding (a "skip" import has no job and shows none).
# The jobs of one persona form a queue: each job's thread first waits for the
# previous job's thread, in the background — the import itself never waits.
#
# Everything below is read and changed only under ``_import_embedding_lock``:
# the job table, the latest thread and job number per persona, and the
# "dirty" flag. The persistent mark in the persona's memory.db
# (``embed_backfill_pending``) is also written and removed under this lock,
# so "is a later job queued?" and "remove the mark" cannot interleave.
_import_embedding_lock = threading.Lock()
# job number -> {"embedding_running", "embedding_progress", "embedding_total",
#                "embedding_failed", "embedding_error"}
_import_embedding_jobs: dict = {}
# Latest embedding thread per persona (tests join it).
_import_embedding_threads: dict = {}
# Latest job number handed out per persona.
_import_embedding_latest: dict = {}
# Per persona: True once a job since the mark was written left work behind.
# Such a mark is removed only by the re-embed (or by a later job that finds
# no mark and writes a fresh one).
_import_embedding_dirty: dict = {}
_import_embedding_seq = 0
# Finished jobs kept for the status endpoints (older ones are dropped).
_KEEP_FINISHED_JOBS = 32


def _embed_imported_messages(
    persona_id: str,
    job: int,
    adapter,
    message_ids: List[str],
    previous: Optional[threading.Thread] = None,
) -> None:
    """Embed the messages one import stored, then close that import's adapter.

    Waits first for ``previous`` (the persona's earlier embedding job), so
    the jobs of one persona run one at a time without making the import
    wait. Uses ``backfill_missing_message_embeddings`` limited to
    ``message_ids``, so messages stored without embeddings on purpose
    elsewhere (an earlier import with "skip", thread-switch notes, ...) stay
    as they are.

    At the end the persistent mark (``embed_backfill_pending``) is removed
    only when this job left nothing behind, no job since the mark was written
    left anything behind, and no later job is queued.
    """
    if previous is not None and previous.is_alive():
        LOGGER.info("Import embedding %d for %s waits for the previous one", job, persona_id)
        previous.join()

    total = len(message_ids)

    def _progress(done: int, total_: int) -> None:
        with _import_embedding_lock:
            _import_embedding_jobs[job] = {
                "embedding_running": True, "embedding_progress": done, "embedding_total": total_,
            }

    _progress(0, total)
    LOGGER.info("Import embedding %d started for %s (%d messages)", job, persona_id, total)
    result: dict = {}
    error: Optional[str] = None
    try:
        result = adapter.backfill_missing_message_embeddings(
            progress_callback=_progress, message_ids=message_ids,
        )
        error = _embedding_result_error(adapter, result)
    except Exception as exc:
        LOGGER.exception("Import embedding failed for %s", persona_id)
        error = f"{type(exc).__name__}: {exc}".splitlines()[0]
    finally:
        try:
            clean = error is None and int(result.get("changed", 0) or 0) == 0
            _settle_backfill_mark(persona_id, job, adapter, clean)
        finally:
            try:
                adapter.close()
            finally:
                final = {
                    "embedding_running": False,
                    "embedding_progress": result.get("embedded", 0),
                    "embedding_total": total,
                    "embedding_failed": result.get("failed", 0),
                }
                if error:
                    final["embedding_error"] = error
                _finish_job(job, final)
    if error:
        LOGGER.warning("Import embedding for %s ended with a problem: %s", persona_id, error)
    LOGGER.info(
        "Import embedding %d finished for %s: embedded=%d failed=%d changed=%d (of %d)",
        job, persona_id, result.get("embedded", 0), result.get("failed", 0),
        result.get("changed", 0), total,
    )


def _settle_backfill_mark(persona_id: str, job: int, adapter, clean: bool) -> None:
    """Remove the persistent mark when nothing is left behind (see the module notes)."""
    with _import_embedding_lock:
        if not clean:
            _import_embedding_dirty[persona_id] = True
            LOGGER.warning(
                "Import embedding %d for %s left messages without vectors; keeping "
                "the pending mark (the re-embed fills them).", job, persona_id,
            )
            return
        if _import_embedding_latest.get(persona_id) != job:
            return  # a later job is queued; it settles the mark
        if _import_embedding_dirty.get(persona_id):
            return
        try:
            adapter.finish_embedding_backfill()
        except Exception:
            LOGGER.warning(
                "Failed to remove the import embedding mark for %s", persona_id, exc_info=True,
            )


def _finish_job(job: int, final: dict) -> None:
    with _import_embedding_lock:
        _import_embedding_jobs[job] = final
        finished = [
            k for k, v in _import_embedding_jobs.items() if not v.get("embedding_running")
        ]
        for k in sorted(finished)[:-_KEEP_FINISHED_JOBS]:
            _import_embedding_jobs.pop(k, None)


def _embedding_result_error(adapter, result: dict) -> Optional[str]:
    """One line describing why a finished backfill left messages unembedded.

    ``None`` when nothing was left behind. Covers the two quiet outcomes of
    ``backfill_missing_message_embeddings``: some messages failed, or none
    could be embedded at all (the embedding model is not loaded).
    """
    failed = int(result.get("failed", 0) or 0)
    embedded = int(result.get("embedded", 0) or 0)
    missing = int(result.get("total_missing", 0) or 0)
    if failed > 0:
        return f"{failed} message(s) could not be embedded."
    if missing > 0 and embedded == 0:
        if adapter.embedder is None:
            return (
                f"Embedding model is not available; {missing} message(s) were "
                "left without embeddings."
            )
        return f"None of {missing} message(s) could be embedded."
    return None


def _reserve_import_embedding(persona_id: str, adapter, total: int) -> int:
    """Write the persistent mark and register a job, before the import reports success.

    The job is registered as running (0/``total``) so a status poll that sees
    the import finished also sees its embedding still to come. Returns the
    job number to put in the import's status (``"embedding_job"``).
    """
    global _import_embedding_seq
    with _import_embedding_lock:
        if adapter.begin_embedding_backfill(total):
            # No mark was there: nothing earlier is left behind.
            _import_embedding_dirty[persona_id] = False
        _import_embedding_seq += 1
        job = _import_embedding_seq
        _import_embedding_latest[persona_id] = job
        _import_embedding_jobs[job] = {
            "embedding_running": True, "embedding_progress": 0, "embedding_total": total,
        }
        return job


def _launch_import_embedding(persona_id: str, job: int, adapter, message_ids: List[str]) -> bool:
    """Start the background thread of ``job``; it takes ``adapter`` over and closes it.

    The thread is created, registered as the persona's latest and started in
    one step under the lock, so two imports finishing at once cannot both
    miss each other's thread. Returns False (and records the job as failed)
    when the thread could not be started; the caller then closes the adapter.
    """
    with _import_embedding_lock:
        previous = _import_embedding_threads.get(persona_id)
        thread = threading.Thread(
            target=_embed_imported_messages,
            args=(persona_id, job, adapter, list(message_ids), previous),
            name=f"import-embedding-{persona_id}-{job}",
            daemon=True,
        )
        try:
            thread.start()
        except Exception as exc:
            LOGGER.exception("Could not start the import embedding for %s", persona_id)
            _import_embedding_dirty[persona_id] = True
            _import_embedding_jobs[job] = {
                "embedding_running": False, "embedding_progress": 0,
                "embedding_total": len(message_ids), "embedding_failed": 0,
                "embedding_error": f"{type(exc).__name__}: {exc}".splitlines()[0],
            }
            return False
        _import_embedding_threads[persona_id] = thread
        return True


def _with_embedding_status(persona_id: str, status: dict) -> dict:
    """Import status plus the state of that import's own embedding job, if any."""
    job = status.get("embedding_job")
    shown = {k: v for k, v in status.items() if k != "embedding_job"}
    if job is None:
        return shown
    with _import_embedding_lock:
        embedding = dict(_import_embedding_jobs.get(job, {}))
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
        adapter = SAIMemoryAdapter(persona_id)
        handed_off = False

        try:
            msg_count = 0
            imported_ids: List[str] = []
            for i, payload in enumerate(payloads):
                # Never embed while importing: with skip_embedding the
                # messages stay unembedded, otherwise they are embedded in
                # the background after the import has finished.
                payload["embedding_chunks"] = 0
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

            # The embedding job is registered (and its persistent mark
            # written) before the import reports success, then started.
            job = None
            if not skip_embedding and imported_ids:
                job = _reserve_import_embedding(persona_id, adapter, len(imported_ids))
            with _extension_import_lock:
                _extension_import_status[persona_id] = {
                    "running": False, "progress": msg_count, "total": total,
                    "message": f"Imported '{conversation.title}' ({msg_count} messages).",
                    "success": True, "title": conversation.title,
                    "embedding_job": job,
                }
            if job is not None:
                handed_off = _launch_import_embedding(persona_id, job, adapter, imported_ids)
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
        adapter = SAIMemoryAdapter(persona_id)
        handed_off = False

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
                    payload["embedding_chunks"] = 0
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
            
            # Registered (with its persistent mark) before reporting success.
            job = None
            if not skip_embedding and imported_ids:
                job = _reserve_import_embedding(persona_id, adapter, len(imported_ids))
            with _official_import_lock:
                _official_import_status[persona_id] = {
                    "running": False, "progress": imported_count, "total": total_conversations,
                    "message": f"Imported {imported_count} conversations ({msg_count} messages).",
                    "success": True, "conversations": imported_count, "messages": msg_count,
                    "embedding_job": job,
                }
            if job is not None:
                handed_off = _launch_import_embedding(persona_id, job, adapter, imported_ids)
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
