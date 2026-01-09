"""
api.routes.phenomena ― フェノメノンルール管理API

フェノメノンルールのCRUD操作とメタ情報の取得を提供する。
"""
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from typing import Any, Dict, List, Optional
import json
import logging

from api.deps import get_manager
from database.models import PhenomenonRule
from phenomena import PHENOMENON_REGISTRY, PHENOMENON_SCHEMAS
from phenomena.triggers import TriggerType, TRIGGER_SCHEMAS

router = APIRouter()
LOGGER = logging.getLogger(__name__)


class PhenomenonRuleCreate(BaseModel):
    trigger_type: str
    condition_json: Optional[str] = None
    phenomenon_name: str
    argument_mapping_json: Optional[str] = None
    enabled: bool = True
    priority: int = 0
    description: str = ""


class PhenomenonRuleUpdate(BaseModel):
    trigger_type: Optional[str] = None
    condition_json: Optional[str] = None
    phenomenon_name: Optional[str] = None
    argument_mapping_json: Optional[str] = None
    enabled: Optional[bool] = None
    priority: Optional[int] = None
    description: Optional[str] = None


@router.get("/rules")
def list_phenomenon_rules(manager=Depends(get_manager)):
    """フェノメノンルール一覧を取得"""
    session = manager.SessionLocal()
    try:
        rules = session.query(PhenomenonRule).order_by(PhenomenonRule.PRIORITY.desc()).all()
        return [
            {
                "rule_id": r.RULE_ID,
                "trigger_type": r.TRIGGER_TYPE,
                "condition_json": r.CONDITION_JSON,
                "phenomenon_name": r.PHENOMENON_NAME,
                "argument_mapping_json": r.ARGUMENT_MAPPING_JSON,
                "enabled": r.ENABLED,
                "priority": r.PRIORITY,
                "description": r.DESCRIPTION,
                "created_at": r.CREATED_AT.isoformat() if r.CREATED_AT else None,
                "updated_at": r.UPDATED_AT.isoformat() if r.UPDATED_AT else None,
            }
            for r in rules
        ]
    finally:
        session.close()


@router.get("/rules/{rule_id}")
def get_phenomenon_rule(rule_id: int, manager=Depends(get_manager)):
    """特定のフェノメノンルールを取得"""
    session = manager.SessionLocal()
    try:
        rule = session.query(PhenomenonRule).filter(PhenomenonRule.RULE_ID == rule_id).first()
        if not rule:
            raise HTTPException(status_code=404, detail="Rule not found")
        return {
            "rule_id": rule.RULE_ID,
            "trigger_type": rule.TRIGGER_TYPE,
            "condition_json": rule.CONDITION_JSON,
            "phenomenon_name": rule.PHENOMENON_NAME,
            "argument_mapping_json": rule.ARGUMENT_MAPPING_JSON,
            "enabled": rule.ENABLED,
            "priority": rule.PRIORITY,
            "description": rule.DESCRIPTION,
            "created_at": rule.CREATED_AT.isoformat() if rule.CREATED_AT else None,
            "updated_at": rule.UPDATED_AT.isoformat() if rule.UPDATED_AT else None,
        }
    finally:
        session.close()


