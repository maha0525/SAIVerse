"""Autonomy manager API endpoints for controlling persona autonomous behavior."""
import json
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from api.deps import get_manager
from database.models import AI

LOGGER = logging.getLogger(__name__)

router = APIRouter()


def _persist_interval_to_judgment_config(manager, persona_id: str, interval_minutes: float) -> None:
    """interval_minutes を AI.META_JUDGMENT_CONFIG.periodic_interval_minutes に永続化する。

    Phase 4-e: 自律行動マネージャーの interval 変更を再起動後も保持するため、
    META_JUDGMENT_CONFIG (DB) の永続値として書き込む。AutonomyManager は起動時に
    この値を読んで初期 interval として使う。
    """
    db = manager.SessionLocal()
    try:
        ai = db.query(AI).filter(AI.AIID == persona_id).first()
        if ai is None:
            LOGGER.warning(
                "[autonomy] Cannot persist interval: persona %s not found in DB",
                persona_id,
            )
            return
        config_dict: dict = {}
        if ai.META_JUDGMENT_CONFIG:
            try:
                parsed = json.loads(ai.META_JUDGMENT_CONFIG)
                if isinstance(parsed, dict):
                    config_dict = parsed
            except (json.JSONDecodeError, TypeError):
                LOGGER.warning(
                    "[autonomy] Existing META_JUDGMENT_CONFIG for %s is invalid JSON; rebuilding",
                    persona_id,
                )
        config_dict["periodic_interval_minutes"] = int(interval_minutes)
        ai.META_JUDGMENT_CONFIG = json.dumps(config_dict, ensure_ascii=False)
        db.commit()
        LOGGER.debug(
            "[autonomy] Persisted periodic_interval_minutes=%d for %s",
            int(interval_minutes), persona_id,
        )
    except Exception:
        db.rollback()
        LOGGER.exception(
            "[autonomy] Failed to persist interval for %s", persona_id,
        )
    finally:
        db.close()


class AutonomyStatusResponse(BaseModel):
    persona_id: str
    state: str
    interval_minutes: float
    decision_model: Optional[str] = None  # Phase C-2 移行で no-op (互換のため残存)
    execution_model: Optional[str] = None  # Phase C-2 移行で no-op (互換のため残存)
    current_cycle_id: Optional[str] = None
    last_report: Optional[dict] = None


class AutonomyStartRequest(BaseModel):
    interval_minutes: float = 5.0
    decision_model: Optional[str] = None
    execution_model: Optional[str] = None


class AutonomyConfigRequest(BaseModel):
    interval_minutes: Optional[float] = None
    decision_model: Optional[str] = None
    execution_model: Optional[str] = None


class AutonomyActionResponse(BaseModel):
    success: bool
    message: str


def _get_or_create_autonomy(persona_id: str, manager):
    """Get or create an AutonomyManager for a persona."""
    from saiverse.autonomy_manager import AutonomyManager

    if not hasattr(manager, "_autonomy_managers"):
        manager._autonomy_managers = {}

    if persona_id not in manager._autonomy_managers:
        manager._autonomy_managers[persona_id] = AutonomyManager(
            persona_id=persona_id,
            manager=manager,
        )

    return manager._autonomy_managers[persona_id]


@router.get("/{persona_id}/autonomy", response_model=AutonomyStatusResponse)
def get_autonomy_status(
    persona_id: str,
    manager=Depends(get_manager),
):
    """Get current autonomy status for a persona."""
    am = _get_or_create_autonomy(persona_id, manager)
    return AutonomyStatusResponse(**am.get_status())


@router.post("/{persona_id}/autonomy/start", response_model=AutonomyActionResponse)
def start_autonomy(
    persona_id: str,
    request: AutonomyStartRequest,
    manager=Depends(get_manager),
):
    """Start autonomous behavior for a persona."""
    am = _get_or_create_autonomy(persona_id, manager)
    am.set_interval(request.interval_minutes)
    # Phase 4-e: interval を META_JUDGMENT_CONFIG に永続化 (再起動後も保持)
    _persist_interval_to_judgment_config(manager, persona_id, request.interval_minutes)
    if request.decision_model:
        am.set_models(decision_model=request.decision_model)
    if request.execution_model:
        am.set_models(execution_model=request.execution_model)

    success = am.start()
    if success:
        return AutonomyActionResponse(success=True, message="自律行動を開始しました")
    else:
        return AutonomyActionResponse(success=False, message="既に実行中です")


@router.post("/{persona_id}/autonomy/stop", response_model=AutonomyActionResponse)
def stop_autonomy(
    persona_id: str,
    manager=Depends(get_manager),
):
    """Stop autonomous behavior for a persona."""
    am = _get_or_create_autonomy(persona_id, manager)
    success = am.stop()
    if success:
        return AutonomyActionResponse(success=True, message="自律行動を停止しました")
    else:
        return AutonomyActionResponse(success=False, message="実行されていません")


@router.put("/{persona_id}/autonomy/config", response_model=AutonomyStatusResponse)
def update_autonomy_config(
    persona_id: str,
    request: AutonomyConfigRequest,
    manager=Depends(get_manager),
):
    """Update autonomy configuration."""
    am = _get_or_create_autonomy(persona_id, manager)
    if request.interval_minutes is not None:
        am.set_interval(request.interval_minutes)
        # Phase 4-e: interval を META_JUDGMENT_CONFIG に永続化
        _persist_interval_to_judgment_config(manager, persona_id, request.interval_minutes)
    am.set_models(
        decision_model=request.decision_model,
        execution_model=request.execution_model,
    )
    return AutonomyStatusResponse(**am.get_status())
