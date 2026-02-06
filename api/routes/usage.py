"""
api.routes.usage ― LLM使用量モニタリングAPI

使用量データの取得と集計を提供する。
"""
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from typing import Any, Dict, List, Optional
from datetime import datetime, date, timedelta
import logging

from sqlalchemy import func
from api.deps import get_manager
from database.models import LLMUsageLog, AI
from model_configs import get_model_pricing, get_model_display_name, MODEL_CONFIGS

router = APIRouter()
LOGGER = logging.getLogger(__name__)


class UsageSummary(BaseModel):
    """使用量サマリー"""
    total_cost_usd: float
    total_input_tokens: int
    total_output_tokens: int
    call_count: int


class DailyUsage(BaseModel):
    """日別使用量"""
    date: str  # YYYY-MM-DD
    model_id: str
    model_display_name: str
    cost_usd: float
    input_tokens: int
    output_tokens: int
    call_count: int


class ModelInfo(BaseModel):
    """モデル情報"""
    model_id: str
    display_name: str
    provider: str
    input_per_1m_tokens: Optional[float] = None
    output_per_1m_tokens: Optional[float] = None
    currency: str = "USD"


@router.get("/summary", response_model=UsageSummary)
def get_usage_summary(
    days: int = Query(30, ge=1, le=365, description="過去何日分を集計するか"),
    persona_id: Optional[str] = Query(None, description="ペルソナIDでフィルタ"),
    category: Optional[str] = Query(None, description="カテゴリでフィルタ"),
    manager=Depends(get_manager),
):
    """使用量サマリーを取得"""
    session = manager.SessionLocal()
    try:
        start_date = datetime.now() - timedelta(days=days)
        query = session.query(
            func.coalesce(func.sum(LLMUsageLog.COST_USD), 0.0).label("total_cost"),
            func.coalesce(func.sum(LLMUsageLog.INPUT_TOKENS), 0).label("total_input"),
            func.coalesce(func.sum(LLMUsageLog.OUTPUT_TOKENS), 0).label("total_output"),
            func.count(LLMUsageLog.ID).label("call_count"),
        ).filter(LLMUsageLog.TIMESTAMP >= start_date)

        if persona_id:
            query = query.filter(LLMUsageLog.PERSONA_ID == persona_id)
        if category:
            query = query.filter(LLMUsageLog.CATEGORY == category)

        result = query.one()
        return UsageSummary(
            total_cost_usd=float(result.total_cost or 0),
            total_input_tokens=int(result.total_input or 0),
            total_output_tokens=int(result.total_output or 0),
            call_count=int(result.call_count or 0),
        )
    finally:
        session.close()


@router.get("/daily", response_model=List[DailyUsage])
def get_daily_usage(
    start_date: Optional[str] = Query(None, description="開始日 (YYYY-MM-DD)"),
    end_date: Optional[str] = Query(None, description="終了日 (YYYY-MM-DD)"),
    persona_id: Optional[str] = Query(None, description="ペルソナIDでフィルタ"),
    category: Optional[str] = Query(None, description="カテゴリでフィルタ"),
    manager=Depends(get_manager),
):
    """日別・モデル別の使用量を取得（グラフ用）"""
    session = manager.SessionLocal()
    try:
        # デフォルトは過去30日
        if end_date:
            end_dt = datetime.strptime(end_date, "%Y-%m-%d") + timedelta(days=1)
        else:
            end_dt = datetime.now() + timedelta(days=1)

        if start_date:
            start_dt = datetime.strptime(start_date, "%Y-%m-%d")
        else:
            start_dt = end_dt - timedelta(days=30)

        # Use func.date() for SQLite compatibility
        date_expr = func.date(LLMUsageLog.TIMESTAMP)
        query = session.query(
            date_expr.label("date"),
            LLMUsageLog.MODEL_ID,
            func.coalesce(func.sum(LLMUsageLog.COST_USD), 0.0).label("cost"),
            func.coalesce(func.sum(LLMUsageLog.INPUT_TOKENS), 0).label("input_tokens"),
            func.coalesce(func.sum(LLMUsageLog.OUTPUT_TOKENS), 0).label("output_tokens"),
            func.count(LLMUsageLog.ID).label("call_count"),
        ).filter(
            LLMUsageLog.TIMESTAMP >= start_dt,
            LLMUsageLog.TIMESTAMP < end_dt,
        ).group_by(
            date_expr,
            LLMUsageLog.MODEL_ID,
        ).order_by(
            date_expr,
        )

        if persona_id:
            query = query.filter(LLMUsageLog.PERSONA_ID == persona_id)
        if category:
            query = query.filter(LLMUsageLog.CATEGORY == category)

        results = query.all()
        return [
            DailyUsage(
                date=str(r.date),
                model_id=r.MODEL_ID,
                model_display_name=get_model_display_name(r.MODEL_ID),
                cost_usd=float(r.cost or 0),
                input_tokens=int(r.input_tokens or 0),
                output_tokens=int(r.output_tokens or 0),
                call_count=int(r.call_count or 0),
            )
            for r in results
        ]
    finally:
        session.close()


