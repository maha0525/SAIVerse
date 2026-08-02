"""経験の台帳 API — 索引と動的合成ページ (読み取り専用)。

experience_ledger.md §3 の UI 側入口。組み立ての本体は
``sai_memory/experience_ledger.py`` (memory.db 分・決定論)。

目的ノード (生きた Track / task) は main DB 側の実体なので、ここで
saiverse.judgment_points の供給関数 (head の一覧・判断 enum と同じ供給源)
から引き、統計だけ memory.db の purpose_tags (episode → 目的の帰属タグ) で
付けて索引に合流させる。目的ノードは memopedia ページではないため、
動的合成 (ページを開く) の対象は v1 では実体ページ / テーマページのみ。
"""
import logging
from typing import Any, Dict, List

from fastapi import APIRouter, Depends, HTTPException

from api.deps import get_manager
from sai_memory.experience_ledger import build_ledger_index, build_ledger_page
from sai_memory.memopedia.storage import CATEGORY_DEFS, category_label

from .utils import get_adapter

router = APIRouter()
LOGGER = logging.getLogger(__name__)


def _purpose_tag_stats(conn, purpose_ref: str) -> Dict[str, Any]:
    """purpose_tags から目的ノード 1 件の経験統計を引く (決定論)。

    record_count = この目的に帰属した出来事等 (target) の行数。
    first/last_date はタグ初回付与日 (created_at は初回付与を保持する仕様)。
    """
    row = conn.execute(
        """
        SELECT COUNT(*),
               MIN(date(created_at, 'unixepoch', 'localtime')),
               MAX(date(created_at, 'unixepoch', 'localtime'))
        FROM purpose_tags
        WHERE purpose_ref = ?
        """,
        (purpose_ref,),
    ).fetchone()
    return {
        "record_count": int(row[0] or 0),
        "first_date": row[1],
        "last_date": row[2],
    }


def _collect_purpose_rows(
    manager: Any, persona_id: str, conn
) -> List[Dict[str, Any]]:
    """生きた目的ノード (タスク / 欲求候補 / Track) の索引行。

    供給は判断 enum・head 一覧と同じ関数 (judgment_points) — 台帳だけ違う
    集合を見せない。main DB が引けない環境 (テストの薄い manager 等) では
    空リスト (索引の memory.db 分は独立して返る。フェイルオープン)。
    """
    rows: List[Dict[str, Any]] = []
    try:
        from saiverse.judgment_points import (
            list_backlog_tasks,
            list_desire_tasks,
            list_pickable_tracks,
        )

        for t in list_backlog_tasks(manager, persona_id):
            ref = t.get("task_ref")
            if ref:
                rows.append(
                    {
                        "ref": ref,
                        "title": t.get("title") or "(無題)",
                        "kind": "task",
                        "stats": _purpose_tag_stats(conn, ref),
                    }
                )
        for t in list_desire_tasks(manager, persona_id):
            ref = t.get("task_ref")
            if ref:
                rows.append(
                    {
                        "ref": ref,
                        "title": t.get("title") or "(無題)",
                        "kind": "desire",
                        "stats": _purpose_tag_stats(conn, ref),
                    }
                )
        for tr in list_pickable_tracks(manager, persona_id):
            ref = f"track:{tr.short_id}"
            rows.append(
                {
                    "ref": ref,
                    "title": getattr(tr, "title", None) or "(無題)",
                    "kind": "track",
                    "stats": _purpose_tag_stats(conn, ref),
                }
            )
    except Exception:
        LOGGER.warning(
            "[experience-ledger] failed to collect purpose nodes (persona=%s); "
            "returning memory.db index only",
            persona_id,
            exc_info=True,
        )
        return []
    return rows


@router.get("/{persona_id}/experience-ledger")
def get_experience_ledger_index(persona_id: str, manager=Depends(get_manager)):
    """台帳の索引 — カテゴリごとにグループ化した棚の一覧 (統計付き)。"""
    with get_adapter(persona_id, manager) as adapter:
        try:
            index_rows = build_ledger_index(adapter.conn)
            purposes = _collect_purpose_rows(manager, persona_id, adapter.conn)
        except Exception as e:
            raise HTTPException(
                status_code=500, detail=f"Experience ledger error: {e}"
            )

    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for row in index_rows:
        grouped.setdefault(row["category"], []).append(row)
    categories = [
        {
            "key": key,
            "label": category_label(key),
            "pages": grouped[key],
        }
        for key in sorted(
            grouped,
            key=lambda k: CATEGORY_DEFS[k].order if k in CATEGORY_DEFS else 99,
        )
    ]
    return {"categories": categories, "purposes": purposes}


@router.get("/{persona_id}/experience-ledger/{page_id}")
def get_experience_ledger_page(
    persona_id: str, page_id: str, manager=Depends(get_manager)
):
    """ページを開く = 動的合成 (fragment / 関与あらすじの履歴 / 共起ページ)。"""
    with get_adapter(persona_id, manager) as adapter:
        try:
            page = build_ledger_page(adapter.conn, page_id)
        except Exception as e:
            raise HTTPException(
                status_code=500, detail=f"Experience ledger error: {e}"
            )
    if page is None:
        raise HTTPException(status_code=404, detail=f"Page not found: {page_id}")
    return page
