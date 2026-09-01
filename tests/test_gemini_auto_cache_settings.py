"""Gemini 自動キャッシュ設定 (ON/OFF + 応答後の保持秒数) のユニットテスト。

実 API は呼ばず、``client.caches.create`` / ``client.caches.delete`` を MagicMock で
差し替えて、保持秒数 0 (応答後すぐ削除) と 1 以上 (TTL 失効に任せる) の分岐を検証する。
"""
from unittest.mock import MagicMock

import pytest

from llm_clients.gemini import (
    AUTO_CACHE_KEEP_SECONDS_MAX,
    GeminiClient,
    clamp_auto_cache_keep_seconds,
)


@pytest.fixture(autouse=True)
def restore_class_attrs():
    """クラス属性を書き換えるテストなので、必ず元へ戻す。"""
    enabled = GeminiClient._AUTO_CACHE_ENABLED
    keep = GeminiClient._AUTO_CACHE_KEEP_SECONDS
    yield
    GeminiClient._AUTO_CACHE_ENABLED = enabled
    GeminiClient._AUTO_CACHE_KEEP_SECONDS = keep


@pytest.fixture
def cache_controller(monkeypatch):
    """プロセス共有のコントローラをテスト専用の新品に差し替える。"""
    from llm_clients import gemini_cache

    controller = gemini_cache.GeminiCacheController()
    monkeypatch.setattr(gemini_cache, "get_gemini_cache_controller", lambda: controller)
    return controller


def _content(role: str, text: str):
    from google.genai import types

    return types.Content(role=role, parts=[types.Part(text=text)])


def _fake_gemini_client():
    client = MagicMock()
    client.caches.create.return_value.name = "cachedContents/abc"
    client.caches.create.return_value.usage_metadata.total_token_count = 2048
    return client


def _make_gemini(client):
    """API キーなしで自動キャッシュ経路だけを動かすための最小インスタンス。"""
    gemini = object.__new__(GeminiClient)
    gemini.client = client
    gemini.free_client = None
    gemini.model = "gemini-2.5-flash"
    gemini.config_key = "gemini-2.5-flash"
    gemini._pending_cache_storage = None
    gemini._auto_cache_pending_cleanup = None
    gemini._latest_usage = None
    return gemini


def _long_contents():
    return [
        _content("user", "history A " * 300),
        _content("model", "reply B " * 300),
        _content("user", "latest question"),
    ]


def _created_ttl(client) -> str:
    return client.caches.create.call_args.kwargs["config"].ttl


# ── 保持秒数の丸め ──

def test_clamp_keep_seconds_rejects_negative_and_garbage():
    assert clamp_auto_cache_keep_seconds(0) == 0
    assert clamp_auto_cache_keep_seconds(-1) == 0
    assert clamp_auto_cache_keep_seconds(None) == 0
    assert clamp_auto_cache_keep_seconds("garbage") == 0


def test_clamp_keep_seconds_passes_through_and_caps():
    # Gemini API は ttl に下限を定めていないので、1 秒でもそのまま通す
    assert clamp_auto_cache_keep_seconds(1) == 1
    assert clamp_auto_cache_keep_seconds(120) == 120
    assert clamp_auto_cache_keep_seconds("120") == 120
    assert clamp_auto_cache_keep_seconds(AUTO_CACHE_KEEP_SECONDS_MAX) == AUTO_CACHE_KEEP_SECONDS_MAX
    assert clamp_auto_cache_keep_seconds(99999) == AUTO_CACHE_KEEP_SECONDS_MAX


def test_create_ttl_uses_insurance_ttl_when_keep_is_zero():
    GeminiClient._AUTO_CACHE_KEEP_SECONDS = 0
    assert GeminiClient.auto_cache_keep_seconds() == 0
    assert GeminiClient.auto_cache_create_ttl() == GeminiClient._AUTO_CACHE_TTL


