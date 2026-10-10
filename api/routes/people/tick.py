"""ティックを手で一発打つ口 (自律行動 v0.4 計画 段 2)。

ティック = ライフ中に間隔で打たれる、ペルソナ本人の自分の時間の 1 Pulse
(docs/intent/autonomous_behavior_v3.md §5)。段 2 では間隔の運転を回さず、
器が正しく動くかを手で確かめるための口だけを置く。間隔での自動の打鍵は
段 3 (ティックスケジューラ)。

⚠️ この口は本人の標準モデルを呼び (課金が起きる)、本人の記憶 (SAIMemory
メインライン) に書く。本番のペルソナに打つのは、まはーがその操作を明示的に
承認したときだけ (CLAUDE.md「Production Persona Safety」)。検証は隔離環境の
合成ペルソナで行う。

同期で走る — 応答はティックが閉じてから返る (LLM 一呼び出し + スペルの実行ぶん)。
"""
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from api.deps import get_manager

LOGGER = logging.getLogger(__name__)

router = APIRouter()


class TickRequest(BaseModel):
    # このティックの割り当て (「このティックは◯◯」という確定情報の文)。
    # 省略・空なら自分のための時間である旨の既定文 (tick Playbook の default)。
    assignment: Optional[str] = None


class TickResponse(BaseModel):
    persona_id: str
    # True = Pulse が実行された (席が空いていて走った)。False = 席が埋まって
    # いて見送られた・実行の前に失敗した (詳細は backend.log の [tick] 行)。
    executed: bool


@router.post("/{persona_id}/tick", response_model=TickResponse)
def fire_tick(
    persona_id: str,
    body: Optional[TickRequest] = None,
    manager=Depends(get_manager),
) -> TickResponse:
    """ティックを一発打つ (``SAIVerseManager.fire_tick``)。"""
    assignment = body.assignment if body is not None else None
    try:
        result = manager.fire_tick(persona_id, assignment_text=assignment)
    except KeyError:
        raise HTTPException(
            status_code=404, detail=f"persona {persona_id} がロードされていません",
        )
    return TickResponse(persona_id=persona_id, executed=result is not None)
