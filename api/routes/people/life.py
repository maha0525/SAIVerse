"""ペルソナの暮らしビュー API (life_concept_map.md の読み出し面)。

画面 B (今日の予定表) / C (会話バブルの点写真ハイライト) /
D (プロフィール) 用の読み取り専用エンドポイント群。全て決定論
(SELECT + 整形のみ) で LLM は呼ばない。

- GET /{persona_id}/day-plan      : 時間割のコマ一覧 (saiverse/day_plan.py)
- GET /{persona_id}/photos        : メッセージに付いた観測点＝点写真 (sai_memory/photos.py)
- GET /{persona_id}/profile-tree  : 目的の木の第一階層 + 候補 (§15 の随意アクセス面)
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

#: /photos の message_ids 一括指定の上限 (チャット 1 画面分を想定)
PHOTOS_BATCH_LIMIT = 100


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
    """日次の作業ラウンド予算 (v2 §4.5)。台帳の無い日は day-plan 応答で None。"""
    total: int
    used: int
    remaining: int


class LifeItem(BaseModel):
    """ライフ宣言 1 件 (life.md §4.1) の表示用整形。ライフビューの帯描画に使う。"""
    index: int                      # get_lives の並び順 (安定キー)
    start: str                      # "HH:MM"
    end: str                        # "HH:MM"
    mode: str                       # "even" (均等) / "free" (自由)
    budget_pulses: int
    used_pulses: int
    used_rounds: int
    consumed: float                 # used_pulses + used_rounds × κ (life_consumed)
    remaining: float                # budget_pulses − consumed (0 未満には丸めない)


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
        budget=DayPlanBudget(**budget) if budget else None,
        lives=life_items,
        life_status=life_status,
    )


# ---------------------------------------------------------------------------
# C: 点写真 (観測点) のバッチ取得
# ---------------------------------------------------------------------------


class PhotoItem(BaseModel):
    photo_id: str                   # フロントの key 用
    message_id: str                 # SAIMemory (memory.db) の message id
    quote: str                      # 本文からの逐語引用 (ハイライト対象)
    purpose_ref: Optional[str]      # 目的ノード参照 ("task:N" 等)。素の予約は None
    created_at: int                 # epoch 秒


class PhotosResponse(BaseModel):
    persona_id: str
    photos: List[PhotoItem]         # message_id 昇順ではなく created_at 昇順 (撮られた順)


@router.get("/{persona_id}/photos", response_model=PhotosResponse)
def list_message_photos(
    persona_id: str,
    message_ids: str,
    manager=Depends(get_manager),
) -> PhotosResponse:
    """メッセージ群に付いた観測点 (点写真) をバッチで返す (画面 C: ハイライト)。

    ``message_ids`` はカンマ区切りの SAIMemory message id (上限
    :data:`PHOTOS_BATCH_LIMIT`)。**建物履歴 (building_messages) の message_id
    とは別体系** — memory.db の messages.id を渡すこと (記憶ブラウズ API
    ``GET /{persona_id}/threads/{thread_id}/messages`` が返す ``id`` と同じ体系)。

    memory.db へのアクセスは記憶ブラウズ系と同じ ``get_adapter`` 経由
    (adapter.conn + adapter._db_lock)。photos テーブルは adapter 初期化時に
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
    if len(ids) > PHOTOS_BATCH_LIMIT:
        raise HTTPException(
            status_code=400,
            detail=f"too many message_ids: {len(ids)} (max {PHOTOS_BATCH_LIMIT})",
        )

    from sai_memory.photos import list_photos

    items: List[PhotoItem] = []
    with get_adapter(persona_id, manager) as adapter:
        with adapter._db_lock:
            for mid in ids:
                for photo in list_photos(adapter.conn, message_id=mid):
                    if not photo.quote:
                        continue  # ハイライトは引用アンカーを持つ点写真のみ対象
                    items.append(PhotoItem(
                        photo_id=photo.photo_id,
                        message_id=photo.message_id,
                        quote=photo.quote,
                        purpose_ref=photo.purpose_ref,
                        created_at=photo.created_at,
                    ))
    items.sort(key=lambda p: (p.created_at, p.photo_id))
    return PhotosResponse(persona_id=persona_id, photos=items)


# ---------------------------------------------------------------------------
# D: プロフィール (目的の木の読み取り専用集約)
# ---------------------------------------------------------------------------