@router.get("/by-persona")
def get_usage_by_persona(
    days: int = Query(30, ge=1, le=365, description="過去何日分を集計するか"),
    manager=Depends(get_manager),
):
    """ペルソナ別の使用量を取得"""
    session = manager.SessionLocal()
    try:
        start_date = datetime.now() - timedelta(days=days)
        query = session.query(
            LLMUsageLog.PERSONA_ID,
            func.coalesce(func.sum(LLMUsageLog.COST_USD), 0.0).label("total_cost"),
            func.coalesce(func.sum(LLMUsageLog.INPUT_TOKENS), 0).label("total_input"),
            func.coalesce(func.sum(LLMUsageLog.OUTPUT_TOKENS), 0).label("total_output"),
            func.count(LLMUsageLog.ID).label("call_count"),
        ).filter(
            LLMUsageLog.TIMESTAMP >= start_date,
        ).group_by(
            LLMUsageLog.PERSONA_ID,
        ).order_by(
            func.sum(LLMUsageLog.COST_USD).desc(),
        )

        results = query.all()

        # ペルソナ名を取得
        persona_names = {}
        persona_ids = [r.PERSONA_ID for r in results if r.PERSONA_ID]
        if persona_ids:
            personas = session.query(AI.AIID, AI.AINAME).filter(AI.AIID.in_(persona_ids)).all()
            persona_names = {p.AIID: p.AINAME for p in personas}

        return [
            {
                "persona_id": r.PERSONA_ID or "system",
                "persona_name": persona_names.get(r.PERSONA_ID, "System/User") if r.PERSONA_ID else "System/User",
                "total_cost_usd": float(r.total_cost or 0),
                "total_input_tokens": int(r.total_input or 0),
                "total_output_tokens": int(r.total_output or 0),
                "call_count": int(r.call_count or 0),
            }
            for r in results
        ]
    finally:
        session.close()


@router.get("/models", response_model=List[ModelInfo])
def get_model_pricing_info():
    """モデル一覧と料金情報を取得"""
    result = []
    for model_id, config in MODEL_CONFIGS.items():
        pricing = config.get("pricing", {})
        result.append(ModelInfo(
            model_id=model_id,
            display_name=config.get("display_name", model_id),
            provider=config.get("provider", "unknown"),
            input_per_1m_tokens=pricing.get("input_per_1m_tokens"),
            output_per_1m_tokens=pricing.get("output_per_1m_tokens"),
            currency=pricing.get("currency", "USD"),
        ))
    return result


@router.get("/personas")
def get_personas_list(manager=Depends(get_manager)):
    """使用量フィルタ用のペルソナ一覧を取得"""
    session = manager.SessionLocal()
    try:
        personas = session.query(AI.AIID, AI.AINAME).all()
        return [
            {"persona_id": p.AIID, "persona_name": p.AINAME}
            for p in personas
        ]
    finally:
        session.close()


@router.get("/categories")
def get_categories_list(manager=Depends(get_manager)):
    """使用量フィルタ用のカテゴリ一覧を取得"""
    session = manager.SessionLocal()
    try:
        # Get distinct categories from usage log
        results = session.query(LLMUsageLog.CATEGORY).distinct().filter(
            LLMUsageLog.CATEGORY.isnot(None)
        ).all()
        categories = [r.CATEGORY for r in results if r.CATEGORY]
        # Add display names for known categories
        category_display = {
            "persona_speak": "Persona Speech",
            "memory_weave_generate": "Memory Weave (Generate)",
        }
        return [
            {
                "category_id": cat,
                "category_name": category_display.get(cat, cat),
            }
            for cat in sorted(categories)
        ]
    finally:
        session.close()


@router.get("/by-category")
def get_usage_by_category(
    days: int = Query(30, ge=1, le=365, description="過去何日分を集計するか"),
    persona_id: Optional[str] = Query(None, description="ペルソナIDでフィルタ"),
    manager=Depends(get_manager),
):
    """カテゴリ別の使用量を取得"""
    session = manager.SessionLocal()
    try:
        start_date = datetime.now() - timedelta(days=days)
        query = session.query(
            LLMUsageLog.CATEGORY,
            func.coalesce(func.sum(LLMUsageLog.COST_USD), 0.0).label("total_cost"),
            func.coalesce(func.sum(LLMUsageLog.INPUT_TOKENS), 0).label("total_input"),
            func.coalesce(func.sum(LLMUsageLog.OUTPUT_TOKENS), 0).label("total_output"),
            func.count(LLMUsageLog.ID).label("call_count"),
        ).filter(
            LLMUsageLog.TIMESTAMP >= start_date,
        ).group_by(
            LLMUsageLog.CATEGORY,
        ).order_by(
            func.sum(LLMUsageLog.COST_USD).desc(),
        )

        if persona_id:
            query = query.filter(LLMUsageLog.PERSONA_ID == persona_id)

        results = query.all()

        # Add display names for known categories
        category_display = {
            "persona_speak": "Persona Speech",
            "memory_weave_generate": "Memory Weave (Generate)",
        }

        return [
            {
                "category": r.CATEGORY or "uncategorized",
                "category_name": category_display.get(r.CATEGORY, r.CATEGORY or "Uncategorized"),
                "total_cost_usd": float(r.total_cost or 0),
                "total_input_tokens": int(r.total_input or 0),
                "total_output_tokens": int(r.total_output or 0),
                "call_count": int(r.call_count or 0),
            }
            for r in results
        ]
    finally:
        session.close()
