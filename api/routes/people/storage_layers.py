"""Storage layers viewer API (Intent A v0.14, Intent B v0.11 — 7-layer storage).

Phase 0 で導入した 7 層ストレージモデルの中身を UI から覗くためのデバッグ用
エンドポイント。各層のメッセージ / イベント / 判断ログを統一フォーマット
(StorageLayerEntry) に正規化して返す。

層ごとのデータソース対応:
- [1] meta_judgment    → meta_judgment_log テーブル (saiverse.db)
- [2] main_cache       → SAIMemory messages (line_role='main_line', scope='committed')
- [3] sub_cache        → SAIMemory messages (line_role='sub_line',  scope='committed')
- [4] nested_temp      → 揮発 (DB 保存なし) — 件数 0、note を返す
- [5] track_local      → track_local_log テーブル (persona の Track に紐づくもの)
- [6] saimemory_core   → 既存 browser タブ参照 — 件数のみ算出
- [7] archive          → 未実装 — note のみ返す
"""
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from api.deps import get_manager
from database.models import ActionTrack, MetaJudgmentLog, TrackLocalLog

from .models import (
    StorageLayerEntry,
    StorageLayerStat,
    StorageLayersResponse,
)
from .utils import get_adapter

router = APIRouter()


_LAYER_LABELS = {
    "meta_judgment":  ("[1] メタ判断ログ", 1),
    "main_cache":     ("[2] メインキャッシュ", 2),
    "sub_cache":      ("[3] Track 内サブキャッシュ群", 3),
    "nested_temp":    ("[4] 入れ子一時コンテキスト", 4),
    "track_local":    ("[5] Track ローカルログ", 5),
    "saimemory_core": ("[6] SAIMemory (会話の核)", 6),
    "archive":        ("[7] アーカイブ", 7),
}


def _epoch(dt) -> Optional[float]:
    """Convert a naive datetime to unix epoch seconds for the JSON response."""
    if dt is None:
        return None
    try:
        return dt.timestamp()
    except Exception:
        return None


def _persona_thread_prefix(persona_id: str) -> str:
    """SAIMemory thread_id is keyed by persona_id (e.g., 'air_city_a:__persona__')."""
    return f"{persona_id}:"


def _fetch_messages_layer(
    adapter,
    persona_id: str,
    line_role: str,
    track_id: Optional[str],
    limit: int,
) -> List[StorageLayerEntry]:
    """Pull SAIMemory messages tagged with the given line_role.

    Excludes scope='discardable' to match the runtime's view (Phase 0 P0-7).
    Per-track filtering is optional — if the caller provides ``track_id``, only
    messages whose ``origin_track_id`` matches are returned.
    """
    query = (
        "SELECT id, role, content, created_at, scope, line_role, line_id, "
        "origin_track_id, paired_action_text "
        "FROM messages WHERE thread_id LIKE ? "
        "AND line_role = ? AND (scope IS NULL OR scope != 'discardable')"
    )
    params = [_persona_thread_prefix(persona_id) + "%", line_role]
    if track_id:
        query += " AND origin_track_id = ?"
        params.append(track_id)
    query += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)

    layer = "main_cache" if line_role == "main_line" else "sub_cache"
    with adapter._db_lock:
        cur = adapter.conn.execute(query, params)
        rows = cur.fetchall()

    items: List[StorageLayerEntry] = []
    for r in rows:
        items.append(
            StorageLayerEntry(
                layer=layer,
                entry_id=r[0],
                role=r[1],
                content=r[2] or "",
                created_at=float(r[3]) if r[3] is not None else None,
                scope=r[4],
                line_role=r[5],
                line_id=r[6],
                origin_track_id=r[7],
                paired_action_text=r[8],
            )
        )
    return items


def _count_messages_layer(
    adapter, persona_id: str, line_role: str
) -> tuple[int, Optional[float]]:
    """Return (count, latest_created_at_epoch) for one line_role layer."""
    query = (
        "SELECT COUNT(*), MAX(created_at) FROM messages "
        "WHERE thread_id LIKE ? AND line_role = ? "
        "AND (scope IS NULL OR scope != 'discardable')"
    )
    with adapter._db_lock:
        cur = adapter.conn.execute(
            query, (_persona_thread_prefix(persona_id) + "%", line_role)
        )
        count, latest = cur.fetchone()
    return int(count or 0), float(latest) if latest is not None else None


