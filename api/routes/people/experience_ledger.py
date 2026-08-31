"""経験の台帳 API — 索引と動的合成ページ (読み取り専用)。

experience_ledger.md §3 の UI 側入口。組み立ての本体は
``sai_memory/experience_ledger.py`` (memory.db 分・決定論)。

目的ノード (生きた Track / task) は main DB 側の実体なので、ここで
saiverse.judgment_points の供給関数 (head の一覧・判断 enum と同じ供給源)
から引き、統計だけ memory.db の purpose_tags (episode → 目的の帰属タグ) で
付けて索引に合流させる。目的ノードは memopedia ページではないため、
動的合成 (ページを開く) の対象は v1 では実体ページ / テーマページのみ。

memory.db (adapter.conn) の読み取りは live adapter の書き込みと並走しうる
ため、他の people ルート (activity / core_memory) と同じく ``adapter._db_lock``
の下で行う (Codex 一巡目 #7 — 慣行逸脱の追従)。
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

_ZERO_STATS = {"record_count": 0, "first_date": None, "last_date": None}


def _purpose_tag_stats_bulk(conn) -> Dict[str, Dict[str, Any]]:
    """purpose_tags 全体を一度の GROUP BY で集計する (決定論)。

    目的ノード数に比例した個別クエリを発行しない (Codex 一巡目 #7)。
    record_count = その目的に帰属した出来事等 (target) の行数。
    first/last_date はタグ初回付与日 (created_at は初回付与を保持する仕様)。
    """
    rows = conn.execute(
        """
        SELECT purpose_ref,
               COUNT(*),
               MIN(date(created_at, 'unixepoch', 'localtime')),
               MAX(date(created_at, 'unixepoch', 'localtime'))
        FROM purpose_tags
        GROUP BY purpose_ref
        """
    ).fetchall()
    return {
        str(r[0]): {
            "record_count": int(r[1] or 0),
            "first_date": r[2],
            "last_date": r[3],
        }
        for r in rows
    }


def _collect_purpose_rows(
    manager: Any, persona_id: str, stats_by_ref: Dict[str, Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """生きたタスクの索引行。

    供給は判断 enum と同じ関数 (judgment_points) — 台帳だけ違う集合を見せない。
    旧 kind の ``desire`` (欲求候補) と ``track`` (関心) は、欲求プールと Track
    操作の退役で供給源ごと消えた (autonomous_behavior_v3.md §8 /
    track_retirement.md §7.2 ④群)。main DB が引けない環境 (テストの薄い manager
    等) では空リスト (索引の memory.db 分は独立して返る。フェイルオープン)。
    統計は集計済みの ``stats_by_ref`` から引くだけ (ここでは memory.db に
    触らない — ロック区間を短く保つ)。
    """
    rows: List[Dict[str, Any]] = []
    try:
        from saiverse.judgment_points import list_backlog_tasks

        for t in list_backlog_tasks(manager, persona_id):
            ref = t.get("task_ref")
            if ref:
                rows.append(
                    {
                        "ref": ref,
                        "title": t.get("title") or "(無題)",
                        "kind": "task",
                        "stats": stats_by_ref.get(ref, dict(_ZERO_STATS)),
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
            with adapter._db_lock:
                index_rows = build_ledger_index(adapter.conn)
                stats_by_ref = _purpose_tag_stats_bulk(adapter.conn)
        except Exception as e:
            raise HTTPException(
                status_code=500, detail=f"Experience ledger error: {e}"
            )

    # 目的ノードの列挙は main DB 側 — memory.db のロックを持たずに行う
    purposes = _collect_purpose_rows(manager, persona_id, stats_by_ref)

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
            with adapter._db_lock:
                page = build_ledger_page(adapter.conn, page_id)
        except Exception as e:
            raise HTTPException(
                status_code=500, detail=f"Experience ledger error: {e}"
            )
    if page is None:
        raise HTTPException(status_code=404, detail=f"Page not found: {page_id}")
    return page
