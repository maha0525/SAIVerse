import contextlib
import logging
import os
import threading
import uuid
from pathlib import Path
from typing import Dict, Any

from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from api.deps import get_manager
from saiverse.data_paths import get_personas_dir
from .models import (
    UpdateMemopediaPageRequest,
    CreateMemopediaPageRequest,
    SetTrunkRequest,
    SetImportantRequest,
    DeskPageRequest,
    MovePagesToTrunkRequest,
    GenerateMemopediaRequest,
    GenerationJobStatus,
    BuildMemopediaFromLogsRequest,
)
from .utils import get_adapter
from sai_memory.memopedia.storage import CATEGORY_DEFS, category_keys

router = APIRouter()
LOGGER = logging.getLogger(__name__)

#: ログからの再構築を打ち切る連続失敗数。たまたま落ちた 1 バッチでは止めず、
#: DB が壊れている等で全部落ちる状態では partial の顔で終わらせない。
_MAX_CONSECUTIVE_BATCH_FAILURES = 3

# In-memory job store for Memopedia generation
_memopedia_jobs: Dict[str, Dict[str, Any]] = {}
_memopedia_jobs_lock = threading.Lock()


def _get_memopedia(adapter):
    """Helper to get Memopedia instance from adapter.

    adapter と同じ錠前で書く。渡さなくても Memopedia が DB ファイルの錠前を
    配り所から取るので同じものになるが、既に手元にあるなら渡すほうが素直
    (docs/issues/memopedia_writers_bypass_adapter_lock.md)。
    """
    from sai_memory.memopedia import Memopedia
    return Memopedia(adapter.conn, db_lock=adapter._db_lock)


def _adapter_db_lock(manager, persona_id: str):
    """このペルソナの memory.db の錠前を返す。

    バックグラウンドワーカーは専用接続を開くが、同じ DB へ adapter 経由の
    書き手が同居しうる。錠前は DB ファイルに紐づく (``sai_memory.db_locks``)
    ので、**ペルソナがロード済みかどうかに関係なく同じものが返る** —— ジョブの
    実行中にペルソナがロードされても、後から現れた adapter と同じ錠前を共有する
    (まはー裁定 2026-08-06、案A)。
    """
    from sai_memory.db_locks import lock_for_path

    persona = manager.personas.get(persona_id) if manager else None
    adapter = getattr(persona, "sai_memory", None)
    if adapter is not None:
        return adapter._db_lock
    return lock_for_path(str(get_personas_dir() / persona_id / "memory.db"))


@router.get("/{persona_id}/memopedia/tree")
def get_memopedia_tree(persona_id: str, manager = Depends(get_manager)):
    """Get the Memopedia knowledge tree with category metadata."""
    with get_adapter(persona_id, manager) as adapter:
        try:
            memopedia = _get_memopedia(adapter)
            tree = memopedia.get_tree()
            categories_meta = [
                {
                    "key": d.key,
                    "label": d.label,
                    "label_en": d.label_en,
                    "hide_when_empty": d.hide_when_empty,
                    "can_generate": d.extractable,
                    "writable": d.writable,
                }
                for d in sorted(CATEGORY_DEFS.values(), key=lambda d: d.order)
                if d.in_tree
            ]
            return {"categories": categories_meta, **tree}
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Memopedia error: {e}")


@router.get("/{persona_id}/memopedia/pages/{page_id}")
def get_memopedia_page(persona_id: str, page_id: str, manager = Depends(get_manager)):
    """Get a Memopedia page content as Markdown, plus fragments."""
    with get_adapter(persona_id, manager) as adapter:
        try:
            memopedia = _get_memopedia(adapter)
            md = memopedia.get_page_markdown(page_id)
            fragments = memopedia.get_fragments(page_id)
            return {
                "content": md,
                "fragments": [
                    {
                        "id": f.id,
                        "content": f.content,
                        "source_date": f.source_date,
                        "chronicle_entry_id": f.chronicle_entry_id,
                        "created_at": f.created_at,
                    }
                    for f in fragments
                ],
            }
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


