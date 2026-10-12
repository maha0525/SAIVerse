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


def _live_embedder_ready(manager, persona_id: str) -> bool:
    """True when the loaded persona can embed queries after the re-embed.

    The re-embed job uses its own Embedder, so a persona whose adapter failed
    to load the model at startup would get fresh vectors it still cannot
    search. Retry the load on that adapter (``ensure_embedder``). A persona
    that is not loaded (or has no memory adapter) reads the model on its next
    load, so it counts as ready.
    """
    persona = manager.personas.get(persona_id)
    adapter = persona.sai_memory if persona is not None else None
    if adapter is None:
        return True
    return bool(adapter.ensure_embedder())


def _run_reembed_task(persona_id: str, force: bool, manager=None):
    """Background task: rebuild every recall vector of one persona.

    Covers all four vector systems (docs/intent/embedding_gemma2_migration.md
    §2): Chronicle / Memopedia page / fragment vectors that are stale (or all
    of them with ``force``) are deleted and regenerated on the spot, then the
    messages are re-embedded. At the end ``embed_metadata.embed_model`` is
    updated and, if the startup sample finds no stale vector any more, the
    persona's startup warning is lifted through ``manager`` so the banner does
    not come back on reload.

    A recorded model label that differs from the current one (or is missing
    while vectors exist) runs the job as ``force`` whatever the dimensions:
    the label is what tells two models of the same dimension apart. The
    memory.db is marked as being re-embedded before anything is deleted
    (``begin_reembed``) and the mark is removed by ``finish_reembed``, so a
    job that dies half way is warned about at the next startup.
    """
    from sai_memory.config import load_settings
    from sai_memory.embedding_migration import (
        RECALL_SOURCES,
        begin_reembed,
        finish_reembed,
        message_reembed_targets,
        model_label_changed,
        rebuild_recall_source_embeddings,
        reembed_in_progress,
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
            # 0. A changed model label, or an in-progress mark left by a job
            #    that died half way, rebuilds everything; then mark the
            #    memory.db before the first deletion.
            if not force and model_label_changed(conn, settings.embed_model):
                LOGGER.info(
                    "Re-embed for %s: recorded embed_model differs from '%s'; "
                    "rebuilding every vector.",
                    persona_id, settings.embed_model,
                )
                force = True
            if not force and reembed_in_progress(conn):
                LOGGER.info(
                    "Re-embed for %s: a previous re-embed did not finish; "
                    "rebuilding every vector.",
                    persona_id,
                )
                force = True
            begin_reembed(conn, settings.embed_model)

            # 1. Chronicle / Memopedia pages / fragments (small; done first so
            #    those memories come back to recall early).
            def _source_progress(source: str, done: int, total: int) -> None:
                sources[source] = {"total": total, "done": done}
                _set(
                    running=True, progress=0, total=0,
                    message=f"{_SOURCE_LABELS[source]} {done}/{total}...",
                )

            source_result = rebuild_recall_source_embeddings(
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
            still_stale = finish_reembed(conn, embedder, settings.embed_model, force=force)
        finally:
            conn.close()

        source_done = sum(v["done"] for v in sources.values())
        summary = (
            f"Re-embedded {result['embedded']} messages and "
            f"{source_done} Chronicle/Memopedia/Fragment entries."
        )
        # Rows edited while their vectors were being computed were not stored.
        changed = result.get("changed", 0) + sum(
            counts.get("skipped", 0) for counts in source_result.values()
        )
        if changed:
            summary += f" Skipped {changed} entries edited during the re-embed."
        if still_stale:
            LOGGER.warning(
                "Re-embed for %s finished but stale vectors remain in %s; "
                "keeping the startup warning.",
                persona_id, ", ".join(still_stale),
            )
            summary += f" Stale vectors remain in: {', '.join(still_stale)}."
        elif manager is not None:
            if _live_embedder_ready(manager, persona_id):
                manager.clear_embed_model_mismatch(persona_id)
            else:
                LOGGER.warning(
                    "Re-embed for %s finished, but the running persona's embedding "
                    "model could not be loaded; keeping the startup warning "
                    "(recall stays unavailable until it loads).",
                    persona_id,
                )
                summary += (
                    " The running persona's embedding model could not be loaded, "
                    "so recall is still unavailable; the warning is kept."
                )

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
    # Verify database exists
    db_path = get_persona_memory_db(persona_id)
    if not db_path.exists():
        raise HTTPException(status_code=404, detail=f"Memory database not found for {persona_id}")

    # Check and reserve in one step: the "running" mark is written here, under
    # the lock, so a second request arriving before the background task has
    # started is refused instead of starting a second job.
    reserved = {"running": True, "progress": 0, "total": 0, "message": "Starting..."}
    with _reembed_lock:
        status = _reembed_status.get(persona_id, {})
        if status.get("running"):
            return {"success": False, "message": "Re-embed already in progress.", "status": status}
        _reembed_status[persona_id] = dict(reserved)

    # Start background task
    background_tasks.add_task(_run_reembed_task, persona_id, request.force, manager)

    return {
        "success": True,
        "message": "Re-embed task started. Check status endpoint for progress.",
        "status": reserved,
    }

@router.get("/{persona_id}/reembed/status", response_model=ReembedStatusResponse)
def get_reembed_status(persona_id: str, manager = Depends(get_manager)):
    """Get the status of the re-embed task."""
    with _reembed_lock:
        status = _reembed_status.get(persona_id, {"running": False, "message": "No task has been run."})
    return ReembedStatusResponse(**status)
