"""経験の台帳 API — 索引と動的合成ページ (読み取り専用)。

experience_ledger.md §3 の UI 側入口。組み立ての本体は
``sai_memory/experience_ledger.py`` (memory.db 分・決定論)。

索引に合流させていた目的ノード (目的の木の task) の一覧は、目的の木の読み手を
撤去した v0.4 段 1-4 で消えた (応答の ``purposes`` 欄ごと。画面側は欄が無い
ときに空として扱う)。

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


@router.get("/{persona_id}/experience-ledger")
def get_experience_ledger_index(persona_id: str, manager=Depends(get_manager)):
    """台帳の索引 — カテゴリごとにグループ化した棚の一覧 (統計付き)。"""
    with get_adapter(persona_id, manager) as adapter:
        try:
            with adapter._db_lock:
                index_rows = build_ledger_index(adapter.conn)
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
    return {"categories": categories}


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
