from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from api.deps import get_manager
from .models import ReembedRequest, ReembedStatusResponse
import threading
import logging

LOGGER = logging.getLogger(__name__)
router = APIRouter()

# Track re-embed status per persona
_reembed_status: dict = {}
_reembed_lock = threading.Lock()

def _run_reembed_task(persona_id: str, force: bool):
    """Background task to run re-embedding."""
    from pathlib import Path
    from sai_memory.config import load_settings
    from sai_memory.memory.chunking import chunk_text
    from sai_memory.memory.recall import Embedder
    from sai_memory.memory.storage import get_message, init_db, replace_message_embeddings
    import json
    import logging
    
    with _reembed_lock:
        _reembed_status[persona_id] = {"running": True, "progress": 0, "total": 0, "message": "Starting..."}
    
    try:
        db_path = Path.home() / ".saiverse" / "personas" / persona_id / "memory.db"
        if not db_path.exists():
            with _reembed_lock:
                _reembed_status[persona_id] = {"running": False, "message": "Database not found"}
            return
        
        settings = load_settings()
        embedder = Embedder(
            model=settings.embed_model or "",
            local_model_path=str(Path(settings.embed_model_path).expanduser().resolve()) if settings.embed_model_path else None,
            model_dim=settings.embed_model_dim,
        )
        expected_dim = embedder.model.embedding_size
        
        conn = init_db(str(db_path), check_same_thread=False)
        
        try:
            if force:
                target_ids = set()
                for (mid,) in conn.execute("SELECT DISTINCT id FROM messages"):
                    target_ids.add(mid)
            else:
                all_message_ids = set()
                for (mid,) in conn.execute("SELECT DISTINCT id FROM messages"):
                    all_message_ids.add(mid)
                
                embedded_ids = set()
                bad_ids = set()
                for mid, _, vec_json in conn.execute(
                    "SELECT message_id, chunk_index, vector FROM message_embeddings"
                ):
                    embedded_ids.add(mid)
                    try:
                        vec = json.loads(vec_json)
                        if len(vec) != expected_dim:
                            bad_ids.add(mid)
                    except json.JSONDecodeError:
                        bad_ids.add(mid)
                
                missing_ids = all_message_ids - embedded_ids
                target_ids = missing_ids | bad_ids
            
            if not target_ids:
                with _reembed_lock:
                    _reembed_status[persona_id] = {"running": False, "progress": 0, "total": 0, "message": "No messages need re-embedding."}
                return
            
            target_list = list(target_ids)
            total = len(target_list)
            with _reembed_lock:
                _reembed_status[persona_id] = {"running": True, "progress": 0, "total": total, "message": f"Processing 0/{total}..."}
            
            fixed = 0
            for i, mid in enumerate(target_list):
                msg = get_message(conn, mid)
                if msg is None or not msg.content:
                    continue
                chunks = chunk_text(
                    msg.content,
                    min_chars=settings.chunk_min_chars,
                    max_chars=settings.chunk_max_chars,
                )
                payload = [c.strip() for c in chunks if c and c.strip()]
                if not payload:
                    payload = [msg.content.strip()]
                if not payload:
                    continue
                vectors = embedder.embed(payload, is_query=False)
                replace_message_embeddings(conn, mid, vectors)
                fixed += 1
                
                # Update progress every 10 messages
                if fixed % 10 == 0:
                    with _reembed_lock:
                        _reembed_status[persona_id] = {"running": True, "progress": fixed, "total": total, "message": f"Processing {fixed}/{total}..."}
            
            with _reembed_lock:
                _reembed_status[persona_id] = {"running": False, "progress": fixed, "total": total, "message": f"Re-embedded {fixed} messages."}
        finally:
            conn.close()
            
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
    """Start re-embedding messages in the background."""
    from pathlib import Path
    
    # Check if already running
    with _reembed_lock:
        status = _reembed_status.get(persona_id, {})
        if status.get("running"):
            return {"success": False, "message": "Re-embed already in progress.", "status": status}
    
    # Verify database exists
    db_path = Path.home() / ".saiverse" / "personas" / persona_id / "memory.db"
    if not db_path.exists():
        raise HTTPException(status_code=404, detail=f"Memory database not found for {persona_id}")
    
    # Start background task
    background_tasks.add_task(_run_reembed_task, persona_id, request.force)
    
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
