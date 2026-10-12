"""ペルソナの暮らしビュー API (life_concept_map.md の読み出し面) とライフ設定。

決定論 (SELECT + 整形 / 設定の保存のみ) で LLM は呼ばない。

- GET /{persona_id}/clips : メッセージに付いた観測点＝点クリップ (sai_memory/clips.py)
- GET / PUT /{persona_id}/life-settings : ライフ設定 (起床・就寝・自律 ON/OFF・
  ティックの間隔 T・モード上書き) のまとめ読み書き (v0.4 計画 段 3-8 の管理画面の
  土台)。起床・就寝は PersonaSchedule の起床・就寝の行 (META_PLAYBOOK が
  ``judgment_day_open`` / ``judgment_day_close`` の periodic 行) が正典で、T と
  モード上書きは起床の行の PLAYBOOK_PARAMS に置く。

退役した仲間:

- 画面 D (プロフィール) の ``GET /{persona_id}/profile-tree`` — 2026-08-21。
  目的の木が概念ごと消えたため (autonomous_behavior_v3.md §9-5)。
- 画面 B (今日の予定表) の ``GET /{persona_id}/day-plan`` — 2026-08-22 (束 6c)。
  唯一の読み手だったライフビュー・できごと UI を v0.3 で隠したため
  (v3 §11「運転 UI は隠す」)。時間割そのものは v0.4 でルーチンへ世代交代する。
"""
import json
import logging
import uuid
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func

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


# ---------------------------------------------------------------------------
# ライフ設定のまとめ読み書き (v0.4 計画 段 3-8 の管理画面の土台)
# ---------------------------------------------------------------------------


class LifeModeInfo(BaseModel):
    derived: str                     # 標準モデルの provider からの自動判定 ("even"/"free")
    override: Optional[str] = None   # ユーザーの明示上書き。未設定は None (= 自動)
    effective: str                   # 上書きがあればそれ、無ければ derived


class LifeSettingsResponse(BaseModel):
    wake: Optional[str] = None       # "HH:MM"。None = 起床の行が無い (ライフ無し)
    close: Optional[str] = None      # "HH:MM"。None = 就寝の行が無い
    autonomy_enabled: bool           # ペルソナの AUTONOMY_ENABLED (設定値。全停止は別)
    tick_interval_minutes: Optional[int] = None   # 設定値そのもの。未設定は None
    effective_tick_interval_minutes: int          # 床・均等モードの上限を掛けた実効値
    mode: LifeModeInfo
    window_minutes: Optional[int] = None          # 起床〜就寝の長さ (両方あるときだけ)
    estimated_ticks_per_day: Optional[int] = None  # 一日およそ何回 (目安。上限でも保証でもない)
    # PUT の応答でだけ入る。起床・就寝の行の予約の再照合 (ScheduleManager への
    # 再登録) が成功したか。失敗しても DB が正典で回復 tick が直すので HTTP は
    # 200 のまま (スケジュール API の W3 A12 D7 と同じ流儀)。GET では常に None。
    scheduler_synced: Optional[bool] = None


class LifeSettingsUpdateRequest(BaseModel):
    """渡された欄だけを変える。null は「未設定に戻す」。

    欄の型は受け口では縛らず (Any)、ルートの中で検査して 400 を返す —
    pydantic の寛容な変換 (``"30"`` → 30、``True`` → 1 等) で不正な値を
    通さないため。
    """
    wake: Optional[Any] = None                   # "HH:MM" | null (ライフ無し)
    close: Optional[Any] = None                  # "HH:MM" | null
    autonomy_enabled: Optional[Any] = None       # bool
    tick_interval_minutes: Optional[Any] = None  # 正の int | null (既定に戻す)
    life_mode_override: Optional[Any] = None     # "even" | "free" | null (自動に戻す)


def _read_autonomy_setting(manager: Any, persona_id: str) -> bool:
    """AUTONOMY_ENABLED の設定値を DB から読む (駆動の門 is_autonomy_on は通さない)。"""
    from database.models import AI as AIModel

    db = manager.SessionLocal()
    try:
        row = db.query(AIModel.AUTONOMY_ENABLED).filter(AIModel.AIID == persona_id).first()
    finally:
        db.close()
    return bool(row[0]) if row is not None else False


