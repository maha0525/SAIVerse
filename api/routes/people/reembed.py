from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from api.deps import get_manager
from saiverse.data_paths import get_persona_memory_db
from .models import ReembedRequest, ReembedStatusResponse
import threading
import logging

LOGGER = logging.getLogger(__name__)
router = APIRouter()

# Track re-embed status per persona
_reembed_status: dict = {}
_reembed_lock = threading.Lock()

_SOURCE_LABELS = {"chronicle": "Chronicle", "memopedia": "Memopedia", "fragment": "Fragment"}


def _run_reembed_task(persona_id: str, force: bool, manager=None):
    """Background task: rebuild every recall vector of one persona.

    Covers all four vector systems (docs/intent/embedding_gemma2_migration.md
    §2): Chronicle / Memopedia page / fragment vectors that are stale (or all
    of them with ``force``) are deleted and regenerated on the spot, then the
    messages are re-embedded. At the end ``embed_metadata.embed_model`` is
    updated and, if the startup sample finds no stale vector any more, the
    persona's startup warning is lifted through ``manager`` so the banner does
    not come back on reload.
    """
    from sai_memory.config import load_settings
    from sai_memory.embedding_migration import (
        RECALL_SOURCES,
        finish_reembed,
        message_reembed_targets,
        rebuild_recall_source_embeddings,
        reembed_messages,
    )
    from sai_memory.memory.recall import Embedder
    from sai_memory.memory.storage import init_db

    sources = {s: {"total": 0, "done": 0} for s in RECALL_SOURCES}

    def _set(**fields):
        with _reembed_lock:
            _reembed_status[persona_id] = {**fields, "sources": {k: dict(v) for k, v in sources.items()}}

    _set(running=True, progress=0, total=0, message="Starting...")

    try:
        db_path = get_persona_memory_db(persona_id)
        if not db_path.exists():
            with _reembed_lock:
                _reembed_status[persona_id] = {"running": False, "message": "Database not found"}
            return

        settings = load_settings()
        embedder = Embedder(local_model_path=settings.embed_model_path)
        expected_dim = embedder.embedding_size

        conn = init_db(str(db_path), check_same_thread=False)

        try:
            # 1. Chronicle / Memopedia pages / fragments (small; done first so
            #    those memories come back to recall early).
            def _source_progress(source: str, done: int, total: int) -> None:
                sources[source] = {"total": total, "done": done}
                _set(
                    running=True, progress=0, total=0,
                    message=f"{_SOURCE_LABELS[source]} {done}/{total}...",
                )

            rebuild_recall_source_embeddings(
                conn, embedder, force=force, progress=_source_progress,
            )

            # 2. Messages.
            target_ids = message_reembed_targets(
                conn, expected_dim, force=force, include_missing=True,
            )
            total = len(target_ids)
            _set(running=True, progress=0, total=total, message=f"Processing 0/{total}...")

            def _message_progress(done: int, total_: int) -> None:
                _set(running=True, progress=done, total=total_, message=f"Processing {done}/{total_}...")

            result = reembed_messages(
                conn, embedder, target_ids,
                chunk_min=settings.chunk_min_chars,
                chunk_max=settings.chunk_max_chars,
                progress=_message_progress,
            )

            # 3. Record the model and lift the warning if nothing stale is left.
            still_stale = finish_reembed(conn, embedder, settings.embed_model)
        finally:
            conn.close()

        source_done = sum(v["done"] for v in sources.values())
        summary = (
            f"Re-embedded {result['embedded']} messages and "
            f"{source_done} Chronicle/Memopedia/Fragment entries."
        )
        if still_stale:
            LOGGER.warning(
                "Re-embed for %s finished but stale vectors remain in %s; "
                "keeping the startup warning.",
                persona_id, ", ".join(still_stale),
            )
            summary += f" Stale vectors remain in: {', '.join(still_stale)}."
        elif manager is not None:
            manager.clear_embed_model_mismatch(persona_id)

        _set(running=False, progress=result["embedded"], total=total, message=summary)

    except Exception as e:
        logging.exception("Re-embed task failed for %s", persona_id)
        with _reembed_lock:
            _reembed_status[persona_id] = {"running": False, "message": f"Error: {str(e)}"}

@router.post("/{persona_id}/reembed")
def reembed_persona_memory(
    persona_id: str,
    request: ReembedRequest,
    background_tasks: BackgroundTasks,
    manager = Depends(get_manager)
):
    """Start re-embedding every recall vector of the persona in the background."""
    # Check if already running
    with _reembed_lock:
        status = _reembed_status.get(persona_id, {})
        if status.get("running"):
            return {"success": False, "message": "Re-embed already in progress.", "status": status}

    # Verify database exists
    db_path = get_persona_memory_db(persona_id)
    if not db_path.exists():
        raise HTTPException(status_code=404, detail=f"Memory database not found for {persona_id}")

    # Start background task
    background_tasks.add_task(_run_reembed_task, persona_id, request.force, manager)

    return {
        "success": True,
        "message": "Re-embed task started. Check status endpoint for progress.",
        "status": {"running": True, "progress": 0, "total": 0, "message": "Starting..."}
    }

@router.get("/{persona_id}/reembed/status", response_model=ReembedStatusResponse)
def get_reembed_status(persona_id: str, manager = Depends(get_manager)):
    """Get the status of the re-embed task."""
    with _reembed_lock:
        status = _reembed_status.get(persona_id, {"running": False, "message": "No task has been run."})
    return ReembedStatusResponse(**status)
