"""Date resolution, storage proration and persisted accounting contracts (no LLM)."""
import copy
import json
import os
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from database.models import LLMUsageLog
from saiverse import data_paths, model_configs
from saiverse.usage_tracker import UsageTracker

START = datetime(2026, 10, 10, tzinfo=timezone.utc)
END = datetime(2026, 12, 31, 10, tzinfo=timezone.utc)


def period(start=START, end=END, **rates):
    return {"starts_at": start.isoformat(), "ends_at": end.isoformat(),
            "rates": rates or {"input_per_1m_tokens": 1.0}}


@pytest.fixture
def pricing(monkeypatch):
    raw = {"input_per_1m_tokens": 2.0, "output_per_1m_tokens": 8.0,
           "cached_input_per_1m_tokens": 0.2, "cache_write_per_1m_tokens": 2.5,
           "cache_write_1h_per_1m_tokens": 4.0,
           "cache_storage_per_1m_tokens_per_hour": 1.0, "currency": "USD",
           "periods": [period(input_per_1m_tokens=1.0, output_per_1m_tokens=4.0,
                              cached_input_per_1m_tokens=0.1,
                              cache_storage_per_1m_tokens_per_hour=0.5)]}
    monkeypatch.setattr(model_configs, "MODEL_CONFIGS", {"dated": {"model": "dated-api", "pricing": raw}})
    return raw


@pytest.mark.parametrize("at,expected", [
    (START - timedelta(microseconds=1), 2), (START, 1),
    (START + timedelta(microseconds=1), 1),
    (END - timedelta(microseconds=1), 1), (END, 2),
    (END + timedelta(microseconds=1), 2),
])
def test_exact_half_open_boundaries(pricing, at, expected):
    assert model_configs.get_model_pricing("dated", at=at)["input_per_1m_tokens"] == expected
    assert model_configs.calculate_cost("dated", 1_000_000, 0, at=at) == expected


def test_offset_equivalence_and_fresh_copy(pricing):
    original = copy.deepcopy(pricing)
    at = END.astimezone(timezone(timedelta(hours=9)))
    resolved = model_configs.get_model_pricing("dated", at=at)
    assert resolved["input_per_1m_tokens"] == 2
    assert "periods" not in resolved
    resolved["input_per_1m_tokens"] = 999
    assert pricing == original
    assert model_configs.get_model_pricing("dated-api", at=START)["input_per_1m_tokens"] == 1


@pytest.mark.skipif(not hasattr(time, "tzset"), reason="requires POSIX timezone switching")
def test_naive_legacy_times_are_host_local(pricing, monkeypatch):
    previous = os.environ.get("TZ")
    try:
        monkeypatch.setenv("TZ", "JST-9")
        time.tzset()
        assert model_configs.get_model_pricing("dated", at=datetime(2026, 12, 31, 18, 59, 59))["input_per_1m_tokens"] == 1
        assert model_configs.get_model_pricing("dated", at=datetime(2026, 12, 31, 19))["input_per_1m_tokens"] == 2
    finally:
        if previous is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = previous
        time.tzset()


@pytest.mark.parametrize("bad", [None, {}, "period", [None], [{}],
    [{"starts_at": "bad", "ends_at": END.isoformat(), "rates": {}}],
    [{"starts_at": "2026-10-10", "ends_at": END.isoformat(), "rates": {"input_per_1m_tokens": 1}}],
    [period(end=START)], [period(start=END, end=START)],
    [period(input_per_1m_tokens=-1)], [period(input_per_1m_tokens=True)],
    [period(input_per_1m_tokens=float("nan"))], [period(input_per_1m_tokens=float("inf"))],
    [period(input_per_1m_tokens="1")], [period(currency="JPY")],
    [period(long_context_threshold_tokens=True)],
    [period(), period()], [period(), {"rates": {"input_per_1m_tokens": 0}}],
])
def test_malformed_schedule_falls_back_as_a_whole(pricing, bad, caplog):
    pricing["periods"] = bad
    assert model_configs.get_model_pricing("dated", at=START)["input_per_1m_tokens"] == 2
    assert model_configs.calculate_cache_storage_cost("dated", 1_000_000, 3600, at=START) == 1
    assert "Invalid pricing periods" in caplog.text