def _build_life_settings(manager: Any, persona_id: str) -> LifeSettingsResponse:
    from saiverse import day_plan
    from saiverse.autonomy_wiring import _find_day_schedules

    try:
        # strict: 読めなかったことを「未設定」と見せない (画面が未設定と誤表示し、
        # ユーザーが上書き保存してしまう)。
        day = _find_day_schedules(manager, persona_id, strict=True)
    except Exception as exc:
        LOGGER.exception("[life-settings] failed to read the day schedules of %s", persona_id)
        raise HTTPException(status_code=500, detail=f"Failed to read life settings: {exc}")
    wake = day.get("wake")
    close = day.get("close")
    settings = day_plan.life_settings_from_params(day.get("day_open_params"))
    override = settings.get("life_mode_override")
    tick = settings.get("tick_interval_minutes")
    info = day_plan.life_mode_and_tick_estimate(
        manager, persona_id, wake, close,
        mode_override=override, tick_interval_minutes=tick,
    )
    return LifeSettingsResponse(
        wake=wake,
        close=close,
        autonomy_enabled=_read_autonomy_setting(manager, persona_id),
        tick_interval_minutes=tick,
        effective_tick_interval_minutes=info["tick_interval_minutes"],
        mode=LifeModeInfo(
            derived=info["derived_mode"],
            override=override,
            effective=info["effective_mode"],
        ),
        window_minutes=info["window_minutes"],
        estimated_ticks_per_day=info["estimated_ticks_per_day"],
    )


@router.get("/{persona_id}/life-settings", response_model=LifeSettingsResponse)
def get_life_settings(persona_id: str, manager=Depends(get_manager)) -> LifeSettingsResponse:
    """ライフ設定の現況 (起床・就寝・自律・T・モード・一日の目安) をまとめて返す。"""
    _require_persona(manager, persona_id)
    return _build_life_settings(manager, persona_id)


def _life_rows(db: Any, persona_id: str, meta_playbook: str) -> List[Any]:
    from database.models import PersonaSchedule

    return (
        db.query(PersonaSchedule)
        .filter(
            PersonaSchedule.PERSONA_ID == persona_id,
            PersonaSchedule.SCHEDULE_TYPE == "periodic",
            PersonaSchedule.META_PLAYBOOK == meta_playbook,
        )
        .order_by(PersonaSchedule.SCHEDULE_ID.asc())
        .all()
    )


def _active_rows(rows: List[Any]) -> List[Any]:
    """読み手 (_find_day_schedules) が数える行 — 有効で時刻のある行。"""
    return [r for r in rows if r.ENABLED and (r.TIME_OF_DAY or "").strip()]


def _pick_life_row(rows: List[Any], *, earliest: bool) -> Optional[Any]:
    """この API が書き換える行を一つ選ぶ。

    読み手 (``_find_day_schedules``) が採る行と同じものを選ぶ — 起床は有効な
    行のうち最も早い時刻、就寝は最も遅い時刻。有効な行が無ければ最小
    SCHEDULE_ID の行 (無効化されたライフの行を再利用する)。行が無ければ None。
    """
    active = _active_rows(rows)
    if active:
        if earliest:
            return min(active, key=lambda r: (r.TIME_OF_DAY.strip(), r.SCHEDULE_ID))
        return max(active, key=lambda r: (r.TIME_OF_DAY.strip(), -r.SCHEDULE_ID))
    return rows[0] if rows else None


def _parse_params(raw: Optional[str]) -> Dict[str, Any]:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _bump_generation(row: Any) -> None:
    from database.models import PersonaSchedule

    # 発火に関わる設定変更は同一 commit で世代 +1 (スケジュール API と同じ
    # サーバー側インクリメント — 並行更新で同じ世代番号を得ない)。
    row.SYNC_GENERATION = func.coalesce(PersonaSchedule.SYNC_GENERATION, 0) + 1


def _new_life_row(persona_id: str, meta_playbook: str, description: str) -> Any:
    from database.models import PersonaSchedule

    return PersonaSchedule(
        PERSONA_ID=persona_id,
        SCHEDULE_TYPE="periodic",
        META_PLAYBOOK=meta_playbook,
        ENABLED=True,
        DESCRIPTION=description,
        PRIORITY=0,
        # 新規行は世代 1 から (スケジュール API の create と同じ)
        SYNC_GENERATION=1,
        # 行一生トークン (SCHEDULE_ID 再利用との分離)。作成時に一度だけ採番する。
        INSTANCE_TOKEN=uuid.uuid4().hex[:12],
    )


