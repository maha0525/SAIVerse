"""GeminiCacheController (Phase 3 / M1) のユニットテスト。

実 API は使わず MagicMock で client.models.count_tokens / client.caches.create を
差し替えて、create / reuse / token guard のロジックを検証する。
"""
from unittest.mock import MagicMock

from llm_clients.gemini_cache import GeminiCacheController, parse_ttl_seconds


def test_parse_ttl_seconds():
    assert parse_ttl_seconds("5m") == 300
    assert parse_ttl_seconds("15m") == 900
    assert parse_ttl_seconds("30m") == 1800
    assert parse_ttl_seconds("1h") == 3600
    assert parse_ttl_seconds("300s") == 300
    assert parse_ttl_seconds(120) == 120
    assert parse_ttl_seconds("garbage", default=42) == 42
    assert parse_ttl_seconds(None, default=300) == 300


def test_controller_creates_then_reuses_same_head():
    ctrl = GeminiCacheController()
    client = MagicMock()
    client.models.count_tokens.return_value.total_tokens = 5000
    client.caches.create.return_value.name = "cachedContents/abc"

    head = "SYSTEM INSTRUCTION " * 500
    name1 = ctrl.ensure(client, "gemini-2.5-flash", head, 300)
    assert name1 == "cachedContents/abc"
    assert client.caches.create.call_count == 1

    # 同一 head・生存中 → 再利用 (新規 create しない)
    name2 = ctrl.ensure(client, "gemini-2.5-flash", head, 300)
    assert name2 == "cachedContents/abc"
    assert client.caches.create.call_count == 1


def test_controller_different_head_creates_new():
    ctrl = GeminiCacheController()
    client = MagicMock()
    client.models.count_tokens.return_value.total_tokens = 5000
    client.caches.create.return_value.name = "cachedContents/x"

    ctrl.ensure(client, "gemini-2.5-flash", "HEAD A " * 500, 300)
    ctrl.ensure(client, "gemini-2.5-flash", "HEAD B " * 500, 300)
    assert client.caches.create.call_count == 2  # head が違えば別 cache


def test_controller_token_guard_skips_small_head():
    ctrl = GeminiCacheController()
    client = MagicMock()
    client.models.count_tokens.return_value.total_tokens = 100  # < 1024

    name = ctrl.ensure(client, "gemini-2.5-flash", "tiny head", 300, min_tokens=1024)
    assert name is None
    assert client.caches.create.call_count == 0  # 作成しない


def test_controller_create_failure_returns_none():
    ctrl = GeminiCacheController()
    client = MagicMock()
    client.models.count_tokens.return_value.total_tokens = 5000
    client.caches.create.side_effect = RuntimeError("api error")

    name = ctrl.ensure(client, "gemini-2.5-flash", "HEAD " * 500, 300)
    assert name is None  # 失敗時は None (= inline フォールバック)
