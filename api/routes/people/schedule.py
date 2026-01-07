from fastapi import APIRouter, Depends, HTTPException
from typing import List
from api.deps import get_manager
from .models import ScheduleItem, CreateScheduleRequest, UpdateScheduleRequest
from database.models import PersonaSchedule, AI as AIModel, City as CityModel
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import json

router = APIRouter()

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
    except:
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
                except: pass
            
            results.append(ScheduleItem(
                schedule_id=s.SCHEDULE_ID,
                schedule_type=s.SCHEDULE_TYPE,
                meta_playbook=s.META_PLAYBOOK,
                description=s.DESCRIPTION,
                priority=s.PRIORITY,
                enabled=s.ENABLED,
                days_of_week=days,
                time_of_day=s.TIME_OF_DAY,
                scheduled_datetime=s.SCHEDULED_DATETIME,
                interval_seconds=s.INTERVAL_SECONDS,
                last_executed_at=s.LAST_EXECUTED_AT,
                completed=s.COMPLETED
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
                except ValueError as e:
                    raise HTTPException(status_code=400, detail=f"Invalid datetime format: YYYY-MM-DD HH:MM")

        elif req.schedule_type == "interval":
            new_schedule.INTERVAL_SECONDS = req.interval_seconds

        session.add(new_schedule)
        session.commit()
        return {"success": True, "schedule_id": new_schedule.SCHEDULE_ID}
    except Exception as e:
        session.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        session.close()

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
        session.commit()
        return {"success": True, "enabled": schedule.ENABLED}
    finally:
        session.close()

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

        session.commit()
        return {"success": True, "schedule_id": schedule.SCHEDULE_ID}
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        session.close()

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
        return {"success": True}
    finally:
        session.close()
