import logging
import os
import threading
import uuid
from pathlib import Path
from typing import Dict, Any

from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from api.deps import get_manager
from .models import (
    UpdateMemopediaPageRequest,
    CreateMemopediaPageRequest,
    SetTrunkRequest,
    SetImportantRequest,
    MovePagesToTrunkRequest,
    GenerateMemopediaRequest,
    GenerationJobStatus,
)
from .utils import get_adapter

router = APIRouter()
LOGGER = logging.getLogger(__name__)

# In-memory job store for Memopedia generation
_memopedia_jobs: Dict[str, Dict[str, Any]] = {}
_memopedia_jobs_lock = threading.Lock()


def _get_memopedia(adapter):
    """Helper to get Memopedia instance from adapter."""
    from sai_memory.memopedia import Memopedia
    return Memopedia(adapter.conn)


@router.get("/{persona_id}/memopedia/tree")
def get_memopedia_tree(persona_id: str, manager = Depends(get_manager)):
    """Get the Memopedia knowledge tree."""
    with get_adapter(persona_id, manager) as adapter:
        try:
            memopedia = _get_memopedia(adapter)
            return memopedia.get_tree()
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Memopedia error: {e}")


@router.get("/{persona_id}/memopedia/pages/{page_id}")
def get_memopedia_page(persona_id: str, page_id: str, manager = Depends(get_manager)):
    """Get a Memopedia page content as Markdown."""
    with get_adapter(persona_id, manager) as adapter:
        try:
            memopedia = _get_memopedia(adapter)
            md = memopedia.get_page_markdown(page_id)
            return {"content": md}
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Memopedia error: {e}")


@router.get("/{persona_id}/memopedia/pages/{page_id}/history")
def get_memopedia_page_history(persona_id: str, page_id: str, limit: int = 50, manager = Depends(get_manager)):
    """Get the edit history for a Memopedia page."""
    with get_adapter(persona_id, manager) as adapter:
        try:
            memopedia = _get_memopedia(adapter)
            history = memopedia.get_page_edit_history(page_id, limit=limit)
            return {
                "history": [
                    {
                        "id": h.id,
                        "page_id": h.page_id,
                        "edited_at": h.edited_at,
                        "diff_text": h.diff_text,
                        "ref_start_message_id": h.ref_start_message_id,
                        "ref_end_message_id": h.ref_end_message_id,
                        "edit_type": h.edit_type,
                        "edit_source": h.edit_source,
                    }
                    for h in history
                ]
            }
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Memopedia error: {e}")


@router.put("/{persona_id}/memopedia/pages/{page_id}")
def update_memopedia_page(
    persona_id: str,
    page_id: str,
    request: UpdateMemopediaPageRequest,
    manager = Depends(get_manager)
):
    """Update a Memopedia page (title, summary, content, keywords)."""
    if page_id.startswith("root_"):
        raise HTTPException(status_code=400, detail="Cannot edit root pages")
    
    with get_adapter(persona_id, manager) as adapter:
        try:
            memopedia = _get_memopedia(adapter)
            updated = memopedia.update_page(
                page_id,
                title=request.title,
                summary=request.summary,
                content=request.content,
                keywords=request.keywords,
                vividness=request.vividness,
                edit_source="manual_ui",
            )
            if request.is_trunk is not None:
                memopedia.set_trunk(page_id, request.is_trunk)
                updated = memopedia.get_page(page_id)
            if not updated:
                raise HTTPException(status_code=404, detail="Page not found")
            return {
                "success": True,
                "page": {
                    "id": updated.id,
                    "title": updated.title,
                    "summary": updated.summary,
                    "content": updated.content,
                    "keywords": updated.keywords,
                    "vividness": updated.vividness,
                    "is_trunk": updated.is_trunk,
                }
            }
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Memopedia error: {e}")


