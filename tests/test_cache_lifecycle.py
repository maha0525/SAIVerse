"""Cache Lifecycle Control Phase 1-2 のユニットテスト。

per-persona cache 設定の解決ロジック (manager) と、それを参照する runtime /
endpoint の薄いラッパを、実 LLM / DB なしで検証する。メソッドは ``__get__`` で
軽量 stub に束縛して実コードパスをそのまま叩く。

対象: docs/intent/cache_lifecycle_control.md §5.4 / Phase 2
"""
from datetime import datetime, timedelta
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from api.routes.people.cache_status import _resolve_cache_setting
from database.models import Base
from saiverse.saiverse_manager import SAIVerseManager
from sea.runtime import SEARuntime
from sea.session_lifecycle import SessionLifecycle


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


# ---- 書き込み時 TTL の記録 (設定変更の遡及影響を防ぐ) ----

def test_anchor_entry_ttl_prefers_stored_value():
    """anchor に記録された ttl_seconds を優先 (= 書き込み時 TTL)。現行設定は見ない。"""
    rt = SimpleNamespace()
    rt.anchor_entry_ttl_seconds = SessionLifecycle.anchor_entry_ttl_seconds.__get__(rt)
    # stored があるとき get_anchor_validity_seconds (現行設定) は呼ばれてはいけない
    rt.get_anchor_validity_seconds = lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("stored ttl があるとき現行設定を見てはいけない")
    )
    assert rt.anchor_entry_ttl_seconds({"ttl_seconds": 3600}, "claude-x", "air") == 3600
    assert rt.anchor_entry_ttl_seconds({"ttl_seconds": 300}, "claude-x", "air") == 300


def test_anchor_entry_ttl_falls_back_when_no_stored():
    """旧 anchor (ttl_seconds 無し) は現行設定にフォールバック (後方互換)。"""
    rt = SimpleNamespace()
    rt.anchor_entry_ttl_seconds = SessionLifecycle.anchor_entry_ttl_seconds.__get__(rt)
    rt.get_anchor_validity_seconds = lambda model, persona_id=None: 1200
    assert rt.anchor_entry_ttl_seconds({}, "model", "air") == 1200
    assert rt.anchor_entry_ttl_seconds({"anchor_id": "x", "updated_at": "t"}, "model", "air") == 1200


# ---- 書き込み時 TTL の更新規則 (生存中は短縮しない = Anthropic 実測) ----
#
# (persona, model) 行分離 (beat_execution_context.md §3.1) 後、延命規則は
# upsert_anchor_entry が session_anchor 行の前回値と比較して適用する。
# ここでは実 in-memory DB 上で update_anchor_for_model → 行 upsert の
# 実コードパスをそのまま叩く (テストの意図 = モデルB の規則固定は不変)。

def _make_anchor_writer(existing):
    """in-memory session_anchor テーブルに existing を seed した lifecycle を返す。"""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    manager = SimpleNamespace(SessionLocal=Session)
    lc = SessionLifecycle(SimpleNamespace(), manager)
    # 空テーブルへの upsert は延命規則が no-op なので、entry がそのまま入る
    for model_key, entry in existing.items():
        lc.upsert_anchor_entry("air", model_key, entry)
    return lc


def _iso_minutes_ago(minutes=0, hours=0):
    """秒精度 (DB の epoch 秒と往復一致する) の過去時刻 isoformat。"""
    return (
        datetime.now() - timedelta(minutes=minutes, hours=hours)
    ).replace(microsecond=0).isoformat()


def test_update_anchor_short_write_keeps_ttl_and_does_not_slide_window():
    """生存中の 1h を 5m で書いても: ttl=3600 維持 + window(updated_at) はスライドしない (モデルB)。"""
    t1 = _iso_minutes_ago(minutes=2)  # 2 分前に 1h 確立 (生存中)
    lc = _make_anchor_writer({"claude-x": {"anchor_id": "a1", "updated_at": t1, "ttl_seconds": 3600}})
    persona = SimpleNamespace(persona_id="air", model="claude-x")
    lc.update_anchor_for_model(persona, "claude-x", "a2", 300)  # 5m 書き込み
    saved = lc.load_anchor_entry("air", "claude-x")
    assert saved["ttl_seconds"] == 3600       # 短縮しない
    assert saved["updated_at"] == t1          # window はスライドしない


def test_update_anchor_same_ttl_refreshes_window():
    """同じ TTL の書き込みは window を now にリフレッシュ (keep-awake / 通常更新)。"""
    t1 = _iso_minutes_ago(minutes=2)
    lc = _make_anchor_writer({"claude-x": {"anchor_id": "a1", "updated_at": t1, "ttl_seconds": 3600}})
    persona = SimpleNamespace(persona_id="air", model="claude-x")
    lc.update_anchor_for_model(persona, "claude-x", "a2", 3600)  # 1h 書き込み
    saved = lc.load_anchor_entry("air", "claude-x")
    assert saved["ttl_seconds"] == 3600
    assert saved["updated_at"] != t1          # now にリフレッシュ


def test_update_anchor_resets_ttl_after_expiry():
    """完全失効後の書き込みは新しい TTL/now でリセット。"""
    old = _iso_minutes_ago(hours=2)
    lc = _make_anchor_writer({"claude-x": {"anchor_id": "a1", "updated_at": old, "ttl_seconds": 3600}})
    persona = SimpleNamespace(persona_id="air", model="claude-x")
    lc.update_anchor_for_model(persona, "claude-x", "a2", 300)
    saved = lc.load_anchor_entry("air", "claude-x")
    assert saved["ttl_seconds"] == 300
    assert saved["updated_at"] != old         # 失効後はリセット


def test_update_anchor_upgrades_to_longer_ttl_and_slides_window():
    """生存中に長い TTL で書けば延長 (5m→1h) + window は now にスライド。"""
    t1 = _iso_minutes_ago(minutes=2)
    lc = _make_anchor_writer({"claude-x": {"anchor_id": "a1", "updated_at": t1, "ttl_seconds": 300}})
    persona = SimpleNamespace(persona_id="air", model="claude-x")
    lc.update_anchor_for_model(persona, "claude-x", "a2", 3600)
    saved = lc.load_anchor_entry("air", "claude-x")
    assert saved["ttl_seconds"] == 3600
    assert saved["updated_at"] != t1
