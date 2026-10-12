"""Continuous-cache reservations and refunds share one dated interval (no API)."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import time

import pytest
from google.genai import types

from llm_clients import gemini_cache
from llm_clients.gemini import GeminiClient
from llm_clients.gemini_cache import GeminiCacheController, prefix_digests
from saiverse import model_configs, usage_tracker
from saiverse.usage_tracker import UsageTracker

BOUNDARY = datetime(2026, 12, 31, 10, tzinfo=timezone.utc).timestamp()


@pytest.fixture
def accounting(monkeypatch):
    tracker = object.__new__(UsageTracker)
    tracker._initialized = False
    tracker.__init__()
    monkeypatch.setattr(tracker, "_flush_to_db", lambda: None)
    monkeypatch.setattr(usage_tracker, "get_usage_tracker", lambda: tracker)
    monkeypatch.setattr(model_configs, "MODEL_CONFIGS", {"dated": {"model": "dated", "pricing": {
        "cache_storage_per_1m_tokens_per_hour": 1.0,
        "currency": "EUR",
        "periods": [{"starts_at": "2026-01-01T00:00:00Z",
                     "ends_at": "2026-12-31T10:00:00Z",
                     "rates": {"cache_storage_per_1m_tokens_per_hour": .5}}],
    }}})
    return tracker


@pytest.mark.parametrize("background", [False, True])
@pytest.mark.parametrize("delete_offset,expected_remaining", [
    (-180, 300), (-60, 240), (0, 180), (60, 120), (240, 0),
])
def test_refund_uses_unused_reservation_interval(accounting, monkeypatch, background,
                                                delete_offset, expected_remaining):
    # Request starts 20s before the server's reservation start; #385 trusts expiry.
    started = BOUNDARY - 140
    reservation_start = BOUNDARY - 120
    expiry = reservation_start + 300
    now = [started]
    monkeypatch.setattr(gemini_cache, "time", SimpleNamespace(time=lambda: now[0], monotonic=time.monotonic))
    cache = SimpleNamespace(name="cachedContents/synthetic", expire_time=datetime.fromtimestamp(expiry, timezone.utc),
                            usage_metadata=SimpleNamespace(total_token_count=1_000_000))
    def create(**kwargs):
        now[0] = started + 25
        return cache
    client = SimpleNamespace(caches=SimpleNamespace(create=create, delete=lambda **kw: None))
    ctrl = GeminiCacheController()
    contents = [types.Content(role="user", parts=[types.Part(text="synthetic")])]
    digests = prefix_digests("", contents)
    args = (client, "dated", "synthetic-persona", "", contents, digests, 300)
    kwargs = dict(min_tokens=1, account_model="dated", persona_id="synthetic-persona", building_id="room")
    if background:
        reservation = ctrl.continuous_reserve("dated", "synthetic-persona", digests[-1])
        result = ctrl.continuous_create_reserved(*args, reservation, **kwargs)
    else:
        result = ctrl.continuous_create_sync(*args, **kwargs)
    assert result.timestamp == reservation_start
    # Real client accounting helper is called much later than cache creation.
    now[0] = BOUNDARY + 90
    GeminiClient._record_continuous_cache_storage(
        "dated", result.cached_tokens, result.ttl_seconds, timestamp=result.timestamp,
        persona_id="synthetic-persona", building_id="room",
    )
    charge = accounting._pending_records[0]
    assert charge["timestamp"].timestamp() == reservation_start
    assert charge["cost_usd"] == pytest.approx((120 * .5 + 180) / 3600)
    entry = ctrl._continuous[result.name]
    now[0] = BOUNDARY + delete_offset
    ctrl._delete_continuous(entry, reason="synthetic test")
    rows = accounting._pending_records
    if not expected_remaining:
        assert len(rows) == 1
        return
    assert len(rows) == 2
    refund = rows[-1]
    unused_start = max(now[0], reservation_start)
    expected = model_configs.calculate_cache_storage_cost(
        "dated", 1_000_000, expected_remaining, at=datetime.fromtimestamp(unused_start),
    )
    assert refund["timestamp"].timestamp() == now[0]
    assert refund["cost_usd"] == pytest.approx(-expected)
    assert refund["currency"] == charge["currency"] == "EUR"
    assert sum(row["cost_usd"] for row in rows) >= -1e-12
    used_seconds = max(0, unused_start - reservation_start)
    used = model_configs.calculate_cache_storage_cost(
        "dated", 1_000_000, used_seconds, at=datetime.fromtimestamp(reservation_start),
    )
    assert sum(row["cost_usd"] for row in rows) == pytest.approx(used)


def test_refund_event_and_interval_are_separate(accounting):
    event = datetime.fromtimestamp(BOUNDARY + 600, timezone(timedelta(hours=9)))
    interval = datetime.fromtimestamp(BOUNDARY - 60, timezone.utc)
    accounting.record_cache_storage_refund("dated", 1_000_000, 120,
        timestamp=event, storage_start=interval)
    refund = accounting._pending_records[-1]
    assert refund["timestamp"].timestamp() == event.timestamp()
    assert refund["cost_usd"] == pytest.approx(-(60 * .5 + 60) / 3600)
    assert refund["currency"] == "EUR"


def test_refund_default_interval_uses_explicit_deletion_event(accounting):
    accounting.record_cache_storage_refund("dated", 1_000_000, 120,
        timestamp=datetime.fromtimestamp(BOUNDARY - 60))
    assert accounting._pending_records[-1]["cost_usd"] == pytest.approx(-(60 * .5 + 60) / 3600)


def test_delete_success_time_not_request_start_owns_refund(accounting, monkeypatch):
    now = [BOUNDARY - 30]
    monkeypatch.setattr(gemini_cache, "time", SimpleNamespace(time=lambda: now[0]))
    def delete(**kwargs):
        now[0] = BOUNDARY + 30
    entry = gemini_cache._ContinuousEntry(
        name="synthetic", model="dated", chain="synthetic", prefix_count=1, prefix_sig="x",
        expire_at=BOUNDARY + 180, ttl_seconds=300, seq=1,
        client=SimpleNamespace(caches=SimpleNamespace(delete=delete)),
        cached_tokens=1_000_000, account_model="dated",
    )
    GeminiCacheController()._delete_continuous(entry, reason="test")
    assert accounting._pending_records[-1]["cost_usd"] == pytest.approx(-150 / 3600)
    assert accounting._pending_records[-1]["timestamp"].timestamp() == BOUNDARY + 30