@router.post("/{persona_id}/memopedia/pages/{page_id}/rollback/{edit_id}")
def rollback_memopedia_page(persona_id: str, page_id: str, edit_id: str, manager = Depends(get_manager)):
    """Rollback a page to the state before a specific edit."""
    LOGGER.info("[rollback API] persona=%s page=%s edit=%s", persona_id, page_id, edit_id)
    with get_adapter(persona_id, manager) as adapter:
        try:
            memopedia = _get_memopedia(adapter)
            result = memopedia.rollback_page(page_id, edit_id)
            if result is None:
                LOGGER.warning("[rollback API] rollback_page returned None")
                raise HTTPException(status_code=404, detail="Page or edit not found")
            LOGGER.info("[rollback API] Success: page=%s title=%s", result.id, result.title)
            return {
                "success": True,
                "page": {
                    "id": result.id,
                    "title": result.title,
                    "summary": result.summary,
                    "content": result.content,
                }
            }
        except HTTPException:
            raise
        except Exception as e:
            LOGGER.exception("[rollback API] Exception: %s", e)
            raise HTTPException(status_code=500, detail=f"Rollback failed: {e}")


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
    
    from sai_memory.memopedia import ChronicleProtectedError

    with get_adapter(persona_id, manager) as adapter:
        try:
            memopedia = _get_memopedia(adapter)
            # 事前検証 (2026-08-19 Codex 第五巡 #2): chronicle × is_trunk=True は
            # **update_page より前に** 409 で止める。update_page 確定後に
            # set_trunk で弾くと、本文だけ変わって trunk 化は拒否という部分適用
            # (409 なのにページは変わった) になる。
            if request.is_trunk:
                page = memopedia.get_page(page_id)
                if page is not None and page.category == "chronicle":
                    raise HTTPException(
                        status_code=409,
                        detail=str(
                            ChronicleProtectedError(page_id, "trunk promotion")
                        ),
                    )
            updated = memopedia.update_page(
                page_id,
                title=request.title,
                summary=request.summary,
                content=request.content,
                keywords=request.keywords,
                edit_source="manual_ui",
            )
            if request.is_trunk is not None:
                try:
                    memopedia.set_trunk(page_id, request.is_trunk)
                except ChronicleProtectedError as exc:
                    # 保険 (事前検証との ms 級競合)。保護は 404 と区別して 409。
                    raise HTTPException(status_code=409, detail=str(exc))
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
    
    from sai_memory.memopedia import ChronicleProtectedError

    with get_adapter(persona_id, manager) as adapter:
        try:
            memopedia = _get_memopedia(adapter)
            try:
                success = memopedia.delete_page(page_id, edit_source="manual_ui")
            except ChronicleProtectedError as exc:
                # 保護は「未発見 (404)」でなく衝突 (409) として区別して返す。
                raise HTTPException(status_code=409, detail=str(exc))
            if not success:
                raise HTTPException(status_code=404, detail="Page not found or could not be deleted")
            return {"success": True}
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Memopedia error: {e}")


@router.get("/{persona_id}/memopedia/export", tags=["Memopedia"])
def export_memopedia(persona_id: str, manager=Depends(get_manager)):
    """Export all Memopedia pages as JSON."""
    with get_adapter(persona_id, manager) as adapter:
        try:
            memopedia = _get_memopedia(adapter)
            data = memopedia.export_json()
            return data
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Memopedia export error: {e}")


@router.post("/{persona_id}/memopedia/import", tags=["Memopedia"])
def import_memopedia(persona_id: str, body: dict, clear: bool = False, manager=Depends(get_manager)):
    """Import Memopedia pages from JSON.

    Query params:
        clear: If true, delete all existing pages before importing.
    Body:
        JSON data in the same format as export_json() output.
    """
    with get_adapter(persona_id, manager) as adapter:
        try:
            memopedia = _get_memopedia(adapter)
            imported = memopedia.import_json(body, clear_existing=clear)
            return {
                "success": True,
                "imported_count": imported,
                "message": f"Imported {imported} pages",
            }
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Memopedia import error: {e}")


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

    from sai_memory.memopedia import ChronicleProtectedError

    with get_adapter(persona_id, manager) as adapter:
        try:
            memopedia = _get_memopedia(adapter)
            try:
                updated = memopedia.set_trunk(page_id, request.is_trunk)
            except ChronicleProtectedError as exc:
                # 保護は「未発見 (404)」でなく衝突 (409) として区別して返す。
                raise HTTPException(status_code=409, detail=str(exc))
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