@pytest.mark.parametrize("periods", [[], None])
def test_legacy_and_empty_schedule(pricing, periods):
    if periods is None:
        pricing.pop("periods")
    else:
        pricing["periods"] = periods
    assert model_configs.calculate_cost("dated", 1_000_000, 1_000_000, at=START) == 10
    assert model_configs.calculate_cache_storage_cost("dated", 1_000_000, 3600, at=END) == 1
    assert model_configs.get_model_pricing("missing", at=START) is None


def test_adjacent_unsorted_periods_and_partial_override(pricing):
    middle = START + timedelta(hours=1)
    pricing["periods"] = [period(middle, END, input_per_1m_tokens=0), period(START, middle)]
    assert model_configs.calculate_cost("dated", 1_000_000, 1_000_000, at=middle) == 8
    assert model_configs.calculate_cost("dated", 1_000_000, 1_000_000, at=START) == 9


def test_long_context_after_date_resolution_and_cache_write_ttls(pricing):
    pricing.update(long_context_threshold_tokens=100_000, long_context_input_per_1m_tokens=3,
                   long_context_output_per_1m_tokens=12, long_context_cached_input_per_1m_tokens=0.3,
                   long_context_cache_write_per_1m_tokens=3.75,
                   long_context_cache_write_1h_per_1m_tokens=6)
    pricing["periods"][0]["rates"].update(long_context_input_per_1m_tokens=1.5,
        long_context_output_per_1m_tokens=6, long_context_cached_input_per_1m_tokens=0.15,
        long_context_cache_write_per_1m_tokens=1.875, long_context_cache_write_1h_per_1m_tokens=3)
    assert model_configs.calculate_cost("dated", 100_000, 0, at=START) == pytest.approx(.1)
    assert model_configs.calculate_cost("dated", 100_001, 0, at=START) == pytest.approx(.1500015)
    for ttl, write_rate in [("5m", 1.875), ("1h", 3)]:
        cost = model_configs.calculate_cost("dated", 1_000_000, 1_000_000, 200_000, 300_000, ttl, at=START)
        assert cost == pytest.approx(.5 * 1.5 + .2 * .15 + .3 * write_rate + 6)
    assert model_configs.calculate_cost("dated", 1_000_000, 0, at=END) == 3


@pytest.mark.parametrize("at,seconds,expected", [
    (START - timedelta(minutes=30), 3600, .75),
    (END - timedelta(minutes=30), 3600, .75),
    (END - timedelta(hours=1), 3600, .5), (END, 3600, 1),
    (START, 0, 0), (START, -1, 0),
])
def test_storage_prorates_start_and_end(pricing, at, seconds, expected):
    assert model_configs.calculate_cache_storage_cost("dated", 1_000_000, seconds, at=at) == expected


def test_storage_spans_multiple_periods_and_gaps(pricing):
    pricing["periods"] = [period(START, START + timedelta(hours=1), cache_storage_per_1m_tokens_per_hour=.5),
                          period(START + timedelta(hours=2), START + timedelta(hours=3), cache_storage_per_1m_tokens_per_hour=0)]
    assert model_configs.calculate_cache_storage_cost("dated", 1_000_000, 4 * 3600, at=START) == 2.5
    assert model_configs.calculate_cache_storage_cost("dated", 0, 3600, at=START) == 0


@pytest.mark.parametrize("model", ["gemini-3.6-flash-paid", "gemini-3.8-flash-paid"])
def test_shipped_promotion_uses_observation_date_and_conservative_expiry(model):
    assert model_configs.calculate_cost(model, 1_000_000, 1_000_000, at=START) == 4.5
    assert model_configs.calculate_cost(model, 1_000_000, 0, cached_tokens=1_000_000, at=START) == .075
    assert model_configs.calculate_cost(model, 1_000_000, 1_000_000, at=END) == 9
    assert model_configs.calculate_cost(model, 1_000_000, 1_000_000, at=START - timedelta(seconds=1)) == 9
    assert model_configs.calculate_cache_storage_cost(model, 1_000_000, 3600, at=END - timedelta(minutes=30)) == .75
    assert model_configs.calculate_cost("gemini-3.7-flash-paid", 1_000_000, 1_000_000, at=START) == 9


