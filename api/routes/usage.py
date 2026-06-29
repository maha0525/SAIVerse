"""
api.routes.usage ― LLM使用量モニタリングAPI

使用量データの取得と集計を提供する。
"""
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from typing import Any, Dict, List, Optional
from datetime import datetime, date, timedelta, timezone
import logging

from sqlalchemy import func
from api.deps import get_manager
from database.models import LLMUsageLog, AI
from saiverse.model_configs import get_model_pricing, get_model_display_name, MODEL_CONFIGS, get_rate_limit_config

router = APIRouter()
LOGGER = logging.getLogger(__name__)


class CostByCurrency(BaseModel):
    """通貨別コスト"""
    currency: str
    total_cost: float

class UsageSummary(BaseModel):
    """使用量サマリー"""
    total_cost_usd: float
    costs_by_currency: List[CostByCurrency] = []
    total_input_tokens: int
    total_output_tokens: int
    call_count: int


class DailyUsage(BaseModel):
    """日別使用量"""
    date: str  # YYYY-MM-DD
    model_id: str
    model_display_name: str
    cost_usd: float
    currency: str = "USD"
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

        base_filter = [LLMUsageLog.TIMESTAMP >= start_date]
        if persona_id:
            base_filter.append(LLMUsageLog.PERSONA_ID == persona_id)
        if category:
            base_filter.append(LLMUsageLog.CATEGORY == category)

        # Token totals and call count (currency-independent)
        totals = session.query(
            func.coalesce(func.sum(LLMUsageLog.INPUT_TOKENS), 0).label("total_input"),
            func.coalesce(func.sum(LLMUsageLog.OUTPUT_TOKENS), 0).label("total_output"),
            func.count(LLMUsageLog.ID).label("call_count"),
        ).filter(*base_filter).one()

        # Cost grouped by currency
        cost_rows = session.query(
            func.coalesce(LLMUsageLog.CURRENCY, "USD").label("currency"),
            func.coalesce(func.sum(LLMUsageLog.COST_USD), 0.0).label("total_cost"),
        ).filter(*base_filter).group_by(
            func.coalesce(LLMUsageLog.CURRENCY, "USD"),
        ).all()

        costs_by_currency = [
            CostByCurrency(currency=r.currency, total_cost=float(r.total_cost or 0))
            for r in cost_rows if float(r.total_cost or 0) > 0
        ]
        # total_cost_usd: only USD rows (backward compat)
        usd_total = sum(c.total_cost for c in costs_by_currency if c.currency == "USD")

        return UsageSummary(
            total_cost_usd=usd_total,
            costs_by_currency=costs_by_currency,
            total_input_tokens=int(totals.total_input or 0),
            total_output_tokens=int(totals.total_output or 0),
            call_count=int(totals.call_count or 0),
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

        # Build currency lookup: model_id -> currency
        model_currencies: Dict[str, str] = {}
        for r in results:
            if r.MODEL_ID not in model_currencies:
                p = get_model_pricing(r.MODEL_ID)
                model_currencies[r.MODEL_ID] = p.get("currency", "USD") if p else "USD"

        return [
            DailyUsage(
                date=str(r.date),
                model_id=r.MODEL_ID,
                model_display_name=get_model_display_name(r.MODEL_ID),
                cost_usd=float(r.cost or 0),
                currency=model_currencies.get(r.MODEL_ID, "USD"),
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

        # Token/call totals per persona
        query = session.query(
            LLMUsageLog.PERSONA_ID,
            func.coalesce(func.sum(LLMUsageLog.INPUT_TOKENS), 0).label("total_input"),
            func.coalesce(func.sum(LLMUsageLog.OUTPUT_TOKENS), 0).label("total_output"),
            func.count(LLMUsageLog.ID).label("call_count"),
        ).filter(
            LLMUsageLog.TIMESTAMP >= start_date,
        ).group_by(
            LLMUsageLog.PERSONA_ID,
        )
        totals_by_persona = {r.PERSONA_ID: r for r in query.all()}

        # Cost per persona per currency
        cost_query = session.query(
            LLMUsageLog.PERSONA_ID,
            func.coalesce(LLMUsageLog.CURRENCY, "USD").label("currency"),
            func.coalesce(func.sum(LLMUsageLog.COST_USD), 0.0).label("total_cost"),
        ).filter(
            LLMUsageLog.TIMESTAMP >= start_date,
        ).group_by(
            LLMUsageLog.PERSONA_ID,
            func.coalesce(LLMUsageLog.CURRENCY, "USD"),
        )
        costs_map: Dict[Optional[str], List[Dict[str, Any]]] = {}
        for r in cost_query.all():
            cost_val = float(r.total_cost or 0)
            if cost_val > 0:
                costs_map.setdefault(r.PERSONA_ID, []).append(
                    {"currency": r.currency, "total_cost": cost_val}
                )

        # ペルソナ名を取得
        persona_names = {}
        persona_ids = [pid for pid in totals_by_persona if pid]
        if persona_ids:
            personas = session.query(AI.AIID, AI.AINAME).filter(AI.AIID.in_(persona_ids)).all()
            persona_names = {p.AIID: p.AINAME for p in personas}

        result = []
        for pid, r in totals_by_persona.items():
            costs = costs_map.get(pid, [])
            usd_total = sum(c["total_cost"] for c in costs if c["currency"] == "USD")
            result.append({
                "persona_id": pid or "system",
                "persona_name": persona_names.get(pid, "System/User") if pid else "System/User",
                "total_cost_usd": usd_total,
                "costs_by_currency": costs,
                "total_input_tokens": int(r.total_input or 0),
                "total_output_tokens": int(r.total_output or 0),
                "call_count": int(r.call_count or 0),
            })
        result.sort(key=lambda x: sum(c["total_cost"] for c in x["costs_by_currency"]), reverse=True)
        return result
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

        base_filter = [LLMUsageLog.TIMESTAMP >= start_date]
        if persona_id:
            base_filter.append(LLMUsageLog.PERSONA_ID == persona_id)

        # Token/call totals per category
        query = session.query(
            LLMUsageLog.CATEGORY,
            func.coalesce(func.sum(LLMUsageLog.INPUT_TOKENS), 0).label("total_input"),
            func.coalesce(func.sum(LLMUsageLog.OUTPUT_TOKENS), 0).label("total_output"),
            func.count(LLMUsageLog.ID).label("call_count"),
        ).filter(*base_filter).group_by(LLMUsageLog.CATEGORY)
        totals_by_cat = {r.CATEGORY: r for r in query.all()}

        # Cost per category per currency
        cost_query = session.query(
            LLMUsageLog.CATEGORY,
            func.coalesce(LLMUsageLog.CURRENCY, "USD").label("currency"),
            func.coalesce(func.sum(LLMUsageLog.COST_USD), 0.0).label("total_cost"),
        ).filter(*base_filter).group_by(
            LLMUsageLog.CATEGORY,
            func.coalesce(LLMUsageLog.CURRENCY, "USD"),
        )
        costs_map: Dict[Optional[str], List[Dict[str, Any]]] = {}
        for r in cost_query.all():
            cost_val = float(r.total_cost or 0)
            if cost_val > 0:
                costs_map.setdefault(r.CATEGORY, []).append(
                    {"currency": r.currency, "total_cost": cost_val}
                )

        category_display = {
            "persona_speak": "Persona Speech",
            "memory_weave_generate": "Memory Weave (Generate)",
        }

        result = []
        for cat, r in totals_by_cat.items():
            costs = costs_map.get(cat, [])
            usd_total = sum(c["total_cost"] for c in costs if c["currency"] == "USD")
            result.append({
                "category": cat or "uncategorized",
                "category_name": category_display.get(cat, cat or "Uncategorized"),
                "total_cost_usd": usd_total,
                "costs_by_currency": costs,
                "total_input_tokens": int(r.total_input or 0),
                "total_output_tokens": int(r.total_output or 0),
                "call_count": int(r.call_count or 0),
            })
        result.sort(key=lambda x: sum(c["total_cost"] for c in x["costs_by_currency"]), reverse=True)
        return result
    finally:
        session.close()


def _get_rpd_period_start(reset_timezone: str) -> datetime:
    """Calculate the start of the current RPD period based on reset timezone.

    For Gemini, RPD resets at midnight Pacific Time.
    Returns a timezone-naive UTC datetime for DB comparison.
    """
    try:
        from zoneinfo import ZoneInfo
    except ImportError:
        from backports.zoneinfo import ZoneInfo  # type: ignore[no-redef]

    tz = ZoneInfo(reset_timezone)
    now_in_tz = datetime.now(tz)
    # Midnight today in the reset timezone
    midnight_local = now_in_tz.replace(hour=0, minute=0, second=0, microsecond=0)
    # Convert to UTC (naive) for DB comparison
    midnight_utc = midnight_local.astimezone(timezone.utc).replace(tzinfo=None)
    return midnight_utc


class RPDUsageItem(BaseModel):
    """RPD使用状況（モデル単位）"""
    model_id: str
    display_name: str
    used: int
    limit: int
    reset_timezone: str


@router.get("/rpd", response_model=List[RPDUsageItem])
def get_rpd_usage(
    model_id: Optional[str] = Query(None, description="特定モデルのみ取得"),
    manager=Depends(get_manager),
):
    """モデルごとのRPD（日次リクエスト数）使用状況を取得。

    rate_limitが設定されたモデルのみ返す。
    """
    # Collect models that have RPD limits
    models_with_rpd: list[tuple[str, int, str]] = []  # (config_key, rpd_limit, reset_tz)
    for config_key, config in MODEL_CONFIGS.items():
        rate_limit = config.get("rate_limit")
        if not isinstance(rate_limit, dict) or not rate_limit.get("rpd"):
            continue
        if model_id and config_key != model_id:
            continue
        models_with_rpd.append((
            config_key,
            int(rate_limit["rpd"]),
            rate_limit.get("reset_timezone", "America/Los_Angeles"),
        ))

    if not models_with_rpd:
        return []

    session = manager.SessionLocal()
    try:
        result = []
        for config_key, rpd_limit, reset_tz in models_with_rpd:
            period_start = _get_rpd_period_start(reset_tz)
            count_row = session.query(
                func.count(LLMUsageLog.ID).label("call_count"),
            ).filter(
                LLMUsageLog.MODEL_ID == config_key,
                LLMUsageLog.TIMESTAMP >= period_start,
            ).one()

            result.append(RPDUsageItem(
                model_id=config_key,
                display_name=get_model_display_name(config_key),
                used=int(count_row.call_count or 0),
                limit=rpd_limit,
                reset_timezone=reset_tz,
            ))
        return result
    finally:
        session.close()
