from fastapi import APIRouter, Depends, HTTPException
from api.deps import get_manager
from .models import UpdateMemopediaPageRequest

router = APIRouter()

@router.get("/{persona_id}/memopedia/tree")
def get_memopedia_tree(persona_id: str, manager = Depends(get_manager)):
    """Get the Memopedia knowledge tree."""
    persona = manager.personas.get(persona_id)
    adapter = getattr(persona, "sai_memory", None) if persona else None
    should_close = False
    
    if not adapter or not adapter.is_ready():
        from saiverse_memory import SAIMemoryAdapter
        try:
            adapter = SAIMemoryAdapter(persona_id)
            should_close = True
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to access memory: {e}")

    try:
        from sai_memory.memopedia import Memopedia
        memopedia = Memopedia(adapter.conn)
        # Verify tables exist (memopedia init does this, but just in case)
        return memopedia.get_tree()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Memopedia error: {e}")
    finally:
        if should_close and adapter:
            adapter.close()

@router.get("/{persona_id}/memopedia/pages/{page_id}")
def get_memopedia_page(persona_id: str, page_id: str, manager = Depends(get_manager)):
    """Get a Memopedia page content as Markdown."""
    persona = manager.personas.get(persona_id)
    adapter = getattr(persona, "sai_memory", None) if persona else None
    should_close = False
    
    if not adapter or not adapter.is_ready():
        from saiverse_memory import SAIMemoryAdapter
        try:
            adapter = SAIMemoryAdapter(persona_id)
            should_close = True
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to access memory: {e}")

    try:
        from sai_memory.memopedia import Memopedia
        memopedia = Memopedia(adapter.conn)
        md = memopedia.get_page_markdown(page_id)
        return {"content": md}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Memopedia error: {e}")
    finally:
        if should_close and adapter:
            adapter.close()

@router.get("/{persona_id}/memopedia/pages/{page_id}/history")
def get_memopedia_page_history(persona_id: str, page_id: str, limit: int = 50, manager = Depends(get_manager)):
    """Get the edit history for a Memopedia page."""
    persona = manager.personas.get(persona_id)
    adapter = getattr(persona, "sai_memory", None) if persona else None
    should_close = False
    
    if not adapter or not adapter.is_ready():
        from saiverse_memory import SAIMemoryAdapter
        try:
            adapter = SAIMemoryAdapter(persona_id)
            should_close = True
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to access memory: {e}")

    try:
        from sai_memory.memopedia import Memopedia
        memopedia = Memopedia(adapter.conn)
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
    finally:
        if should_close and adapter:
            adapter.close()

@router.put("/{persona_id}/memopedia/pages/{page_id}")
def update_memopedia_page(
    persona_id: str,
    page_id: str,
    request: UpdateMemopediaPageRequest,
    manager = Depends(get_manager)
):
    """Update a Memopedia page (title, summary, content, keywords)."""
    # Prevent editing root pages
    if page_id.startswith("root_"):
        raise HTTPException(status_code=400, detail="Cannot edit root pages")
    
    persona = manager.personas.get(persona_id)
    adapter = getattr(persona, "sai_memory", None) if persona else None
    should_close = False
    
    if not adapter or not adapter.is_ready():
        from saiverse_memory import SAIMemoryAdapter
        try:
            adapter = SAIMemoryAdapter(persona_id)
            should_close = True
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to access memory: {e}")

    try:
        from sai_memory.memopedia import Memopedia
        memopedia = Memopedia(adapter.conn)
        updated = memopedia.update_page(
            page_id,
            title=request.title,
            summary=request.summary,
            content=request.content,
            keywords=request.keywords,
            edit_source="manual_ui",
        )
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
            }
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Memopedia error: {e}")
    finally:
        if should_close and adapter:
            adapter.close()

@router.delete("/{persona_id}/memopedia/pages/{page_id}")
def delete_memopedia_page(persona_id: str, page_id: str, manager = Depends(get_manager)):
    """Delete a Memopedia page (soft delete)."""
    # Prevent deleting root pages
    if page_id.startswith("root_"):
        raise HTTPException(status_code=400, detail="Cannot delete root pages")
    
    persona = manager.personas.get(persona_id)
    adapter = getattr(persona, "sai_memory", None) if persona else None
    should_close = False
    
    if not adapter or not adapter.is_ready():
        from saiverse_memory import SAIMemoryAdapter
        try:
            adapter = SAIMemoryAdapter(persona_id)
            should_close = True
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to access memory: {e}")

    try:
        from sai_memory.memopedia import Memopedia
        memopedia = Memopedia(adapter.conn)
        success = memopedia.delete_page(page_id, edit_source="manual_ui")
        if not success:
            raise HTTPException(status_code=404, detail="Page not found or could not be deleted")
        return {"success": True}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Memopedia error: {e}")
    finally:
        if should_close and adapter:
            adapter.close()
