"""ペルソナの暮らしビュー API (life_concept_map.md の読み出し面)。

画面 C (会話バブルの点クリップハイライト) 用の読み取り専用エンドポイント。
決定論 (SELECT + 整形のみ) で LLM は呼ばない。

- GET /{persona_id}/clips : メッセージに付いた観測点＝点クリップ (sai_memory/clips.py)

退役した仲間:

- 画面 D (プロフィール) の ``GET /{persona_id}/profile-tree`` — 2026-08-21。
  目的の木が概念ごと消えたため (autonomous_behavior_v3.md §9-5)。
- 画面 B (今日の予定表) の ``GET /{persona_id}/day-plan`` — 2026-08-22 (束 6c)。
  唯一の読み手だったライフビュー・できごと UI を v0.3 で隠したため
  (v3 §11「運転 UI は隠す」)。時間割そのものは v0.4 でルーチンへ世代交代する。
"""
import logging
from typing import Any, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from api.deps import get_manager

from .utils import get_adapter

LOGGER = logging.getLogger(__name__)

router = APIRouter()

#: /clips の message_ids 一括指定の上限 (チャット 1 画面分を想定)
CLIPS_BATCH_LIMIT = 100


def _require_persona(manager: Any, persona_id: str) -> None:
    """AI 行の存在確認。無ければ 404 (未知ペルソナで空応答を返さないため)。"""
    from database.models import AI as AIModel

    db = manager.SessionLocal()
    try:
        exists = (
            db.query(AIModel.AIID).filter(AIModel.AIID == persona_id).first()
        )
    finally:
        db.close()
    if exists is None:
        raise HTTPException(status_code=404, detail=f"Persona {persona_id} not found")


# ---------------------------------------------------------------------------
# C: 点クリップ (観測点) のバッチ取得
# ---------------------------------------------------------------------------


class ClipItem(BaseModel):
    clip_id: str                   # フロントの key 用
    message_id: str                 # SAIMemory (memory.db) の message id
    quote: str                      # 本文からの逐語引用 (ハイライト対象)
    purpose_ref: Optional[str]      # 目的ノード参照 ("task:N" 等)。素の予約は None
    created_at: int                 # epoch 秒


class ClipsResponse(BaseModel):
    persona_id: str
    clips: List[ClipItem]         # message_id 昇順ではなく created_at 昇順 (切り出された順)


@router.get("/{persona_id}/clips", response_model=ClipsResponse)
def list_message_clips(
    persona_id: str,
    message_ids: str,
    manager=Depends(get_manager),
) -> ClipsResponse:
    """メッセージ群に付いた観測点 (点クリップ) をバッチで返す (画面 C: ハイライト)。

    ``message_ids`` はカンマ区切りの SAIMemory message id (上限
    :data:`CLIPS_BATCH_LIMIT`)。**建物履歴 (building_messages) の message_id
    とは別体系** — memory.db の messages.id を渡すこと (記憶ブラウズ API
    ``GET /{persona_id}/threads/{thread_id}/messages`` が返す ``id`` と同じ体系)。

    memory.db へのアクセスは記憶ブラウズ系と同じ ``get_adapter`` 経由
    (adapter.conn + adapter._db_lock)。clips テーブルは adapter 初期化時に
    冪等作成されるため、観測点ゼロのペルソナでも空リストで正しく返る。
    """
    _require_persona(manager, persona_id)
    ids: List[str] = []
    seen = set()
    for raw in message_ids.split(","):
        mid = raw.strip()
        if mid and mid not in seen:
            seen.add(mid)
            ids.append(mid)
    if not ids:
        raise HTTPException(status_code=400, detail="message_ids is empty")
    if len(ids) > CLIPS_BATCH_LIMIT:
        raise HTTPException(
            status_code=400,
            detail=f"too many message_ids: {len(ids)} (max {CLIPS_BATCH_LIMIT})",
        )

    from sai_memory.clips import list_clips

    items: List[ClipItem] = []
    with get_adapter(persona_id, manager) as adapter:
        with adapter._db_lock:
            for mid in ids:
                for clip in list_clips(adapter.conn, message_id=mid):
                    if not clip.quote:
                        continue  # ハイライトは引用アンカーを持つ点クリップのみ対象
                    items.append(ClipItem(
                        clip_id=clip.clip_id,
                        message_id=clip.message_id,
                        quote=clip.quote,
                        purpose_ref=clip.purpose_ref,
                        created_at=clip.created_at,
                    ))
    items.sort(key=lambda p: (p.created_at, p.clip_id))
    return ClipsResponse(persona_id=persona_id, clips=items)

