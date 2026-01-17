from fastapi import APIRouter, Depends, HTTPException
from api.deps import get_manager
from .models import (
    UpdateMemopediaPageRequest,
    CreateMemopediaPageRequest,
    SetTrunkRequest,
    MovePagesToTrunkRequest,
)
from .utils import get_adapter

router = APIRouter()


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
