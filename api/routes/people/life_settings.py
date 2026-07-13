"""ライフ設定 API (life.md v0.5 §9.2-1)。

起床・就寝・予算を「1 画面で設定する」ためのバックエンド。既存の器
(PersonaSchedule の judgment_day_open / judgment_day_close 行) はそのまま
使うが、この API は「エアの一日」というまとまりで読み書きできる一本化された
入出力を提供する — 起床就寝は ScheduleModal (汎用スケジュール UI)、日予算は
別の入力欄、というこれまでの分散を解消する。

- GET /{persona_id}/life-settings : 現在の設定 + 自動判定モード + 最低予算ガイド
- PUT /{persona_id}/life-settings : 起床・就寝・予算・モード上書きを保存

保存内容は judgment_day_open / judgment_day_close の PersonaSchedule 行
(periodic, TIME_OF_DAY + PLAYBOOK_PARAMS) にそのまま反映される。実際の
ライフ確定 (meta_json.lives への書き込み) は day_open 発火時に
``saiverse.day_plan.confirm_life_for_today`` が行う (このAPIは設定の保存のみ)。
"""
import json
import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from api.deps import get_manager
from database.models import AI as AIModel
from database.models import PersonaSchedule
from saiverse import day_plan

LOGGER = logging.getLogger(__name__)

router = APIRouter()

#: judgment_day_open / judgment_day_close の PersonaSchedule 行が持つ META_PLAYBOOK 名
_DAY_OPEN_PLAYBOOK = "judgment_day_open"
_DAY_CLOSE_PLAYBOOK = "judgment_day_close"


def _require_persona(manager: Any, persona_id: str) -> None:
    db = manager.SessionLocal()
    try:
        exists = db.query(AIModel.AIID).filter(AIModel.AIID == persona_id).first()
    finally:
        db.close()
    if exists is None:
        raise HTTPException(status_code=404, detail=f"Persona {persona_id} not found")


def _parse_params(raw: Optional[str]) -> Dict[str, Any]:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except (TypeError, ValueError):
        return {}


def _find_row(db, persona_id: str, meta_playbook: str) -> Optional[PersonaSchedule]:
    """periodic な judgment_day_open/close 行を 1 件見つける (最小 SCHEDULE_ID を正とする)。

    複数行が存在する場合 (旧 ScheduleModal で手動追加した等) はここでは統合しない —
    最小 ID の 1 行だけをこの API の対象にする (現状把握は報告に譲る)。
    """
    return (
        db.query(PersonaSchedule)
        .filter(
            PersonaSchedule.PERSONA_ID == persona_id,
            PersonaSchedule.SCHEDULE_TYPE == "periodic",
            PersonaSchedule.META_PLAYBOOK == meta_playbook,
        )
        .order_by(PersonaSchedule.SCHEDULE_ID.asc())
        .first()
    )


class LifeSettingsResponse(BaseModel):
    wake: Optional[str] = None                    # "HH:MM"、未設定なら None
    close: Optional[str] = None                    # "HH:MM"、未設定なら None
    daily_budget_rounds: Optional[int] = None       # 作業にあてられる回数 (日次ラウンド予算)
    daily_budget_pulses: Optional[int] = None       # ユーザー設定の標準パルス予算 (未設定なら None = 最低値を使う)
    life_mode_override: Optional[str] = None        # "even" / "free" (明示上書き)。未設定は None = 自動
    derived_mode: str                               # 自動判定結果 ("even"/"free")
    effective_mode: str                              # 上書きがあればそれ、無ければ derived_mode と同じ
    min_budget_pulses: Optional[int] = None          # wake/close 揃っているときの最低予算 (パルス)
    window_minutes: Optional[int] = None             # wake/close 揃っているときの活動時間 (分)
    default_budget_rounds: int                        # 未設定時に使われる既定の作業回数


class LifeSettingsRequest(BaseModel):
    wake: str                                        # "HH:MM"
    close: str                                        # "HH:MM"
    daily_budget_rounds: Optional[int] = None
    daily_budget_pulses: Optional[int] = None
    life_mode_override: Optional[str] = None          # "even" / "free" / None (自動に戻す)


def _build_response(manager: Any, persona_id: str) -> LifeSettingsResponse:
    db = manager.SessionLocal()
    try:
        open_row = _find_row(db, persona_id, _DAY_OPEN_PLAYBOOK)
        close_row = _find_row(db, persona_id, _DAY_CLOSE_PLAYBOOK)
        wake = (open_row.TIME_OF_DAY or None) if open_row else None
        close = (close_row.TIME_OF_DAY or None) if close_row else None
        params = _parse_params(open_row.PLAYBOOK_PARAMS if open_row else None)
    finally:
        db.close()

    daily_budget_rounds = params.get("daily_budget_rounds")
    if not (isinstance(daily_budget_rounds, int) and not isinstance(daily_budget_rounds, bool)):
        daily_budget_rounds = None
    daily_budget_pulses = params.get("daily_budget_pulses")
    if not (isinstance(daily_budget_pulses, int) and not isinstance(daily_budget_pulses, bool)):
        daily_budget_pulses = None
    life_mode_override = params.get("life_mode_override")
    if life_mode_override not in day_plan.LIFE_MODES:
        life_mode_override = None

    info = day_plan.life_mode_and_min_budget(
        manager, persona_id, wake, close, mode_override=life_mode_override,
    )
    return LifeSettingsResponse(
        wake=wake,
        close=close,
        daily_budget_rounds=daily_budget_rounds,
        daily_budget_pulses=daily_budget_pulses,
        life_mode_override=life_mode_override,
        derived_mode=info["derived_mode"],
        effective_mode=info["effective_mode"],
        min_budget_pulses=info["min_budget_pulses"],
        window_minutes=info["window_minutes"],
        default_budget_rounds=day_plan.DEFAULT_BUDGET_ROUNDS,
    )


