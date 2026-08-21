"""ペルソナの暮らしビュー API (life_concept_map.md の読み出し面)。

画面 B (今日の予定表) / C (会話バブルの点クリップハイライト) 用の読み取り専用
エンドポイント群。全て決定論 (SELECT + 整形のみ) で LLM は呼ばない。

- GET /{persona_id}/day-plan      : 時間割のコマ一覧 (saiverse/day_plan.py)
- GET /{persona_id}/clips        : メッセージに付いた観測点＝点クリップ (sai_memory/clips.py)

画面 D (プロフィール) の ``GET /{persona_id}/profile-tree`` は 2026-08-21 に
退役した — 目的の木そのものが概念ごと消えたため
(autonomous_behavior_v3.md §9-5)。フロント側 (PersonaProfileModal) の追従は
別便。
"""
import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from api.deps import get_manager
from saiverse import clock
from saiverse.day_plan import (
    get_budget_state,
    get_life_status_now,
    get_lives,
    life_consumed,
    load_day_plan,
    slot_result_label,
)

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
# B: 今日の予定表 (時間割)
# ---------------------------------------------------------------------------


class DayPlanSlot(BaseModel):
    """時間割のコマ 1 つ (persona_day_plan.slots_json の 1 要素 + 表示ラベル)。"""
    index: int                      # slots_json 内の位置 (コマの安定キー)
    start: str                      # "HH:MM"
    kind: str                       # 六型 ("話す"…) / "暮らし" / "休む"
    ref: str                        # "task:N" / "desire:N" / "none"
    facility: str                   # building_id or "own_room"
    title: str                      # 「○○をする」の短い表題 (旧データは "")
    note: str
    status: str                     # pending / fired / deferred / skipped / done
    skip_reason: Optional[str]      # skipped の理由 (無ければ None)
    record_level: Optional[str]     # 'presence_only' = 詳細な実行記録なし
    budget_rounds: int
    defer_count: int
    result_label: str               # 実績の表示文字列 (day_plan.slot_result_label)


class DayPlanBudget(BaseModel):
    """日次の作業ラウンド予算 (v2 §4.5)。台帳の無い日は day-plan 応答で None。

    ライフ宣言がある日の消費は used_pulses + used_rounds × κ (day_plan.
    life_consumed) で小数になるため used / remaining は float。int のままだと
    Pydantic の応答検証が 0.4 → int 変換を拒否して 500 になる (2026-07-18 実バグ:
    ライフビューの「今日の予定」が air だけ取得失敗)。
    """
    total: int
    used: float
    remaining: float


class LifeItem(BaseModel):
    """ライフ 1 件 (life.md v0.5 §4.1) の表示用整形。ライフビューの帯描画に使う。"""
    index: int                      # get_lives の並び順 (安定キー)
    start: str                      # "HH:MM"
    end: str                        # "HH:MM"
    mode: str                       # "even" (均等) / "free" (自由)
    budget_pulses: int
    used_pulses: int
    used_rounds: int
    consumed: float                 # used_pulses + used_rounds × κ (life_consumed)
    remaining: float                # budget_pulses − consumed (0 未満には丸めない)
    # 判断点 (起床・会話終了・セッション終了・イベント・就寝) の発火回数。
    # 予算 (budget_pulses/used_pulses) には含めない別枠の観測値 (life.md v0.5
    # §5.3/§8.2)。改修B でライフビューの別枠表示に使う。
    judgment_pulses: int = 0


class LifeStatus(BaseModel):
    """「いま」のライフ状態 — life.md §9.1 試金石の判定結果。

    ``date`` クエリが今日 (営業日) 以外を指しているときは day-plan 応答に
    含めない (None) — 過去日を眺めているのに「いま話しかけやすい」を
    出すのは嘘になるため。
    """
    lives_declared: bool
    in_life: bool
    life_index: Optional[int] = None


class DayPlanResponse(BaseModel):
    persona_id: str
    date: str                       # "YYYY-MM-DD"
    slots: List[DayPlanSlot]        # plan の無い日は空配列
    budget: Optional[DayPlanBudget]
    lives: List[LifeItem] = []      # 宣言が無い日は空配列 (life.md §9.2 帯描画用)
    life_status: Optional[LifeStatus] = None


@router.get("/{persona_id}/day-plan", response_model=DayPlanResponse)
def get_day_plan(
    persona_id: str,
    date: Optional[str] = None,
    manager=Depends(get_manager),
) -> DayPlanResponse:
    """ペルソナの時間割 (画面 B: 今日の予定表)。plan を持たない日は空配列。

    ``date`` 省略時は今日 (仮想クロック尊重のため ``saiverse.clock.now()``)。
    時間割はペルソナ自身が起床判断で組む成果物 (§15 層②) であり、本 API は
    読み取りのみ — 組み替えはペルソナの判断点だけが行う。
    """
    _require_persona(manager, persona_id)
    plan_date = date if date is not None else clock.now().date().isoformat()
    try:
        slots = load_day_plan(manager, persona_id, plan_date)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    slot_items = [
        DayPlanSlot(
            index=i,
            start=str(slot.get("start") or ""),
            kind=str(slot.get("kind") or ""),
            ref=str(slot.get("ref") or "none"),
            facility=str(slot.get("facility") or ""),
            title=str(slot.get("title") or ""),
            note=str(slot.get("note") or ""),
            status=str(slot.get("status") or "pending"),
            skip_reason=slot.get("skip_reason") or None,
            record_level=slot.get("record_level") or None,
            budget_rounds=int(slot.get("budget_rounds") or 0),
            defer_count=int(slot.get("defer_count") or 0),
            result_label=slot_result_label(slot),
        )
        for i, slot in enumerate(slots or [])
    ]
    budget = get_budget_state(manager, persona_id, plan_date)

    # ライフ (life.md §9.2 帯描画): 宣言が無い日は空配列 (従来表示のまま)
    lives_raw = get_lives(manager, persona_id, plan_date)
    life_items = [
        LifeItem(
            index=i,
            start=str(life.get("start") or ""),
            end=str(life.get("end") or ""),
            mode=str(life.get("mode") or ""),
            budget_pulses=int(life.get("budget_pulses") or 0),
            used_pulses=int(life.get("used_pulses") or 0),
            used_rounds=int(life.get("used_rounds") or 0),
            consumed=life_consumed(life),
            remaining=max(0.0, int(life.get("budget_pulses") or 0) - life_consumed(life)),
            judgment_pulses=int(life.get("judgment_pulses") or 0),
        )
        for i, life in enumerate(lives_raw)
    ]

    # 「いま」のライフ状態 (§9.1): 見ている日が現在の営業日と一致するときのみ
    # 付与する。過去日ブラウズ中に「いま話しかけやすい」を出すと嘘になるため。
    life_status: Optional[LifeStatus] = None
    now_status = get_life_status_now(manager, persona_id)
    if now_status.get("plan_date") == plan_date:
        life_status = LifeStatus(
            lives_declared=now_status["lives_declared"],
            in_life=now_status["in_life"],
            life_index=now_status["life_index"],
        )

    return DayPlanResponse(
        persona_id=persona_id,
        date=plan_date,
        slots=slot_items,
        # used は κ 積算の float — 二進小数の桁あふれ (0.6000000000000001 等) を
        # UI にそのまま見せないよう表示層のここで丸める
        budget=DayPlanBudget(
            total=budget["total"],
            used=round(budget["used"], 2),
            remaining=round(budget["remaining"], 2),
        ) if budget else None,
        lives=life_items,
        life_status=life_status,
    )


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

