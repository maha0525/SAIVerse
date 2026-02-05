"""SAIMemory native export/import: full-fidelity thread round-trip.

Exports and imports threads with all metadata preserved,
enabling external editing (e.g., find-replace) and re-import.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional

from sai_memory.memory.storage import (
    add_message,
    delete_thread,
    get_or_create_thread,
    get_stelis_thread,
    init_db,
    set_thread_overview,
)

LOGGER = logging.getLogger(__name__)

FORMAT_VERSION = "saiverse_saimemory_v1"


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

def _resolve_thread_ids(
    conn: sqlite3.Connection,
    persona_id: str,
    thread_suffixes: Optional[Iterable[str]],
) -> List[str]:
    """Resolve thread suffixes to full thread IDs."""
    if thread_suffixes:
        resolved: List[str] = []
        for item in thread_suffixes:
            item = item.strip()
            if not item:
                continue
            if ":" in item:
                resolved.append(item)
            else:
                resolved.append(f"{persona_id}:{item}")
        return resolved

    cur = conn.execute(
        "SELECT DISTINCT thread_id FROM messages WHERE thread_id LIKE ? ORDER BY thread_id",
        (f"{persona_id}:%",),
    )
    return [row[0] for row in cur.fetchall()]


def _export_thread(
    conn: sqlite3.Connection,
    thread_id: str,
    start_epoch: Optional[int] = None,
    end_epoch: Optional[int] = None,
) -> Dict[str, Any]:
    """Export a single thread with all metadata."""
    # Thread-level info
    cur = conn.execute(
        "SELECT resource_id, overview, overview_updated_at FROM threads WHERE id=?",
        (thread_id,),
    )
    thread_row = cur.fetchone()
    resource_id = thread_row[0] if thread_row else None
    overview = thread_row[1] if thread_row else None
    overview_updated_at = thread_row[2] if thread_row else None

    # Stelis info
    stelis_data = None
    stelis = get_stelis_thread(conn, thread_id)
    if stelis:
        stelis_data = {
            "parent_thread_id": stelis.parent_thread_id,
            "depth": stelis.depth,
            "window_ratio": stelis.window_ratio,
            "status": stelis.status,
            "chronicle_prompt": stelis.chronicle_prompt,
            "chronicle_summary": stelis.chronicle_summary,
            "created_at": stelis.created_at,
            "completed_at": stelis.completed_at,
            "label": stelis.label,
        }

    # Messages (raw, no role conversion or content expansion)
    query_parts = [
        "SELECT id, role, content, resource_id, created_at, metadata",
        "FROM messages WHERE thread_id=?",
    ]
    params: List[Any] = [thread_id]
    if start_epoch is not None:
        query_parts.append("AND created_at >= ?")
        params.append(start_epoch)
    if end_epoch is not None:
        query_parts.append("AND created_at <= ?")
        params.append(end_epoch)
    query_parts.append("ORDER BY created_at ASC")

    rows = conn.execute(" ".join(query_parts), params).fetchall()
    messages: List[Dict[str, Any]] = []
    for mid, role, content, res_id, created_at, meta_raw in rows:
        meta = None
        if meta_raw:
            try:
                meta = json.loads(meta_raw) if isinstance(meta_raw, str) else meta_raw
            except (json.JSONDecodeError, TypeError):
                meta = None
        messages.append({
            "id": mid,
            "role": role,
            "content": content,
            "resource_id": res_id,
            "created_at": int(created_at) if created_at is not None else None,
            "metadata": meta,
        })

    return {
        "thread_id": thread_id,
        "resource_id": resource_id,
        "overview": overview,
        "overview_updated_at": overview_updated_at,
        "stelis": stelis_data,
        "messages": messages,
    }


def export_threads_native(
    persona_id: str,
    thread_suffixes: Optional[Iterable[str]] = None,
    start: Optional[str] = None,
    end: Optional[str] = None,
) -> Dict[str, Any]:
    """Export threads to native format dict.

    Args:
        persona_id: Persona identifier.
        thread_suffixes: Thread suffixes or full IDs to export. None = all.
        start: Start ISO timestamp filter (inclusive).
        end: End ISO timestamp filter (inclusive).

    Returns:
        Dict in saiverse_saimemory_v1 format.
    """
    db_path = Path.home() / ".saiverse" / "personas" / persona_id / "memory.db"
    if not db_path.exists():
        raise FileNotFoundError(f"memory.db not found for persona {persona_id}: {db_path}")

    start_epoch = int(datetime.fromisoformat(start).timestamp()) if start else None
    end_epoch = int(datetime.fromisoformat(end).timestamp()) if end else None

    conn = sqlite3.connect(str(db_path))
    try:
        thread_ids = _resolve_thread_ids(conn, persona_id, thread_suffixes)
        threads = [
            _export_thread(conn, tid, start_epoch, end_epoch)
            for tid in thread_ids
        ]
        return {
            "format": FORMAT_VERSION,
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "persona_id": persona_id,
            "threads": threads,
        }
    finally:
        conn.close()


def export_thread_by_id(
    persona_id: str,
    thread_id: str,
) -> Dict[str, Any]:
    """Export a single thread by its full thread_id. Used by API."""
    db_path = Path.home() / ".saiverse" / "personas" / persona_id / "memory.db"
    if not db_path.exists():
        raise FileNotFoundError(f"memory.db not found for persona {persona_id}: {db_path}")

    conn = sqlite3.connect(str(db_path))
    try:
        thread_data = _export_thread(conn, thread_id)
        return {
            "format": FORMAT_VERSION,
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "persona_id": persona_id,
            "threads": [thread_data],
        }
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------

def _validate_native_format(data: Dict[str, Any]) -> None:
    """Validate the native format structure."""
    fmt = data.get("format")
    if fmt != FORMAT_VERSION:
        raise ValueError(
            f"Unsupported format: {fmt!r} (expected {FORMAT_VERSION!r})"
        )
    threads = data.get("threads")
    if not isinstance(threads, list):
        raise ValueError("'threads' must be a list")
    for i, thread in enumerate(threads):
        if not isinstance(thread, dict):
            raise ValueError(f"threads[{i}] must be a dict")
        if "thread_id" not in thread:
            raise ValueError(f"threads[{i}] missing 'thread_id'")
        messages = thread.get("messages")
        if not isinstance(messages, list):
            raise ValueError(f"threads[{i}] 'messages' must be a list")


def _import_stelis(conn: sqlite3.Connection, thread_id: str, stelis_data: Dict[str, Any]) -> None:
    """Restore Stelis thread info."""
    # Delete existing stelis record if any
    conn.execute("DELETE FROM stelis_threads WHERE thread_id=?", (thread_id,))

    conn.execute(
        """
        INSERT INTO stelis_threads
        (thread_id, parent_thread_id, depth, window_ratio, status,
         chronicle_prompt, chronicle_summary, created_at, completed_at, label)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            thread_id,
            stelis_data.get("parent_thread_id"),
            stelis_data.get("depth", 0),
            stelis_data.get("window_ratio", 0.8),
            stelis_data.get("status", "active"),
            stelis_data.get("chronicle_prompt"),
            stelis_data.get("chronicle_summary"),
            stelis_data.get("created_at"),
            stelis_data.get("completed_at"),
            stelis_data.get("label"),
        ),
    )
    conn.commit()


