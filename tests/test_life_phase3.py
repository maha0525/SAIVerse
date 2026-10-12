"""ライフ Phase 3「キャッシュ連動」のテスト (docs/intent/life.md §5.1/§5.2/§6.2 v0.4)。

対象:
- keep-alive のライフ従属 (``day_plan.is_keepalive_allowed`` /
  ``sea.runtime.SEARuntime.run_cache_keepalive``): lives 未宣言は従来どおり許可、
  宣言済みの日はライフ区間内のみ許可 (谷では touch せず連鎖を自然停止)、
  判定失敗時は許可側にフォールバック
- ライフ終端の節目 (``day_plan.apply_life_boundary`` の end、§6.2 v0.4。実行
  台帳の下で決着し、通知は outbox → saimemory.append の実ハンドラで届く): 終端が能動的に
  行うのは keep-alive 予約 (``ttl:{persona_id}``) の cancel と TTL override の
  遅延解除予約だけ。**anchor は触らない** (session_anchor 行は不変) — touch が
  止まれば TTL で自然失効する。
  ※ (persona, model) 行分離 (beat_execution_context.md §3.1) で予約 key は
  ``ttl:{persona_id}:{model_key}`` になり、終端の cancel は prefix 一括
  (``EventScheduler.cancel_prefix``) で全 model 分を止める。
  即時失効は惜しい谷 (終了直後〜TTL 内の再訪、
  実キャッシュがまだ生きている) の再訪を Case 3 に落として生きたキャッシュを
  捨てるため誤り (v0.3→v0.4 で訂正)
- 均等モードの cache TTL 運転 (``day_plan._sync_cache_ttl_for_life_start`` /
  ``_sync_cache_ttl_for_life_end``): mode=even のライフ開始で TTL=1h override
  (前のライフの遅延解除予約は cancel)、終端では**即時 clear せず**
  「終端 + anchor validity 秒」の遅延解除 (``life_ttl_clear:{persona_id}``) を
  予約、発火体は厳密一致チェック付きで clear (ユーザーの明示設定は尊重して
  触らない)、mode=free では一切触らない
- 後方互換: lives 未宣言の日 / ペルソナは既存挙動のまま (test_cache_keepalive.py
  の既存テスト群が緑であることも参照)

teardown で engine.dispose() + clock.disable_virtual() を必ず行う
(test_life_phase2.py と同じ規律)。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any, Dict, List
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from database.models import AI, Base, City, User
from saiverse import clock
from saiverse import day_plan
from saiverse.event_scheduler import EventScheduler
from saiverse.execution_ledger import ExecutionLedger
from saiverse.execution_ledger_wiring import (
    TARGET_SAIMEMORY_APPEND,
    _make_saimemory_append_handler,
)
from saiverse.saiverse_manager import SAIVerseManager
from sea.runtime import SEARuntime

PERSONA_ID = "alice"
PLAN_DATE = "2026-07-04"
BASE = datetime(2026, 7, 4, 0, 0, 0)

LIFE_SET_OVERRIDE = {"enabled": True, "ttl": "1h"}
TTL_CLEAR_KEY = f"life_ttl_clear:{PERSONA_ID}"


# ---------------------------------------------------------------------------
# fixtures (test_life_phase2.py と同型の最小スタブ + SEARuntime/cache override)
# ---------------------------------------------------------------------------


@pytest.fixture
def session_factory():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    yield Session
    engine.dispose()


@pytest.fixture(autouse=True)
def _reset_clock():
    yield
    clock.disable_virtual()


class FakeAdapter:
    """SAIMemory adapter の最小スタブ (追記の記録のみ)。

    ライフ境界の通知は台帳の outbox (saimemory.append) を経て
    ``append_ledger_message`` で届く。``append_persona_message`` は本物の adapter
    が持つ口で、境界側はこれの有無で「届け先があるか」を判定する。
    """

    def __init__(self):
        self.messages: List[Dict[str, Any]] = []

    def append_persona_message(self, payload):
        self.messages.append(payload)

    def append_ledger_message(
        self, message, *, execution_id, outbox_id, building_id=None,
        thread_suffix=None,
    ):
        self.messages.append(message)
        return f"msg-{outbox_id}"


@pytest.fixture
def manager(session_factory):
    db = session_factory()
    try:
        db.add(User(USERID=1, PASSWORD="x", USERNAME="tester"))
        db.flush()
        city = City(USERID=1, CITY_SLUG="test_city", UI_PORT=3001, API_PORT=8001)
        db.add(city)
        db.flush()
        db.add(AI(AIID=PERSONA_ID, HOME_CITYID=city.CITYID, AINAME="Alice"))
        db.commit()
    finally:
        db.close()

    persona = SimpleNamespace(
        persona_id=PERSONA_ID,
        autonomy_enabled=True,
        model="claude-x",
        current_building_id="alice_room",
        private_room_id="alice_room",
        sai_memory=FakeAdapter(),
        history_manager=None,
    )
    mgr = SimpleNamespace(
        SessionLocal=session_factory,
        personas={PERSONA_ID: persona},
        event_scheduler=EventScheduler(),  # start() しない (シム/同期検証)
        _persona_cache_overrides={},
        state=SimpleNamespace(cache_enabled=True, cache_ttl="5m"),
        buildings=[],
    )
    mgr.get_persona_cache_override = SAIVerseManager.get_persona_cache_override.__get__(mgr)
    mgr.set_persona_cache_override = SAIVerseManager.set_persona_cache_override.__get__(mgr)
    mgr.clear_persona_cache_override = SAIVerseManager.clear_persona_cache_override.__get__(mgr)
    mgr.resolve_persona_cache = SAIVerseManager.resolve_persona_cache.__get__(mgr)
    # 本番 manager は実行台帳を無条件に持つ。境界通知の配送は本番と同じ
    # saimemory.append の実ハンドラを通す。
    ledger = ExecutionLedger(session_factory=session_factory)
    ledger.register_outbox_handler(
        TARGET_SAIMEMORY_APPEND, _make_saimemory_append_handler(mgr),
    )
    mgr.execution_ledger = ledger

    runtime = SEARuntime(mgr)
    mgr.sea_runtime = runtime
    return mgr


def _life_start(manager, life, *, index=0, plan_date=PLAN_DATE):
    """day_open 経路と同じ入口 (apply_life_boundary) でライフ開始の節目を決着させる。"""
    assert day_plan.apply_life_boundary(
        manager, PERSONA_ID, plan_date, life, boundary="start", index=index,
    ) is True


def _life_end(manager, life, *, index=0, plan_date=PLAN_DATE):
    """day_close 経路と同じ入口 (apply_life_boundary) でライフ終了の節目を決着させる。"""
    assert day_plan.apply_life_boundary(
        manager, PERSONA_ID, plan_date, life, boundary="end", index=index,
    ) is True


def _take_handed_off_ttl_clear(manager):
    """遅延解除の予約を、EventScheduler が手渡す時と同じく辞書から外して返す。

    手渡し済みの callback には次のライフの開始の cancel が届かない (2026-10-12
    Codex 敵対レビュー 段 3 5 巡目 medium の再現の前提)。
    """
    entry = manager.event_scheduler._entries_by_key.pop(TTL_CLEAR_KEY)
    return entry.callback


def _fire_ttl_clear(manager):
    """予約された遅延解除 (終了したライフの印を焼き込んだ callback) を発火する。"""
    _take_handed_off_ttl_clear(manager)()


def _save_life(manager, *, start="09:00", end="11:00", mode="free"):
    day_plan.save_lives(manager, PERSONA_ID, PLAN_DATE, [
        {"start": start, "end": end, "mode": mode},
    ])
    return day_plan.get_lives(manager, PERSONA_ID, PLAN_DATE)


def _live_anchors(anchor_id="abc123", ttl_seconds=3600, updated_at=None):
    # session_anchor テーブルは updated_at を epoch 秒で持つため、往復比較が
    # 成立するよう秒精度に丸めた時刻を使う。
    updated = (updated_at or datetime.now()).replace(microsecond=0)
    return {
        "claude-x": {
            "anchor_id": anchor_id,
            "updated_at": updated.isoformat(),
            "ttl_seconds": ttl_seconds,
        },
    }


def _save_anchor_rows(lifecycle, anchors):
    """行単位 API (upsert_anchor_entry) で anchors dict を保存するヘルパ。"""
    for model_key, entry in anchors.items():
        lifecycle.upsert_anchor_entry(PERSONA_ID, model_key, entry)


# ---------------------------------------------------------------------------
# is_keepalive_allowed: 3 状態 + 後方互換 + 失敗時フォールバック
# ---------------------------------------------------------------------------


def test_keepalive_allowed_true_when_no_lives_declared(manager):
    clock.enable_virtual(BASE + timedelta(hours=15))
    assert day_plan.is_keepalive_allowed(manager, PERSONA_ID) is True


def test_keepalive_allowed_true_inside_declared_life(manager):
    _save_life(manager, start="09:00", end="11:00")
    clock.enable_virtual(BASE + timedelta(hours=9, minutes=30))
    assert day_plan.is_keepalive_allowed(manager, PERSONA_ID) is True


def test_keepalive_allowed_false_in_valley(manager):
    _save_life(manager, start="09:00", end="11:00")
    clock.enable_virtual(BASE + timedelta(hours=12))
    assert day_plan.is_keepalive_allowed(manager, PERSONA_ID) is False


def test_keepalive_allowed_defaults_true_on_lookup_failure():
    """manager が SessionLocal を持たない (異常系) → 許可側にフォールバック。"""
    broken_manager = SimpleNamespace()
    assert day_plan.is_keepalive_allowed(broken_manager, PERSONA_ID) is True


# ---------------------------------------------------------------------------
# ライフ終端の節目: anchor 不変 (§6.2 v0.4) + keep-alive 予約 cancel
# ---------------------------------------------------------------------------


def test_life_end_does_not_touch_anchor(manager):
    """終端は anchor を触らない — 惜しい谷 (TTL 内の再訪) でキャッシュヒット
    再開できるよう、session_anchor 行は不変のまま TTL の自然失効に任せる。"""
    persona = manager.personas[PERSONA_ID]
    lifecycle = manager.sea_runtime.session_lifecycle
    anchors = _live_anchors()
    _save_anchor_rows(lifecycle, anchors)

    lives = _save_life(manager, start="09:00", end="11:00", mode="free")
    _life_end(manager, lives[0])

    # anchor は不変。TTL 内の resolve は従来どおり self anchor を返す
    # (= 惜しい谷の再訪は Case 3 に落ちず、生きたキャッシュで再開できる)
    assert lifecycle.load_anchors(persona) == anchors
    anchor_id, resolution = lifecycle.resolve_metabolism_anchor(persona)
    assert (anchor_id, resolution) == ("abc123", "self")


def test_life_end_cancels_keepalive_reservation(manager):
    """終端は (persona, model) 単位の keep-alive 予約を全 model 分 prefix 一括で止める。
    他 persona の予約には触れない。"""
    for key in (f"ttl:{PERSONA_ID}:claude-x", f"ttl:{PERSONA_ID}:light-model"):
        manager.event_scheduler.schedule(
            fire_at=BASE + timedelta(hours=5), callback=lambda: None, key=key,
        )
    manager.event_scheduler.schedule(
        fire_at=BASE + timedelta(hours=5), callback=lambda: None,
        key="ttl:other_persona:claude-x",
    )

    lives = _save_life(manager, start="09:00", end="11:00", mode="free")
    _life_end(manager, lives[0])

    assert not manager.event_scheduler.has_key(f"ttl:{PERSONA_ID}:claude-x")
    assert not manager.event_scheduler.has_key(f"ttl:{PERSONA_ID}:light-model")
    assert manager.event_scheduler.has_key("ttl:other_persona:claude-x")


def test_life_end_notifies_boundary(manager):
    lives = _save_life(manager, start="09:00", end="11:00", mode="free")
    _life_end(manager, lives[0])
    texts = [m["content"] for m in manager.personas[PERSONA_ID].sai_memory.messages]
    assert any("活動終了" in t for t in texts)


def test_life_end_without_session_lifecycle_does_not_crash(manager):
    """sea_runtime が無い異常系でも例外を投げない。TTL clear の遅延解決は
    既定 (3600s) にフォールバックして予約され、通知も正常に記録される。"""
    manager.sea_runtime = None
    clock.enable_virtual(BASE + timedelta(hours=9, minutes=40))
    lives = _save_life(manager, start="09:00", end="09:40", mode="even")
    _life_end(manager, lives[0])
    texts = [m["content"] for m in manager.personas[PERSONA_ID].sai_memory.messages]
    assert any("活動終了" in t for t in texts)
    assert manager.event_scheduler.has_key(TTL_CLEAR_KEY)


# ---------------------------------------------------------------------------
# 均等モードの cache TTL 運転 (life.md §5.1 / §6.2 v0.4: 遅延解除)
# ---------------------------------------------------------------------------


def test_even_mode_life_start_sets_ttl_override(manager):
    # 開始はライフの窓の中で撃つ (窓が過ぎた開始は「開始」として扱わない —
    # 2026-10-10 Codex 敵対レビュー 3 巡目 修正 3)。
    clock.enable_virtual(BASE + timedelta(hours=9))
    lives = _save_life(manager, start="09:00", end="09:40", mode="even")
    assert manager.get_persona_cache_override(PERSONA_ID) is None
    _life_start(manager, lives[0])
    assert manager.get_persona_cache_override(PERSONA_ID) == LIFE_SET_OVERRIDE


def test_even_mode_life_end_schedules_delayed_clear_not_immediate(manager):
    """終端では即時 clear せず、遅延解除の予約だけ入る。override は残る
    (即時に 5m へ戻すと anchor の生存評価が実キャッシュの寿命とズレるため)。"""
    # 開始はライフの窓の中で撃つ (窓が過ぎた開始は「開始」として扱わない —
    # 2026-10-10 Codex 敵対レビュー 3 巡目 修正 3)。
    clock.enable_virtual(BASE + timedelta(hours=9))
    lives = _save_life(manager, start="09:00", end="09:40", mode="even")
    _life_start(manager, lives[0])
    assert manager.get_persona_cache_override(PERSONA_ID) == LIFE_SET_OVERRIDE

    clock.advance_to(BASE + timedelta(hours=9, minutes=40))
    _life_end(manager, lives[0])
    # 即時 clear されない
    assert manager.get_persona_cache_override(PERSONA_ID) == LIFE_SET_OVERRIDE
    # 遅延解除の予約が入っている
    assert manager.event_scheduler.has_key(TTL_CLEAR_KEY)

    # 発火体: 厳密一致するので clear される
    _fire_ttl_clear(manager)
    assert manager.get_persona_cache_override(PERSONA_ID) is None


def test_life_ttl_clear_fire_respects_user_change(manager):
    """予約〜発火の間にユーザーが override を変更していたら、発火体は触らない。"""
    clock.enable_virtual(BASE + timedelta(hours=9))
    lives = _save_life(manager, start="09:00", end="09:40", mode="even")
    _life_start(manager, lives[0])
    clock.advance_to(BASE + timedelta(hours=9, minutes=40))
    _life_end(manager, lives[0])

    # 発火前にユーザーが人設定タブで明示変更
    manager.set_persona_cache_override(PERSONA_ID, enabled=True, ttl="5m")
    _fire_ttl_clear(manager)
    assert manager.get_persona_cache_override(PERSONA_ID) == {"enabled": True, "ttl": "5m"}


def test_a_user_override_set_before_the_life_survives_the_life_end_clear(manager):
    """ライフの**前**に利用者が明示した 1h は、ライフ終了の遅延解除で消えない。

    ライフ開始は、既存の override に持ち主の記録が無い (= 利用者の明示設定) とき
    持ち主を自分に付け替えない — 付け替えると、値がライフの 1h と同じなので
    終了の解除が「自分の持ち分」として消してしまう (2026-10-12 段 3 敵対レビュー
    6 巡目の消し込みで実装側が発見した残骸)。
    """
    clock.enable_virtual(BASE + timedelta(hours=8, minutes=50))
    # 利用者がライフの前に、ライフと同じ値 (有効・1h) を明示設定する
    from saiverse.day_plan import apply_user_cache_override
    apply_user_cache_override(manager, PERSONA_ID, enabled=True, ttl="1h")
    assert manager.get_persona_cache_override(PERSONA_ID) == LIFE_SET_OVERRIDE

    clock.advance_to(BASE + timedelta(hours=9))
    lives = _save_life(manager, start="09:00", end="09:40", mode="even")
    _life_start(manager, lives[0])
    clock.advance_to(BASE + timedelta(hours=9, minutes=40))
    _life_end(manager, lives[0])

    # 遅延解除が発火しても、利用者の 1h は残る (持ち主が付いていないので no-op)
    _fire_ttl_clear(manager)
    assert manager.get_persona_cache_override(PERSONA_ID) == LIFE_SET_OVERRIDE


def test_next_life_start_cancels_pending_ttl_clear(manager):
    """次のライフが TTL 経過前に始まったら、前のライフの遅延解除予約を cancel
    する — ライフの最中に解除が発火して override が外れる事故を防ぐ。

    次のライフは次の営業日のもの (v0.5 はライフ = その日の起床〜就寝で、境界は
    (persona, 営業日) につき一度だけ台帳に claim される)。時刻は「前のライフの
    遅延解除が発火する前に次の開始が来る」並びを作るための合成。

    次のライフの確定は前のライフの終了の後 — 新しい営業日のライフが確定した
    後の前日の節目は「現在の世代」ではなく、TTL・keep-alive に触らない
    (2026-10-10 Codex 敵対レビュー 3 巡目 修正 3)。
    """
    next_date = "2026-07-05"
    clock.enable_virtual(BASE + timedelta(hours=9))
    lives = _save_life(manager, start="09:00", end="09:40", mode="even")

    _life_start(manager, lives[0])
    clock.advance_to(BASE + timedelta(hours=9, minutes=40))
    _life_end(manager, lives[0])
    assert manager.event_scheduler.has_key(TTL_CLEAR_KEY)

    day_plan.save_lives(manager, PERSONA_ID, next_date, [
        {"start": "10:00", "end": "10:40", "mode": "even"},
    ])
    next_lives = day_plan.get_lives(manager, PERSONA_ID, next_date)

    # 20 分後に次のライフが開始 (TTL 経過前)
    clock.advance_to(BASE + timedelta(hours=10))
    _life_start(manager, next_lives[0], plan_date=next_date)
    assert not manager.event_scheduler.has_key(TTL_CLEAR_KEY)
    # override は 1h のまま維持される (前のライフの値が望む値と同じ)
    assert manager.get_persona_cache_override(PERSONA_ID) == LIFE_SET_OVERRIDE


def test_a_handed_off_ttl_clear_of_the_previous_life_does_not_clear_the_next_life(manager):
    """段 3 5 巡目 medium の再現順序: 前のライフの遅延解除がワーカーへ手渡された
    (辞書から外れて次のライフの開始の cancel が届かない) まま保留 → 次のライフの
    開始の TTL 同期 (既存の 1h を見て成功) → 古い解除が再開。古い解除は値の一致
    だけで外していたので新しいライフの 1h を消していた — 解除に焼き込んだ
    終了したライフの印と、override の持ち主 (次のライフの開始が付け替えた) が
    一致しないので何もしない。次のライフの終了の解除は普通に外す。"""
    next_date = "2026-07-05"
    clock.enable_virtual(BASE + timedelta(hours=9))
    lives = _save_life(manager, start="09:00", end="09:40", mode="even")
    _life_start(manager, lives[0])
    clock.advance_to(BASE + timedelta(hours=9, minutes=40))
    _life_end(manager, lives[0])

    # 旧解除を手渡し済みにして保留する
    stale_clear = _take_handed_off_ttl_clear(manager)

    day_plan.save_lives(manager, PERSONA_ID, next_date, [
        {"start": "10:00", "end": "10:40", "mode": "even"},
    ])
    next_lives = day_plan.get_lives(manager, PERSONA_ID, next_date)
    clock.advance_to(BASE + timedelta(days=1, hours=10))
    _life_start(manager, next_lives[0], plan_date=next_date)
    assert manager.get_persona_cache_override(PERSONA_ID) == LIFE_SET_OVERRIDE

    # 旧解除が再開しても、新しいライフの 1h は消えない
    stale_clear()
    assert manager.get_persona_cache_override(PERSONA_ID) == LIFE_SET_OVERRIDE

    # 次のライフの終了の解除は、持ち主が自分なので外す
    clock.advance_to(BASE + timedelta(days=1, hours=10, minutes=40))
    _life_end(manager, next_lives[0], plan_date=next_date)
    _fire_ttl_clear(manager)
    assert manager.get_persona_cache_override(PERSONA_ID) is None


def test_free_mode_life_does_not_touch_cache_ttl(manager):
    clock.enable_virtual(BASE + timedelta(hours=9))
    lives = _save_life(manager, start="09:00", end="11:00", mode="free")
    _life_start(manager, lives[0])
    assert manager.get_persona_cache_override(PERSONA_ID) is None
    clock.advance_to(BASE + timedelta(hours=11))
    _life_end(manager, lives[0])
    assert manager.get_persona_cache_override(PERSONA_ID) is None
    assert not manager.event_scheduler.has_key(TTL_CLEAR_KEY)


def test_even_mode_life_respects_existing_explicit_override(manager):
    """ユーザーが人設定タブで明示設定した override は、ライフの宣言で上書き
    しないし、遅延解除の発火でも clear されない (厳密一致しないため)。"""
    manager.set_persona_cache_override(PERSONA_ID, enabled=True, ttl="5m")
    clock.enable_virtual(BASE + timedelta(hours=9))
    lives = _save_life(manager, start="09:00", end="09:40", mode="even")

    _life_start(manager, lives[0])
    assert manager.get_persona_cache_override(PERSONA_ID) == {"enabled": True, "ttl": "5m"}

    clock.advance_to(BASE + timedelta(hours=9, minutes=40))
    _life_end(manager, lives[0])
    _fire_ttl_clear(manager)
    # ライフが設定した値 (1h) と一致しないので clear されず、明示設定のまま残る
    assert manager.get_persona_cache_override(PERSONA_ID) == {"enabled": True, "ttl": "5m"}


# ---------------------------------------------------------------------------
# run_cache_keepalive のライフ従属 (統合: SEARuntime + day_plan)
# ---------------------------------------------------------------------------


class FakeUsage(SimpleNamespace):
    pass


class FakeLLMClient:
    def __init__(self):
        self.calls: List[Dict[str, Any]] = []
        self._usage = FakeUsage(
            model="claude-x", input_tokens=1000, output_tokens=1,
            cached_tokens=990, cache_write_tokens=10, cache_ttl="1h",
        )

    def generate(self, messages, tools=None, response_schema=None, *,
                 temperature=None, **kwargs):
        self.calls.append({"messages": list(messages), "kwargs": kwargs})
        return "."

    def consume_usage(self):
        return self._usage


def _wire_keepalive(runtime, persona, client, anchors=None, messages=None):
    # anchor の生存判定 (run_cache_keepalive 内) は実時刻 (datetime.now()) を
    # 見る — 仮想クロックの対象外 (cache lifecycle は clock モジュール導入前
    # からの独立した機構、docs/intent/autonomous_behavior_v2.md §12 の対象外)。
    # 読み口は行単位 API (load_anchor_entry、S8 根治後)。
    _anchors = anchors if anchors is not None else _live_anchors(anchor_id="a1")
    runtime.session_lifecycle.load_anchor_entry = lambda pid, mk: _anchors.get(mk)

    def _fake_prepare(p, b, u, *a, **k):
        # 実物と同じく、組成に採用した anchor を context_meta へ書き戻す
        # (keepalive の逸脱ガードがこれを生存確認の値と突き合わせる)。
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
def test_keepalive_fires_inside_declared_life(_mock_cache, manager):
    persona = manager.personas[PERSONA_ID]
    _save_life(manager, start="09:00", end="11:00", mode="free")
    clock.enable_virtual(BASE + timedelta(hours=9, minutes=30))

    client = FakeLLMClient()
    touched = _wire_keepalive(manager.sea_runtime, persona, client)

    assert manager.sea_runtime.run_cache_keepalive(PERSONA_ID) is True
    assert len(client.calls) == 1
    assert len(touched) == 1


@patch("saiverse.model_configs.get_cache_config", return_value={"type": "explicit"})
def test_keepalive_skips_in_valley_between_lives(_mock_cache, manager):
    persona = manager.personas[PERSONA_ID]
    _save_life(manager, start="09:00", end="11:00", mode="free")
    clock.enable_virtual(BASE + timedelta(hours=12))  # ライフ終了後の谷

    client = FakeLLMClient()
    touched = _wire_keepalive(manager.sea_runtime, persona, client)

    assert manager.sea_runtime.run_cache_keepalive(PERSONA_ID) is False
    assert client.calls == []
    assert touched == []


@patch("saiverse.model_configs.get_cache_config", return_value={"type": "explicit"})
def test_keepalive_without_lives_declared_is_unaffected(_mock_cache, manager):
    """lives 未宣言のペルソナ/日は従来どおり keep-alive が走る (後方互換)。"""
    persona = manager.personas[PERSONA_ID]
    clock.enable_virtual(BASE + timedelta(hours=15))

    client = FakeLLMClient()
    touched = _wire_keepalive(manager.sea_runtime, persona, client)

    assert manager.sea_runtime.run_cache_keepalive(PERSONA_ID) is True
    assert len(client.calls) == 1
    assert len(touched) == 1


# ---------------------------------------------------------------------------
# 統合: ライフ境界を実発火させ、終端の全挙動を通しで確認
# ---------------------------------------------------------------------------


def test_life_boundary_simulation_end_behavior(manager):
    """even モードのライフ開始・終了処理を通しで確認:
    - 終端で anchor は不変 (惜しい谷でキャッシュヒット再開できる)
    - 終端直後は TTL override が残り、遅延解除予約がある
    - 遅延経過後 (EventScheduler の予約を仮想時刻で発火) に override が
      global 既定へ戻る

    ライフ開始/終了処理は起床・就寝の帳簿処理
    (``day_plan.handle_scheduled_life_boundary``) から呼ばれる。ここではその
    呼び出し方 (``apply_life_boundary`` を直接呼ぶ) を模して統合挙動を
    確認する — TTL 遅延解除の予約だけは EventScheduler 経由なので、仮想時刻を
    進めて ``run_due`` で発火させる (旧 DaySimulator は段 1-4 で撤去)。
    """
    persona = manager.personas[PERSONA_ID]
    lifecycle = manager.sea_runtime.session_lifecycle
    anchors = _live_anchors(updated_at=BASE + timedelta(hours=9, minutes=30))
    _save_anchor_rows(lifecycle, anchors)

    clock.enable_virtual(BASE + timedelta(hours=9))
    lives = _save_life(manager, start="09:00", end="09:40", mode="even")
    _life_start(manager, lives[0])
    assert manager.get_persona_cache_override(PERSONA_ID) == LIFE_SET_OVERRIDE

    clock.advance_to(BASE + timedelta(hours=9, minutes=40))
    _life_end(manager, lives[0])

    # anchor は不変 — 惜しい谷の再訪は生きたキャッシュで再開できる
    assert lifecycle.load_anchors(persona) == anchors
    # TTL override はまだ残り、遅延解除予約が立っている
    assert manager.get_persona_cache_override(PERSONA_ID) == LIFE_SET_OVERRIDE
    assert manager.event_scheduler.has_key(TTL_CLEAR_KEY)

    # 遅延経過後 (一日の終わりまで) 進めると override は解除される
    clock.advance_to(BASE + timedelta(hours=24))
    manager.event_scheduler.run_due(clock.now())
    assert manager.get_persona_cache_override(PERSONA_ID) is None
    # anchor は依然不変 (解除は override の話で、anchor には触らない)
    assert lifecycle.load_anchors(persona) == anchors


# ---------------------------------------------------------------------------
# 2026-10-12 Codex 敵対レビュー 段 3 6 巡目: 利用者のキャッシュ設定の保存が
# ライフの持ち分を引き取る (medium 2)・保温の送信直前のライフの再検査 (medium 4)
# ---------------------------------------------------------------------------


def _even_life_ended_with_held_clear(manager):
    """均等モードのライフを開始→終了し、遅延解除を手渡し済みにして返す。"""
    clock.enable_virtual(BASE + timedelta(hours=9))
    lives = _save_life(manager, start="09:00", end="09:40", mode="even")
    _life_start(manager, lives[0])
    clock.advance_to(BASE + timedelta(hours=9, minutes=40))
    _life_end(manager, lives[0])
    return _take_handed_off_ttl_clear(manager)


def test_a_user_save_while_the_ttl_clear_is_held_is_not_erased(manager):
    """medium 2 の再現順序: 解除の発火を保留 → 利用者が人設定タブで保存 → 解除の
    再開。利用者の保存は持ち主の記録を消す (ライフの持ち分ではなくなる) ので、
    解除は持ち主の不一致で何もしない — 利用者が 1h を選んでも、OFF を選んでも残る。"""
    held_clear = _even_life_ended_with_held_clear(manager)
    day_plan.apply_user_cache_override(manager, PERSONA_ID, enabled=True, ttl="1h")
    held_clear()
    assert manager.get_persona_cache_override(PERSONA_ID) == {"enabled": True, "ttl": "1h"}

    manager._persona_cache_overrides.clear()
    manager._life_ttl_owners.clear()
    # 次の営業日のライフで、利用者が OFF を選ぶ並び
    next_date = "2026-07-05"
    day_plan.save_lives(manager, PERSONA_ID, next_date, [
        {"start": "10:00", "end": "10:40", "mode": "even"},
    ])
    next_life = day_plan.get_lives(manager, PERSONA_ID, next_date)[0]
    clock.advance_to(BASE + timedelta(days=1, hours=10))
    _life_start(manager, next_life, plan_date=next_date)
    clock.advance_to(BASE + timedelta(days=1, hours=10, minutes=40))
    _life_end(manager, next_life, plan_date=next_date)
    held_clear_2 = _take_handed_off_ttl_clear(manager)
    day_plan.apply_user_cache_override(manager, PERSONA_ID, enabled=False, ttl="5m")
    held_clear_2()
    assert manager.get_persona_cache_override(PERSONA_ID) == {"enabled": False, "ttl": "5m"}


def test_a_user_save_racing_inside_the_ttl_clear_is_not_erased(manager):
    """medium 2 (Codex の再現): 解除が旧 1h を読んだ**後**・消す前に利用者が OFF を
    保存した。利用者の保存は解除と同じロックに並ぶので、解除が終わってから
    書かれる — OFF が残る (ロックを取らない保存は、解除の clear に消されていた)。"""
    import threading

    held_clear = _even_life_ended_with_held_clear(manager)
    real_get = manager.get_persona_cache_override
    user = {}

    def _read_then_user_saves(persona_id):
        value = real_get(persona_id)
        if "thread" not in user:
            user["thread"] = threading.Thread(
                target=day_plan.apply_user_cache_override,
                args=(manager, PERSONA_ID), kwargs={"enabled": False, "ttl": "5m"},
            )
            user["thread"].start()
            # ロックを取らない保存なら、ここで書き終わる
            user["thread"].join(timeout=0.3)
        return value

    manager.get_persona_cache_override = _read_then_user_saves
    try:
        held_clear()
    finally:
        manager.get_persona_cache_override = real_get
    user["thread"].join(timeout=5)
    assert not user["thread"].is_alive()
    assert manager.get_persona_cache_override(PERSONA_ID) == {"enabled": False, "ttl": "5m"}


@patch("saiverse.model_configs.get_cache_config", return_value={"type": "explicit"})
def test_keepalive_does_not_send_when_the_life_ends_during_composition(_mock_cache, manager):
    """medium 4 の再現順序: 冒頭のライフのゲートは通った (10:59:59) が、文脈の組成中に
    ライフが終わった (11:00:01)。送信の直前のライフの再検査で送らない — LLM を
    呼ばず、touch もしない (連鎖はここで止まる)。"""
    persona = manager.personas[PERSONA_ID]
    _save_life(manager, start="09:00", end="11:00", mode="free")
    clock.enable_virtual(BASE + timedelta(hours=10, minutes=59, seconds=59))

    client = FakeLLMClient()
    touched = _wire_keepalive(manager.sea_runtime, persona, client)
    composed = manager.sea_runtime._prepare_context

    def _compose_across_the_life_end(*a, **k):
        messages = composed(*a, **k)
        clock.advance_to(BASE + timedelta(hours=11, seconds=1))
        return messages

    manager.sea_runtime._prepare_context = _compose_across_the_life_end
    assert manager.sea_runtime.run_cache_keepalive(PERSONA_ID) is False
    assert client.calls == []
    assert touched == []


# ---------------------------------------------------------------------------
# 2026-10-12 Codex 敵対レビュー 段 3 7 巡目 medium 3: 保温のライフ生存の判定は
# 終了の節目 (ended) を見る
# ---------------------------------------------------------------------------


def _life_ended_early(manager):
    """07:00〜22:00 で確定・開始したライフを、就寝設定の前倒しで 12:00 に終えた
    (ended は立つが、保存された end は 22:00 のまま)。"""
    clock.enable_virtual(BASE + timedelta(hours=7))
    lives = _save_life(manager, start="07:00", end="22:00", mode="free")
    _life_start(manager, lives[0])
    clock.advance_to(BASE + timedelta(hours=12))
    _life_end(manager, lives[0])
    life = day_plan.get_lives(manager, PERSONA_ID, PLAN_DATE)[0]
    assert life["ended"] and life["end"] == "22:00"


def test_keepalive_is_not_allowed_after_an_early_life_end(manager):
    """medium 3 の再現: 終了の節目が済んだ後、保存された end (22:00) より前の
    時刻 (12:30) でも許可しない。開始済み・未終了の区間内は従来どおり許可。"""
    clock.enable_virtual(BASE + timedelta(hours=7))
    lives = _save_life(manager, start="07:00", end="22:00", mode="free")
    _life_start(manager, lives[0])
    clock.advance_to(BASE + timedelta(hours=11))
    assert day_plan.is_keepalive_allowed(manager, PERSONA_ID) is True

    clock.advance_to(BASE + timedelta(hours=12))
    _life_end(manager, lives[0])
    clock.advance_to(BASE + timedelta(hours=12, minutes=30))
    assert day_plan.is_keepalive_allowed(manager, PERSONA_ID) is False


@patch("saiverse.model_configs.get_cache_config", return_value={"type": "explicit"})
def test_keepalive_after_an_early_life_end_neither_sends_nor_rearms(_mock_cache, manager):
    """medium 3: 終了の節目の後に届いた保温 (手渡し済みで取り消しが届かなかった
    もの) は、保存された区間の内側でも送らない — LLM を呼ばず、touch (= 次の保温の
    再予約の入口) もしない。"""
    persona = manager.personas[PERSONA_ID]
    _life_ended_early(manager)
    clock.advance_to(BASE + timedelta(hours=12, minutes=30))

    client = FakeLLMClient()
    touched = _wire_keepalive(manager.sea_runtime, persona, client)
    assert manager.sea_runtime.run_cache_keepalive(PERSONA_ID) is False
    assert client.calls == []
    assert touched == []
    assert not any(
        key.startswith(f"ttl:{PERSONA_ID}")
        for key in manager.event_scheduler._entries_by_key
    )


@patch("saiverse.model_configs.get_cache_config", return_value={"type": "explicit"})
def test_keepalive_does_not_send_when_an_early_life_end_lands_during_composition(
    _mock_cache, manager,
):
    """medium 3 (レビューの再現順序): 冒頭のライフのゲートは通った (11:59、区間内・
    未終了)。保温の組成の最中に、前倒しした就寝設定で終了の節目が走った (ended が
    立つ。保存された end は 22:00 のまま)。送信の直前の再検査で送らない。"""
    persona = manager.personas[PERSONA_ID]
    clock.enable_virtual(BASE + timedelta(hours=7))
    lives = _save_life(manager, start="07:00", end="22:00", mode="free")
    _life_start(manager, lives[0])
    clock.advance_to(BASE + timedelta(hours=11, minutes=59))

    client = FakeLLMClient()
    touched = _wire_keepalive(manager.sea_runtime, persona, client)
    composed = manager.sea_runtime._prepare_context

    def _compose_across_the_early_life_end(*a, **k):
        messages = composed(*a, **k)
        clock.advance_to(BASE + timedelta(hours=12))
        _life_end(manager, lives[0])
        return messages

    manager.sea_runtime._prepare_context = _compose_across_the_early_life_end
    assert manager.sea_runtime.run_cache_keepalive(PERSONA_ID) is False
    assert client.calls == []
    assert touched == []