@router.post("/{persona_id}/memopedia/pages/{page_id}/desk")
def set_memopedia_page_desk(
    persona_id: str,
    page_id: str,
    request: DeskPageRequest,
    manager = Depends(get_manager),
):
    """机に開く / 棚に戻す (open=true: 机に開く、open=false: 棚に戻す)。

    P4-c: vividness UI の後継。ページを机に開いておくと Metabolism を跨いで
    head に残り続ける（memory_open / memory_close スペルと同等）。
    """
    if page_id.startswith("root_"):
        raise HTTPException(status_code=400, detail="Cannot open/close root pages on desk")

    with get_adapter(persona_id, manager) as adapter:
        try:
            # short_id を取得して m:N ref を組む
            memopedia = _get_memopedia(adapter)
            page = memopedia.get_page(page_id)
            if not page:
                raise HTTPException(status_code=404, detail="Page not found")
            if page.short_id is None:
                raise HTTPException(status_code=422, detail="Page has no short_id; cannot open on desk")

            ref = f"m:{page.short_id}"
            from saiverse.memory_atlas import open_page, close_page, AtlasRefError
            try:
                if request.open:
                    result_text = open_page(adapter, ref, manager=manager)
                else:
                    result_text = close_page(adapter, ref, manager=manager)
            except AtlasRefError as e:
                raise HTTPException(status_code=400, detail=str(e))

            return {"success": True, "message": result_text, "ref": ref}
        except HTTPException:
            raise
        except Exception as e:
            LOGGER.exception("[desk API] Exception for page=%s: %s", page_id, e)
            raise HTTPException(status_code=500, detail=f"Desk operation failed: {e}")


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
    _valid = set(category_keys("extractable"))
    if category not in _valid:
        raise HTTPException(status_code=400, detail=f"Invalid category. Must be one of: {', '.join(sorted(_valid))}")

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
    db_lock=None,
) -> None:
    """Background worker for Memopedia page generation.

    Args:
        db_lock: ペルソナがロード済みなら adapter の ``_db_lock``。接続は専用でも、
            ロックを共有すればロックを尊重する書き手 (adapter 経由の追記) と
            直列化される (docs/issues/memopedia_writers_bypass_adapter_lock.md)。
    """
    from sai_memory.memory.storage import init_db
    from sai_memory.memopedia import init_memopedia_tables
    from sai_memory.memopedia.generator import generate_memopedia_page
    from saiverse.model_configs import find_model_config
    from llm_clients.factory import get_llm_client
    
    try:
        _update_memopedia_job(job_id, message="Initializing...")
        
        # Get persona database path
        persona_dir = get_personas_dir() / persona_id
        db_path = persona_dir / "memory.db"
        
        if not db_path.exists():
            _update_memopedia_job(job_id, status="failed", error=f"Database not found: {db_path}")
            return
        
        # テーブルの用意は generate_memopedia_page の中の Memopedia が
        # **ロックの内側で**行う (ここで先に呼ぶと commit がロック外で走る)
        # init_db は列の追加や既存行の補完まで行う書き込み。接続は専用でも DB は
        # 共有なので、ロード済みペルソナの書き手と同じ錠前の内側で行う
        # (Codex 八巡 #5)。テーブルの用意は generate_memopedia_page の中の
        # Memopedia が同じ錠前で行う
        with (db_lock or contextlib.nullcontext()):
            conn = init_db(str(db_path), check_same_thread=False)

        # Initialize LLM client
        _update_memopedia_job(job_id, message="Initializing LLM client...")
        
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
            db_lock=db_lock,
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
    persona_dir = get_personas_dir() / persona_id
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
        db_lock=_adapter_db_lock(manager, persona_id),
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


# -----------------------------------------------------------------------------
# Build Memopedia from Logs API (entity extraction)
# -----------------------------------------------------------------------------

