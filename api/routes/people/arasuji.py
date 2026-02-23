from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from typing import Dict, List, Optional
from api.deps import get_manager
from .models import (
    ArasujiStatsResponse, ArasujiListResponse, ArasujiEntryItem, SourceMessageItem,
    GenerateArasujiRequest, GenerationJobStatus, ChronicleCostEstimate,
    MessagesByIdsRequest, UpdateArasujiEntryRequest,
)
import sqlite3
import logging
import uuid
import time
import threading

from llm_clients.exceptions import LLMError

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
            "error_code": None,
            "error_detail": None,
            "error_meta": None,
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

@router.get("/{persona_id}/arasuji/cost-estimate", response_model=ChronicleCostEstimate)
def estimate_chronicle_cost(
    persona_id: str,
    batch_size: Optional[int] = None,
    consolidation_size: Optional[int] = None,
    manager=Depends(get_manager),
):
    """Estimate the cost of generating Chronicle for unprocessed messages."""
    import math
    import os
    from sai_memory.memory.storage import count_messages
    from sai_memory.arasuji.storage import (
        get_total_message_count,
        count_entries_by_level,
        get_max_level,
    )
    from saiverse.model_configs import get_model_pricing

    conn = _get_arasuji_db(persona_id)
    if not conn:
        raise HTTPException(status_code=404, detail=f"Memory database not found for {persona_id}")

    try:
        total_messages = count_messages(conn)
        processed_messages = get_total_message_count(conn)

        # Use query params if provided, otherwise fall back to env vars
        batch_size = batch_size or int(os.getenv("MEMORY_WEAVE_BATCH_SIZE", "20"))
        consolidation_size = consolidation_size or int(os.getenv("MEMORY_WEAVE_CONSOLIDATION_SIZE", "10"))
        model_name = os.getenv("MEMORY_WEAVE_MODEL", "gemini-2.5-flash-lite-preview-09-2025")

        # Calculate qualifying unprocessed messages using the same
        # contiguous-run logic as generate_unprocessed().  Messages in
        # runs shorter than batch_size are skipped during generation,
        # so they should not be counted here either.
        _cur = conn.execute(
            "SELECT DISTINCT json_each.value "
            "FROM arasuji_entries, json_each(source_ids_json) "
            "WHERE level = 1"
        )
        _processed_ids = {row[0] for row in _cur.fetchall()}

        _msg_ids_cur = conn.execute(
            "SELECT id FROM messages ORDER BY created_at ASC"
        )
        _runs_lengths: list[int] = []
        _run_len = 0
        for (msg_id,) in _msg_ids_cur:
            if msg_id in _processed_ids:
                if _run_len > 0:
                    _runs_lengths.append(_run_len)
                    _run_len = 0
                continue
            _run_len += 1
        if _run_len > 0:
            _runs_lengths.append(_run_len)

        # Count only full batches within qualifying runs (incomplete
        # trailing batches are skipped by generate_from_messages).
        level1_calls = sum(n // batch_size for n in _runs_lengths if n >= batch_size)
        unprocessed = level1_calls * batch_size
        # Consolidation calls: every consolidation_size level-1 entries -> 1 level-2 call, etc.
        consolidation_calls = 0
        entries_at_level = level1_calls
        while entries_at_level >= consolidation_size:
            next_level_calls = math.ceil(entries_at_level / consolidation_size)
            consolidation_calls += next_level_calls
            entries_at_level = next_level_calls

        total_calls = level1_calls + consolidation_calls

        # --- Estimate episode context tokens ---
        # The reverse level promotion algorithm selects context entries from existing
        # Chronicles. Theoretical entry count:
        #   entries_at_max_level + (max_level - 1) * consolidation_size
        # Capped by max_entries (20 for Level 1, 10 for consolidation).
        current_max_level = get_max_level(conn)
        entries_by_level = count_entries_by_level(conn)
        existing_total = sum(entries_by_level.values())

        if current_max_level > 0:
            entries_at_max = entries_by_level.get(current_max_level, 0)
            theoretical_existing = entries_at_max + (current_max_level - 1) * consolidation_size
        else:
            theoretical_existing = 0

        # Project post-generation state: recalculate max_level from total Level 1 count
        existing_lv1 = entries_by_level.get(1, 0)
        total_lv1_after = existing_lv1 + level1_calls
        if total_lv1_after > 0:
            final_max_level = 1
            temp_count = total_lv1_after
            while temp_count >= consolidation_size:
                final_max_level += 1
                temp_count = math.ceil(temp_count / consolidation_size)
            theoretical_after = temp_count + max(0, final_max_level - 1) * consolidation_size
        else:
            theoretical_after = 0

        # Average context entries per call (start..end average, capped by max_entries)
        MAX_ENTRIES_LV1 = 20   # generator.py:167
        MAX_ENTRIES_CONS = 10  # generator.py:326
        ctx_start_lv1 = min(theoretical_existing, MAX_ENTRIES_LV1)
        ctx_end_lv1 = min(theoretical_after, MAX_ENTRIES_LV1)
        avg_context_lv1 = (ctx_start_lv1 + ctx_end_lv1) / 2

        ctx_start_cons = min(theoretical_existing + consolidation_size, MAX_ENTRIES_CONS)
        ctx_end_cons = min(theoretical_after, MAX_ENTRIES_CONS)
        avg_context_cons = (ctx_start_cons + ctx_end_cons) / 2

        # Average tokens per context entry (from existing Chronicle content)
        row = conn.execute("SELECT AVG(LENGTH(content)) FROM arasuji_entries").fetchone()
        avg_content_chars = row[0] if row and row[0] else None
        if avg_content_chars and existing_total > 0:
            avg_entry_tokens = avg_content_chars / 3.5  # Conservative CJK/English estimate
        else:
            avg_entry_tokens = 50  # Default for first-time generation (~3-5 sentences)

        context_tokens_lv1 = avg_context_lv1 * avg_entry_tokens
        context_tokens_cons = avg_context_cons * avg_entry_tokens

        # --- Estimate Memopedia context tokens (Level 1 only) ---
        memopedia_tokens = 0
        try:
            from sai_memory.memopedia import Memopedia, init_memopedia_tables
            init_memopedia_tables(conn)
            memopedia = Memopedia(conn)
            text = memopedia.get_tree_markdown(include_keywords=False, show_markers=False)
            if text and text != "(まだページはありません)":
                memopedia_tokens = len(text) / 3.5
        except Exception:
            pass  # Memopedia not initialized → 0

        # --- Estimate cost ---
        pricing = get_model_pricing(model_name)
        is_free_tier = pricing is None
        estimated_cost = 0.0

        if pricing and total_calls > 0:
            input_rate = pricing.get("input_per_1m_tokens", 0)
            output_rate = pricing.get("output_per_1m_tokens", 0)

            # Level 1: messages + prompt + episode context + Memopedia
            avg_input_lv1 = (
                batch_size * 200  # ~200 tokens/message (mixed CJK/English)
                + 500             # prompt instructions overhead
                + context_tokens_lv1
                + memopedia_tokens
            )
            # Level 2+: arasuji text + prompt + episode context (no Memopedia)
            avg_input_cons = (
                consolidation_size * avg_entry_tokens  # arasuji entries as input
                + 500                                   # prompt instructions overhead
                + context_tokens_cons
            )
            avg_output_per_call = 400  # ~3-5 sentence summary

            total_input = level1_calls * avg_input_lv1 + consolidation_calls * avg_input_cons
            total_output = total_calls * avg_output_per_call
            estimated_cost = (
                (total_input / 1_000_000) * input_rate
                + (total_output / 1_000_000) * output_rate
            )

        return ChronicleCostEstimate(
            total_messages=total_messages,
            processed_messages=processed_messages,
            unprocessed_messages=unprocessed,
            estimated_llm_calls=total_calls,
            estimated_cost_usd=round(estimated_cost, 6),
            model_name=model_name,
            is_free_tier=is_free_tier,
            batch_size=batch_size,
        )
    finally:
        conn.close()


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
                LOGGER.warning("Failed to get message number map for %s", persona_id, exc_info=True)

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
                LOGGER.warning("Failed to get message number range for entry %s", entry_id, exc_info=True)

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
    from sai_memory.arasuji.storage import delete_entry

    conn = _get_arasuji_db(persona_id)
    if not conn:
        raise HTTPException(status_code=404, detail=f"Memory database not found for {persona_id}")

    try:
        # delete_entry handles child reset (is_consolidated=0, parent_id=NULL)
        success = delete_entry(conn, entry_id)
        if not success:
            raise HTTPException(status_code=404, detail=f"Chronicle entry {entry_id} not found")

        return {
            "success": True,
            "message": f"Deleted Chronicle entry {entry_id}",
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to delete Chronicle entry: {e}")
    finally:
        conn.close()

@router.patch("/{persona_id}/arasuji/{entry_id}", tags=["Chronicle"])
def update_arasuji_entry(
    persona_id: str,
    entry_id: str,
    request: UpdateArasujiEntryRequest,
    manager=Depends(get_manager),
):
    """Update a Chronicle entry's content."""
    from sai_memory.arasuji.storage import update_entry_content

    conn = _get_arasuji_db(persona_id)
    if not conn:
        raise HTTPException(status_code=404, detail=f"Memory database not found for {persona_id}")

    try:
        success = update_entry_content(conn, entry_id, request.content)
        if not success:
            raise HTTPException(status_code=404, detail=f"Chronicle entry {entry_id} not found")
        return {"success": True}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to update Chronicle entry: {e}")
    finally:
        conn.close()


@router.delete("/{persona_id}/arasuji", tags=["Chronicle"])
def delete_all_arasuji_entries(persona_id: str, manager=Depends(get_manager)):
    """Delete ALL Chronicle entries and reset progress."""
    from sai_memory.arasuji.storage import clear_all_entries

    conn = _get_arasuji_db(persona_id)
    if not conn:
        raise HTTPException(status_code=404, detail=f"Memory database not found for {persona_id}")

    try:
        deleted_count = clear_all_entries(conn)
        return {
            "success": True,
            "deleted_count": deleted_count,
            "message": f"Deleted {deleted_count} Chronicle entries",
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to delete Chronicle entries: {e}")
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

@router.post("/{persona_id}/arasuji/messages-by-ids", response_model=List[SourceMessageItem], tags=["Chronicle"])
def get_messages_by_ids(
    persona_id: str,
    request: MessagesByIdsRequest,
    manager = Depends(get_manager),
):
    """Get messages by their IDs (for error investigation)."""
    from sai_memory.memory.storage import get_message

    if not request.ids:
        return []

    conn = _get_arasuji_db(persona_id)
    if not conn:
        raise HTTPException(status_code=404, detail=f"Memory database not found for {persona_id}")

    try:
        messages = []
        for msg_id in request.ids:
            msg = get_message(conn, msg_id)
            if msg:
                messages.append(SourceMessageItem(
                    id=msg.id,
                    role=msg.role,
                    content=msg.content or "",
                    created_at=msg.created_at,
                ))
        messages.sort(key=lambda m: m.created_at)
        return messages
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get messages: {e}")
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
        new_entry = regenerate_entry(conn, entry_id, persona_id=persona_id)
        
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
    include_timestamp: bool = True,
):
    """Background worker for Chronicle generation."""
    import json
    import os
    from pathlib import Path
    from sai_memory.memory.storage import init_db, Message
    from sai_memory.arasuji import init_arasuji_tables
    from sai_memory.arasuji.generator import ArasujiGenerator
    from saiverse.model_configs import find_model_config
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

        # Fetch all messages ordered by time (oldest first)
        _update_job(job_id, message="Fetching messages...")

        cur = conn.execute("""
            SELECT id, thread_id, role, content, resource_id, created_at, metadata
            FROM messages
            ORDER BY created_at ASC
        """)

        all_messages = []
        for row in cur.fetchall():
            msg_id, tid, role, content, resource_id, created_at, metadata_raw = row
            metadata = {}
            if metadata_raw:
                try:
                    metadata = json.loads(metadata_raw)
                except Exception:
                    LOGGER.warning("Failed to parse metadata JSON for message %s", msg_id, exc_info=True)
            all_messages.append(Message(
                id=msg_id,
                thread_id=tid,
                role=role,
                content=content,
                resource_id=resource_id,
                created_at=created_at,
                metadata=metadata,
            ))

        if not all_messages:
            _update_job(job_id, status="completed", progress=0, total=0, entries_created=0, message="No messages found")
            conn.close()
            return

        LOGGER.info(f"[Chronicle Gen] Loaded {len(all_messages)} messages")

        # Initialize LLM client
        _update_job(job_id, message="Initializing LLM client...")

        env_model = os.getenv("MEMORY_WEAVE_MODEL", "gemini-2.5-flash-lite-preview-09-2025")
        model_to_use = model_name or env_model

        resolved_model_id, model_config = find_model_config(model_to_use)
        if not resolved_model_id:
            _update_job(job_id, status="failed", error=f"Model '{model_to_use}' not found")
            conn.close()
            return

        actual_model_id = model_config.get("model", resolved_model_id)
        context_length = model_config.get("context_length", 128000)
        provider = model_config.get("provider", "gemini")

        client = get_llm_client(resolved_model_id, provider, context_length, config=model_config)
        LOGGER.info(f"[Chronicle Gen] LLM client initialized: {actual_model_id} / {provider} (config_key={resolved_model_id})")

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
            include_timestamp=include_timestamp,
            memopedia_context=memopedia_context,
            persona_id=persona_id,
        )

        # Progress callback
        def progress_callback(processed: int, total: int):
            _update_job(job_id, progress=processed, total=total, message=f"Processing... {processed}/{total}")

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

        # Generate using unified method (filters processed, groups into runs, generates)
        _update_job(job_id, message="Generating Chronicle entries...")

        def cancel_check():
            job = _get_job(job_id)
            return job is not None and job.get("status") == "cancelling"

        level1_entries, consolidated_entries = generator.generate_unprocessed(
            all_messages,
            max_messages=max_messages,
            progress_callback=progress_callback,
            batch_callback=batch_callback,
            cancel_check=cancel_check,
        )

        total_entries = len(level1_entries) + len(consolidated_entries)

        conn.close()

        # Check if cancelled
        if cancel_check():
            _update_job(
                job_id,
                status="cancelled",
                entries_created=total_entries,
                message=f"ユーザーにより中止されました（{total_entries}件生成済み）",
            )
            return

        _update_job(
            job_id,
            status="completed",
            entries_created=total_entries,
            message=f"Completed. Created {len(level1_entries)} level-1 + {len(consolidated_entries)} consolidated entries."
            + (f" Memopedia pages: {memopedia_pages_total}" if with_memopedia else "")
        )

    except LLMError as e:
        LOGGER.exception(f"Chronicle generation failed (LLM error): {e}")
        _update_job(
            job_id, status="failed",
            error=e.user_message,
            error_code=e.error_code,
            error_detail=str(e),
            error_meta=getattr(e, "batch_meta", None),
        )
    except Exception as e:
        LOGGER.exception(f"Chronicle generation failed: {e}")
        _update_job(
            job_id, status="failed",
            error=str(e),
            error_code="unknown",
            error_detail=str(e),
        )


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
        include_timestamp=request.include_timestamp,
    )

    return {"job_id": job_id, "status": "started"}


@router.post("/{persona_id}/arasuji/generate/{job_id}/cancel", tags=["Chronicle"])
async def cancel_arasuji_generation(
    persona_id: str,
    job_id: str,
):
    """Cancel a running Chronicle generation job."""
    job = _get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    if job.get("persona_id") != persona_id:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found for persona {persona_id}")

    if job.get("status") not in ("pending", "running", "started"):
        return {"cancelled": False, "reason": "Job is not running"}

    _update_job(job_id, status="cancelling")
    return {"cancelled": True}


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
        error_code=job.get("error_code"),
        error_detail=job.get("error_detail"),
        error_meta=job.get("error_meta"),
    )
