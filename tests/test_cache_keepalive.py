"""cache TTL keep-alive の無害化テスト (A3 項目4)。

旧実装は TTL 接近時に MetaLayer.on_periodic_tick (v1 状況分類のメタ判断) を
発火していた — v2 判断点 5 種と併走する空判断。置き換え後は:

- TTL 接近の予約 callback が **旧状況分類を一切呼ばない** こと
- 代わりに run_cache_keepalive (意味的に不活性な極小 LLM コール) が走ること
- keep-alive はメインラインと同じ context + 不活性な末尾 1 文で LLM を 1 回
  呼び、SAIMemory へは何も書かず、成功時のみ anchor touch (連鎖予約) すること
- Active でない / anchor 失効済み のときは呼ばず、連鎖が自然停止すること

NOTE: 当時対象外とした MetaLayer の alert 即応経路 (on_track_alert) は、
その後 v1 メタ判断ごと退役した (track_retirement.md §7.4)。FakeMetaLayer の
should_fire / on_periodic_tick は「旧経路が呼ばれないこと」の観測用として残す
(実 MetaLayer からは両メソッドとも消えている)。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any, Dict, List
from unittest.mock import patch

from sea.runtime import SEARuntime


class FakeScheduler:
    def __init__(self):
        self.scheduled: Dict[str, Any] = {}
        self.cancelled: List[str] = []

    def schedule(self, fire_at, callback, key):
        self.scheduled[key] = {"fire_at": fire_at, "callback": callback}

    def cancel(self, key):
        self.cancelled.append(key)
        return self.scheduled.pop(key, None) is not None


class FakeMetaLayer:
    """旧経路の観測用: on_periodic_tick / should_fire が呼ばれたら記録する。"""

    def __init__(self):
        self.periodic_ticks: List[Any] = []
        self.should_fire_calls: List[Any] = []

    def _load_judgment_config(self, persona):
        return {"keep_cache_alive": True, "cache_threshold_ratio": 0.3}

    def should_fire(self, persona_id, trigger_kind):
        self.should_fire_calls.append((persona_id, trigger_kind))
        return {"trigger": trigger_kind}

    def on_periodic_tick(self, persona_id, context=None, force=False):
        self.periodic_ticks.append((persona_id, context))


class FakeUsage(SimpleNamespace):
    pass


class FakeLLMClient:
    def __init__(self):
        self.calls: List[Dict[str, Any]] = []
        self._usage = FakeUsage(
            model="claude-x", input_tokens=1000, output_tokens=1,
            cached_tokens=990, cache_write_tokens=10, cache_ttl="5m",
        )

    def generate(self, messages, tools=None, response_schema=None, *,
                 temperature=None, **kwargs):
        self.calls.append({
            "messages": list(messages), "tools": tools, "kwargs": kwargs,
        })
        return "."

    def consume_usage(self):
        return self._usage


def _make_runtime(persona, *, meta_layer=None, scheduler=None):
    manager = SimpleNamespace(
        personas={persona.persona_id: persona},
        event_scheduler=scheduler or FakeScheduler(),
        meta_layer=meta_layer or FakeMetaLayer(),
        building_histories={},
        state=SimpleNamespace(cache_enabled=True, cache_ttl="5m"),
        _persona_cache_overrides={},
    )
    # per-persona cache override 解決 (saiverse_manager と同形の最小実装)
    manager.resolve_persona_cache = lambda pid=None: (True, "5m")
    runtime = SEARuntime(manager)
    return runtime, manager


def _persona(**over):
    base = dict(
        persona_id="air",
        autonomy_enabled=True,
        model="claude-x",
        current_building_id="room",
        history_manager=None,
    )
    base.update(over)
    return SimpleNamespace(**base)


def _live_anchor_entry(ttl_seconds=1200, age_seconds=60):
    updated = datetime.now() - timedelta(seconds=age_seconds)
    return {
        "anchor_id": "a1",
        "updated_at": updated.isoformat(),
        "ttl_seconds": ttl_seconds,
    }


# ---------------------------------------------------------------------------
# 予約 callback: 旧状況分類メタ判断が走らないこと
# ---------------------------------------------------------------------------


def test_ttl_callback_does_not_fire_v1_meta_judgment():
    persona = _persona()
    scheduler = FakeScheduler()
    meta_layer = FakeMetaLayer()
    runtime, _ = _make_runtime(persona, meta_layer=meta_layer, scheduler=scheduler)
    runtime.session_lifecycle.get_anchor_validity_seconds = lambda model, persona_id=None: 1200

    keepalive_calls: List[Any] = []
    runtime.run_cache_keepalive = lambda pid, mk=None: keepalive_calls.append((pid, mk))

    runtime.session_lifecycle.schedule_cache_ttl_pulse(persona, "claude-x", "explicit")
    # 予約 key は (persona, model) 単位 (beat_execution_context.md §3.1)
    assert "ttl:air:claude-x" in scheduler.scheduled

    scheduler.scheduled["ttl:air:claude-x"]["callback"]()

    # 旧経路 (v1 状況分類) は一切呼ばれない
    assert meta_layer.periodic_ticks == []
    assert meta_layer.should_fire_calls == []
    # 置き換え先の keep-alive が model 付きで呼ばれる
    assert keepalive_calls == [("air", "claude-x")]


def test_ttl_schedule_deadline_follows_virtual_clock():
    """期限は EventScheduler と同じ saiverse.clock.now() 基準で刻む
    (issue autonomy_manager_tick_uses_wall_clock の同族)。datetime.now() で刻むと
    仮想クロックのシム中に期限が実時刻 (シム終了後) へ飛ぶ。"""
    from saiverse import clock

    persona = _persona()
    scheduler = FakeScheduler()
    runtime, _ = _make_runtime(persona, scheduler=scheduler)
    runtime.session_lifecycle.get_anchor_validity_seconds = lambda model, persona_id=None: 1200

    virtual_now = datetime(2020, 1, 1, 12, 0, 0)
    clock.enable_virtual(virtual_now)
    try:
        runtime.session_lifecycle.schedule_cache_ttl_pulse(persona, "claude-x", "explicit")
    finally:
        clock.disable_virtual()

    # threshold 0.3 → TTL 1200 秒の 70% = 840 秒後
    assert scheduler.scheduled["ttl:air:claude-x"]["fire_at"] == (
        virtual_now + timedelta(seconds=840)
    )


def test_ttl_schedule_respects_keep_cache_alive_off():
    persona = _persona()
    scheduler = FakeScheduler()

    class OffMetaLayer(FakeMetaLayer):
        def _load_judgment_config(self, persona):
            return {"keep_cache_alive": False}

    runtime, _ = _make_runtime(
        persona, meta_layer=OffMetaLayer(), scheduler=scheduler,
    )
    runtime.session_lifecycle.get_anchor_validity_seconds = lambda model, persona_id=None: 1200
    runtime.session_lifecycle.schedule_cache_ttl_pulse(persona, "claude-x", "explicit")
    assert "ttl:air:claude-x" not in scheduler.scheduled


# ---------------------------------------------------------------------------
# run_cache_keepalive 本体
# ---------------------------------------------------------------------------


def _wire_keepalive(runtime, persona, client, anchors=None, messages=None):
    # anchor は行単位 API (load_anchor_entry) で読まれる (S8 根治後)
    _anchors = anchors if anchors is not None else {"claude-x": _live_anchor_entry()}
    runtime.session_lifecycle.load_anchor_entry = lambda pid, mk: _anchors.get(mk)

    def _fake_prepare(p, b, u, *a, **k):
        # 実物と同じく、組成に採用した anchor を context_meta へ書き戻す
        # (keepalive はこれを生存確認の値と突き合わせる — 逸脱ガード)。
        meta = k.get("context_meta")
        if meta is not None:
            e = _anchors.get(k.get("model_key")) or {}
            meta["prefix_anchor_id"] = e.get("anchor_id")
        return list(messages or [{"role": "user", "content": "履歴"}])

    runtime._prepare_context = _fake_prepare
    runtime.select_llm_client = lambda node_def, p, **k: (client, "claude-x")
    touched: List[Any] = []
    runtime.session_lifecycle.touch_anchor_after_llm_call = (
        lambda p, usage, anchor_id=None, **kwargs: touched.append(usage)
    )
    return touched


@patch("saiverse.model_configs.get_cache_config", return_value={"type": "explicit"})
def test_keepalive_touches_cache_without_writing_memory(_mock_cache):
    persona = _persona()
    runtime, _ = _make_runtime(persona)
    client = FakeLLMClient()
    touched = _wire_keepalive(runtime, persona, client)

    stored: List[Any] = []
    runtime._store_memory = lambda *a, **k: stored.append(a)

    assert runtime.run_cache_keepalive("air") is True
    # LLM は 1 回だけ、メインライン context + 不活性な末尾 1 文で呼ばれる
    assert len(client.calls) == 1
    msgs = client.calls[0]["messages"]
    assert msgs[0] == {"role": "user", "content": "履歴"}
    assert "キャッシュ維持" in msgs[-1]["content"]
    # cache kwargs (prefix を同一条件で温める) が渡る
    assert client.calls[0]["kwargs"].get("enable_cache") is True
    # SAIMemory へは何も書かない
    assert stored == []
    # 成功 = anchor touch → 次回 keep-alive が再予約される連鎖
    assert len(touched) == 1


def test_keepalive_skips_when_not_active():
    persona = _persona(autonomy_enabled=False)
    runtime, _ = _make_runtime(persona)
    client = FakeLLMClient()
    _wire_keepalive(runtime, persona, client)
    assert runtime.run_cache_keepalive("air") is False
    assert client.calls == []


@patch("saiverse.model_configs.get_cache_config", return_value={"type": "explicit"})
def test_keepalive_skips_when_anchor_expired(_mock_cache):
    """失効済みキャッシュは温め直さない — 連鎖は自然停止する。"""
    persona = _persona()
    runtime, _ = _make_runtime(persona)
    client = FakeLLMClient()
    touched = _wire_keepalive(
        runtime, persona, client,
        anchors={"claude-x": _live_anchor_entry(ttl_seconds=300, age_seconds=600)},
    )
    assert runtime.run_cache_keepalive("air") is False
    assert client.calls == []
    assert touched == []


@patch("saiverse.model_configs.get_cache_config", return_value={"type": "explicit"})
def test_keepalive_skips_when_no_anchor(_mock_cache):
    persona = _persona()
    runtime, _ = _make_runtime(persona)
    client = FakeLLMClient()
    _wire_keepalive(runtime, persona, client, anchors={})
    assert runtime.run_cache_keepalive("air") is False
    assert client.calls == []


@patch("saiverse.model_configs.get_cache_config", return_value={"type": "explicit"})
def test_keepalive_aborts_when_prefix_anchor_diverges(_mock_cache):
    """Codex 6巡目 (2026-07-30): 生存確認〜組成の間に anchor が動いたら温めない。

    keepalive は Beat ロックの外を走るため、組成は persist_anchor_advance=False
    (読みだけ) で行い、組成に採用された anchor が生存確認で読んだ行の値とズレて
    いたら LLM を呼ばず連鎖を止める — ズレた prefix はキャッシュと一致せず温めは
    無駄で、その後の touch は温めていない prefix の温度偽装になる。
    """
    persona = _persona()
    runtime, _ = _make_runtime(persona)
    client = FakeLLMClient()
    touched = _wire_keepalive(runtime, persona, client)

    prepare_kwargs: List[Any] = []
    _orig_prepare = runtime._prepare_context

    def _diverging_prepare(p, b, u, *a, **k):
        prepare_kwargs.append(dict(k))
        result = _orig_prepare(p, b, u, *a, **k)
        meta = k.get("context_meta")
        if meta is not None:
            meta["prefix_anchor_id"] = "moved-anchor"  # 組成中に前進が挟まった形
        return result

    runtime._prepare_context = _diverging_prepare

    assert runtime.run_cache_keepalive("air") is False
    assert client.calls == []   # LLM は呼ばれない
    assert touched == []        # touch もされない
    # 組成は「前進を永続化しない読み」で行われている
    assert prepare_kwargs and prepare_kwargs[0].get("persist_anchor_advance") is False


@patch("saiverse.model_configs.get_cache_config", return_value={"type": "explicit"})
def test_keepalive_failure_does_not_touch_anchor(_mock_cache):
    persona = _persona()
    runtime, _ = _make_runtime(persona)

    class FailingClient(FakeLLMClient):
        def generate(self, *a, **k):
            raise RuntimeError("boom")

    client = FailingClient()
    touched = _wire_keepalive(runtime, persona, client)
    assert runtime.run_cache_keepalive("air") is False
    assert touched == []


@patch("saiverse.model_configs.get_cache_config", return_value={"type": "implicit"})
def test_keepalive_non_explicit_does_nothing(_mock_cache):
    """非 explicit キャッシュ (Gemini/implicit 等) では keep-alive LLM を呼ばず、
    再予約もしない。

    予約は explicit にしか立たない (schedule_cache_ttl_pulse) が、予約後に model の
    キャッシュ設定が非 explicit へ変わっていたらここへ来る — 何もせず落ちる
    (2026-08-24: 見張りだけを再予約していた経路は、その唯一の目的だった
    セッションクローズ採取の撤去と同時に消した)。"""
    persona = _persona()
    scheduler = FakeScheduler()
    runtime, _ = _make_runtime(persona, scheduler=scheduler)
    runtime.session_lifecycle.get_anchor_validity_seconds = lambda model, persona_id=None: 1200
    client = FakeLLMClient()
    _wire_keepalive(runtime, persona, client)

    assert runtime.run_cache_keepalive("air") is False
    # 温め直す実キャッシュがないので LLM は呼ばない
    assert client.calls == []
    # 再予約もしない (次の本物の呼び出しが改めて予約する)
    assert "ttl:air:claude-x" not in scheduler.scheduled


# ---------------------------------------------------------------------------
# v0.4 計画 段 3: 最後の標準呼び出し (決定 10) と、ティックが保温を兼ねる (C6)
# ---------------------------------------------------------------------------


class FakeTickAutonomy:
    """AutonomyManager のティックの口だけを持つ偽物 (呼ばれた回数を数える)。"""

    def __init__(self, tick_at=None):
        self.notified = 0
        self.tick_at = tick_at

    def notify_standard_call(self):
        self.notified += 1
        return True

    def tick_fire_at(self):
        return self.tick_at


def _usage(model):
    return FakeUsage(
        model=model, input_tokens=10, output_tokens=1, cached_tokens=0,
        cache_write_tokens=0, cache_ttl="5m",
    )


def test_a_standard_model_call_pushes_the_tick_and_others_do_not():
    """本人の標準モデル (usage.model == persona.model) の成功した呼び出しだけが
    ティックの予約を押し直す。軽量モデル・model 不明・keep-alive の保温は
    押さない。anchor の有無には依らない (呼び出しの成功だけで数える)。"""
    persona = _persona(lightweight_model="light-x")
    runtime, manager = _make_runtime(persona)
    am = FakeTickAutonomy()
    manager._autonomy_managers = {"air": am}
    lc = runtime.session_lifecycle

    lc.touch_anchor_after_llm_call(persona, _usage("claude-x"), anchor_id=None)
    assert am.notified == 1
    lc.touch_anchor_after_llm_call(persona, _usage("light-x"), anchor_id=None)
    lc.touch_anchor_after_llm_call(persona, _usage(""), anchor_id=None)
    lc.touch_anchor_after_llm_call(
        persona, _usage("claude-x"), anchor_id=None, counts_as_standard_call=False,
    )
    assert am.notified == 1


def test_a_standard_call_moves_a_real_tick_reservation_only_when_it_exists():
    """本物の AutonomyManager + EventScheduler: 予約があれば now + T へ押し直し、
    無ければ生やさない (ライフ外の会話でティックを生やさない)。"""
    from saiverse import clock
    from saiverse.autonomy_manager import AutonomyManager, tick_reservation_key
    from saiverse.event_scheduler import EventScheduler

    persona = _persona()
    scheduler = EventScheduler()
    runtime, manager = _make_runtime(persona, scheduler=scheduler)
    am = AutonomyManager("air", manager)
    manager._autonomy_managers = {"air": am}
    now = datetime(2020, 1, 1, 9, 0, 0)
    clock.enable_virtual(now)
    try:
        am.start()
        key = tick_reservation_key("air")
        runtime.session_lifecycle.touch_anchor_after_llm_call(
            persona, _usage("claude-x"), anchor_id=None,
        )
        assert scheduler.fire_time_of(key) is None  # 生やさない

        scheduler.schedule(fire_at=now + timedelta(minutes=5), callback=lambda: None, key=key)
        clock.advance_to(now + timedelta(minutes=2))
        runtime.session_lifecycle.touch_anchor_after_llm_call(
            persona, _usage("claude-x"), anchor_id=None,
        )
        # T は既定の 30 分 (この最小の manager には設定もライフも無い)
        assert scheduler.fire_time_of(key) == now + timedelta(minutes=32)
    finally:
        am.stop()
        clock.disable_virtual()


@patch("saiverse.model_configs.get_cache_config", return_value={"type": "explicit"})
def test_keepalive_warm_is_not_counted_as_a_standard_call(_mock_cache):
    """keep-alive の保温は「最後の標準呼び出し」に数えない (保温がティックを
    永遠に先送りしないため)。"""
    persona = _persona()
    runtime, _ = _make_runtime(persona)
    client = FakeLLMClient()
    _wire_keepalive(runtime, persona, client)
    seen: List[Dict[str, Any]] = []
    runtime.session_lifecycle.touch_anchor_after_llm_call = (
        lambda p, usage, anchor_id=None, **kwargs: seen.append(kwargs)
    )
    assert runtime.run_cache_keepalive("air") is True
    assert seen and seen[0].get("counts_as_standard_call") is False


def test_keepalive_is_not_reserved_when_the_tick_warms_the_cache_first():
    """ティックの予約がキャッシュの失効より前に発火するなら keep-alive は予約
    しない (古い予約も消す)。失効より後なら従来どおり予約する。"""
    from saiverse import clock

    persona = _persona()
    scheduler = FakeScheduler()
    runtime, manager = _make_runtime(persona, scheduler=scheduler)
    runtime.session_lifecycle.get_anchor_validity_seconds = lambda model, persona_id=None: 1200
    now = datetime(2020, 1, 1, 12, 0, 0)
    am = FakeTickAutonomy()
    manager._autonomy_managers = {"air": am}
    clock.enable_virtual(now)
    try:
        # ティックが 10 分後 (TTL 20 分より前) → keep-alive は予約しない
        scheduler.scheduled["ttl:air:claude-x"] = {"fire_at": now, "callback": None}
        am.tick_at = now + timedelta(minutes=10)
        runtime.session_lifecycle.schedule_cache_ttl_pulse(persona, "claude-x", "explicit")
        assert "ttl:air:claude-x" not in scheduler.scheduled
        assert "ttl:air:claude-x" in scheduler.cancelled

        # ティックが 30 分後 (TTL 20 分より後) → 従来どおり予約する
        am.tick_at = now + timedelta(minutes=30)
        runtime.session_lifecycle.schedule_cache_ttl_pulse(persona, "claude-x", "explicit")
        assert scheduler.scheduled["ttl:air:claude-x"]["fire_at"] == now + timedelta(seconds=840)

        # ティックの予約が無い (ライフ外) → 従来どおり予約する
        scheduler.scheduled.clear()
        am.tick_at = None
        runtime.session_lifecycle.schedule_cache_ttl_pulse(persona, "claude-x", "explicit")
        assert "ttl:air:claude-x" in scheduler.scheduled

        # ティックが温めるのは標準モデルの Session だけ — 別 model の予約は抑えない
        am.tick_at = now + timedelta(minutes=10)
        runtime.session_lifecycle.schedule_cache_ttl_pulse(persona, "light-x", "explicit")
        assert "ttl:air:light-x" in scheduler.scheduled
    finally:
        clock.disable_virtual()


def test_keepalive_skips_under_the_global_all_stop():
    """グローバル自律全停止中は keep-alive も温め直さない (自発の課金を一括で
    止める非常ブレーキ — v0.4 計画 決定 12)。"""
    persona = _persona()
    runtime, manager = _make_runtime(persona)
    manager._autonomy_all_stop = True
    client = FakeLLMClient()
    _wire_keepalive(runtime, persona, client)
    assert runtime.run_cache_keepalive("air") is False
    assert client.calls == []