def _apply_time(
    db: Any,
    persona_id: str,
    meta_playbook: str,
    rows: List[Any],
    value: Optional[str],
    *,
    earliest: bool,
    description: str,
    touched: List[Any],
) -> Optional[Any]:
    """起床または就寝の時刻を書く。value=None は「ライフ無し」(行を無効化)。

    行は消さない — 無効化した行は読み手 (``_find_day_schedules``) から見えない
    ので「行が無い = 未設定」と同じに読まれ、起床の行の PLAYBOOK_PARAMS (T・
    モード上書き) は次に起床を設定し直したときに戻ってくる。

    Returns:
        書いた後の、この API の対象の行 (無ければ None)。
    """
    if value is None:
        active = _active_rows(rows)
        for row in active:
            row.ENABLED = False
            _bump_generation(row)
            touched.append(row)
        return _pick_life_row(rows, earliest=earliest)
    row = _pick_life_row(rows, earliest=earliest)
    if row is None:
        row = _new_life_row(persona_id, meta_playbook, description)
        db.add(row)
    else:
        _bump_generation(row)
    row.TIME_OF_DAY = value
    row.ENABLED = True
    touched.append(row)
    others = [r for r in _active_rows(rows) if r is not row]
    if others:
        LOGGER.warning(
            "[life-settings] persona %s has %d other enabled %s row(s) (ids=%s); "
            "they still count for the life window", persona_id, len(others),
            meta_playbook, [r.SCHEDULE_ID for r in others],
        )
    return row


def _bad_request(detail: str) -> HTTPException:
    return HTTPException(status_code=400, detail=detail)