@router.get("/{persona_id}/life-settings", response_model=LifeSettingsResponse)
def get_life_settings(persona_id: str, manager=Depends(get_manager)) -> LifeSettingsResponse:
    """現在のライフ設定 + 自動判定モード + 最低予算ガイドをまとめて返す。"""
    _require_persona(manager, persona_id)
    return _build_response(manager, persona_id)


def _upsert_day_row(
    db, persona_id: str, meta_playbook: str, time_of_day: str,
    *, description: str, playbook_params: Optional[Dict[str, Any]] = None,
) -> int:
    row = _find_row(db, persona_id, meta_playbook)
    if row is None:
        row = PersonaSchedule(
            PERSONA_ID=persona_id,
            SCHEDULE_TYPE="periodic",
            META_PLAYBOOK=meta_playbook,
            ENABLED=True,
            DESCRIPTION=description,
            PRIORITY=0,
        )
        db.add(row)
    row.TIME_OF_DAY = time_of_day
    row.ENABLED = True
    if playbook_params is not None:
        row.PLAYBOOK_PARAMS = (
            json.dumps(playbook_params, ensure_ascii=False) if playbook_params else None
        )
    db.flush()
    return row.SCHEDULE_ID


@router.put("/{persona_id}/life-settings", response_model=LifeSettingsResponse)
def update_life_settings(
    persona_id: str,
    request: LifeSettingsRequest,
    manager=Depends(get_manager),
) -> LifeSettingsResponse:
    """起床・就寝・予算・モード上書きを 1 リクエストで保存する。

    最低予算未満の ``daily_budget_pulses`` や不正な時刻・モードは 400 で弾く
    (life.md v0.5 §3「不正な値は検証でなく、書ける口をなくす」の設定 UI 側の
    実行——保存前に検証で理由を明示して伝える。バックエンドの切り上げ
    (:func:`confirm_life_for_today`) は最終防衛として別途動く)。
    """
    _require_persona(manager, persona_id)

    if not day_plan.is_valid_hhmm(request.wake):
        raise HTTPException(status_code=400, detail=f"wake は 'HH:MM' 形式で指定してください (got {request.wake!r})")
    if not day_plan.is_valid_hhmm(request.close):
        raise HTTPException(status_code=400, detail=f"close は 'HH:MM' 形式で指定してください (got {request.close!r})")
    if request.wake == request.close:
        raise HTTPException(status_code=400, detail="起床と就寝が同時刻です。活動時間が 0 分になるため設定できません")

    mode_override = request.life_mode_override
    if mode_override is not None and mode_override not in day_plan.LIFE_MODES:
        raise HTTPException(
            status_code=400,
            detail=f"life_mode_override は {sorted(day_plan.LIFE_MODES)} または未指定にしてください (got {mode_override!r})",
        )

    if request.daily_budget_rounds is not None:
        if isinstance(request.daily_budget_rounds, bool) or request.daily_budget_rounds < 1:
            raise HTTPException(status_code=400, detail="daily_budget_rounds は 1 以上の整数で指定してください")

    if request.daily_budget_pulses is not None:
        if isinstance(request.daily_budget_pulses, bool) or request.daily_budget_pulses < 1:
            raise HTTPException(status_code=400, detail="daily_budget_pulses は 1 以上の整数で指定してください")
        info = day_plan.life_mode_and_min_budget(
            manager, persona_id, request.wake, request.close, mode_override=mode_override,
        )
        min_budget = info["min_budget_pulses"]
        if min_budget is not None and request.daily_budget_pulses < min_budget:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"この活動時間 ({info['window_minutes']}分、モード={info['effective_mode']}) "
                    f"なら最低 {min_budget} 回が必要です (指定値: {request.daily_budget_pulses})"
                ),
            )

    playbook_params: Dict[str, Any] = {}
    if request.daily_budget_rounds is not None:
        playbook_params["daily_budget_rounds"] = request.daily_budget_rounds
    if request.daily_budget_pulses is not None:
        playbook_params["daily_budget_pulses"] = request.daily_budget_pulses
    if mode_override is not None:
        playbook_params["life_mode_override"] = mode_override

    db = manager.SessionLocal()
    open_id: Optional[int] = None
    close_id: Optional[int] = None
    try:
        open_id = _upsert_day_row(
            db, persona_id, _DAY_OPEN_PLAYBOOK, request.wake,
            description="起床 (ライフ設定)", playbook_params=playbook_params,
        )
        close_id = _upsert_day_row(
            db, persona_id, _DAY_CLOSE_PLAYBOOK, request.close,
            description="就寝 (ライフ設定)",
        )
        db.commit()
    except Exception as exc:
        db.rollback()
        LOGGER.exception("[life-settings] failed to save for %s", persona_id)
        raise HTTPException(status_code=500, detail=str(exc))
    finally:
        db.close()

    # Phase 4-e 流儀: 保存直後に EventScheduler へ (再) push する
    for schedule_id in (open_id, close_id):
        if schedule_id is None:
            continue
        try:
            manager.schedule_manager.register_schedule(schedule_id)
        except Exception:
            LOGGER.exception(
                "[life-settings] failed to register schedule %d on EventScheduler",
                schedule_id,
            )

    LOGGER.info(
        "[life-settings] saved: persona=%s wake=%s close=%s rounds=%s pulses=%s mode_override=%s",
        persona_id, request.wake, request.close,
        request.daily_budget_rounds, request.daily_budget_pulses, mode_override,
    )
    return _build_response(manager, persona_id)
