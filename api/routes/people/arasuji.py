from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from typing import Dict, List, Optional
from api.deps import get_manager
from .models import (
    ArasujiStatsResponse, ArasujiListResponse, ArasujiEntryItem, SourceMessageItem,
    GenerateArasujiRequest, GenerationJobStatus,
)
import sqlite3
import logging
import uuid
import time
import threading

LOGGER = logging.getLogger(__name__)
router = APIRouter()

# -----------------------------------------------------------------------------
# In-memory job store for async generation
# -----------------------------------------------------------------------------
_generation_jobs: Dict[str, dict] = {}
_jobs_lock = threading.Lock()


def _create_job(persona_id: str) -> str:
    """Create a new generation job and return its ID."""
    job_id = str(uuid.uuid4())
    with _jobs_lock:
        _generation_jobs[job_id] = {
            "persona_id": persona_id,
            "status": "pending",
            "progress": 0,
            "total": 0,
            "message": "Initializing...",
            "entries_created": 0,
            "error": None,
            "created_at": time.time(),
        }
    return job_id


def _update_job(job_id: str, **kwargs):
    """Update job status."""
    with _jobs_lock:
        if job_id in _generation_jobs:
            _generation_jobs[job_id].update(kwargs)


def _get_job(job_id: str) -> Optional[dict]:
    """Get job status."""
    with _jobs_lock:
        return _generation_jobs.get(job_id, {}).copy() if job_id in _generation_jobs else None


def _get_arasuji_db(persona_id: str):
    """Get database connection for arasuji tables."""
    from pathlib import Path
    import sqlite3
    from sai_memory.arasuji.storage import init_arasuji_tables

    db_path = Path.home() / ".saiverse" / "personas" / persona_id / "memory.db"
    if not db_path.exists():
        return None
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    init_arasuji_tables(conn)
    return conn

def _get_message_number_map(conn: sqlite3.Connection) -> dict:
    """Build a mapping of message_id -> row number (1-indexed) matching build_arasuji.py order.

    Messages are ordered globally by created_at ASC across all threads.
    This ensures consistent chronological ordering where message #1 is always the oldest.
    """
    cur = conn.execute("""
        SELECT id FROM messages ORDER BY created_at ASC
    """)

    msg_num_map = {}
    for msg_num, (msg_id,) in enumerate(cur.fetchall(), start=1):
        msg_num_map[msg_id] = msg_num

    return msg_num_map

