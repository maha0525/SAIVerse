"""Tracks viewer API (Intent A v0.14, Intent B v0.11 — action_track 一覧表示).

Phase 0 で Track 機構が実運用に乗り始めたので、ペルソナの Track 状態を UI から
覗くデバッグ・検証用エンドポイント。検証目的に限り、最小限の状態遷移操作
(running/alert → pending) も提供する。
"""
import json
from collections import Counter
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from api.deps import get_manager
from database.models import ActionTrack
from saiverse.track_manager import (
    InvalidTrackStateError,
    PersistentTrackError,
    TrackNotFoundError,
)

from .models import TrackItem, TracksResponse, TracksStatusCount

router = APIRouter()


def _epoch(dt) -> Optional[float]:
    if dt is None:
        return None
    try:
        return dt.timestamp()
    except Exception:
        return None


def _parse_metadata(raw: Optional[str]) -> Optional[dict]:
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else None
    except (TypeError, ValueError):
        return None


def _to_item(
    row: ActionTrack,
    last_message_at: Optional[float] = None,
) -> TrackItem:
    return TrackItem(
        track_id=row.track_id,
        persona_id=row.persona_id,
        title=row.title,
        track_type=row.track_type,
        is_persistent=bool(row.is_persistent),
        output_target=row.output_target or "none",
        status=row.status,
        is_forgotten=bool(row.is_forgotten),
        intent=row.intent,
        track_metadata=_parse_metadata(row.track_metadata),
        last_active_at=_epoch(row.last_active_at),
        last_message_at=last_message_at,
        waiting_for=row.waiting_for,
        waiting_timeout_at=_epoch(row.waiting_timeout_at),
        created_at=_epoch(row.created_at),
        completed_at=_epoch(row.completed_at),
        aborted_at=_epoch(row.aborted_at),
    )


def _resolve_persona_memory(manager, persona_id: str):
    """Return the persona's SAIMemoryAdapter if loaded, else None.

    Returns None for personas that are not currently loaded (e.g. dispatched
    away, or referenced via a Track row that survived the persona being
    deleted) — the API still returns the Track list, just without the
    last_message_at field.
    """
    personas = getattr(manager, "personas", None) or {}
    persona = personas.get(persona_id)
    if persona is None:
        return None
    return getattr(persona, "sai_memory", None)


def _bulk_last_message_times(manager, persona_id: str, track_ids):
    adapter = _resolve_persona_memory(manager, persona_id)
    if adapter is None:
        return {}
    try:
        return adapter.get_track_last_message_times(track_ids)
    except Exception:
        # Adapter may exist but be in a degraded state. Returning empty falls
        # back to "no last_message_at known" rather than failing the whole
        # tracks listing.
        import logging
        logging.exception(
            "[tracks] failed to bulk-fetch last_message_times persona=%s",
            persona_id,
        )
        return {}


_VALID_STATUSES = {
    "running", "alert", "pending", "waiting",
    "unstarted", "completed", "aborted",
}


@router.get("/{persona_id}/tracks", response_model=TracksResponse)
def get_tracks(
    persona_id: str,
    status: Optional[str] = Query(
        None,
        description="ステータスでフィルタ (running/alert/pending/waiting/unstarted/completed/aborted)。"
                    "未指定なら全ステータス。",
    ),
    include_forgotten: bool = Query(
        False,
        description="True で is_forgotten=true の Track も含める。",
    ),
    manager=Depends(get_manager),
):
    """List ActionTracks for the persona, with status-count summary.

    Intent B 不変条件 1 (= running は同時 1 本のみ) を満たすかは UI 側でも
    確認できるよう、ステータス別件数を summary として返す。
    """
    if status is not None and status not in _VALID_STATUSES:
        raise HTTPException(status_code=400, detail=f"unknown status: {status}")

    db = manager.SessionLocal()
    try:
        # Total count (after the optional filter — the UI uses this to label the list)
        query = db.query(ActionTrack).filter(ActionTrack.persona_id == persona_id)
        if not include_forgotten:
            query = query.filter(ActionTrack.is_forgotten == False)  # noqa: E712
        if status is not None:
            query = query.filter(ActionTrack.status == status)
        rows = (
            query.order_by(ActionTrack.last_active_at.desc().nullslast())
            .all()
        )

        # Status breakdown (always over the unfiltered set so the UI can show
        # "5 running, 3 pending..." regardless of the current filter)
        breakdown_query = db.query(ActionTrack).filter(
            ActionTrack.persona_id == persona_id
        )
        if not include_forgotten:
            breakdown_query = breakdown_query.filter(
                ActionTrack.is_forgotten == False  # noqa: E712
            )
        breakdown_rows = breakdown_query.all()
        counter = Counter(r.status for r in breakdown_rows)

        last_message_map = _bulk_last_message_times(
            manager, persona_id, [r.track_id for r in rows]
        )
        items: List[TrackItem] = [
            _to_item(
                r,
                last_message_at=(
                    last_message_map[r.track_id].timestamp()
                    if r.track_id in last_message_map else None
                ),
            )
            for r in rows
        ]
        status_counts = [
            TracksStatusCount(status=s, count=counter.get(s, 0))
            for s in (
                "running", "alert", "pending", "waiting",
                "unstarted", "completed", "aborted",
            )
        ]

        return TracksResponse(
            items=items, total=len(items), status_counts=status_counts
        )
    finally:
        db.close()


@router.post("/{persona_id}/tracks/{track_id}/pause", response_model=TrackItem)
def pause_track(
    persona_id: str,
    track_id: str,
    manager=Depends(get_manager),
):
    """Pause a running/alert Track (→ pending).

    検証用: メタ判断 alert からの遷移を手動で再現するため、UI から running の
    対ユーザー Track を pending に戻す操作を提供する。Track が他ペルソナのもの
    だった場合は 404、状態遷移が許可されない場合は 409 を返す。
    """
    track_manager = getattr(manager, "track_manager", None)
    if track_manager is None:
        raise HTTPException(status_code=503, detail="track_manager not initialized")
    try:
        existing = track_manager.get(track_id)
    except TrackNotFoundError:
        raise HTTPException(status_code=404, detail=f"track not found: {track_id}")
    if existing.persona_id != persona_id:
        raise HTTPException(
            status_code=404,
            detail=f"track {track_id} does not belong to persona {persona_id}",
        )
    try:
        track = track_manager.pause(track_id)
    except TrackNotFoundError:
        raise HTTPException(status_code=404, detail=f"track not found: {track_id}")
    except InvalidTrackStateError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except PersistentTrackError as e:
        raise HTTPException(status_code=409, detail=str(e))
    adapter = _resolve_persona_memory(manager, persona_id)
    last_at: Optional[float] = None
    if adapter is not None:
        try:
            dt = adapter.get_track_last_message_time(track_id)
            if dt is not None:
                last_at = dt.timestamp()
        except Exception:
            pass
    return _to_item(track, last_message_at=last_at)