def import_threads_native(
    persona_id: str,
    data: Dict[str, Any],
    *,
    replace: bool = True,
    skip_embed: bool = False,
    progress_callback: Optional[Callable[[int, int, str], None]] = None,
) -> Dict[str, Any]:
    """Import threads from native format dict.

    Args:
        persona_id: Target persona ID.
        data: Parsed JSON dict in saiverse_saimemory_v1 format.
        replace: If True, delete existing thread before import.
        skip_embed: If True, skip embedding generation.
        progress_callback: Called with (current, total, message).

    Returns:
        Dict with import summary: {"threads_imported", "messages_imported"}.
    """
    _validate_native_format(data)

    db_path = Path.home() / ".saiverse" / "personas" / persona_id / "memory.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = init_db(str(db_path), check_same_thread=True)

    # Count total messages for progress
    total_messages = sum(len(t.get("messages", [])) for t in data["threads"])
    imported = 0
    threads_imported = 0

    try:
        for thread_data in data["threads"]:
            thread_id = thread_data["thread_id"]
            messages = thread_data.get("messages", [])

            if progress_callback:
                progress_callback(imported, total_messages, f"Processing thread: {thread_id}")

            # Replace mode: delete existing thread
            if replace:
                delete_thread(conn, thread_id)

            # Create thread
            resource_id = thread_data.get("resource_id")
            get_or_create_thread(conn, thread_id, resource_id)

            # Restore overview
            overview = thread_data.get("overview")
            if overview:
                set_thread_overview(conn, thread_id, overview)

            # Restore Stelis info
            stelis = thread_data.get("stelis")
            if stelis and isinstance(stelis, dict):
                _import_stelis(conn, thread_id, stelis)

            # Insert messages preserving original IDs
            for msg in messages:
                msg_id = msg.get("id")
                role = msg.get("role", "user")
                content = msg.get("content", "")
                res_id = msg.get("resource_id", resource_id)
                created_at = msg.get("created_at")
                metadata = msg.get("metadata")

                meta_json = json.dumps(metadata, ensure_ascii=False) if metadata else None

                if msg_id:
                    # Use original message ID
                    conn.execute(
                        "INSERT OR REPLACE INTO messages"
                        "(id, thread_id, role, content, resource_id, created_at, metadata)"
                        " VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (msg_id, thread_id, role, content, res_id, created_at, meta_json),
                    )
                    conn.commit()
                else:
                    add_message(
                        conn,
                        thread_id=thread_id,
                        role=role,
                        content=content,
                        resource_id=res_id,
                        created_at=created_at,
                        metadata=metadata,
                    )

                imported += 1
                if progress_callback and imported % 50 == 0:
                    progress_callback(imported, total_messages, f"Imported {imported}/{total_messages} messages")

            threads_imported += 1

        # Generate embeddings if requested
        if not skip_embed and total_messages > 0:
            if progress_callback:
                progress_callback(imported, total_messages, "Generating embeddings...")
            _regenerate_embeddings(conn, data["threads"], progress_callback, imported, total_messages)

        if progress_callback:
            progress_callback(total_messages, total_messages, "Import complete")

        return {
            "threads_imported": threads_imported,
            "messages_imported": imported,
        }
    finally:
        conn.close()