def _count_saimemory_core(adapter, persona_id: str) -> tuple[int, Optional[float]]:
    """Total message count for the persona, regardless of line_role.

    Layer [6] (SAIMemory 会話の核) corresponds to all stored messages — the
    existing browser tab is the canonical viewer, so we just surface the count
    here and direct the user to switch tabs.
    """
    query = (
        "SELECT COUNT(*), MAX(created_at) FROM messages "
        "WHERE thread_id LIKE ? "
        "AND (scope IS NULL OR scope != 'discardable')"
    )
    with adapter._db_lock:
        cur = adapter.conn.execute(
            query, (_persona_thread_prefix(persona_id) + "%",)
        )
        count, latest = cur.fetchone()
    return int(count or 0), float(latest) if latest is not None else None


def _fetch_meta_judgment(
    db_session,
    persona_id: str,
    limit: int,
) -> tuple[List[StorageLayerEntry], int, Optional[float]]:
    """Read meta_judgment_log entries newest-first."""
    rows = (
        db_session.query(MetaJudgmentLog)
        .filter(MetaJudgmentLog.persona_id == persona_id)
        .order_by(MetaJudgmentLog.judged_at.desc())
        .limit(limit)
        .all()
    )
    count = (
        db_session.query(MetaJudgmentLog)
        .filter(MetaJudgmentLog.persona_id == persona_id)
        .count()
    )
    latest_dt = (
        db_session.query(MetaJudgmentLog.judged_at)
        .filter(MetaJudgmentLog.persona_id == persona_id)
        .order_by(MetaJudgmentLog.judged_at.desc())
        .first()
    )
    latest = _epoch(latest_dt[0]) if latest_dt else None

    items = [
        StorageLayerEntry(
            layer="meta_judgment",
            entry_id=row.judgment_id,
            created_at=_epoch(row.judged_at),
            judgment_action=row.judgment_action,
            judgment_thought=row.judgment_thought,
            switch_to_track_id=row.switch_to_track_id,
            trigger_type=row.trigger_type,
            trigger_context=row.trigger_context,
            notify_to_track=row.notify_to_track,
            committed_to_main_cache=row.committed_to_main_cache,
            track_at_judgment_id=row.track_at_judgment_id,
        )
        for row in rows
    ]
    return items, count, latest


def _fetch_track_local(
    db_session,
    persona_id: str,
    track_id: Optional[str],
    limit: int,
) -> tuple[List[StorageLayerEntry], int, Optional[float]]:
    """Read track_local_log entries scoped to the persona's Tracks."""
    persona_track_ids = [
        t.track_id
        for t in db_session.query(ActionTrack.track_id)
        .filter(ActionTrack.persona_id == persona_id)
        .all()
    ]
    if not persona_track_ids:
        return [], 0, None

    base_q = db_session.query(TrackLocalLog).filter(
        TrackLocalLog.track_id.in_(persona_track_ids)
    )
    if track_id:
        base_q = base_q.filter(TrackLocalLog.track_id == track_id)

    rows = base_q.order_by(TrackLocalLog.occurred_at.desc()).limit(limit).all()
    count = base_q.count()
    latest_dt = (
        base_q.order_by(TrackLocalLog.occurred_at.desc())
        .with_entities(TrackLocalLog.occurred_at)
        .first()
    )
    latest = _epoch(latest_dt[0]) if latest_dt else None

    items = [
        StorageLayerEntry(
            layer="track_local",
            entry_id=row.log_id,
            created_at=_epoch(row.occurred_at),
            log_kind=row.log_kind,
            payload=row.payload,
            source_line_id=row.source_line_id,
            track_id=row.track_id,
        )
        for row in rows
    ]
    return items, count, latest


