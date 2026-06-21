"""Observer API — push 型データ受信 / Fixture & Observer CRUD / メトリクス参照。

push 型 Observer のデータ受信エンドポイントは Bearer token 認証付き。
token は環境変数 OBSERVER_PUSH_TOKEN で設定する。
"""
import os
import logging
from datetime import datetime, timezone
from typing import Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Header
from pydantic import BaseModel, Field

from api.deps import get_manager

LOGGER = logging.getLogger(__name__)
router = APIRouter()


# ------------------------------------------------------------------
# Auth
# ------------------------------------------------------------------

def _verify_push_token(authorization: Optional[str] = Header(None)) -> None:
    """Bearer token を検証する。OBSERVER_PUSH_TOKEN 環境変数と照合。"""
    expected = os.environ.get("OBSERVER_PUSH_TOKEN")
    if not expected:
        raise HTTPException(status_code=503, detail="OBSERVER_PUSH_TOKEN not configured")
    if not authorization:
        raise HTTPException(status_code=401, detail="Authorization header required")
    parts = authorization.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise HTTPException(status_code=401, detail="Invalid authorization format")
    if parts[1] != expected:
        raise HTTPException(status_code=403, detail="Invalid token")


# ------------------------------------------------------------------
# Request / Response models
# ------------------------------------------------------------------

class MetricValue(BaseModel):
    value_num: Optional[float] = None
    value_text: Optional[str] = None


class PushMetricsRequest(BaseModel):
    metrics: Dict[str, MetricValue]
    recorded_at: Optional[str] = None


class CreateFixtureRequest(BaseModel):
    fixture_id: str
    building_id: str
    name: str
    type: str = "object"
    description: str = ""
    state_json: Optional[str] = None
    source_context: Optional[str] = None


class CreateObserverRequest(BaseModel):
    observer_id: str
    fixture_id: str
    exec_kind: str = "push"
    exec_target: Optional[str] = None
    exec_args_json: Optional[str] = None
    interval_sec: Optional[int] = None
    metric_keys_json: Optional[str] = None
    notify_rules_json: Optional[str] = None
    enabled: bool = True


class MetricHistoryQuery(BaseModel):
    metric_name: str
    limit: int = Field(default=100, le=1000)


# ------------------------------------------------------------------
# Push endpoint (authenticated)
# ------------------------------------------------------------------

@router.post("/{observer_id}/push")
def push_metrics(
    observer_id: str,
    body: PushMetricsRequest,
    manager=Depends(get_manager),
    _auth: None = Depends(_verify_push_token),
):
    """外部アプリから Observer にメトリクスを push する。"""
    obs_mgr = manager.observer_manager
    if not obs_mgr:
        raise HTTPException(status_code=503, detail="Observer manager not available")

    config = obs_mgr.get_observer(observer_id)
    if not config:
        raise HTTPException(status_code=404, detail=f"Observer not found: {observer_id}")
    if config.EXEC_KIND != "push":
        raise HTTPException(status_code=400, detail="Observer is not push type")

    recorded_at = None
    if body.recorded_at:
        try:
            recorded_at = datetime.fromisoformat(body.recorded_at)
            if recorded_at.tzinfo is not None:
                recorded_at = recorded_at.astimezone(timezone.utc).replace(tzinfo=None)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid recorded_at format")

    metrics_dict = {k: v.model_dump() for k, v in body.metrics.items()}
    result = obs_mgr.record_metrics(observer_id, metrics_dict, recorded_at)

    return {"status": "ok", "recorded": len(result)}


# ------------------------------------------------------------------
# Fixture CRUD
# ------------------------------------------------------------------

@router.post("/fixture")
def create_fixture(body: CreateFixtureRequest, manager=Depends(get_manager)):
    """Fixture を作成 (upsert) する。"""
    obs_mgr = manager.observer_manager
    if not obs_mgr:
        raise HTTPException(status_code=503, detail="Observer manager not available")

    obs_mgr.create_fixture(
        fixture_id=body.fixture_id,
        building_id=body.building_id,
        name=body.name,
        fixture_type=body.type,
        description=body.description,
        state_json=body.state_json,
        source_context=body.source_context,
    )
    return {"status": "ok", "fixture_id": body.fixture_id}


@router.get("/fixture/{fixture_id}")
def get_fixture(fixture_id: str, manager=Depends(get_manager)):
    """Fixture の情報を取得する。"""
    obs_mgr = manager.observer_manager
    if not obs_mgr:
        raise HTTPException(status_code=503, detail="Observer manager not available")

    fixture = obs_mgr.get_fixture(fixture_id)
    if not fixture:
        raise HTTPException(status_code=404, detail="Fixture not found")
    return {
        "fixture_id": fixture.FIXTURE_ID,
        "building_id": fixture.BUILDING_ID,
        "name": fixture.NAME,
        "type": fixture.TYPE,
        "description": fixture.DESCRIPTION,
        "state_json": fixture.STATE_JSON,
    }


@router.get("/building/{building_id}/fixtures")
def get_building_fixtures(building_id: str, manager=Depends(get_manager)):
    """Building に設置された Fixture の一覧を取得する。"""
    obs_mgr = manager.observer_manager
    if not obs_mgr:
        raise HTTPException(status_code=503, detail="Observer manager not available")

    fixtures = obs_mgr.get_building_fixtures(building_id)
    return [
        {
            "fixture_id": f.FIXTURE_ID,
            "name": f.NAME,
            "type": f.TYPE,
            "description": f.DESCRIPTION,
        }
        for f in fixtures
    ]


# ------------------------------------------------------------------
# Observer CRUD
# ------------------------------------------------------------------

@router.post("/config")
def create_observer(body: CreateObserverRequest, manager=Depends(get_manager)):
    """Observer を作成 (upsert) する。"""
    obs_mgr = manager.observer_manager
    if not obs_mgr:
        raise HTTPException(status_code=503, detail="Observer manager not available")

    obs_mgr.create_observer(
        observer_id=body.observer_id,
        fixture_id=body.fixture_id,
        exec_kind=body.exec_kind,
        exec_target=body.exec_target,
        exec_args_json=body.exec_args_json,
        interval_sec=body.interval_sec,
        metric_keys_json=body.metric_keys_json,
        notify_rules_json=body.notify_rules_json,
        enabled=body.enabled,
    )
    return {"status": "ok", "observer_id": body.observer_id}


# ------------------------------------------------------------------
# Metrics query
# ------------------------------------------------------------------

@router.get("/{observer_id}/latest")
def get_latest_metrics(observer_id: str, manager=Depends(get_manager)):
    """Observer の最新メトリクス (STATE_JSON キャッシュ) を取得する。"""
    obs_mgr = manager.observer_manager
    if not obs_mgr:
        raise HTTPException(status_code=503, detail="Observer manager not available")

    metrics = obs_mgr.get_latest_metrics(observer_id)
    if not metrics:
        raise HTTPException(status_code=404, detail="No metrics found")
    return metrics


@router.get("/{observer_id}/history/{metric_name}")
def get_metric_history(
    observer_id: str,
    metric_name: str,
    limit: int = 100,
    manager=Depends(get_manager),
):
    """Observer の指定メトリクスの履歴を取得する。"""
    obs_mgr = manager.observer_manager
    if not obs_mgr:
        raise HTTPException(status_code=503, detail="Observer manager not available")

    history = obs_mgr.get_metric_history(observer_id, metric_name, limit=min(limit, 1000))
    return {"metric_name": metric_name, "history": history}


