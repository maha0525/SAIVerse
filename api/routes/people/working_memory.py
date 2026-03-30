"""Working memory API endpoints for viewing and managing recalled memories."""
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from api.deps import get_manager
from .utils import get_adapter

router = APIRouter()


class RecalledIdItem(BaseModel):
    type: str
    id: str
    title: str
    uri: str
    recalled_at: Optional[float] = None


class WorkingMemoryResponse(BaseModel):
    recalled_ids: List[RecalledIdItem]
    max_capacity: int


class AddRecalledIdRequest(BaseModel):
    source_type: str
    source_id: str
    title: str
    uri: str


class RemoveResponse(BaseModel):
    removed: bool


class ClearResponse(BaseModel):
    cleared_count: int


@router.get("/{persona_id}/working-memory", response_model=WorkingMemoryResponse)
def get_working_memory(
    persona_id: str,
    manager=Depends(get_manager),
):
    """Get current working memory recalled IDs."""
    with get_adapter(persona_id, manager) as adapter:
        ids = adapter.get_recalled_ids()
        return WorkingMemoryResponse(
            recalled_ids=[RecalledIdItem(**item) for item in ids],
            max_capacity=adapter.RECALLED_IDS_MAX,
        )


@router.post("/{persona_id}/working-memory/recall", response_model=RecalledIdItem)
def add_recalled_id(
    persona_id: str,
    request: AddRecalledIdRequest,
    manager=Depends(get_manager),
):
    """Add a recalled ID to working memory."""
    with get_adapter(persona_id, manager) as adapter:
        adapter.add_recalled_id(
            source_type=request.source_type,
            source_id=request.source_id,
            title=request.title,
            uri=request.uri,
        )
        # Return the added item
        ids = adapter.get_recalled_ids()
        for item in ids:
            if item.get("id") == request.source_id:
                return RecalledIdItem(**item)
        raise HTTPException(status_code=500, detail="Failed to add recalled ID")


@router.delete("/{persona_id}/working-memory/recall/{source_id}", response_model=RemoveResponse)
def remove_recalled_id(
    persona_id: str,
    source_id: str,
    manager=Depends(get_manager),
):
    """Remove a specific recalled ID from working memory."""
    with get_adapter(persona_id, manager) as adapter:
        removed = adapter.remove_recalled_id(source_id)
        return RemoveResponse(removed=removed)


@router.delete("/{persona_id}/working-memory/recall", response_model=ClearResponse)
def clear_recalled_ids(
    persona_id: str,
    manager=Depends(get_manager),
):
    """Clear all recalled IDs from working memory."""
    with get_adapter(persona_id, manager) as adapter:
        count = adapter.clear_recalled_ids()
        return ClearResponse(cleared_count=count)