@router.get("/{persona_id}/storage-layers", response_model=StorageLayersResponse)
def get_storage_layers(
    persona_id: str,
    layer: Optional[str] = Query(
        None,
        description="絞り込む層 (meta_judgment / main_cache / sub_cache / track_local)。"
                    "未指定なら全層から取得して時系列で混合する",
    ),
    scope: Optional[str] = Query(
        None,
        description="messages 系の scope フィルタ (committed / discardable / volatile)。"
                    "メタ判断ログ / track_local には適用されない",
    ),
    track_id: Optional[str] = Query(
        None,
        description="messages 系の origin_track_id または track_local の track_id でフィルタ",
    ),
    limit: int = Query(50, ge=1, le=500, description="取得件数上限 (層ごとに適用)"),
    manager=Depends(get_manager),
):
    """Return a unified view of the 7-layer storage for one persona.

    Always returns the per-layer summary stats; ``items`` contains a
    chronologically-mixed list of entries from the requested layers (or all
    layers when ``layer`` is omitted). Layers without a DB-backed source
    ([4] nested_temp / [7] archive) only contribute to the summary.
    """
    if layer is not None and layer not in _LAYER_LABELS:
        raise HTTPException(status_code=400, detail=f"unknown layer: {layer}")

    summary: List[StorageLayerStat] = []
    items: List[StorageLayerEntry] = []
    truncated = False

    db_session = manager.SessionLocal()
    try:
        with get_adapter(persona_id, manager) as adapter:
            # --- [1] meta_judgment ---
            mj_items, mj_count, mj_latest = _fetch_meta_judgment(
                db_session, persona_id, limit
            )
            summary.append(
                StorageLayerStat(
                    layer="meta_judgment",
                    layer_index=1,
                    label=_LAYER_LABELS["meta_judgment"][0],
                    count=mj_count,
                    latest_at=mj_latest,
                )
            )
            if layer in (None, "meta_judgment"):
                items.extend(mj_items)
                if mj_count > limit:
                    truncated = True

            # --- [2] main_cache ---
            mc_count, mc_latest = _count_messages_layer(
                adapter, persona_id, "main_line"
            )
            summary.append(
                StorageLayerStat(
                    layer="main_cache",
                    layer_index=2,
                    label=_LAYER_LABELS["main_cache"][0],
                    count=mc_count,
                    latest_at=mc_latest,
                )
            )
            if layer in (None, "main_cache"):
                main_items = _fetch_messages_layer(
                    adapter, persona_id, "main_line", track_id, limit
                )
                if scope:
                    main_items = [m for m in main_items if (m.scope or "committed") == scope]
                items.extend(main_items)
                if mc_count > limit:
                    truncated = True

            # --- [3] sub_cache ---
            sc_count, sc_latest = _count_messages_layer(
                adapter, persona_id, "sub_line"
            )
            summary.append(
                StorageLayerStat(
                    layer="sub_cache",
                    layer_index=3,
                    label=_LAYER_LABELS["sub_cache"][0],
                    count=sc_count,
                    latest_at=sc_latest,
                )
            )
            if layer in (None, "sub_cache"):
                sub_items = _fetch_messages_layer(
                    adapter, persona_id, "sub_line", track_id, limit
                )
                if scope:
                    sub_items = [m for m in sub_items if (m.scope or "committed") == scope]
                items.extend(sub_items)
                if sc_count > limit:
                    truncated = True

            # --- [4] nested_temp (volatile, no DB) ---
            summary.append(
                StorageLayerStat(
                    layer="nested_temp",
                    layer_index=4,
                    label=_LAYER_LABELS["nested_temp"][0],
                    count=0,
                    latest_at=None,
                    note="揮発 (Pulse 内のみ存在、DB 保存なし)",
                )
            )

            # --- [5] track_local ---
            tl_items, tl_count, tl_latest = _fetch_track_local(
                db_session, persona_id, track_id, limit
            )
            summary.append(
                StorageLayerStat(
                    layer="track_local",
                    layer_index=5,
                    label=_LAYER_LABELS["track_local"][0],
                    count=tl_count,
                    latest_at=tl_latest,
                )
            )
            if layer in (None, "track_local"):
                items.extend(tl_items)
                if tl_count > limit:
                    truncated = True

            # --- [6] saimemory_core (count only — refer to browser tab for content) ---
            core_count, core_latest = _count_saimemory_core(adapter, persona_id)
            summary.append(
                StorageLayerStat(
                    layer="saimemory_core",
                    layer_index=6,
                    label=_LAYER_LABELS["saimemory_core"][0],
                    count=core_count,
                    latest_at=core_latest,
                    note="個別の閲覧は browser タブを使ってください",
                )
            )

            # --- [7] archive (not implemented) ---
            summary.append(
                StorageLayerStat(
                    layer="archive",
                    layer_index=7,
                    label=_LAYER_LABELS["archive"][0],
                    count=0,
                    latest_at=None,
                    note="未実装 (worker 系結果の保管庫予定)",
                )
            )

        # Chronological sort across layers (newest first)
        items.sort(key=lambda e: e.created_at or 0.0, reverse=True)

        return StorageLayersResponse(
            summary=summary,
            items=items,
            total_returned=len(items),
            truncated=truncated,
        )
    finally:
        db_session.close()
