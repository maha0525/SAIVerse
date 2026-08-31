"""Memory notes API endpoints for viewing and managing knowledge memos."""
from typing import List, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from api.deps import get_manager
from .utils import get_adapter

router = APIRouter()


class MemoryNoteItem(BaseModel):
    id: str
    thread_id: str
    content: str
    source_pulse_id: Optional[str] = None
    source_time: Optional[int] = None
    resolved: bool
    created_at: int
    group_label: Optional[str] = None
    action: Optional[str] = None
    target_page_id: Optional[str] = None
    suggested_title: Optional[str] = None
    target_category: Optional[str] = None


class MemoryNotesResponse(BaseModel):
    items: List[MemoryNoteItem]
    total_unresolved: int


class ResolveRequest(BaseModel):
    note_ids: List[str]


class ResolveResponse(BaseModel):
    resolved_count: int


@router.get("/{persona_id}/memory-notes", response_model=MemoryNotesResponse)
def list_memory_notes(
    persona_id: str,
    limit: int = 100,
    manager=Depends(get_manager),
):
    """List unresolved memory notes."""
    with get_adapter(persona_id, manager) as adapter:
        notes = adapter.get_unresolved_notes(limit=limit)
        total = adapter.count_unresolved_notes()
        return MemoryNotesResponse(
            items=[
                MemoryNoteItem(
                    id=n.id,
                    thread_id=n.thread_id,
                    content=n.content,
                    source_pulse_id=n.source_pulse_id,
                    source_time=n.source_time,
                    resolved=n.resolved,
                    created_at=n.created_at,
                    group_label=n.group_label,
                    action=n.action,
                    target_page_id=n.target_page_id,
                    suggested_title=n.suggested_title,
                    target_category=n.target_category,
                )
                for n in notes
            ],
            total_unresolved=total,
        )


@router.post("/{persona_id}/memory-notes/resolve", response_model=ResolveResponse)
def resolve_notes(
    persona_id: str,
    request: ResolveRequest,
    manager=Depends(get_manager),
):
    """Mark memory notes as resolved."""
    with get_adapter(persona_id, manager) as adapter:
        count = adapter.resolve_memory_notes(request.note_ids)
        return ResolveResponse(resolved_count=count)
