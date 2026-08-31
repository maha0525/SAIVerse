import json
import logging
import uuid
from fastapi import APIRouter, Depends, HTTPException
from typing import List
from api.deps import get_manager
from .models import ScheduleItem, CreateScheduleRequest, UpdateScheduleRequest
from sqlalchemy import func

from database.models import PersonaSchedule, AI as AIModel, City as CityModel
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

_log = logging.getLogger(__name__)

router = APIRouter()

def _as_utc_aware(dt):
    """DB から読んだ naive datetime を UTC-aware にして返す。

    SCHEDULED_DATETIME / LAST_EXECUTED_AT は UTC 基準で保存されている
    (create/update で JST→UTC 変換済み、発火側も naive を UTC とみなす) が、
    Column(DateTime) は tz を持たないため naive で読み戻る。そのまま Pydantic が
    シリアライズすると ``2025-12-07T00:00:00`` (オフセット無し) になり、
    フロントの ``new Date()`` がローカル時刻と誤読して UTC→JST 変換が効かない。
    ここで明示的に UTC を付与し、``...+00:00`` を出させることで表示を正す。
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt

def _get_persona_timezone(manager, persona_id: str) -> ZoneInfo:
    session = manager.SessionLocal()
    try:
        persona_model = session.query(AIModel).filter(AIModel.AIID == persona_id).first()
        if not persona_model:
            return ZoneInfo("UTC")
        city_model = session.query(CityModel).filter(CityModel.CITYID == persona_model.HOME_CITYID).first()
        if not city_model or not city_model.TIMEZONE:
            return ZoneInfo("UTC")
        return ZoneInfo(city_model.TIMEZONE)
    except Exception:
        _log.warning("Failed to get timezone for persona %s, defaulting to UTC", persona_id, exc_info=True)
        return ZoneInfo("UTC")
    finally:
        session.close()

@router.get("/{persona_id}/schedules", response_model=List[ScheduleItem])
def list_schedules(persona_id: str, manager = Depends(get_manager)):
    """List schedules for a persona."""
    session = manager.SessionLocal()
    try:
        schedules = (
            session.query(PersonaSchedule)
            .filter(PersonaSchedule.PERSONA_ID == persona_id)
            .order_by(PersonaSchedule.PRIORITY.desc(), PersonaSchedule.SCHEDULE_ID.desc())
            .all()
        )
        results = []
        for s in schedules:
            days = None
            if s.DAYS_OF_WEEK:
                try:
                    days = json.loads(s.DAYS_OF_WEEK)
                except Exception:
                    _log.warning("Failed to parse DAYS_OF_WEEK for schedule %d", s.SCHEDULE_ID, exc_info=True)
            
            # Parse args from DB column PLAYBOOK_PARAMS
            parsed_args = None
            if s.PLAYBOOK_PARAMS:
                try:
                    parsed_args = json.loads(s.PLAYBOOK_PARAMS)
                except Exception:
                    _log.warning("Failed to parse PLAYBOOK_PARAMS for schedule %d", s.SCHEDULE_ID, exc_info=True)

            results.append(ScheduleItem(
                schedule_id=s.SCHEDULE_ID,
                schedule_type=s.SCHEDULE_TYPE,
                meta_playbook=s.META_PLAYBOOK,
                description=s.DESCRIPTION,
                priority=s.PRIORITY,
                enabled=s.ENABLED,
                days_of_week=days,
                time_of_day=s.TIME_OF_DAY,
                scheduled_datetime=_as_utc_aware(s.SCHEDULED_DATETIME),
                interval_seconds=s.INTERVAL_SECONDS,
                last_executed_at=_as_utc_aware(s.LAST_EXECUTED_AT),
                completed=s.COMPLETED,
                args=parsed_args
            ))
        return results
    finally:
        session.close()

@router.post("/{persona_id}/schedules")
def create_schedule(
    persona_id: str,
    req: CreateScheduleRequest,
    manager = Depends(get_manager)
):
    """Create a new schedule."""
    session = manager.SessionLocal()
    try:
        new_schedule = PersonaSchedule(
            PERSONA_ID=persona_id,
            SCHEDULE_TYPE=req.schedule_type,
            META_PLAYBOOK=req.meta_playbook,
            DESCRIPTION=req.description,
            PRIORITY=req.priority,
            ENABLED=req.enabled,
            PLAYBOOK_PARAMS=json.dumps(req.args) if req.args else None,
            # W3 A12: 新規行は世代 1 から始める (設定変更ごとに +1)
            SYNC_GENERATION=1,
            # W3 Codex 第三陣: 行一生トークン (SCHEDULE_ID 再利用との分離)。
            # 作成時に一度だけ採番し、更新では変えない。
            INSTANCE_TOKEN=uuid.uuid4().hex[:12],
        )

        if req.schedule_type == "periodic":
            if req.days_of_week:
                new_schedule.DAYS_OF_WEEK = json.dumps(req.days_of_week)
            new_schedule.TIME_OF_DAY = req.time_of_day

        elif req.schedule_type == "oneshot":
            if req.scheduled_datetime:
                try:
                    tz = _get_persona_timezone(manager, persona_id)
                    dt_naive = datetime.strptime(req.scheduled_datetime, "%Y-%m-%d %H:%M")
                    dt_local = dt_naive.replace(tzinfo=tz)
                    dt_utc = dt_local.astimezone(timezone.utc)
                    new_schedule.SCHEDULED_DATETIME = dt_utc
                except ValueError:
                    raise HTTPException(status_code=400, detail="Invalid datetime format: YYYY-MM-DD HH:MM")

        elif req.schedule_type == "interval":
            new_schedule.INTERVAL_SECONDS = req.interval_seconds

        session.add(new_schedule)
        session.commit()
        new_schedule_id = new_schedule.SCHEDULE_ID
    except Exception as e:
        session.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        session.close()

    # Phase 4-e: 作成直後に EventScheduler に push (有効なら)。
    # W3 A12 (D7): 同期失敗は握り潰さず scheduler_synced=False で応答に明示する
    # (HTTP は 200 のまま — DB が正典で reconciliation が回復するため)。
    # register_schedule は tri-state: not_registrable (有効なのに設定不備等で
    # 予約を作れない = reconciliation でも回復不能) も False に含める。
    try:
        result = manager.schedule_manager.register_schedule(new_schedule_id)
        scheduler_synced = result in ("registered", "no_reservation_needed")
    except Exception:
        scheduler_synced = False
        _log.exception("Failed to register schedule %d on EventScheduler", new_schedule_id)
    return {"success": True, "schedule_id": new_schedule_id, "scheduler_synced": scheduler_synced}

@router.post("/{persona_id}/schedules/{schedule_id}/toggle")
def toggle_schedule(
    persona_id: str,
    schedule_id: int,
    manager = Depends(get_manager)
):
    """Toggle schedule enabled status."""
    session = manager.SessionLocal()
    try:
        schedule = session.query(PersonaSchedule).filter(
            PersonaSchedule.SCHEDULE_ID == schedule_id,
            PersonaSchedule.PERSONA_ID == persona_id
        ).first()
        if not schedule:
            raise HTTPException(status_code=404, detail="Schedule not found")
        
        schedule.ENABLED = not schedule.ENABLED
        # W3 A12 (D2): 発火に影響する設定変更は同一 commit で世代 +1。
        # サーバー側インクリメント (Codex W3 第七陣) — read-modify-write だと
        # 並行する二更新が同じ世代番号を得て、reconciliation が「予約=DB 世代」
        # の偽同期判定をする (lost update)。SQL 式なら DB が直列化して必ず別番号。
        schedule.SYNC_GENERATION = func.coalesce(PersonaSchedule.SYNC_GENERATION, 0) + 1
        session.commit()
        new_enabled = schedule.ENABLED
    finally:
        session.close()

    # Phase 4-e: トグル結果に応じて EventScheduler を更新。
    # W3 A12 (D7): 同期失敗は scheduler_synced=False で応答に明示 (HTTP は 200)。
    # register_schedule は tri-state: not_registrable も False に含める。
    scheduler_synced = True
    try:
        if new_enabled:
            result = manager.schedule_manager.register_schedule(schedule_id)
            scheduler_synced = result in ("registered", "no_reservation_needed")
        else:
            manager.schedule_manager.unregister_schedule(schedule_id)
    except Exception:
        scheduler_synced = False
        _log.exception("Failed to update EventScheduler for schedule %d", schedule_id)
    return {"success": True, "enabled": new_enabled, "scheduler_synced": scheduler_synced}

@router.put("/{persona_id}/schedules/{schedule_id}")
def update_schedule(
    persona_id: str,
    schedule_id: int,
    req: UpdateScheduleRequest,
    manager = Depends(get_manager)
):
    """Update an existing schedule."""
    session = manager.SessionLocal()
    try:
        schedule = session.query(PersonaSchedule).filter(
            PersonaSchedule.SCHEDULE_ID == schedule_id,
            PersonaSchedule.PERSONA_ID == persona_id
        ).first()
        if not schedule:
            raise HTTPException(status_code=404, detail="Schedule not found")

        # Update basic fields if provided
        if req.schedule_type is not None:
            schedule.SCHEDULE_TYPE = req.schedule_type
        if req.meta_playbook is not None:
            schedule.META_PLAYBOOK = req.meta_playbook
        if req.description is not None:
            schedule.DESCRIPTION = req.description
        if req.priority is not None:
            schedule.PRIORITY = req.priority
        if req.enabled is not None:
            schedule.ENABLED = req.enabled
        if req.args is not None:
            schedule.PLAYBOOK_PARAMS = json.dumps(req.args) if req.args else None

        # Update type-specific fields based on schedule type
        schedule_type = req.schedule_type if req.schedule_type is not None else schedule.SCHEDULE_TYPE

        if schedule_type == "periodic":
            if req.days_of_week is not None:
                schedule.DAYS_OF_WEEK = json.dumps(req.days_of_week) if req.days_of_week else None
            if req.time_of_day is not None:
                schedule.TIME_OF_DAY = req.time_of_day
            # Clear non-periodic fields
            schedule.SCHEDULED_DATETIME = None
            schedule.INTERVAL_SECONDS = None
            schedule.COMPLETED = False

        elif schedule_type == "oneshot":
            if req.scheduled_datetime is not None:
                try:
                    tz = _get_persona_timezone(manager, persona_id)
                    dt_naive = datetime.strptime(req.scheduled_datetime, "%Y-%m-%d %H:%M")
                    dt_local = dt_naive.replace(tzinfo=tz)
                    dt_utc = dt_local.astimezone(timezone.utc)
                    schedule.SCHEDULED_DATETIME = dt_utc
                except ValueError:
                    raise HTTPException(status_code=400, detail="Invalid datetime format: YYYY-MM-DD HH:MM")
            # Clear non-oneshot fields
            schedule.DAYS_OF_WEEK = None
            schedule.TIME_OF_DAY = None
            schedule.INTERVAL_SECONDS = None

        elif schedule_type == "interval":
            if req.interval_seconds is not None:
                schedule.INTERVAL_SECONDS = req.interval_seconds
            # Clear non-interval fields
            schedule.DAYS_OF_WEEK = None
            schedule.TIME_OF_DAY = None
            schedule.SCHEDULED_DATETIME = None
            schedule.COMPLETED = False

        # W3 A12 (D2): 発火に影響する設定変更は同一 commit で世代 +1
        # (サーバー側インクリメント — toggle 側と同じ lost update 対策)
        schedule.SYNC_GENERATION = func.coalesce(PersonaSchedule.SYNC_GENERATION, 0) + 1
        session.commit()
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        session.close()

    # Phase 4-e: 更新内容で次回発火時刻が変わった可能性があるので再 register。
    # W3 A12 (D7): 同期失敗は scheduler_synced=False で応答に明示 (HTTP は 200)。
    # register_schedule は tri-state: not_registrable も False に含める。
    try:
        result = manager.schedule_manager.register_schedule(schedule_id)
        scheduler_synced = result in ("registered", "no_reservation_needed")
    except Exception:
        scheduler_synced = False
        _log.exception("Failed to re-register schedule %d on EventScheduler", schedule_id)
    return {"success": True, "schedule_id": schedule_id, "scheduler_synced": scheduler_synced}

@router.delete("/{persona_id}/schedules/{schedule_id}")
def delete_schedule(
    persona_id: str,
    schedule_id: int,
    manager = Depends(get_manager)
):
    """Delete a schedule."""
    session = manager.SessionLocal()
    try:
        schedule = session.query(PersonaSchedule).filter(
            PersonaSchedule.SCHEDULE_ID == schedule_id,
            PersonaSchedule.PERSONA_ID == persona_id
        ).first()
        if not schedule:
            raise HTTPException(status_code=404, detail="Schedule not found")

        session.delete(schedule)
        session.commit()
    finally:
        session.close()

    # Phase 4-e: 削除と同時に EventScheduler の予約も cancel。
    # W3 A12 (D7): 同期失敗は scheduler_synced=False で応答に明示 (HTTP は 200)。
    scheduler_synced = True
    try:
        manager.schedule_manager.unregister_schedule(schedule_id)
    except Exception:
        scheduler_synced = False
        _log.exception("Failed to unregister schedule %d from EventScheduler", schedule_id)
    return {"success": True, "scheduler_synced": scheduler_synced}
