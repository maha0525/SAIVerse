"""Cache Lifecycle Control Phase 1-2 のユニットテスト。

per-persona cache 設定の解決ロジック (manager) と、それを参照する runtime /
endpoint の薄いラッパを、実 LLM / DB なしで検証する。メソッドは ``__get__`` で
軽量 stub に束縛して実コードパスをそのまま叩く。

対象: docs/intent/cache_lifecycle_control.md §5.4 / Phase 2
"""
from types import SimpleNamespace

from api.routes.people.cache_status import _resolve_cache_setting
from saiverse.saiverse_manager import SAIVerseManager
from sea.runtime import SEARuntime


def _make_manager(cache_enabled=True, cache_ttl="5m"):
    """per-persona cache メソッドだけを実装で束縛した軽量 manager stub。"""
    mgr = SimpleNamespace(
        _persona_cache_overrides={},
        state=SimpleNamespace(cache_enabled=cache_enabled, cache_ttl=cache_ttl),
    )
    mgr.get_persona_cache_override = SAIVerseManager.get_persona_cache_override.__get__(mgr)
    mgr.set_persona_cache_override = SAIVerseManager.set_persona_cache_override.__get__(mgr)
    mgr.resolve_persona_cache = SAIVerseManager.resolve_persona_cache.__get__(mgr)
    return mgr


# ---- manager: per-persona override の get/set/resolve ----

def test_no_override_falls_back_to_global():
    mgr = _make_manager(cache_enabled=True, cache_ttl="5m")
    assert mgr.get_persona_cache_override("air") is None
    assert mgr.resolve_persona_cache("air") == (True, "5m")


def test_global_default_respects_state():
    mgr = _make_manager(cache_enabled=False, cache_ttl="1h")
    assert mgr.resolve_persona_cache("air") == (False, "1h")


def test_set_override_takes_precedence_over_global():
    mgr = _make_manager(cache_enabled=True, cache_ttl="5m")
    mgr.set_persona_cache_override("air", enabled=True, ttl="1h")
    assert mgr.get_persona_cache_override("air") == {"enabled": True, "ttl": "1h"}
    assert mgr.resolve_persona_cache("air") == (True, "1h")
    # 別 persona は依然 global
    assert mgr.resolve_persona_cache("nox") == (True, "5m")


def test_override_off_disables_cache():
    mgr = _make_manager(cache_enabled=True, cache_ttl="5m")
    mgr.set_persona_cache_override("air", enabled=False, ttl="5m")
    assert mgr.resolve_persona_cache("air") == (False, "5m")


def test_resolve_with_none_persona_returns_global():
    mgr = _make_manager(cache_enabled=True, cache_ttl="1h")
    assert mgr.resolve_persona_cache(None) == (True, "1h")


# ---- endpoint: 実効設定 -> "off" | "5m" | "1h" ----

def test_resolve_cache_setting_mapping():
    mgr = _make_manager()
    mgr.set_persona_cache_override("a", enabled=False, ttl="5m")
    assert _resolve_cache_setting(mgr, "a") == "off"
    mgr.set_persona_cache_override("b", enabled=True, ttl="5m")
    assert _resolve_cache_setting(mgr, "b") == "5m"
    mgr.set_persona_cache_override("c", enabled=True, ttl="1h")
    assert _resolve_cache_setting(mgr, "c") == "1h"


def test_resolve_cache_setting_uses_global_when_unset():
    mgr = _make_manager(cache_enabled=True, cache_ttl="1h")
    assert _resolve_cache_setting(mgr, "unknown") == "1h"


# ---- runtime: TTL / enabled の解決 + cache_kwargs ----

def _make_runtime(mgr):
    rt = SimpleNamespace(manager=mgr)
    rt._resolve_cache_ttl_str = SEARuntime._resolve_cache_ttl_str.__get__(rt)
    rt._resolve_cache_enabled = SEARuntime._resolve_cache_enabled.__get__(rt)
    rt._get_cache_kwargs = SEARuntime._get_cache_kwargs.__get__(rt)
    return rt


def test_runtime_resolves_per_persona_ttl():
    mgr = _make_manager(cache_enabled=True, cache_ttl="5m")
    mgr.set_persona_cache_override("air", enabled=True, ttl="1h")
    rt = _make_runtime(mgr)
    assert rt._resolve_cache_ttl_str("air") == "1h"
    assert rt._resolve_cache_ttl_str("nox") == "5m"  # global fallback
    assert rt._resolve_cache_ttl_str(None) == "5m"


def test_runtime_resolves_per_persona_enabled():
    mgr = _make_manager(cache_enabled=True, cache_ttl="5m")
    mgr.set_persona_cache_override("air", enabled=False, ttl="5m")
    rt = _make_runtime(mgr)
    assert rt._resolve_cache_enabled("air") is False
    assert rt._resolve_cache_enabled("nox") is True


def test_get_cache_kwargs_per_persona():
    mgr = _make_manager(cache_enabled=True, cache_ttl="5m")
    mgr.set_persona_cache_override("air", enabled=True, ttl="1h")
    rt = _make_runtime(mgr)
    assert rt._get_cache_kwargs("air") == {"enable_cache": True, "cache_ttl": "1h"}
    assert rt._get_cache_kwargs("nox") == {"enable_cache": True, "cache_ttl": "5m"}
    # persona_id=None は global 挙動 (後方互換)
    assert rt._get_cache_kwargs() == {"enable_cache": True, "cache_ttl": "5m"}