def test_user_override_replaces_builtin_schedule_without_mutation(tmp_path, monkeypatch):
    user = tmp_path / "user"
    (user / "models").mkdir(parents=True)
    config = {"model": "gemini-3.8-flash", "provider": "gemini", "pricing": {"input_per_1m_tokens": 7}}
    path = user / "models/gemini-3.8-flash-paid.json"
    path.write_text(json.dumps(config))
    original = path.read_bytes()
    monkeypatch.setattr(data_paths, "USER_DATA_DIR", user)
    monkeypatch.setattr(data_paths, "EXPANSION_DATA_DIR", tmp_path / "empty")
    monkeypatch.setattr(model_configs, "MODEL_CONFIGS", model_configs.load_configs())
    assert model_configs.calculate_cost("gemini-3.8-flash-paid", 1_000_000, 0, at=START) == 7
    assert path.read_bytes() == original
    assert "periods" not in model_configs.get_model_config("gemini-3.8-flash-paid")["pricing"]


def test_event_time_persistence_and_historical_aggregation(pricing, monkeypatch):
    # A private in-memory DB, with only the usage table and a separate tracker.
    engine = create_engine("sqlite:///:memory:")
    LLMUsageLog.__table__.create(engine)
    factory = sessionmaker(bind=engine)
    tracker = object.__new__(UsageTracker)
    tracker._initialized = False
    tracker.__init__()
    tracker.configure(factory)
    before = (END - timedelta(seconds=1)).astimezone().replace(tzinfo=None)
    after = END.astimezone().replace(tzinfo=None)
    tracker.record_usage("dated", 1_000_000, 0, timestamp=before)
    tracker.record_usage("dated", 1_000_000, 0, timestamp=after)
    tracker.record_cache_storage("dated", 1_000_000, 3600,
        timestamp=(END - timedelta(minutes=30)).astimezone().replace(tzinfo=None))
    with factory() as session:
        rows = session.query(LLMUsageLog).order_by(LLMUsageLog.ID).all()
        assert [row.COST_USD for row in rows] == [1, 2, .75]
        assert rows[0].TIMESTAMP == before
    # Settings may change after the event; neither DB nor daily API reprices it.
    pricing["input_per_1m_tokens"] = 999
    pricing["periods"] = []
    from api.routes.usage import get_daily_usage
    rows = get_daily_usage("2026-12-30", "2027-01-02", None, None, SimpleNamespace(SessionLocal=factory))
    assert sum(row.cost_usd for row in rows) == 3.75
    engine.dispose()


def test_aware_usage_is_persisted_in_legacy_local_time(pricing, monkeypatch):
    tracker = object.__new__(UsageTracker)
    tracker._initialized = False
    tracker.__init__()
    monkeypatch.setattr(tracker, "_flush_to_db", lambda: None)
    aware = START.astimezone(timezone(timedelta(hours=9)))
    tracker.record_usage("dated", 1_000_000, 0, timestamp=aware)
    record = tracker._pending_records[-1]
    assert record["cost_usd"] == 1
    assert record["timestamp"] == START.astimezone().replace(tzinfo=None)
    assert record["timestamp"].tzinfo is None
    tracker.record_cache_storage("dated", 1_000_000, 3600, timestamp=aware)
    assert tracker._pending_records[-1]["cost_usd"] == .5
    assert tracker._pending_records[-1]["timestamp"] == record["timestamp"]


@pytest.mark.skipif(not hasattr(time, "tzset"), reason="requires POSIX timezone switching")
@pytest.mark.parametrize("aware", [False, True])
def test_dst_second_occurrence_keeps_event_instant(pricing, monkeypatch, aware):
    previous = os.environ.get("TZ")
    try:
        monkeypatch.setenv("TZ", "America/New_York")
        time.tzset()
        start = datetime(2026, 11, 1, 6, tzinfo=timezone.utc)
        pricing["periods"] = [period(start, start + timedelta(hours=1),
            input_per_1m_tokens=1, cache_storage_per_1m_tokens_per_hour=.5)]
        instant = start + timedelta(minutes=30)
        timestamp = instant if aware else datetime.fromtimestamp(instant.timestamp())
        tracker = object.__new__(UsageTracker)
        tracker._initialized = False
        tracker.__init__()
        monkeypatch.setattr(tracker, "_flush_to_db", lambda: None)
        tracker.record_usage("dated", 1_000_000, 0, timestamp=timestamp)
        row = tracker._pending_records[-1]
        assert row["timestamp"].fold == 1
        assert row["timestamp"].timestamp() == instant.timestamp()
        assert row["cost_usd"] == 1
        tracker.record_cache_storage("dated", 1_000_000, 1800, timestamp=timestamp)
        assert tracker._pending_records[-1]["cost_usd"] == .25
        assert tracker._pending_records[-1]["timestamp"].fold == 1
    finally:
        if previous is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = previous
        time.tzset()