def _run_build_memopedia_from_logs(
    job_id: str,
    persona_id: str,
    batch_size: int,
    limit: int,
    start_after: float,
    model_name: str | None,
    db_lock=None,
    start_after_rowid: int = 0,
) -> None:
    """Background worker for building Memopedia from chat logs.

    Args:
        db_lock: ペルソナがロード済みなら adapter の ``_db_lock``
            (``_adapter_db_lock`` 参照)。
        start_after: 再開位置の時刻。``start_after_rowid`` と対で使う。
        start_after_rowid: 再開位置の行番号。時刻だけでは同じ秒のメッセージの
            順序を表せず、「その時刻より後」だと同秒の行を取りこぼし、
            「その時刻から」だと同じバッチを何度も処理し続ける
            (Codex 四巡 #1)。
    """
    import json
    import time as _time
    from sai_memory.memory.storage import init_db, Message
    from sai_memory.memopedia import Memopedia, init_memopedia_tables
    from sai_memory.arasuji import init_arasuji_tables
    from sai_memory.memory.entity_extractor import (
        ExtractionFailed,
        extract_entities,
        reflect_to_memopedia,
        _format_page_list,
    )
    from sai_memory.arasuji.context import get_episode_context_for_timerange

    try:
        _update_memopedia_job(job_id, message="データベースを初期化中...")

        persona_dir = get_personas_dir() / persona_id
        db_path = persona_dir / "memory.db"

        if not db_path.exists():
            _update_memopedia_job(job_id, status="failed", error=f"Database not found: {db_path}")
            return

        # init_db も列の追加や既存行の補完まで行う書き込み。接続は専用でも DB は
        # 共有なので、ロード済みペルソナの書き手と同じ錠前の内側で行う
        # (Codex 五巡 #3 / 七巡 #5)。Memopedia のテーブルは下の
        # Memopedia(conn, db_lock=...) が同じ錠前で用意する
        with (db_lock or contextlib.nullcontext()):
            conn = init_db(str(db_path), check_same_thread=False)
            init_arasuji_tables(conn)

        # Initialize LLM client
        _update_memopedia_job(job_id, message="LLMクライアントを初期化中...")

        from saiverse.model_defaults import BUILTIN_DEFAULT_LITE_MODEL
        from saiverse.model_configs import find_model_config
        from llm_clients.factory import get_llm_client
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
            _update_memopedia_job(job_id, status="failed", error=f"Model '{model_to_use}' not found")
            conn.close()
            return

        provider = model_config.get("provider", "gemini")
        context_length = model_config.get("context_length", 128000)
        client = get_llm_client(resolved_model_id, provider, context_length, config=model_config)
        LOGGER.info("[Build Memopedia] LLM: %s / %s", model_config.get("model", resolved_model_id), provider)

        # Fetch messages
        _update_memopedia_job(job_id, message="メッセージを取得中...")

        query = """
            SELECT rowid, id, thread_id, role, content, resource_id, created_at, metadata
            FROM messages
            WHERE thread_id NOT IN (SELECT thread_id FROM stelis_threads)
        """
        params = []
        if start_after > 0:
            # (時刻, 行番号) の組で「その先」を指す。時刻だけだと、同じ秒の
            # メッセージがバッチの境目をまたいだとき、「より後」では境目の行が
            # 落ち、「その時刻から」では同じバッチを永久に処理し続ける
            # (Codex 三巡 #2 / 四巡 #1)
            query += " AND (created_at > ? OR (created_at = ? AND rowid > ?))"
            params.extend([start_after, start_after, start_after_rowid])
        query += " ORDER BY created_at ASC, rowid ASC"
        if limit > 0:
            query += " LIMIT ?"
            params.append(limit)

        cur = conn.execute(query, params)
        messages = []
        # メッセージ id → 行番号。再開位置を (時刻, 行番号) で記録するため
        rowid_of: dict[str, int] = {}
        for row in cur.fetchall():
            row_id, msg_id, tid, role, content, resource_id, created_at, metadata_raw = row
            rowid_of[msg_id] = row_id
            metadata = {}
            if metadata_raw:
                try:
                    metadata = json.loads(metadata_raw)
                except Exception:
                    pass
            messages.append(Message(
                id=msg_id, thread_id=tid, role=role, content=content,
                resource_id=resource_id, created_at=created_at, metadata=metadata,
            ))

        if not messages:
            _update_memopedia_job(
                job_id, status="completed", progress=0, total=0,
                message="処理対象のメッセージがありません",
            )
            conn.close()
            return

        total_batches = (len(messages) + batch_size - 1) // batch_size
        _update_memopedia_job(
            job_id, progress=0, total=total_batches,
            message=f"{len(messages)} メッセージを {total_batches} バッチで処理開始",
        )

        memopedia = Memopedia(conn, db_lock=db_lock)
        total_entities = 0
        total_new_pages = 0
        total_updated_pages = 0
        total_deduped = 0
        batch_count = 0
        failed_batches = 0
        consecutive_failures = 0
        # 「処理した」は抽出まで通ったバッチ／メッセージだけ。失敗も、小さすぎて
        # 飛ばした末尾も、この数には入れない (画面に出る数字なので実態と合わせる)
        processed_batches = 0
        processed_messages = 0
        skipped_messages = 0
        failed_ranges: list[tuple[float, float]] = []
        # 次回の再開位置 (時刻, 行番号)。「連続して成功したところまで」で止める
        checkpoint_ts = start_after
        checkpoint_rowid = start_after_rowid

        for i in range(0, len(messages), batch_size):
            batch = messages[i:i + batch_size]
            if len(batch) < batch_size // 2 and i > 0:
                LOGGER.info("Skipping small final batch (%d messages)", len(batch))
                skipped_messages += len(batch)
                continue

            batch_count += 1
            start_time = min(m.created_at for m in batch)
            end_time = max(m.created_at for m in batch)
            time_str = _time.strftime("%Y-%m-%d %H:%M", _time.localtime(start_time))

            _update_memopedia_job(
                job_id, progress=batch_count, total=total_batches,
                message=f"バッチ {batch_count}/{total_batches} ({time_str})",
            )

            # Episode context
            ep_ctx = ""
            try:
                ep_ctx = get_episode_context_for_timerange(
                    conn, start_time=start_time, end_time=end_time, max_entries=10,
                )
            except Exception:
                pass

            existing_pages = _format_page_list(memopedia)

            # 抽出と反映を**ひとつの try** で捕まえる。反映側 (DB ロック /
            # スキーマ / 接続) の例外が外へ抜けると、そのバッチだけでなく
            # 再構築全体が止まる (Codex 六巡 #3)
            try:
                entities = extract_entities(
                    client, batch,
                    episode_context=ep_ctx,
                    existing_pages=existing_pages,
                    persona_id=persona_id,
                )
                results = reflect_to_memopedia(
                    entities, memopedia,
                    source_time=int(end_time),
                ) if entities else []
            except Exception:
                # 一つのバッチの失敗で再構築全体を止めない。ただし黙って飛ばさず
                # 数えて、完了メッセージに出す (失敗を成功の顔で終わらせない)。
                # 再開位置 (checkpoint_ts) はここで止める —— 先へ進めると、
                # 次回この範囲が取得されず、失敗した範囲は二度と拾えない。
                failed_batches += 1
                consecutive_failures += 1
                failed_ranges.append((start_time, end_time))
                LOGGER.warning(
                    "[memopedia-build] バッチ %d/%d (%s〜) が失敗しました",
                    batch_count, total_batches, time_str, exc_info=True,
                )
                if consecutive_failures >= _MAX_CONSECUTIVE_BATCH_FAILURES:
                    # たまたま落ちた 1 バッチと、DB が壊れている状態は別物。
                    # 続けて落ちるなら後者なので、partial の顔で終わらせずに
                    # 止める (Codex 七巡 #3)
                    raise RuntimeError(
                        f"{consecutive_failures} バッチ続けて失敗したため中断しました"
                        "（データベースかモデルの側に継続的な問題があります）"
                    )
                continue

            consecutive_failures = 0
            processed_batches += 1
            processed_messages += len(batch)
            # 再開位置を進めるのは**反映まで終わってから**。抽出だけ済んだ時点で
            # 進めると、反映で落ちた範囲が次回に取得されない
            if failed_batches == 0:
                # 失敗が一度も無いあいだだけ再開位置を進める (連続して成功した
                # ところまでが、次回に安全に飛ばせる範囲)。位置は「このバッチで
                # 最後に処理したメッセージ」そのもの
                last_in_batch = batch[-1]
                checkpoint_ts = last_in_batch.created_at
                checkpoint_rowid = rowid_of.get(last_in_batch.id, checkpoint_rowid)

            if not entities:
                continue

            total_entities += len(entities)
            total_new_pages += sum(1 for r in results if r.is_new_page)
            total_updated_pages += sum(1 for r in results if not r.is_new_page)
            # 「既にある」で作らなかった分。数として見せないと、重複検査が
            # 黙って落としているように見える
            total_deduped += sum(r.notes_deduped for r in results)

        conn.close()

        # 再開位置は「連続して成功したバッチの最後のメッセージ」。実際に処理した
        # ところまでしか進めない —— 失敗した範囲も、小さすぎて飛ばした末尾も、
        # 再開位置の向こう側へ置き去りにしない (Codex 三巡 #2)
        last_ts = checkpoint_ts

        _update_memopedia_job(
            job_id,
            # 末尾が小さくて次回に回した分も「まだ全部は終わっていない」。
            # completed にすると画面が続きの再開位置を捨て、その末尾は
            # 何度実行しても飛ばされ続ける (Codex 七巡 #7)
            status="partial" if (failed_batches or skipped_messages) else "completed",
            progress=total_batches, total=total_batches,
            message=(
                f"完了: {total_entities} エンティティ抽出, "
                f"{total_new_pages} 新規ページ, {total_updated_pages} 更新"
                + (
                    f"（{failed_batches} バッチは抽出に失敗しました。"
                    "再開位置は最初の失敗の手前で止めてあるので、"
                    "もう一度実行すると、そこからやり直します）"
                    if failed_batches else ""
                )
            ),
            result={
                "total_entities": total_entities,
                "new_pages": total_new_pages,
                "updated_pages": total_updated_pages,
                "deduped_notes": total_deduped,
                "failed_batches": failed_batches,
                "failed_ranges": [
                    {"start": s, "end": e} for s, e in failed_ranges
                ],
                "batches_processed": processed_batches,
                "messages_processed": processed_messages,
                "messages_fetched": len(messages),
                "messages_skipped": skipped_messages,
                "last_message_timestamp": last_ts,
                "last_message_rowid": checkpoint_rowid,
            },
        )

    except Exception as e:
        LOGGER.exception("Build Memopedia from logs failed: %s", e)
        _update_memopedia_job(job_id, status="failed", error=str(e))