def _regenerate_embeddings(
    conn: sqlite3.Connection,
    threads_data: List[Dict[str, Any]],
    progress_callback: Optional[Callable[[int, int, str], None]],
    base_progress: int,
    total: int,
) -> None:
    """Regenerate embeddings for imported messages."""
    try:
        from sai_memory.memory.chunking import chunk_text
        from sai_memory.memory.storage import replace_message_embeddings
        from saiverse_memory.adapter import SAIMemoryAdapter

        # Create a temporary adapter just for embedder
        # We need persona_id from the first thread
        if not threads_data:
            return
        first_thread = threads_data[0]["thread_id"]
        persona_id = first_thread.split(":")[0] if ":" in first_thread else first_thread

        adapter = SAIMemoryAdapter(persona_id)
        if not adapter.is_ready() or adapter.embedder is None:
            LOGGER.warning("Embedder not available, skipping embedding generation")
            return

        count = 0
        for thread_data in threads_data:
            for msg in thread_data.get("messages", []):
                msg_id = msg.get("id")
                content = msg.get("content", "")
                if not msg_id or not content or not content.strip():
                    count += 1
                    continue

                chunks = chunk_text(
                    content.strip(),
                    min_chars=adapter.settings.chunk_min_chars,
                    max_chars=adapter.settings.chunk_max_chars,
                )
                payload = [c.strip() for c in chunks if c and c.strip()]
                if payload:
                    vectors = adapter.embedder.embed(payload, is_query=False)
                    replace_message_embeddings(conn, msg_id, vectors)

                count += 1
                if progress_callback and count % 20 == 0:
                    progress_callback(
                        base_progress + count, total + base_progress,
                        f"Embedding {count} messages...",
                    )

        adapter.close()
    except Exception as exc:
        LOGGER.warning("Failed to regenerate embeddings: %s", exc)