@router.delete("/{persona_id}/memopedia/pages/{page_id}")
def delete_memopedia_page(persona_id: str, page_id: str, manager = Depends(get_manager)):
    """Delete a Memopedia page (soft delete)."""
    if page_id.startswith("root_"):
        raise HTTPException(status_code=400, detail="Cannot delete root pages")
    
    with get_adapter(persona_id, manager) as adapter:
        try:
            memopedia = _get_memopedia(adapter)
            success = memopedia.delete_page(page_id, edit_source="manual_ui")
            if not success:
                raise HTTPException(status_code=404, detail="Page not found or could not be deleted")
            return {"success": True}
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Memopedia error: {e}")


@router.delete("/{persona_id}/memopedia/pages", tags=["Memopedia"])
def delete_all_memopedia_pages(persona_id: str, manager=Depends(get_manager)):
    """Delete ALL non-root Memopedia pages (and their edit history)."""
    with get_adapter(persona_id, manager) as adapter:
        try:
            memopedia = _get_memopedia(adapter)
            deleted_count = memopedia.clear_all_pages()
            return {
                "success": True,
                "deleted_count": deleted_count,
                "message": f"Deleted {deleted_count} Memopedia pages",
            }
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to delete Memopedia pages: {e}")


@router.post("/{persona_id}/memopedia/pages")
def create_memopedia_page(
    persona_id: str,
    request: CreateMemopediaPageRequest,
    manager = Depends(get_manager)
):
    """Create a new Memopedia page."""
    with get_adapter(persona_id, manager) as adapter:
        try:
            memopedia = _get_memopedia(adapter)
            page = memopedia.create_page(
                parent_id=request.parent_id,
                title=request.title,
                summary=request.summary,
                content=request.content,
                keywords=request.keywords,
                vividness=request.vividness,
                is_trunk=request.is_trunk,
                edit_source="manual_ui",
            )
            return {
                "success": True,
                "page": {
                    "id": page.id,
                    "parent_id": page.parent_id,
                    "title": page.title,
                    "summary": page.summary,
                    "content": page.content,
                    "category": page.category,
                    "keywords": page.keywords,
                    "vividness": page.vividness,
                    "is_trunk": page.is_trunk,
                }
            }
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Memopedia error: {e}")


@router.get("/{persona_id}/memopedia/trunks")
def get_memopedia_trunks(
    persona_id: str,
    category: str = None,
    manager = Depends(get_manager)
):
    """Get all trunk pages, optionally filtered by category."""
    with get_adapter(persona_id, manager) as adapter:
        try:
            memopedia = _get_memopedia(adapter)
            trunks = memopedia.get_trunks(category)
            return {
                "trunks": [
                    {
                        "id": t.id,
                        "parent_id": t.parent_id,
                        "title": t.title,
                        "summary": t.summary,
                        "category": t.category,
                        "keywords": t.keywords,
                        "vividness": t.vividness,
                    }
                    for t in trunks
                ]
            }
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Memopedia error: {e}")


@router.put("/{persona_id}/memopedia/pages/{page_id}/trunk")
def set_memopedia_page_trunk(
    persona_id: str,
    page_id: str,
    request: SetTrunkRequest,
    manager = Depends(get_manager)
):
    """Set or unset the trunk flag for a page."""
    if page_id.startswith("root_"):
        raise HTTPException(status_code=400, detail="Cannot modify root pages")

    with get_adapter(persona_id, manager) as adapter:
        try:
            memopedia = _get_memopedia(adapter)
            updated = memopedia.set_trunk(page_id, request.is_trunk)
            if not updated:
                raise HTTPException(status_code=404, detail="Page not found")
            return {
                "success": True,
                "page": {
                    "id": updated.id,
                    "title": updated.title,
                    "is_trunk": updated.is_trunk,
                }
            }
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Memopedia error: {e}")