def test_create_ttl_uses_keep_seconds_when_positive():
    GeminiClient._AUTO_CACHE_KEEP_SECONDS = 120
    assert GeminiClient.auto_cache_keep_seconds() == 120
    assert GeminiClient.auto_cache_create_ttl() == 120


def test_keep_seconds_reader_clamps_a_bad_class_attribute():
    """クラス属性へ直接おかしな値が入っても、読み出しで丸まる。"""
    GeminiClient._AUTO_CACHE_KEEP_SECONDS = 99999
    assert GeminiClient.auto_cache_keep_seconds() == AUTO_CACHE_KEEP_SECONDS_MAX
    GeminiClient._AUTO_CACHE_KEEP_SECONDS = -5
    assert GeminiClient.auto_cache_keep_seconds() == 0
    assert GeminiClient.auto_cache_create_ttl() == GeminiClient._AUTO_CACHE_TTL


# ── ON/OFF ──

def test_disabled_does_not_touch_the_cache_api(cache_controller):
    GeminiClient._AUTO_CACHE_ENABLED = False
    client = _fake_gemini_client()
    gemini = _make_gemini(client)

    contents = _long_contents()
    name, sent, cleanup = gemini._auto_cache_wrap("SYSTEM " * 200, contents)

    assert name is None
    assert cleanup is None
    assert sent == contents  # 加工せずそのまま
    assert client.caches.create.call_count == 0


def test_toggling_the_class_attribute_takes_effect_immediately(cache_controller):
    """既に生きているインスタンスにもクラス属性の切り替えが効く (再起動不要)。"""
    GeminiClient._AUTO_CACHE_ENABLED = False
    GeminiClient._AUTO_CACHE_KEEP_SECONDS = 0
    client = _fake_gemini_client()
    gemini = _make_gemini(client)

    assert gemini._auto_cache_wrap("SYSTEM " * 200, _long_contents())[0] is None

    GeminiClient._AUTO_CACHE_ENABLED = True
    assert gemini._auto_cache_wrap("SYSTEM " * 200, _long_contents())[0] == "cachedContents/abc"


# ── 保持秒数 0: 従来どおり保険 TTL で作って応答後に削除 ──

def test_keep_zero_creates_with_insurance_ttl_and_asks_for_cleanup(cache_controller):
    GeminiClient._AUTO_CACHE_ENABLED = True
    GeminiClient._AUTO_CACHE_KEEP_SECONDS = 0
    client = _fake_gemini_client()
    gemini = _make_gemini(client)

    name, sent, cleanup = gemini._auto_cache_wrap("SYSTEM " * 200, _long_contents())

    assert name == "cachedContents/abc"
    assert cleanup == "cachedContents/abc"  # 応答後に削除する
    assert len(sent) == 1  # 最新の 1 件だけ送る
    assert _created_ttl(client) == f"{GeminiClient._AUTO_CACHE_TTL}s"
    assert gemini._pending_cache_storage == ("gemini-2.5-flash", 2048, GeminiClient._AUTO_CACHE_TTL)


def test_keep_zero_deletes_the_cache_after_usage_is_recorded(cache_controller):
    GeminiClient._AUTO_CACHE_ENABLED = True
    GeminiClient._AUTO_CACHE_KEEP_SECONDS = 0
    client = _fake_gemini_client()
    gemini = _make_gemini(client)

    _, _, cleanup = gemini._auto_cache_wrap("SYSTEM " * 200, _long_contents())
    gemini._auto_cache_pending_cleanup = cleanup
    gemini._store_usage(100, 20)

    client.caches.delete.assert_called_once_with(name="cachedContents/abc")
    assert gemini._auto_cache_pending_cleanup is None


# ── 保持秒数 1 以上: TTL 失効に任せ、手動削除しない ──

