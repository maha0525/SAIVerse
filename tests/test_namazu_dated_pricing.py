"""Shipped Namazu price change: isolated config, accounting and UTC/JST boundaries."""

import copy
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from database.models import LLMUsageLog
from saiverse import data_paths, model_configs
from saiverse.usage_tracker import UsageTracker

MODEL = "sakana-namazu-v1.0"
CHANGE = datetime(2026, 12, 1, 1, tzinfo=timezone.utc)
JST = timezone(timedelta(hours=9))
OLD = (0.95, 0.15, 4)
NEW = (1.90, 0.30, 6)


@pytest.fixture(autouse=True)
def builtin_models(tmp_path, monkeypatch):
    """Load actual shipped files without user or expansion overrides."""
    monkeypatch.setattr(data_paths, "USER_DATA_DIR", tmp_path / "user")
    monkeypatch.setattr(data_paths, "EXPANSION_DATA_DIR", tmp_path / "expansion")
    monkeypatch.setattr(model_configs, "MODEL_CONFIGS", model_configs.load_configs())


@pytest.mark.parametrize("at,rates", [
    (datetime(2026, 9, 1, tzinfo=timezone.utc), OLD),
    (datetime(2026, 12, 1, tzinfo=JST), OLD),
    (CHANGE - timedelta(microseconds=1), OLD),
    (CHANGE, NEW),
    (CHANGE + timedelta(microseconds=1), NEW),
    (datetime(2026, 12, 1, 10, tzinfo=JST), NEW),
    (datetime(2030, 1, 1, tzinfo=timezone.utc), NEW),
])
def test_shipped_rates_and_costs(at, rates):
    pricing = model_configs.get_model_pricing(MODEL, at=at)
    input_rate, cache_rate, output_rate = rates
    assert pricing["currency"] == "USD"
    assert pricing["input_per_1m_tokens"] == input_rate
    assert pricing["cached_input_per_1m_tokens"] == cache_rate
    assert pricing["output_per_1m_tokens"] == output_rate
    # Cached input is part of total input, not an additional token count.
    assert model_configs.calculate_cost(
        MODEL, 1_000_000, 1_000_000, cached_tokens=250_000, at=at,
    ) == pytest.approx(0.75 * input_rate + 0.25 * cache_rate + output_rate)
    if at < CHANGE:
        assert datetime.fromisoformat(pricing["period_ends_at"]) == CHANGE
    else:
        assert "period_ends_at" not in pricing


def test_resolution_keeps_config_and_historical_rates_unchanged():
    raw = model_configs.get_model_config(MODEL)
    original = copy.deepcopy(raw)
    assert raw["pricing"]["input_per_1m_tokens"] == NEW[0]
    model_configs.get_model_pricing(MODEL, at=CHANGE)["input_per_1m_tokens"] = 999
    assert raw == original
    assert model_configs.get_model_pricing(
        MODEL, at=datetime.min.replace(tzinfo=timezone.utc),
    )["input_per_1m_tokens"] == OLD[0]
    assert model_configs.get_model_pricing(
        MODEL, at=CHANGE - timedelta(days=365),
    )["input_per_1m_tokens"] == OLD[0]


def test_usage_event_time_and_saved_cost_survive_price_change():
    engine = create_engine("sqlite:///:memory:")
    LLMUsageLog.__table__.create(engine)
    factory = sessionmaker(bind=engine)
    tracker = object.__new__(UsageTracker)
    tracker._initialized = False
    tracker.__init__()
    tracker.configure(factory)
    try:
        for at in (CHANGE - timedelta(microseconds=1), CHANGE):
            tracker.record_usage(
                MODEL, 1_000_000, 1_000_000, cached_tokens=250_000, timestamp=at,
            )
        with factory() as session:
            rows = session.query(LLMUsageLog).order_by(LLMUsageLog.ID).all()
            expected = [0.75 * rate[0] + 0.25 * rate[1] + rate[2] for rate in (OLD, NEW)]
            assert [row.COST_USD for row in rows] == pytest.approx(expected)
        # Resolving current/new pricing must not reprice already persisted usage.
        model_configs.get_model_pricing(MODEL, at=CHANGE + timedelta(days=1))
        with factory() as session:
            rows = session.query(LLMUsageLog).order_by(LLMUsageLog.ID).all()
            assert [row.COST_USD for row in rows] == pytest.approx(expected)
    finally:
        engine.dispose()