class ProfileTreeResponse(BaseModel):
    persona_id: str
    # 夢中になっていること = 目的の木の第一階層 (purpose_tree.list_first_tier の
    # node dict + last_activity_at)。track ノードと task ノードでキー構成が違う
    # (node_kind で判別) ため自由形 dict で返す。
    first_tier: List[Dict[str, Any]]
    # やってみたいこと = stage=candidate の目的ノード (まだ木の外の候補 §3.1)
    candidates: List[Dict[str, Any]]


@router.get("/{persona_id}/profile-tree", response_model=ProfileTreeResponse)
def get_profile_tree(
    persona_id: str,
    manager=Depends(get_manager),
) -> ProfileTreeResponse:
    """ペルソナのプロフィール用の目的の木 (画面 D)。読み取り専用の集約。

    - ``first_tier``: 夢中になっていること = 木の第一階層
      (saiverse/purpose_tree.py:list_first_tier)。各ノードに直近活動の目安
      ``last_activity_at`` (ISO / None) を付ける (track は last_active_at、
      task は last_touched_at / updated_at)。
    - ``candidates``: やってみたいこと = stage=candidate の persona_task 行。
      アーカイブ済み (status=cancelled → 期限切れ欲求含む) と休眠
      (stage=dormant) は生存 status フィルタで除外される。
      desire_engine._list_desires (parent_kind='note' 限定) でなく stage で
      引くのは、purpose_tree.create_candidate が作る親なし候補
      (parent_kind=None / stage=candidate) も含めるため。
    - タスク一覧は返さない — フロントは既存の ``GET /{persona_id}/tasks`` を叩く。
    - 来歴 (§15 自己所有感の担保): 各項目の ``source_ref`` (接地参照) と
      ``promoted_from`` (命名・昇格の来歴) をそのまま含める。
    """
    _require_persona(manager, persona_id)

    from database.models import ActionTrack, PersonaTask
    from saiverse.persona_task_manager import STAGE_CANDIDATE, PersonaTaskManager
    from saiverse.purpose_tree import LIVE_TASK_STATUSES, list_first_tier

    nodes = list_first_tier(manager, persona_id)

    # 直近活動の目安を 1 クエリずつのバッチで解決する (N+1 回避)
    track_ids = [n["id"] for n in nodes if n.get("node_kind") == "track"]
    task_ids = [n["id"] for n in nodes if n.get("node_kind") == "task"]
    last_activity: Dict[str, Optional[str]] = {}
    db = manager.SessionLocal()
    try:
        if track_ids:
            rows = (
                db.query(ActionTrack.track_id, ActionTrack.last_active_at)
                .filter(ActionTrack.track_id.in_(track_ids))
                .all()
            )
            for track_id, last_active_at in rows:
                last_activity[track_id] = (
                    last_active_at.isoformat() if last_active_at else None
                )
        if task_ids:
            rows = (
                db.query(
                    PersonaTask.id, PersonaTask.last_touched_at, PersonaTask.updated_at
                )
                .filter(PersonaTask.id.in_(task_ids))
                .all()
            )
            for task_id, last_touched_at, updated_at in rows:
                ts = last_touched_at or updated_at
                last_activity[task_id] = ts.isoformat() if ts else None
    finally:
        db.close()

    first_tier = [
        {**node, "last_activity_at": last_activity.get(node["id"])}
        for node in nodes
    ]

    # 候補 (stage=candidate)。生存 status のみ = アーカイブ・休眠は除外
    ptm = PersonaTaskManager(manager.SessionLocal)
    candidates = [
        {
            "ref": task.get("task_ref") or task["id"],
            "id": task["id"],
            "title": task.get("title"),
            "stage": task.get("stage"),
            "status": task.get("status"),
            "desire_type": task.get("desire_type"),
            "desire_state": task.get("desire_state"),
            "touch_count": task.get("touch_count") or 0,
            "source_ref": task.get("desire_source"),
            "promoted_from": task.get("promoted_from") or [],
            "created_at": task.get("created_at"),
            "last_touched_at": task.get("last_touched_at"),
        }
        for task in ptm.list_tasks(
            persona_id, statuses=LIVE_TASK_STATUSES, include_steps=False
        )
        if task.get("stage") == STAGE_CANDIDATE
    ]

    return ProfileTreeResponse(
        persona_id=persona_id, first_tier=first_tier, candidates=candidates,
    )
