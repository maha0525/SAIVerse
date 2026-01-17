from fastapi import APIRouter, Depends, HTTPException
from api.deps import get_manager
from .models import MemoryRecallRequest, MemoryRecallResponse
from .utils import get_adapter

router = APIRouter()

@router.post("/{persona_id}/recall", response_model=MemoryRecallResponse)
def memory_recall(
    persona_id: str,
    request: MemoryRecallRequest,
    manager = Depends(get_manager)
):
    """Execute memory recall, similar to the memory_recall tool."""
    query = request.query.strip()
    if not query:
        raise HTTPException(status_code=400, detail="Query is required")
    
    with get_adapter(persona_id, manager) as adapter:
        try:
            result = adapter.recall_snippet(
                None,
                query_text=query,
                max_chars=request.max_chars,
                topk=request.topk,
            )
            return MemoryRecallResponse(
                query=query,
                result=result or "(no relevant memory)",
                topk=request.topk,
                max_chars=request.max_chars,
            )
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Memory recall failed: {e}")