def test_keep_positive_creates_with_that_ttl_and_skips_cleanup(cache_controller):
    GeminiClient._AUTO_CACHE_ENABLED = True
    GeminiClient._AUTO_CACHE_KEEP_SECONDS = 120
    client = _fake_gemini_client()
    gemini = _make_gemini(client)

    name, sent, cleanup = gemini._auto_cache_wrap("SYSTEM " * 200, _long_contents())

    assert name == "cachedContents/abc"
    assert cleanup is None  # 削除は Gemini の TTL 失効に任せる
    assert len(sent) == 1
    assert _created_ttl(client) == "120s"
    assert gemini._pending_cache_storage == ("gemini-2.5-flash", 2048, 120)


def test_keep_positive_never_calls_delete(cache_controller):
    GeminiClient._AUTO_CACHE_ENABLED = True
    GeminiClient._AUTO_CACHE_KEEP_SECONDS = 120
    client = _fake_gemini_client()
    gemini = _make_gemini(client)

    _, _, cleanup = gemini._auto_cache_wrap("SYSTEM " * 200, _long_contents())
    gemini._auto_cache_pending_cleanup = cleanup  # None なので削除経路は起きない
    gemini._store_usage(100, 20)

    assert client.caches.delete.call_count == 0


def test_keep_over_the_cap_is_clamped_before_create(cache_controller):
    GeminiClient._AUTO_CACHE_ENABLED = True
    GeminiClient._AUTO_CACHE_KEEP_SECONDS = 99999
    client = _fake_gemini_client()
    gemini = _make_gemini(client)

    _, _, cleanup = gemini._auto_cache_wrap("SYSTEM " * 200, _long_contents())

    assert cleanup is None
    assert _created_ttl(client) == f"{AUTO_CACHE_KEEP_SECONDS_MAX}s"


# ── API エンドポイント ──

def test_api_updates_class_attributes_and_persists_to_env(monkeypatch):
    from api.routes import admin, config as config_routes

    written = {}
    monkeypatch.setattr(admin, "write_env_updates", lambda updates: written.update(updates))

    GeminiClient._AUTO_CACHE_ENABLED = False
    GeminiClient._AUTO_CACHE_KEEP_SECONDS = 0

    result = config_routes.set_gemini_auto_cache(
        config_routes.GeminiAutoCacheRequest(enabled=True, keep_seconds=99999)
    )

    assert result == {"success": True, "enabled": True, "keep_seconds": AUTO_CACHE_KEEP_SECONDS_MAX}
    assert GeminiClient._AUTO_CACHE_ENABLED is True
    assert GeminiClient._AUTO_CACHE_KEEP_SECONDS == AUTO_CACHE_KEEP_SECONDS_MAX
    assert written == {
        "SAIVERSE_GEMINI_AUTO_CACHE": "1",
        "SAIVERSE_GEMINI_AUTO_CACHE_KEEP_SECONDS": str(AUTO_CACHE_KEEP_SECONDS_MAX),
    }

    assert config_routes.get_gemini_auto_cache() == {
        "enabled": True,
        "keep_seconds": AUTO_CACHE_KEEP_SECONDS_MAX,
        "keep_seconds_max": AUTO_CACHE_KEEP_SECONDS_MAX,
    }


def test_api_off_writes_zero_flag(monkeypatch):
    from api.routes import admin, config as config_routes

    written = {}
    monkeypatch.setattr(admin, "write_env_updates", lambda updates: written.update(updates))

    GeminiClient._AUTO_CACHE_ENABLED = True
    GeminiClient._AUTO_CACHE_KEEP_SECONDS = 300

    result = config_routes.set_gemini_auto_cache(
        config_routes.GeminiAutoCacheRequest(enabled=False, keep_seconds=-1)
    )

    assert result == {"success": True, "enabled": False, "keep_seconds": 0}
    assert GeminiClient._AUTO_CACHE_ENABLED is False
    assert GeminiClient._AUTO_CACHE_KEEP_SECONDS == 0
    assert written["SAIVERSE_GEMINI_AUTO_CACHE"] == "0"
    assert written["SAIVERSE_GEMINI_AUTO_CACHE_KEEP_SECONDS"] == "0"