@router.get("/{persona_id}/arasuji/stats", response_model=ArasujiStatsResponse)
def get_arasuji_stats(persona_id: str, manager = Depends(get_manager)):
    """Get arasuji statistics for a persona."""
    from sai_memory.arasuji.storage import count_entries_by_level, get_max_level

    conn = _get_arasuji_db(persona_id)
    if not conn:
        raise HTTPException(status_code=404, detail=f"Memory database not found for {persona_id}")

    try:
        counts = count_entries_by_level(conn)
        max_level = get_max_level(conn)
        total = sum(counts.values())
        return ArasujiStatsResponse(
            max_level=max_level,
            counts_by_level=counts,
            total_count=total
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get arasuji stats: {e}")
    finally:
        conn.close()

@router.get("/{persona_id}/arasuji", response_model=ArasujiListResponse, tags=["Chronicle"])
def list_arasuji_entries(
    persona_id: str,
    level: Optional[int] = None,
    limit: int = 500,
    manager = Depends(get_manager)
):
    """List Chronicle entries for a persona (part of Memory Weave)."""
    from sai_memory.arasuji.storage import get_entries_by_level

    conn = _get_arasuji_db(persona_id)
    if not conn:
        raise HTTPException(status_code=404, detail=f"Memory database not found for {persona_id}")

    try:
        if level is not None:
            entries = get_entries_by_level(conn, level, order_by_time=True)
        else:
            # Get all entries ordered by level DESC, then start_time ASC
            cur = conn.execute("""
                SELECT id, level, content, source_ids_json, start_time, end_time,
                       source_count, message_count, parent_id, is_consolidated, created_at
                FROM arasuji_entries
                ORDER BY level DESC, start_time ASC
                LIMIT ?
            """, (limit,))
            from sai_memory.arasuji.storage import _row_to_entry
            entries = [_row_to_entry(row) for row in cur.fetchall()]

        # Build message number map for level 1 entries
        msg_num_map = None
        has_level1 = any(e.level == 1 for e in entries)
        if has_level1:
            try:
                msg_num_map = _get_message_number_map(conn)
            except Exception:
                pass  # Table might not exist or be empty

        items = []
        for e in entries:
            source_start_num = None
            source_end_num = None

            if e.level == 1 and e.source_ids and msg_num_map:
                # Calculate message number range
                nums = [msg_num_map.get(sid) for sid in e.source_ids if sid in msg_num_map]
                if nums:
                    source_start_num = min(nums)
                    source_end_num = max(nums)

            items.append(ArasujiEntryItem(
                id=e.id,
                level=e.level,
                content=e.content,
                start_time=e.start_time,
                end_time=e.end_time,
                message_count=e.message_count,
                is_consolidated=e.is_consolidated,
                created_at=e.created_at,
                source_ids=e.source_ids,
                source_start_num=source_start_num,
                source_end_num=source_end_num,
            ))

        return ArasujiListResponse(
            entries=items,
            total=len(items),
            level_filter=level
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to list Chronicle entries: {e}")
    finally:
        conn.close()

@router.get("/{persona_id}/arasuji/{entry_id}", response_model=ArasujiEntryItem, tags=["Chronicle"])
def get_arasuji_entry(
    persona_id: str,
    entry_id: str,
    manager = Depends(get_manager)
):
    """Get a detailed Chronicle entry by ID."""
    from sai_memory.arasuji.storage import get_entry

    conn = _get_arasuji_db(persona_id)
    if not conn:
        raise HTTPException(status_code=404, detail=f"Memory database not found for {persona_id}")

    try:
        entry = get_entry(conn, entry_id)
        if not entry:
            raise HTTPException(status_code=404, detail=f"Chronicle entry {entry_id} not found")

        # Calculate message number range for level 1
        source_start_num = None
        source_end_num = None
        if entry.level == 1 and entry.source_ids:
            try:
                msg_num_map = _get_message_number_map(conn)
                nums = [msg_num_map.get(sid) for sid in entry.source_ids if sid in msg_num_map]
                if nums:
                    source_start_num = min(nums)
                    source_end_num = max(nums)
            except Exception:
                pass

        return ArasujiEntryItem(
            id=entry.id,
            level=entry.level,
            content=entry.content,
            start_time=entry.start_time,
            end_time=entry.end_time,
            message_count=entry.message_count,
            is_consolidated=entry.is_consolidated,
            created_at=entry.created_at,
            source_ids=entry.source_ids,
            source_start_num=source_start_num,
            source_end_num=source_end_num,
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get Chronicle entry: {e}")
    finally:
        conn.close()

@router.delete("/{persona_id}/arasuji/{entry_id}")
def delete_arasuji_entry(persona_id: str, entry_id: str, manager = Depends(get_manager)):
    """Delete a Chronicle entry and unmark child entries as consolidated."""
    from sai_memory.arasuji.storage import delete_entry, get_entry

    conn = _get_arasuji_db(persona_id)
    if not conn:
        raise HTTPException(status_code=404, detail=f"Memory database not found for {persona_id}")

    try:
        # Get entry first to find child entries
        entry = get_entry(conn, entry_id)
        if not entry:
            raise HTTPException(status_code=404, detail=f"Chronicle entry {entry_id} not found")

        # If this is a consolidated entry (level 2+), unmark children as consolidated
        if entry.level >= 2 and entry.source_ids:
            placeholders = ",".join("?" for _ in entry.source_ids)
            conn.execute(
                f"""
                UPDATE arasuji_entries
                SET is_consolidated = 0, parent_id = NULL
                WHERE id IN ({placeholders})
                """,
                entry.source_ids
            )
            conn.commit()

        # Now delete the entry
        success = delete_entry(conn, entry_id)
        if not success:
            raise HTTPException(status_code=404, detail=f"Chronicle entry {entry_id} not found")

        return {
            "success": True,
            "message": f"Deleted Chronicle entry {entry_id}",
            "children_unmarked": len(entry.source_ids) if entry.level >= 2 else 0
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to delete Chronicle entry: {e}")
    finally:
        conn.close()

@router.get("/{persona_id}/arasuji/{entry_id}/messages", response_model=List[SourceMessageItem], tags=["Chronicle"])
def get_arasuji_messages(
    persona_id: str,
    entry_id: str,
    manager = Depends(get_manager)
):
    """Get the source raw messages for a Level 1 Chronicle entry."""
    from sai_memory.arasuji.storage import get_entry
    from sai_memory.memory.storage import get_message

    conn = _get_arasuji_db(persona_id)
    if not conn:
        raise HTTPException(status_code=404, detail=f"Memory database not found for {persona_id}")

    try:
        entry = get_entry(conn, entry_id)
        if not entry:
            raise HTTPException(status_code=404, detail=f"Arasuji entry {entry_id} not found")

        if entry.level != 1:
            raise HTTPException(status_code=400, detail="This endpoint only works for level-1 arasuji entries")

        # Fetch messages by IDs
        messages = []
        for msg_id in entry.source_ids:
            msg = get_message(conn, msg_id)
            if msg:
                messages.append(SourceMessageItem(
                    id=msg.id,
                    role=msg.role,
                    content=msg.content or "",
                    created_at=msg.created_at,
                ))

        # Sort by created_at
        messages.sort(key=lambda m: m.created_at)
        return messages

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get source messages: {e}")
    finally:
        conn.close()

@router.post("/{persona_id}/arasuji/{entry_id}/regenerate", tags=["Chronicle"])
async def regenerate_arasuji_entry(
    persona_id: str,
    entry_id: str,
    manager = Depends(get_manager)
):
    """Regenerate a specific Chronicle entry while preserving parent relationship.
    
    This endpoint delegates to the storage layer which handles:
    1. Saving parent relationship
    2. Deleting and updating parent
    3. Regenerating with LLM
    4. Restoring parent relationship
    """
    from sai_memory.arasuji.storage import regenerate_entry
    
    conn = _get_arasuji_db(persona_id)
    
    try:
        new_entry = regenerate_entry(conn, entry_id)
        
        if not new_entry:
            raise HTTPException(
                status_code=500,
                detail="Failed to regenerate entry"
            )
        
        return {
            "success": True,
            "old_entry_id": entry_id,
            "new_entry_id": new_entry.id,
            "content": new_entry.content
        }
    
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        LOGGER.exception(f"[regenerate] Exception during regeneration: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to regenerate entry: {e}"
        )
    finally:
        conn.close()


# -----------------------------------------------------------------------------
# Chronicle Generation (Async Background Job)
# -----------------------------------------------------------------------------

def _run_chronicle_generation(
    job_id: str,
    persona_id: str,
    max_messages: int,
    batch_size: int,
    consolidation_size: int,
    model_name: Optional[str],
    with_memopedia: bool,
):
    """Background worker for Chronicle generation."""
    import os
    from pathlib import Path
    from sai_memory.memory.storage import init_db, Message
    from sai_memory.arasuji import init_arasuji_tables
    from sai_memory.arasuji.storage import get_progress, update_progress
    from sai_memory.arasuji.generator import ArasujiGenerator
    from model_configs import find_model_config
    from llm_clients.factory import get_llm_client

    _update_job(job_id, status="running", message="Loading database...")

    try:
        # Get database path
        db_path = Path.home() / ".saiverse" / "personas" / persona_id / "memory.db"
        if not db_path.exists():
            _update_job(job_id, status="failed", error=f"Database not found: {db_path}")
            return

        conn = init_db(str(db_path), check_same_thread=False)
        init_arasuji_tables(conn)

        # Fetch unprocessed messages (not in any existing Chronicle's source_ids)
        _update_job(job_id, message="Fetching unprocessed messages...")
        
        # Get all message IDs already included in level-1 Chronicles
        # Using json_each to extract source_ids_json array elements
        cur = conn.execute("""
            SELECT DISTINCT json_each.value 
            FROM arasuji_entries, json_each(source_ids_json)
            WHERE level = 1
        """)
        processed_ids = {row[0] for row in cur.fetchall()}
        LOGGER.info(f"[Chronicle Gen] Found {len(processed_ids)} already-processed message IDs")
        
        # Fetch messages NOT in processed_ids, ordered by time (oldest first)
        cur = conn.execute("""
            SELECT id, thread_id, role, content, resource_id, created_at, metadata
            FROM messages
            ORDER BY created_at ASC
        """)
        
        import json
        all_rows = cur.fetchall()
        messages = []
        for row in all_rows:
            msg_id, tid, role, content, resource_id, created_at, metadata_raw = row
            # Skip messages already in existing Chronicles
            if msg_id in processed_ids:
                continue
            metadata = {}
            if metadata_raw:
                try:
                    metadata = json.loads(metadata_raw)
                except:
                    pass
            messages.append(Message(
                id=msg_id,
                thread_id=tid,
                role=role,
                content=content,
                resource_id=resource_id,
                created_at=created_at,
                metadata=metadata,
            ))
            # Stop once we have enough messages
            if len(messages) >= max_messages:
                break

        total_messages = len(messages)
        
        # Batch minimum threshold check
        if total_messages < batch_size:
            _update_job(
                job_id, 
                status="completed",
                progress=0,
                total=total_messages,
                entries_created=0,
                message=f"Not enough unprocessed messages ({total_messages} < batch_size {batch_size}). Skipping."
            )
            conn.close()
            return

        LOGGER.info(f"[Chronicle Gen] Found {total_messages} messages to process")
        _update_job(job_id, total=total_messages, message=f"Found {total_messages} messages to process")

        # Initialize LLM client
        _update_job(job_id, message="Initializing LLM client...")
        
        env_model = os.getenv("MEMORY_WEAVE_MODEL", "gemini-2.0-flash")
        model_to_use = model_name or env_model
        
        resolved_model_id, model_config = find_model_config(model_to_use)
        if not resolved_model_id:
            _update_job(job_id, status="failed", error=f"Model '{model_to_use}' not found")
            conn.close()
            return

        actual_model_id = model_config.get("model", resolved_model_id)
        context_length = model_config.get("context_length", 128000)
        provider = model_config.get("provider", "gemini")
        
        client = get_llm_client(actual_model_id, provider, context_length, config=model_config)
        LOGGER.info(f"[Chronicle Gen] LLM client initialized: {actual_model_id} / {provider}")

        # Get Memopedia context if available
        memopedia_context = None
        try:
            from sai_memory.memopedia import Memopedia, init_memopedia_tables
            init_memopedia_tables(conn)
            memopedia = Memopedia(conn)
            memopedia_context = memopedia.get_tree_markdown(include_keywords=False, show_markers=False)
            if memopedia_context == "(まだページはありません)":
                memopedia_context = None
        except Exception as e:
            LOGGER.warning(f"Failed to get Memopedia context: {e}")

        # Create generator
        generator = ArasujiGenerator(
            client,
            conn,
            batch_size=batch_size,
            consolidation_size=consolidation_size,
            include_timestamp=True,
            memopedia_context=memopedia_context,
        )

        # Progress callback
        def progress_callback(processed: int, total: int):
            _update_job(job_id, progress=processed, message=f"Processing... {processed}/{total}")

        # Memopedia batch callback if enabled
        batch_callback = None
        memopedia_pages_total = 0
        
        if with_memopedia:
            try:
                from scripts.build_memopedia import extract_knowledge
                
                def memopedia_batch_callback(batch_messages):
                    nonlocal memopedia_pages_total
                    if not batch_messages:
                        return
                    try:
                        pages = extract_knowledge(
                            client,
                            batch_messages,
                            memopedia,
                            batch_size=len(batch_messages),
                            dry_run=False,
                            refine_writes=True,
                            episode_context_conn=conn,
                        )
                        memopedia_pages_total += len(pages)
                        # Update Memopedia context for next batch
                        generator.memopedia_context = memopedia.get_tree_markdown(
                            include_keywords=False, show_markers=False
                        )
                    except Exception as e:
                        LOGGER.error(f"Memopedia extraction failed: {e}")
                
                batch_callback = memopedia_batch_callback
            except ImportError as e:
                LOGGER.warning(f"Memopedia modules not available: {e}")

        # Generate
        _update_job(job_id, message="Generating Chronicle entries...")
        LOGGER.info(f"[Chronicle Gen] Starting generation for {len(messages)} messages with batch_size={batch_size}")
        
        level1_entries, consolidated_entries = generator.generate_from_messages(
            messages,
            dry_run=False,
            progress_callback=progress_callback,
            batch_callback=batch_callback,
        )

        total_entries = len(level1_entries) + len(consolidated_entries)

        conn.close()

        _update_job(
            job_id,
            status="completed",
            progress=total_messages,
            entries_created=total_entries,
            message=f"Completed. Created {len(level1_entries)} level-1 + {len(consolidated_entries)} consolidated entries."
            + (f" Memopedia pages: {memopedia_pages_total}" if with_memopedia else "")
        )

    except Exception as e:
        LOGGER.exception(f"Chronicle generation failed: {e}")
        _update_job(job_id, status="failed", error=str(e))


@router.post("/{persona_id}/arasuji/generate", tags=["Chronicle"])
async def start_arasuji_generation(
    persona_id: str,
    request: GenerateArasujiRequest,
    background_tasks: BackgroundTasks,
    manager = Depends(get_manager),
):
    """Start Chronicle generation as a background job.
    
    Returns a job_id that can be used to poll for status.
    """
    # Verify persona exists
    from pathlib import Path
    db_path = Path.home() / ".saiverse" / "personas" / persona_id / "memory.db"
    if not db_path.exists():
        raise HTTPException(status_code=404, detail=f"Memory database not found for {persona_id}")

    # Create job
    job_id = _create_job(persona_id)
    
    # Start background task
    background_tasks.add_task(
        _run_chronicle_generation,
        job_id=job_id,
        persona_id=persona_id,
        max_messages=request.max_messages,
        batch_size=request.batch_size,
        consolidation_size=request.consolidation_size,
        model_name=request.model,
        with_memopedia=request.with_memopedia,
    )

    return {"job_id": job_id, "status": "started"}


@router.get("/{persona_id}/arasuji/generate/{job_id}", response_model=GenerationJobStatus, tags=["Chronicle"])
async def get_arasuji_generation_status(
    persona_id: str,
    job_id: str,
    manager = Depends(get_manager),
):
    """Get the status of a Chronicle generation job."""
    job = _get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
    
    if job.get("persona_id") != persona_id:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found for persona {persona_id}")
    
    return GenerationJobStatus(
        job_id=job_id,
        status=job.get("status", "unknown"),
        progress=job.get("progress"),
        total=job.get("total"),
        message=job.get("message"),
        entries_created=job.get("entries_created"),
        error=job.get("error"),
    )
