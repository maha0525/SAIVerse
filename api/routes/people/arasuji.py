from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from typing import Dict, List, Optional
from api.deps import get_manager
from saiverse.data_paths import get_persona_memory_db
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

    db_path = get_persona_memory_db(persona_id)
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
        SELECT id FROM messages ORDER BY created_at ASC, rowid ASC
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
    """Estimate the cost of generating Chronicle for unprocessed messages.

    W4: 見積もりは生成経路と同じ episode 整列計画 (plan_alignment) から数える。
    ``batch_size`` / ``consolidation_size`` クエリパラメータは廃止 — 受理して
    無視する (旧 frontend 互換)。
    """
    import os
    from sai_memory.arasuji.estimate import estimate_chronicle_generation_cost

    conn = _get_arasuji_db(persona_id)
    if not conn:
        raise HTTPException(status_code=404, detail=f"Memory database not found for {persona_id}")

    try:
        from saiverse.model_defaults import BUILTIN_DEFAULT_LITE_MODEL
        persona = manager.personas.get(persona_id) if manager else None
        model_name = (getattr(persona, "memory_weave_model", None)
                      or os.getenv("MEMORY_WEAVE_MODEL", BUILTIN_DEFAULT_LITE_MODEL))

        episode_digests = {}
        if manager is not None:
            try:
                from saiverse.episodes import collect_episode_digest_index
                episode_digests = collect_episode_digest_index(
                    manager, persona_id, conn,
                )
            except Exception:
                LOGGER.warning("[cost-estimate] episode digest index failed", exc_info=True)

        estimate = estimate_chronicle_generation_cost(
            conn,
            model_name=model_name,
            episode_digests=episode_digests,
        )

        return ChronicleCostEstimate(
            total_messages=estimate.total_messages,
            processed_messages=estimate.processed_messages,
            unprocessed_messages=estimate.unprocessed_messages,
            estimated_llm_calls=estimate.estimated_llm_calls,
            estimated_cost_usd=estimate.estimated_cost_usd,
            model_name=estimate.model_name,
            is_free_tier=estimate.is_free_tier,
            chunks_identity=estimate.chunks_identity,
            chunks_episode=estimate.chunks_episode,
            currency=estimate.currency,
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

@router.get("/{persona_id}/arasuji/diagnosis")
def get_chronicle_diagnosis(persona_id: str, manager=Depends(get_manager)):
    """Get diagnostic information about Chronicle structure (no message content)."""
    from sai_memory.arasuji.storage import (
        get_entries_by_level,
        count_entries_by_level,
        count_unconsolidated_by_level,
        get_max_level,
        get_progress,
    )
    from sai_memory.memory.storage import count_messages

    conn = _get_arasuji_db(persona_id)
    if not conn:
        raise HTTPException(status_code=404, detail=f"Memory database not found for {persona_id}")

    try:
        total_messages = count_messages(conn)
        counts_by_level = count_entries_by_level(conn)
        unconsolidated_by_level = count_unconsolidated_by_level(conn)
        max_level = get_max_level(conn)
        progress = get_progress(conn)

        # --- Level-1 source_ids 実態調査 ---
        # DB保存値（source_count/message_count）ではなく source_ids_json の実際の内容を集計

        # 各エントリの実際の source_ids 長（json_array_length）をまとめて取得
        cur = conn.execute(
            "SELECT id, json_array_length(source_ids_json) FROM arasuji_entries WHERE level = 1"
        )
        lv1_actual_lengths: Dict[str, int] = {row[0]: (row[1] or 0) for row in cur.fetchall()}

        if lv1_actual_lengths:
            lengths = list(lv1_actual_lengths.values())
            lv1_actual_total = sum(lengths)
            lv1_actual_avg = lv1_actual_total / len(lengths)
            lv1_actual_max = max(lengths)
            lv1_actual_min = min(lengths)
        else:
            lv1_actual_total = lv1_actual_avg = lv1_actual_max = lv1_actual_min = 0

        # ユニーク source_ids 数（整列計画 plan_alignment が「処理済み」とみなす件数と同じ）
        cur = conn.execute(
            "SELECT COUNT(DISTINCT value) "
            "FROM arasuji_entries, json_each(source_ids_json) WHERE level = 1"
        )
        lv1_unique_source_ids = cur.fetchone()[0] or 0

        # source_ids 重複数（合計 - ユニーク）
        lv1_duplicate_source_ids = lv1_actual_total - lv1_unique_source_ids

        # 存在しないメッセージを指す source_ids（孤児）
        cur = conn.execute(
            "SELECT COUNT(DISTINCT value) "
            "FROM arasuji_entries, json_each(source_ids_json) "
            "WHERE level = 1 AND value NOT IN (SELECT id FROM messages)"
        )
        lv1_orphan_source_ids = cur.fetchone()[0] or 0

        # source_count フィールドと実際の長さが異なるエントリ
        cur = conn.execute(
            "SELECT COUNT(*) FROM arasuji_entries "
            "WHERE level = 1 AND source_count != json_array_length(source_ids_json)"
        )
        lv1_mismatched_entries = cur.fetchone()[0] or 0

        # --- Stelis 除外後の統計 ---
        stelis_excluded: Optional[int] = None
        non_stelis_total: Optional[int] = None
        non_stelis_after_last: Optional[int] = None
        stelis_error: Optional[str] = None
        try:
            cur = conn.execute(
                "SELECT COUNT(*) FROM messages "
                "WHERE thread_id IN (SELECT thread_id FROM stelis_threads)"
            )
            stelis_excluded = cur.fetchone()[0] or 0
            non_stelis_total = total_messages - stelis_excluded
        except Exception as ex:
            stelis_error = str(ex)

        # --- per-level エントリ詳細（実際の source_ids 長を含む）---
        level_details: Dict[int, list] = {}
        lv1_entries = []
        for level in range(1, max_level + 1):
            entries = get_entries_by_level(conn, level, order_by_time=True)
            if level == 1:
                lv1_entries = entries

            if level == 1:
                level_details[level] = [
                    {
                        "id": e.id,
                        "start_time": e.start_time,
                        "end_time": e.end_time,
                        "source_count_stored": e.source_count,
                        "actual_source_ids_count": lv1_actual_lengths.get(e.id, 0),
                        "message_count": e.message_count,
                        "is_consolidated": e.is_consolidated,
                        "parent_id": e.parent_id,
                    }
                    for e in entries
                ]
            else:
                level_details[level] = [
                    {
                        "id": e.id,
                        "start_time": e.start_time,
                        "end_time": e.end_time,
                        "source_count_stored": e.source_count,
                        "message_count": e.message_count,
                        "is_consolidated": e.is_consolidated,
                        "parent_id": e.parent_id,
                    }
                    for e in entries
                ]

        # --- Gap analysis: Level-1 Chronicle 間の孤立メッセージ ---
        gaps = []
        for i in range(len(lv1_entries) - 1):
            e1 = lv1_entries[i]
            e2 = lv1_entries[i + 1]
            if e1.end_time is not None and e2.start_time is not None and e1.end_time < e2.start_time:
                cur = conn.execute(
                    "SELECT COUNT(*) FROM messages WHERE created_at > ? AND created_at < ?",
                    (e1.end_time, e2.start_time),
                )
                gap_count = cur.fetchone()[0]
                if gap_count > 0:
                    gaps.append({
                        "prev_chronicle_id": e1.id,
                        "next_chronicle_id": e2.id,
                        "gap_start_time": e1.end_time,
                        "gap_end_time": e2.start_time,
                        "isolated_message_count": gap_count,
                    })

        # --- 最後の Chronicle 以降のメッセージ数 ---
        messages_after_last = 0
        last_end_time = None
        if lv1_entries:
            last_end_time = lv1_entries[-1].end_time
            if last_end_time is not None:
                cur = conn.execute(
                    "SELECT COUNT(*) FROM messages WHERE created_at > ?",
                    (last_end_time,),
                )
                messages_after_last = cur.fetchone()[0]
                if non_stelis_total is not None:
                    try:
                        cur = conn.execute(
                            "SELECT COUNT(*) FROM messages WHERE created_at > ? "
                            "AND thread_id NOT IN (SELECT thread_id FROM stelis_threads)",
                            (last_end_time,),
                        )
                        non_stelis_after_last = cur.fetchone()[0] or 0
                    except Exception:
                        pass
        else:
            messages_after_last = total_messages
            non_stelis_after_last = non_stelis_total

        # DB保存の message_count 合計（旧来の値）
        messages_covered_stored = sum(e.message_count for e in lv1_entries)

        return {
            "persona_id": persona_id,
            "generated_at": int(time.time()),
            # --- 全体サマリー ---
            "total_messages": total_messages,
            "messages_covered_by_lv1_stored": messages_covered_stored,  # DB保存値の合計（不正確な可能性あり）
            "messages_after_last_chronicle": messages_after_last,
            "max_level": max_level,
            "counts_by_level": counts_by_level,
            "unconsolidated_by_level": unconsolidated_by_level,
            "last_chronicle_end_time": last_end_time,
            "last_processed_message_id": progress.last_processed_message_id if progress else None,
            "last_processed_at": progress.last_processed_at if progress else None,
            # --- source_ids 実態調査 ---
            "lv1_actual_source_ids_total": lv1_actual_total,
            "lv1_unique_source_ids": lv1_unique_source_ids,        # plan_alignment の processed_ids 件数と同値
            "lv1_duplicate_source_ids": lv1_duplicate_source_ids,  # 重複分（0でなければ異常）
            "lv1_orphan_source_ids": lv1_orphan_source_ids,        # 存在しないメッセージ参照数
            "lv1_mismatched_entries": lv1_mismatched_entries,      # source_count != 実際長 のエントリ数
            "lv1_actual_source_ids_avg": round(lv1_actual_avg, 1),
            "lv1_actual_source_ids_max": lv1_actual_max,
            "lv1_actual_source_ids_min": lv1_actual_min,
            # --- Stelis 除外後の統計 ---
            "stelis_excluded_messages": stelis_excluded,
            "non_stelis_total_messages": non_stelis_total,
            "non_stelis_after_last_chronicle": non_stelis_after_last,
            "stelis_stats_error": stelis_error,
            # --- 詳細 ---
            "level_details": level_details,
            "gaps": gaps,
        }
    except Exception as e:
        LOGGER.error("Failed to get chronicle diagnosis for %s: %s", persona_id, e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to get chronicle diagnosis: {e}")
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

@router.get("/{persona_id}/arasuji/{entry_id}/fragments", tags=["Chronicle"])
def get_arasuji_fragments(
    persona_id: str,
    entry_id: str,
    manager = Depends(get_manager),
):
    """Get Memopedia fragments generated from a Chronicle entry."""
    conn = _get_arasuji_db(persona_id)
    if not conn:
        raise HTTPException(status_code=404, detail=f"Memory database not found for {persona_id}")

    try:
        rows = conn.execute(
            """
            SELECT f.id, f.content, f.source_date, p.title AS page_title
            FROM memopedia_fragments f
            JOIN memopedia_pages p ON f.entity_id = p.id
            WHERE f.chronicle_entry_id = ?
            ORDER BY p.title, f.created_at
            """,
            (entry_id,),
        ).fetchall()

        fragments = [
            {
                "id": r[0],
                "content": r[1],
                "source_date": r[2],
                "page_title": r[3],
            }
            for r in rows
        ]
        return {"fragments": fragments}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get fragments: {e}")
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
    model_name: Optional[str],
    with_memopedia: bool,
    include_timestamp: bool = True,
    manager=None,
):
    """Background worker for Chronicle generation.

    W4 (体験の構造 工程(2)): episode 整列チャンク (plan_alignment →
    execute_plan → run_band_overflow) で生成する。Metabolism 側
    (sea/session_lifecycle.generate_chronicle) と同じ計画・同じ実行器を使い、
    冪等 claim (実行台帳 kind="metabolism.run"、M1) も同じキーで通す — 手動
    生成と自動 Metabolism が同じ窓を同時に編纂する二重 LLM コストを台帳が
    収束させる (旧実装は claim を通らない別コネクション入口だった)。
    """
    import os
    from sai_memory.memory.storage import init_db
    from sai_memory.arasuji import init_arasuji_tables
    from sai_memory.arasuji.alignment import (
        chronicle_band_budget,
        chronicle_min_digest_chars,
        plan_alignment,
        truncate_plan,
    )
    from saiverse.model_configs import find_model_config
    from llm_clients.factory import get_llm_client

    _update_job(job_id, status="running", message="Loading database...")

    try:
        # Get database path
        db_path = get_persona_memory_db(persona_id)
        if not db_path.exists():
            _update_job(job_id, status="failed", error=f"Database not found: {db_path}")
            return

        conn = init_db(str(db_path), check_same_thread=False)
        init_arasuji_tables(conn)

        # 帰化バックフィル — dry 予測より前 (Codex W4 三巡 #3。近似 dry と
        # 実測 backfill の食い違いで band backlog へ永久に到達しない穴の閉塞)
        from sai_memory.arasuji.bands import backfill_coverage
        try:
            backfill_coverage(conn)
        except Exception:
            LOGGER.exception("[Chronicle Gen] coverage backfill failed; continuing")

        # Fetch all messages suitable for Chronicle (shared filter logic)
        _update_job(job_id, message="Fetching messages...")

        from sai_memory.memory.storage import get_messages_for_chronicle
        all_messages = get_messages_for_chronicle(conn)
        # 空でも早期 return しない — plan は空 chunks になり、帯 backlog
        # (前回の束ね失敗の残り) の統合だけでも実行できる (Codex W4 二巡 #5)。

        LOGGER.info("[Chronicle Gen] Loaded %d messages (filtered by get_messages_for_chronicle)", len(all_messages))

        # Initialize LLM client
        _update_job(job_id, message="Initializing LLM client...")

        from saiverse.model_defaults import BUILTIN_DEFAULT_LITE_MODEL
        from database.models import AI as AIModel
        from database.session import SessionLocal
        _db = SessionLocal()
        try:
            _ai = _db.query(AIModel).filter(AIModel.AIID == persona_id).first()
            persona_mw_model = getattr(_ai, "MEMORY_WEAVE_MODEL", None) if _ai else None
        finally:
            _db.close()
        env_model = os.getenv("MEMORY_WEAVE_MODEL", BUILTIN_DEFAULT_LITE_MODEL)
        model_to_use = model_name or persona_mw_model or env_model

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

        # Episode 整列計画 (Metabolism 側と同じ計画の一点管理)
        _update_job(job_id, message="Planning chunks...")
        cur = conn.execute(
            "SELECT DISTINCT json_each.value "
            "FROM arasuji_entries, json_each(source_ids_json) "
            "WHERE level = 1"
        )
        processed_ids = {row[0] for row in cur.fetchall()}
        episode_digests = {}
        if manager is not None:
            try:
                from saiverse.episodes import collect_episode_digest_index
                episode_digests = collect_episode_digest_index(
                    manager, persona_id, conn,
                )
            except Exception:
                LOGGER.warning("[Chronicle Gen] episode digest index failed", exc_info=True)

        plan = plan_alignment(
            all_messages,
            processed_ids,
            episode_digests,
            target_chars=chronicle_band_budget(),
            min_llm_chars=chronicle_min_digest_chars(),
        )
        plan = truncate_plan(plan, max_messages)

        # 帯あふれ backlog の dry 予測 (Codex W4 #3): 新チャンクが無くても
        # 前回の束ね失敗の残りがあれば統合だけ実行する。
        from sai_memory.arasuji.bands import plan_band_overflow
        try:
            band_plan_count = plan_band_overflow(
                conn,
                extra_leaves=[
                    (
                        c.coverage_chars,
                        min((m.created_at for m in c.messages), default=None),
                        max((m.created_at for m in c.messages), default=None),
                    )
                    for c in plan.chunks
                ],
            )
        except Exception:
            LOGGER.warning("[Chronicle Gen] band dry-plan failed", exc_info=True)
            band_plan_count = 0

        if not plan.chunks and band_plan_count == 0:
            _update_job(
                job_id, status="completed", progress=0, total=0,
                entries_created=0, message="No unprocessed messages to compile",
            )
            conn.close()
            return

        # 冪等 claim (実行台帳 M1) — Metabolism と同じ kind・同じキー形。
        # 台帳が無い環境 (manager なし / テスト) は claim なしで実行する
        # (degrade — executor の重複再検査が残る)。plan 空 (帯統合のみ) も
        # claim しない — 束ねの並走防御は bands の tx 内再検査が担う。
        ledger = getattr(manager, "execution_ledger", None) if manager else None
        execution_id = None
        if ledger is not None and plan.chunks:
            window_end_id = plan.chunks[-1].messages[-1].id
            try:
                # claim_execution: failed (前回失敗 / キャンセル) はキー退避で
                # 再実行可能 (Codex W4 二巡 #6)。running / completed 等はブロック。
                execution_id, runnable, _existing = ledger.claim_execution(
                    kind="metabolism.run",
                    idempotency_key=f"{persona_id}:{window_end_id}",
                    persona_id=persona_id,
                )
                if runnable and not ledger.try_mark_running(execution_id):
                    runnable = False
                if not runnable:
                    _update_job(
                        job_id, status="failed",
                        error="同じ範囲の編纂が既に実行中または実行済みです（Metabolism と競合）。しばらく待って再実行してください。",
                        error_code="window_claimed",
                    )
                    conn.close()
                    return
            except Exception:
                LOGGER.warning("[Chronicle Gen] ledger claim failed; proceeding without claim", exc_info=True)
                execution_id = None

        # Progress callback
        def progress_callback(processed: int, total: int):
            _update_job(job_id, progress=processed, total=total, message=f"Processing... {processed}/{total}")

        # Memopedia batch callback if enabled
        batch_callback = None
        memopedia_pages_total = 0

        if with_memopedia:
            try:
                from scripts.build_memopedia import extract_knowledge

                def memopedia_batch_callback(batch_messages, chronicle_entry_id=None):
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
                    except Exception as e:
                        LOGGER.error(f"Memopedia extraction failed: {e}")

                batch_callback = memopedia_batch_callback
            except ImportError as e:
                LOGGER.warning(f"Memopedia modules not available: {e}")

        # Entity extraction callback (runs alongside Memopedia callback)
        entity_batches_total = 0
        try:
            from sai_memory.memory.entity_extractor import make_batch_callback as make_entity_callback
            entity_callback = make_entity_callback(
                client, conn,
                persona_id=persona_id,
            )

            # Chain with existing batch_callback if present
            existing_callback = batch_callback

            def combined_batch_callback(batch_messages, chronicle_entry_id=None):
                nonlocal entity_batches_total
                if existing_callback:
                    existing_callback(batch_messages, chronicle_entry_id)
                entity_callback(batch_messages, chronicle_entry_id)
                entity_batches_total += 1

            batch_callback = combined_batch_callback
        except Exception as e:
            LOGGER.warning(f"Entity extraction setup failed: {e}")

        # Execute plan (chunk 単位の単一 tx、W4 D4)
        _update_job(job_id, message="Generating Chronicle entries...")

        def cancel_check():
            job = _get_job(job_id)
            return job is not None and job.get("status") == "cancelling"

        from sai_memory.arasuji.bands import run_band_overflow
        from sai_memory.arasuji.executor import execute_plan

        try:
            exec_result = execute_plan(
                plan, client, conn,
                persona_id=persona_id,
                include_timestamp=include_timestamp,
                memopedia_context=memopedia_context,
                progress_callback=progress_callback,
                batch_callback=batch_callback,
                cancel_check=cancel_check,
            )
        except Exception:
            if ledger is not None and execution_id:
                try:
                    ledger.mark_failed(execution_id, "chronicle generation raised")
                except Exception:
                    LOGGER.warning("[Chronicle Gen] ledger mark_failed failed", exc_info=True)
            raise

        # キャンセル = 部分適用。completed で封印すると同じ窓が window_claimed で
        # 再実行不能になる (冪等マーカーは適用の成功だけを封印 — Codex W4 #8)。
        # failed 終端で claim を退け、帯統合もスキップする。
        if exec_result.cancelled:
            if ledger is not None and execution_id:
                try:
                    ledger.mark_failed(execution_id, "cancelled by user")
                except Exception:
                    LOGGER.warning("[Chronicle Gen] ledger mark_failed failed", exc_info=True)
            total_entries = exec_result.created_count
            conn.close()
            _update_job(
                job_id,
                status="cancelled",
                entries_created=total_entries,
                message=f"ユーザーにより中止されました（{total_entries}件生成済み）",
            )
            return

        consolidated_count = 0
        try:
            consolidated_count = run_band_overflow(
                conn, client,
                persona_id=persona_id,
                cancel_check=cancel_check,
            )
        except Exception:
            LOGGER.exception("[Chronicle Gen] band overflow failed; continuing")

        if ledger is not None and execution_id:
            try:
                result_payload = dict(plan.summary)
                result_payload.update({
                    "created": exec_result.created_count,
                    "skipped_duplicates": exec_result.skipped_duplicates,
                    "bands_consolidated": consolidated_count,
                    "entrance": "api_generate",
                })
                ledger.mark_applied(execution_id, result=result_payload)
                ledger.mark_completed(execution_id)
            except Exception:
                LOGGER.warning("[Chronicle Gen] ledger apply/complete failed", exc_info=True)

        total_entries = exec_result.created_count + consolidated_count

        conn.close()

        _update_job(
            job_id,
            status="completed",
            entries_created=total_entries,
            message=f"Completed. Created {exec_result.created_count} chunk + {consolidated_count} consolidated entries."
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
    db_path = get_persona_memory_db(persona_id)
    if not db_path.exists():
        raise HTTPException(status_code=404, detail=f"Memory database not found for {persona_id}")

    # Create job
    job_id = _create_job(persona_id)

    # Start background task (batch_size / consolidation_size は W4 で廃止 —
    # チャンク分割は episode 整列 + サイズ束ねが決める。リクエストの旧
    # フィールドは受理して無視する)
    background_tasks.add_task(
        _run_chronicle_generation,
        job_id=job_id,
        persona_id=persona_id,
        max_messages=request.max_messages,
        model_name=request.model,
        with_memopedia=request.with_memopedia,
        include_timestamp=request.include_timestamp,
        manager=manager,
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
