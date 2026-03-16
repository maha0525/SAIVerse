"""Pulse logs API endpoints for viewing SEA runtime execution traces."""
from fastapi import APIRouter, Depends
from api.deps import get_manager
from .models import PulseSummaryItem, PulseListResponse, PulseLogEntry, PulseLogsResponse
from .utils import get_adapter

router = APIRouter()


@router.get("/{persona_id}/pulse-logs", response_model=PulseListResponse)
def list_pulses(
    persona_id: str,
    page: int = 1,
    page_size: int = 50,
    manager=Depends(get_manager),
):
    """List pulse_id summaries with pagination (newest first)."""
    with get_adapter(persona_id, manager) as adapter:
        total = adapter.count_pulses()
        if total == 0:
            return PulseListResponse(items=[], total=0, page=1, page_size=page_size)

        if page < 1:
            page = 1
        offset = (page - 1) * page_size
        summaries = adapter.list_pulse_summaries(limit=page_size, offset=offset)

        items = [
            PulseSummaryItem(
                pulse_id=s["pulse_id"],
                entry_count=s["entry_count"],
                latest_created_at=s["latest_created_at"],
                playbook_name=s["playbook_name"],
            )
            for s in summaries
        ]
        return PulseListResponse(
            items=items, total=total, page=page, page_size=page_size
        )


@router.get("/{persona_id}/pulse-logs/{pulse_id}", response_model=PulseLogsResponse)
def get_pulse_logs(
    persona_id: str,
    pulse_id: str,
    manager=Depends(get_manager),
):
    """Get all log entries for a specific pulse."""
    with get_adapter(persona_id, manager) as adapter:
        logs = adapter.get_pulse_logs(pulse_id)
        items = [PulseLogEntry(**log) for log in logs]
        return PulseLogsResponse(
            items=items, pulse_id=pulse_id, total=len(items)
        )