@router.post("/{persona_id}/memopedia/build-from-logs", tags=["Memopedia"])
async def start_build_memopedia_from_logs(
    persona_id: str,
    request: BuildMemopediaFromLogsRequest,
    background_tasks: BackgroundTasks,
    manager=Depends(get_manager),
):
    """Start building Memopedia pages from chat logs as a background job.

    Processes messages in batches, extracting entities and reflecting them
    to Memopedia pages (creating new pages or appending to existing ones).
    """
    persona_dir = get_personas_dir() / persona_id
    if not persona_dir.exists():
        raise HTTPException(status_code=404, detail=f"Persona not found: {persona_id}")

    job_id = str(uuid.uuid4())
    with _memopedia_jobs_lock:
        _memopedia_jobs[job_id] = {
            "job_id": job_id,
            "status": "running",
            "progress": 0,
            "total": 0,
            "message": "Starting...",
            "result": None,
            "error": None,
        }

    background_tasks.add_task(
        _run_build_memopedia_from_logs,
        job_id=job_id,
        persona_id=persona_id,
        batch_size=request.batch_size,
        limit=request.limit,
        start_after=request.start_after,
        start_after_rowid=request.start_after_rowid,
        model_name=request.model,
        db_lock=_adapter_db_lock(manager, persona_id),
    )

    return {"job_id": job_id, "status": "running"}