@router.post("/rules")
def create_phenomenon_rule(data: PhenomenonRuleCreate, manager=Depends(get_manager)):
    """新しいフェノメノンルールを作成"""

    # Validate trigger type
    valid_triggers = [t.value for t in TriggerType]
    if data.trigger_type not in valid_triggers:
        raise HTTPException(status_code=400, detail=f"Invalid trigger_type. Valid types: {valid_triggers}")

    # Validate phenomenon name
    if data.phenomenon_name not in PHENOMENON_REGISTRY:
        available = list(PHENOMENON_REGISTRY.keys())
        raise HTTPException(status_code=400, detail=f"Invalid phenomenon_name. Available: {available}")

    # Validate JSON fields
    if data.condition_json:
        try:
            json.loads(data.condition_json)
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="Invalid condition_json format")

    if data.argument_mapping_json:
        try:
            json.loads(data.argument_mapping_json)
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="Invalid argument_mapping_json format")

    session = manager.SessionLocal()
    try:
        rule = PhenomenonRule(
            TRIGGER_TYPE=data.trigger_type,
            CONDITION_JSON=data.condition_json,
            PHENOMENON_NAME=data.phenomenon_name,
            ARGUMENT_MAPPING_JSON=data.argument_mapping_json,
            ENABLED=data.enabled,
            PRIORITY=data.priority,
            DESCRIPTION=data.description,
        )
        session.add(rule)
        session.commit()
        session.refresh(rule)
        return {"rule_id": rule.RULE_ID, "message": "Rule created successfully"}
    except Exception as e:
        session.rollback()
        LOGGER.error("Failed to create phenomenon rule: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        session.close()


@router.put("/rules/{rule_id}")
def update_phenomenon_rule(rule_id: int, data: PhenomenonRuleUpdate, manager=Depends(get_manager)):
    """フェノメノンルールを更新"""
    session = manager.SessionLocal()
    try:
        rule = session.query(PhenomenonRule).filter(PhenomenonRule.RULE_ID == rule_id).first()
        if not rule:
            raise HTTPException(status_code=404, detail="Rule not found")

        if data.trigger_type is not None:
            valid_triggers = [t.value for t in TriggerType]
            if data.trigger_type not in valid_triggers:
                raise HTTPException(status_code=400, detail=f"Invalid trigger_type. Valid types: {valid_triggers}")
            rule.TRIGGER_TYPE = data.trigger_type

        if data.phenomenon_name is not None:
            if data.phenomenon_name not in PHENOMENON_REGISTRY:
                available = list(PHENOMENON_REGISTRY.keys())
                raise HTTPException(status_code=400, detail=f"Invalid phenomenon_name. Available: {available}")
            rule.PHENOMENON_NAME = data.phenomenon_name

        if data.condition_json is not None:
            if data.condition_json:
                try:
                    json.loads(data.condition_json)
                except json.JSONDecodeError:
                    raise HTTPException(status_code=400, detail="Invalid condition_json format")
            rule.CONDITION_JSON = data.condition_json

        if data.argument_mapping_json is not None:
            if data.argument_mapping_json:
                try:
                    json.loads(data.argument_mapping_json)
                except json.JSONDecodeError:
                    raise HTTPException(status_code=400, detail="Invalid argument_mapping_json format")
            rule.ARGUMENT_MAPPING_JSON = data.argument_mapping_json

        if data.enabled is not None:
            rule.ENABLED = data.enabled
        if data.priority is not None:
            rule.PRIORITY = data.priority
        if data.description is not None:
            rule.DESCRIPTION = data.description

        session.commit()
        return {"message": "Rule updated successfully"}
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        LOGGER.error("Failed to update phenomenon rule: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        session.close()


@router.delete("/rules/{rule_id}")
def delete_phenomenon_rule(rule_id: int, manager=Depends(get_manager)):
    """フェノメノンルールを削除"""
    session = manager.SessionLocal()
    try:
        rule = session.query(PhenomenonRule).filter(PhenomenonRule.RULE_ID == rule_id).first()
        if not rule:
            raise HTTPException(status_code=404, detail="Rule not found")
        session.delete(rule)
        session.commit()
        return {"message": "Rule deleted successfully"}
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        LOGGER.error("Failed to delete phenomenon rule: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        session.close()


@router.get("/available")
def list_available_phenomena():
    """利用可能なフェノメノン一覧を取得"""
    return [
        {
            "name": schema.name,
            "description": schema.description,
            "parameters": schema.parameters,
            "is_async": schema.is_async,
        }
        for schema in PHENOMENON_SCHEMAS
    ]


@router.get("/triggers")
def list_trigger_types():
    """利用可能なトリガータイプ一覧を取得"""
    return [
        {
            "type": trigger_type.value,
            "fields": fields,
        }
        for trigger_type, fields in TRIGGER_SCHEMAS.items()
    ]
