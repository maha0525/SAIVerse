"""Event timestamps survive usage transport without live models or production data."""
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from llm_clients.base import UsageInfo, usage_event_time


def _usage(**kwargs):
    return UsageInfo(model="synthetic-model", input_tokens=100, output_tokens=10,
                     timestamp=1798711199.5, **kwargs)


def test_epoch_to_local_naive_preserves_the_instant():
    usage = _usage()
    event_time = usage_event_time(usage)
    assert event_time == datetime.fromtimestamp(usage.timestamp)
    assert event_time.tzinfo is None
    assert event_time.timestamp() == usage.timestamp


def test_cache_creation_and_response_are_distinct_events():
    usage = _usage(cache_storage_timestamp=1798711100.0)
    assert usage_event_time(usage, cache_storage=True) == datetime.fromtimestamp(1798711100.0)
    assert usage_event_time(usage) == datetime.fromtimestamp(usage.timestamp)


def test_legacy_cache_without_creation_time_uses_response_time():
    usage = _usage()
    assert usage_event_time(usage, cache_storage=True) == usage_event_time(usage)


@pytest.mark.parametrize("usage", [
    SimpleNamespace(), MagicMock(), SimpleNamespace(timestamp=None),
    SimpleNamespace(timestamp=1e300), SimpleNamespace(timestamp=float("nan")),
    SimpleNamespace(timestamp=float("inf")), SimpleNamespace(timestamp=True),
])
def test_legacy_doubles_keep_now_fallback(usage):
    before = datetime.now()
    actual = usage_event_time(usage)
    assert before <= actual <= datetime.now()


def test_work_session_shares_event_time_with_cost(monkeypatch):
    from sea.work_session import _record_llm_usage

    tracker = MagicMock()
    calculate_cost = MagicMock(return_value=0.5)
    monkeypatch.setattr("sea.work_session.get_usage_tracker", lambda: tracker)
    monkeypatch.setattr("saiverse.model_configs.calculate_cost", calculate_cost)
    usage = _usage()
    client = MagicMock()
    client.consume_usage.return_value = usage
    _record_llm_usage(MagicMock(), {}, client, SimpleNamespace(persona_id="synthetic"), "b", "llm")
    event_time = tracker.record_usage.call_args.kwargs["timestamp"]
    assert event_time == datetime.fromtimestamp(usage.timestamp)
    assert calculate_cost.call_args.kwargs["at"] is event_time


@pytest.mark.parametrize("module", ["arasuji", "memopedia"])
def test_memory_generators_preserve_event_time(monkeypatch, module):
    from importlib import import_module

    generator = import_module(f"sai_memory.{module}.generator")
    tracker = MagicMock()
    monkeypatch.setattr("saiverse.usage_tracker.get_usage_tracker", lambda: tracker)
    if module == "memopedia":
        monkeypatch.setattr(generator, "get_usage_tracker", lambda: tracker)
    usage = _usage()
    client = MagicMock()
    client.consume_usage.return_value = usage
    generator._record_llm_usage(client, "synthetic", "synthetic")
    assert tracker.record_usage.call_args.kwargs["timestamp"] == datetime.fromtimestamp(usage.timestamp)


def test_event_conversion_respects_existing_non_utc_local_time(monkeypatch):
    import time
    from datetime import timezone

    if not hasattr(time, "tzset"):
        pytest.skip("Process timezone overrides are unavailable on this platform")
    epoch = datetime(2026, 12, 31, 10, tzinfo=timezone.utc).timestamp()
    try:
        with monkeypatch.context() as env:
            env.setenv("TZ", "JST-9")
            time.tzset()
            usage = UsageInfo("synthetic", 1, 1, epoch)
            event_time = usage_event_time(usage)
            assert event_time == datetime(2026, 12, 31, 19)
            assert event_time.timestamp() == epoch
    finally:
        time.tzset()


@pytest.mark.parametrize("entrypoint", ["_call_capture_llm", "_call_mechanism_llm"])
def test_sluice_capture_preserves_event_time(monkeypatch, entrypoint):
    from sea import sluice

    usage = _usage()
    client = MagicMock()
    client.consume_usage.return_value = usage
    tracker = MagicMock()
    monkeypatch.setattr("saiverse.usage_tracker.get_usage_tracker", lambda: tracker)
    monkeypatch.setattr(sluice, "_select_capture_llm", lambda *a: (client, SimpleNamespace(model_key=usage.model)))
    monkeypatch.setattr(sluice, "_ensure_input_fits", lambda *a, **k: None)
    monkeypatch.setattr(sluice, "_build_capture_instruction", lambda *a: {
        "prompt": "synthetic", "offered_activities": [], "offered_tasks": [], "core_snapshot": [],
    })
    monkeypatch.setattr(sluice, "_build_capture_preamble", lambda *a: "synthetic")
    monkeypatch.setattr(sluice, "_build_mechanism_prompt", lambda *a: "synthetic")
    monkeypatch.setattr(sluice, "_parse_structured_result", lambda *a: ({}, []))
    monkeypatch.setattr(sluice, "_parse_candidate_result", lambda *a: {})
    runtime = SimpleNamespace(_default_temperature=lambda p: None, _get_cache_kwargs=lambda p: {})
    persona = SimpleNamespace(persona_id="synthetic", persona_name="Synthetic", current_building_id="b")
    getattr(sluice, entrypoint)(SimpleNamespace(runtime=runtime), persona, [])
    assert tracker.record_usage.call_args.kwargs["timestamp"] == datetime.fromtimestamp(usage.timestamp)