@router.put("/{persona_id}/memopedia/pages/{page_id}/important")
def set_memopedia_page_important(
    persona_id: str,
    page_id: str,
    request: SetImportantRequest,
    manager = Depends(get_manager)
):
    """Set or unset the important flag for a page."""
    if page_id.startswith("root_"):
        raise HTTPException(status_code=400, detail="Cannot modify root pages")

    with get_adapter(persona_id, manager) as adapter:
        try:
            memopedia = _get_memopedia(adapter)
            updated = memopedia.set_important(page_id, request.is_important)
            if not updated:
                raise HTTPException(status_code=404, detail="Page not found")
            return {
                "success": True,
                "page": {
                    "id": updated.id,
                    "title": updated.title,
                    "is_important": updated.is_important,
                }
            }
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Memopedia error: {e}")


@router.post("/{persona_id}/memopedia/pages/move")
def move_memopedia_pages(
    persona_id: str,
    request: MovePagesToTrunkRequest,
    manager = Depends(get_manager)
):
    """Move multiple pages to a trunk (or any parent page)."""
    with get_adapter(persona_id, manager) as adapter:
        try:
            memopedia = _get_memopedia(adapter)
            result = memopedia.move_pages_to_trunk(request.page_ids, request.trunk_id)
            return result
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Memopedia error: {e}")


@router.get("/{persona_id}/memopedia/unorganized")
def get_unorganized_pages(
    persona_id: str,
    category: str,
    manager = Depends(get_manager)
):
    """Get pages that are direct children of the root (not in any trunk)."""
    if category not in ("people", "terms", "plans"):
        raise HTTPException(status_code=400, detail="Invalid category. Must be 'people', 'terms', or 'plans'")

    with get_adapter(persona_id, manager) as adapter:
        try:
            memopedia = _get_memopedia(adapter)
            pages = memopedia.get_unorganized_pages(category)
            return {
                "category": category,
                "pages": [
                    {
                        "id": p.id,
                        "title": p.title,
                        "summary": p.summary,
                        "keywords": p.keywords,
                        "vividness": p.vividness,
                    }
                    for p in pages
                ]
            }
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Memopedia error: {e}")


# -----------------------------------------------------------------------------
# Memopedia Generation API
# -----------------------------------------------------------------------------

def _update_memopedia_job(job_id: str, **kwargs) -> None:
    """Update job status in the store."""
    with _memopedia_jobs_lock:
        if job_id in _memopedia_jobs:
            _memopedia_jobs[job_id].update(kwargs)