@router.put("/{persona_id}/life-settings", response_model=LifeSettingsResponse)
def update_life_settings(
    persona_id: str,
    req: LifeSettingsUpdateRequest,
    manager=Depends(get_manager),
) -> LifeSettingsResponse:
    """ライフ設定を保存する。渡された欄だけを変え、null は「未設定に戻す」。

    - ``wake`` / ``close``: 起床・就寝の行 (PersonaSchedule) の時刻。行が無ければ
      作る。null は行の無効化 (= ライフ無し)。保存後は ScheduleManager へ
      再登録して予約を再照合する (スケジュール API の作法と同じ)
    - ``tick_interval_minutes`` / ``life_mode_override``: 起床の行の
      PLAYBOOK_PARAMS に**設定値のまま**保存する (床 10 分・均等 50 分の丸めは
      読み時の実効値の計算で掛ける)。起床が未設定のときは置き場が無いので 400
    - ``autonomy_enabled``: ペルソナ設定の保存と同じ経路
      (:func:`saiverse.autonomy_wiring.set_persona_autonomy_enabled`)

    返り値は GET と同じ形 (保存後の現況) に ``scheduler_synced`` を足したもの。
    """
    from saiverse import day_plan
    from saiverse.autonomy_wiring import (
        LIFE_END_SCHEDULE_PLAYBOOK,
        LIFE_START_SCHEDULE_PLAYBOOK,
        set_persona_autonomy_enabled,
    )

    _require_persona(manager, persona_id)
    fields = req.model_fields_set

    # --- 検査 (書く前に全部) ---
    for name in ("wake", "close"):
        if name in fields:
            value = getattr(req, name)
            if value is not None and not day_plan.is_valid_hhmm(value):
                raise _bad_request(
                    f"{name} は 'HH:MM' 形式か null で指定してください (got {value!r})"
                )
    if "autonomy_enabled" in fields and not isinstance(req.autonomy_enabled, bool):
        raise _bad_request(
            f"autonomy_enabled は true / false で指定してください (got {req.autonomy_enabled!r})"
        )
    if "tick_interval_minutes" in fields and req.tick_interval_minutes is not None:
        t = req.tick_interval_minutes
        if not (isinstance(t, int) and not isinstance(t, bool) and t >= 1):
            raise _bad_request(
                f"tick_interval_minutes は正の整数か null で指定してください (got {t!r})"
            )
    if "life_mode_override" in fields and req.life_mode_override is not None:
        if req.life_mode_override not in day_plan.LIFE_MODES:
            raise _bad_request(
                f"life_mode_override は {list(day_plan.LIFE_MODES)} か null で指定して"
                f"ください (got {req.life_mode_override!r})"
            )
    params_fields = [
        name for name in ("tick_interval_minutes", "life_mode_override") if name in fields
    ]

    # --- 起床・就寝の行 ---
    touched: List[Any] = []
    if {"wake", "close"} & fields or params_fields:
        db = manager.SessionLocal()
        try:
            open_rows = _life_rows(db, persona_id, LIFE_START_SCHEDULE_PLAYBOOK)
            close_rows = _life_rows(db, persona_id, LIFE_END_SCHEDULE_PLAYBOOK)
            open_row = _pick_life_row(open_rows, earliest=True)
            close_row = _pick_life_row(close_rows, earliest=False)

            def _current(row: Optional[Any]) -> Optional[str]:
                if row is None or not row.ENABLED:
                    return None
                return (row.TIME_OF_DAY or "").strip() or None

            new_wake = req.wake if "wake" in fields else _current(open_row)
            new_close = req.close if "close" in fields else _current(close_row)
            if new_wake and new_close and new_wake == new_close:
                raise _bad_request(
                    "起床と就寝が同時刻です。活動時間が 0 分になるため設定できません"
                )

            if "wake" in fields:
                open_row = _apply_time(
                    db, persona_id, LIFE_START_SCHEDULE_PLAYBOOK, open_rows, req.wake,
                    earliest=True, description="起床 (ライフ設定)", touched=touched,
                )
            if "close" in fields:
                _apply_time(
                    db, persona_id, LIFE_END_SCHEDULE_PLAYBOOK, close_rows, req.close,
                    earliest=False, description="就寝 (ライフ設定)", touched=touched,
                )

            if params_fields:
                if open_row is None or not open_row.ENABLED:
                    raise _bad_request(
                        "起床が未設定のあいだは tick_interval_minutes / "
                        "life_mode_override を保存できません (起床の行に保存するため)"
                    )
                params = _parse_params(open_row.PLAYBOOK_PARAMS)
                for name in params_fields:
                    value = getattr(req, name)
                    if value is None:
                        params.pop(name, None)
                    else:
                        params[name] = value
                open_row.PLAYBOOK_PARAMS = (
                    json.dumps(params, ensure_ascii=False) if params else None
                )
                if open_row not in touched:
                    if open_row.SCHEDULE_ID is not None:
                        _bump_generation(open_row)
                    touched.append(open_row)
            db.flush()
            touched_ids = [
                (row.SCHEDULE_ID, bool(row.ENABLED)) for row in touched
            ]
            db.commit()
        except HTTPException:
            db.rollback()
            raise
        except Exception as exc:
            db.rollback()
            LOGGER.exception("[life-settings] failed to save for %s", persona_id)
            raise HTTPException(status_code=500, detail=f"Failed to save life settings: {exc}")
        finally:
            db.close()
    else:
        touched_ids = []

    # --- 予約の再照合 (スケジュール API と同じ: 有効なら再登録、無効なら取り消し) ---
    scheduler_synced: Optional[bool] = True if touched_ids else None
    seen: set = set()
    for schedule_id, enabled in touched_ids:
        if schedule_id in seen:
            continue
        seen.add(schedule_id)
        try:
            if enabled:
                result = manager.schedule_manager.register_schedule(schedule_id)
                if result not in ("registered", "no_reservation_needed"):
                    scheduler_synced = False
                    LOGGER.warning(
                        "[life-settings] schedule %d is not registrable (%s)",
                        schedule_id, result,
                    )
            else:
                manager.schedule_manager.unregister_schedule(schedule_id)
        except Exception:
            scheduler_synced = False
            LOGGER.exception(
                "[life-settings] failed to sync schedule %d with the EventScheduler",
                schedule_id,
            )

    # --- 自律 ON/OFF (ペルソナ設定の保存と同じ経路・同じ副作用) ---
    # 起床・就寝の保存の後に行う — ON で立ち上がる見張りが新しい窓を読むように。
    if "autonomy_enabled" in fields:
        try:
            set_persona_autonomy_enabled(manager, persona_id, req.autonomy_enabled)
        except Exception as exc:
            LOGGER.exception("[life-settings] failed to save AUTONOMY_ENABLED for %s", persona_id)
            raise HTTPException(
                status_code=500, detail=f"Failed to save autonomy setting: {exc}",
            )

    LOGGER.info(
        "[life-settings] saved: persona=%s fields=%s", persona_id, sorted(fields),
    )
    response = _build_life_settings(manager, persona_id)
    response.scheduler_synced = scheduler_synced
    return response