def _run_memopedia_generation(
    job_id: str,
    persona_id: str,
    keyword: str,
    directions: str | None,
    category: str | None,
    max_loops: int,
    context_window: int,
    with_chronicle: bool,
    model_name: str | None,
) -> None:
    """Background worker for Memopedia page generation."""
    from sai_memory.memory.storage import init_db
    from sai_memory.memopedia import init_memopedia_tables
    from sai_memory.memopedia.generator import generate_memopedia_page
    from saiverse.model_configs import find_model_config
    from llm_clients.factory import get_llm_client
    
    try:
        _update_memopedia_job(job_id, message="Initializing...")
        
        # Get persona database path
        persona_dir = Path.home() / ".saiverse" / "personas" / persona_id
        db_path = persona_dir / "memory.db"
        
        if not db_path.exists():
            _update_memopedia_job(job_id, status="failed", error=f"Database not found: {db_path}")
            return
        
        conn = init_db(str(db_path), check_same_thread=False)
        init_memopedia_tables(conn)
        
        # Initialize LLM client
        _update_memopedia_job(job_id, message="Initializing LLM client...")
        
        env_model = os.getenv("MEMORY_WEAVE_MODEL", "gemini-2.5-flash-lite-preview-09-2025")
        model_to_use = model_name or env_model
        
        resolved_model_id, model_config = find_model_config(model_to_use)
        if not resolved_model_id:
            _update_memopedia_job(job_id, status="failed", error=f"Model '{model_to_use}' not found")
            conn.close()
            return
        
        provider = model_config.get("provider", "gemini")
        context_length = model_config.get("context_length", 128000)
        actual_model_id = model_config.get("model", resolved_model_id)
        
        client = get_llm_client(resolved_model_id, provider, context_length, config=model_config)
        LOGGER.info(f"[Memopedia Gen] LLM client initialized: {actual_model_id} / {provider} (config_key={resolved_model_id})")
        
        _update_memopedia_job(job_id, message=f"Searching for keyword: {keyword}")
        
        def progress_callback(loop: int, max_loops: int, message: str):
            _update_memopedia_job(
                job_id,
                progress=loop,
                total=max_loops,
                message=message,
            )
        
        # Run generation
        result = generate_memopedia_page(
            conn=conn,
            client=client,
            keyword=keyword,
            directions=directions,
            category=category,
            persona_id=persona_id,
            persona_dir=str(persona_dir),
            max_loops=max_loops,
            context_window=context_window,
            with_chronicle=with_chronicle,
            progress_callback=progress_callback,
        )
        
        conn.close()
        
        if result:
            # Check if it's an error diagnostic or a successful result
            if result.get("error") == "no_info_collected":
                # Generation completed but no info was collected
                loops = result.get("loops_completed", 0)
                msgs = result.get("messages_processed", 0)
                queries = result.get("queries_tried", [])
                detail = f"ループ{loops}回、メッセージ{msgs}件を処理したが情報を抽出できませんでした。"
                if queries:
                    detail += f" 試したクエリ: {', '.join(queries[:3])}"
                _update_memopedia_job(
                    job_id,
                    status="completed",
                    progress=max_loops,
                    result=result,
                    message=detail
                )
            else:
                # Successful page creation
                _update_memopedia_job(
                    job_id,
                    status="completed",
                    progress=max_loops,
                    result=result,
                    message=f"Created page: {result.get('title', keyword)}"
                )
        else:
            _update_memopedia_job(
                job_id,
                status="completed",
                progress=max_loops,
                message=f"生成に失敗しました: {keyword}"
            )
        
    except Exception as e:
        LOGGER.exception(f"Memopedia generation failed: {e}")
        _update_memopedia_job(job_id, status="failed", error=str(e))


@router.post("/{persona_id}/memopedia/generate", tags=["Memopedia"])
async def start_memopedia_generation(
    persona_id: str,
    request: GenerateMemopediaRequest,
    background_tasks: BackgroundTasks,
    manager = Depends(get_manager),
):
    """Start Memopedia page generation as a background job.
    
    Uses a Deep Research-style loop:
    1. Search with memory_recall for relevant messages
    2. Expand context around found messages  
    3. Extract knowledge via LLM
    4. Check if information is sufficient
    5. Repeat with different queries if needed
    6. Save as Memopedia page
    """
    # Validate that persona exists
    persona_dir = Path.home() / ".saiverse" / "personas" / persona_id
    if not persona_dir.exists():
        raise HTTPException(status_code=404, detail=f"Persona not found: {persona_id}")
    
    # Create job
    job_id = str(uuid.uuid4())
    with _memopedia_jobs_lock:
        _memopedia_jobs[job_id] = {
            "job_id": job_id,
            "status": "running",
            "progress": 0,
            "total": request.max_loops,
            "message": "Starting...",
            "keyword": request.keyword,
            "result": None,
            "error": None,
        }
    
    # Start background task
    background_tasks.add_task(
        _run_memopedia_generation,
        job_id=job_id,
        persona_id=persona_id,
        keyword=request.keyword,
        directions=request.directions,
        category=request.category,
        max_loops=request.max_loops,
        context_window=request.context_window,
        with_chronicle=request.with_chronicle,
        model_name=request.model,
    )
    
    return {"job_id": job_id, "status": "running"}


@router.get("/{persona_id}/memopedia/generate/{job_id}", tags=["Memopedia"])
def get_memopedia_generation_status(
    persona_id: str,
    job_id: str,
    manager = Depends(get_manager),
):
    """Get the status of a Memopedia generation job."""
    with _memopedia_jobs_lock:
        job = _memopedia_jobs.get(job_id)
    
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    
    return job
