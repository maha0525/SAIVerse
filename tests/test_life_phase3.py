"""ライフ Phase 3「キャッシュ連動」のテスト (docs/intent/life.md §5.1/§5.2/§6.2 v0.4)。

対象:
- keep-alive のライフ従属 (``day_plan.is_keepalive_allowed`` /
  ``sea.runtime.SEARuntime.run_cache_keepalive``): lives 未宣言は従来どおり許可、
  宣言済みの日はライフ区間内のみ許可 (谷では touch せず連鎖を自然停止)、
  判定失敗時は許可側にフォールバック
- ライフ終端の節目 (``day_plan._handle_life_end``、§6.2 v0.4): 終端が能動的に
  行うのは keep-alive 予約 (``ttl:{persona_id}``) の cancel と TTL override の
  遅延解除予約だけ。**anchor は触らない** (METABOLISM_ANCHORS 不変) — touch が
  止まれば TTL で自然失効する。即時失効は惜しい谷 (終了直後〜TTL 内の再訪、
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
from saiverse.day_simulator import DaySimulator
from saiverse.event_scheduler import EventScheduler
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
    """SAIMemory adapter の最小スタブ (append_persona_message の記録のみ)。"""

    def __init__(self):
        self.messages: List[Dict[str, Any]] = []

    def append_persona_message(self, payload):
        self.messages.append(payload)


@pytest.fixture
def manager(session_factory):
    db = session_factory()
    try:
        db.add(User(USERID=1, PASSWORD="x", USERNAME="tester"))
        db.flush()
        city = City(USERID=1, CITYNAME="test_city", UI_PORT=3001, API_PORT=8001)
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

    runtime = SEARuntime(mgr)
    mgr.sea_runtime = runtime
    return mgr


def _save_life(manager, *, start="09:00", end="11:00", budget=4, mode="free"):
    day_plan.save_lives(manager, PERSONA_ID, PLAN_DATE, [
        {"start": start, "end": end, "budget_pulses": budget, "mode": mode},
    ])
    return day_plan.get_lives(manager, PERSONA_ID, PLAN_DATE)


def _live_anchors(anchor_id="abc123", ttl_seconds=3600, updated_at=None):
    return {
        "claude-x": {
            "anchor_id": anchor_id,
            "updated_at": (updated_at or datetime.now()).isoformat(),
            "ttl_seconds": ttl_seconds,
        },
    }


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
    再開できるよう、METABOLISM_ANCHORS は不変のまま TTL の自然失効に任せる。"""
    persona = manager.personas[PERSONA_ID]
    lifecycle = manager.sea_runtime.session_lifecycle
    anchors = _live_anchors()
    lifecycle.save_anchors(persona, anchors)

    lives = _save_life(manager, start="09:00", end="11:00", mode="free")
    day_plan._handle_life_end(manager, PERSONA_ID, PLAN_DATE, 0, lives[0])

    # anchor は不変。TTL 内の resolve は従来どおり self anchor を返す
    # (= 惜しい谷の再訪は Case 3 に落ちず、生きたキャッシュで再開できる)
    assert lifecycle.load_anchors(persona) == anchors
    anchor_id, resolution = lifecycle.resolve_metabolism_anchor(persona)
    assert (anchor_id, resolution) == ("abc123", "self")


def test_life_end_cancels_keepalive_reservation(manager):
    manager.event_scheduler.schedule(
        fire_at=BASE + timedelta(hours=5), callback=lambda: None,
        key=f"ttl:{PERSONA_ID}",
    )
    assert manager.event_scheduler.has_key(f"ttl:{PERSONA_ID}")

    lives = _save_life(manager, start="09:00", end="11:00", mode="free")
    day_plan._handle_life_end(manager, PERSONA_ID, PLAN_DATE, 0, lives[0])

    assert not manager.event_scheduler.has_key(f"ttl:{PERSONA_ID}")


def test_life_end_notifies_boundary(manager):
    lives = _save_life(manager, start="09:00", end="11:00", mode="free")
    day_plan._handle_life_end(manager, PERSONA_ID, PLAN_DATE, 0, lives[0])
    texts = [m["content"] for m in manager.personas[PERSONA_ID].sai_memory.messages]
    assert any("活動終了" in t for t in texts)


def test_life_end_without_session_lifecycle_does_not_crash(manager):
    """sea_runtime が無い異常系でも例外を投げない。TTL clear の遅延解決は
    既定 (3600s) にフォールバックして予約され、通知も正常に記録される。"""
    manager.sea_runtime = None
    clock.enable_virtual(BASE + timedelta(hours=9, minutes=40))
    lives = _save_life(manager, start="09:00", end="09:40", budget=2, mode="even")
    day_plan._handle_life_end(manager, PERSONA_ID, PLAN_DATE, 0, lives[0])
    texts = [m["content"] for m in manager.personas[PERSONA_ID].sai_memory.messages]
    assert any("活動終了" in t for t in texts)
    assert manager.event_scheduler.has_key(TTL_CLEAR_KEY)


# ---------------------------------------------------------------------------
# 均等モードの cache TTL 運転 (life.md §5.1 / §6.2 v0.4: 遅延解除)
# ---------------------------------------------------------------------------


def test_even_mode_life_start_sets_ttl_override(manager):
    lives = _save_life(manager, start="09:00", end="09:40", budget=2, mode="even")
    assert manager.get_persona_cache_override(PERSONA_ID) is None
    day_plan._handle_life_start(manager, PERSONA_ID, PLAN_DATE, 0, lives[0])
    assert manager.get_persona_cache_override(PERSONA_ID) == LIFE_SET_OVERRIDE


def test_even_mode_life_end_schedules_delayed_clear_not_immediate(manager):
    """終端では即時 clear せず、遅延解除の予約だけ入る。override は残る
    (即時に 5m へ戻すと anchor の生存評価が実キャッシュの寿命とズレるため)。"""
    clock.enable_virtual(BASE + timedelta(hours=9, minutes=40))
    lives = _save_life(manager, start="09:00", end="09:40", budget=2, mode="even")
    day_plan._handle_life_start(manager, PERSONA_ID, PLAN_DATE, 0, lives[0])
    assert manager.get_persona_cache_override(PERSONA_ID) == LIFE_SET_OVERRIDE

    day_plan._handle_life_end(manager, PERSONA_ID, PLAN_DATE, 0, lives[0])
    # 即時 clear されない
    assert manager.get_persona_cache_override(PERSONA_ID) == LIFE_SET_OVERRIDE
    # 遅延解除の予約が入っている
    assert manager.event_scheduler.has_key(TTL_CLEAR_KEY)

    # 発火体: 厳密一致するので clear される
    day_plan._clear_life_ttl_override(manager, PERSONA_ID)
    assert manager.get_persona_cache_override(PERSONA_ID) is None


def test_life_ttl_clear_fire_respects_user_change(manager):
    """予約〜発火の間にユーザーが override を変更していたら、発火体は触らない。"""
    clock.enable_virtual(BASE + timedelta(hours=9, minutes=40))
    lives = _save_life(manager, start="09:00", end="09:40", budget=2, mode="even")
    day_plan._handle_life_start(manager, PERSONA_ID, PLAN_DATE, 0, lives[0])
    day_plan._handle_life_end(manager, PERSONA_ID, PLAN_DATE, 0, lives[0])

    # 発火前にユーザーが人設定タブで明示変更
    manager.set_persona_cache_override(PERSONA_ID, enabled=True, ttl="5m")
    day_plan._clear_life_ttl_override(manager, PERSONA_ID)
    assert manager.get_persona_cache_override(PERSONA_ID) == {"enabled": True, "ttl": "5m"}


def test_next_life_start_cancels_pending_ttl_clear(manager):
    """次のライフが TTL 経過前に始まったら、前のライフの遅延解除予約を cancel
    する — ライフの最中に解除が発火して override が外れる事故を防ぐ。"""
    clock.enable_virtual(BASE + timedelta(hours=9, minutes=40))
    day_plan.save_lives(manager, PERSONA_ID, PLAN_DATE, [
        {"start": "09:00", "end": "09:40", "budget_pulses": 2, "mode": "even"},
        {"start": "10:00", "end": "10:40", "budget_pulses": 2, "mode": "even"},
    ])
    lives = day_plan.get_lives(manager, PERSONA_ID, PLAN_DATE)

    day_plan._handle_life_start(manager, PERSONA_ID, PLAN_DATE, 0, lives[0])
    day_plan._handle_life_end(manager, PERSONA_ID, PLAN_DATE, 0, lives[0])
    assert manager.event_scheduler.has_key(TTL_CLEAR_KEY)

    # 20 分後に次のライフが開始 (TTL 経過前)
    clock.advance_to(BASE + timedelta(hours=10))
    day_plan._handle_life_start(manager, PERSONA_ID, PLAN_DATE, 1, lives[1])
    assert not manager.event_scheduler.has_key(TTL_CLEAR_KEY)
    # override は 1h のまま維持される (前のライフの値が望む値と同じ)
    assert manager.get_persona_cache_override(PERSONA_ID) == LIFE_SET_OVERRIDE


def test_free_mode_life_does_not_touch_cache_ttl(manager):
    lives = _save_life(manager, start="09:00", end="11:00", mode="free")
    day_plan._handle_life_start(manager, PERSONA_ID, PLAN_DATE, 0, lives[0])
    assert manager.get_persona_cache_override(PERSONA_ID) is None
    day_plan._handle_life_end(manager, PERSONA_ID, PLAN_DATE, 0, lives[0])
    assert manager.get_persona_cache_override(PERSONA_ID) is None
    assert not manager.event_scheduler.has_key(TTL_CLEAR_KEY)


def test_even_mode_life_respects_existing_explicit_override(manager):
    """ユーザーが人設定タブで明示設定した override は、ライフの宣言で上書き
    しないし、遅延解除の発火でも clear されない (厳密一致しないため)。"""
    manager.set_persona_cache_override(PERSONA_ID, enabled=True, ttl="5m")
    clock.enable_virtual(BASE + timedelta(hours=9, minutes=40))
    lives = _save_life(manager, start="09:00", end="09:40", budget=2, mode="even")

    day_plan._handle_life_start(manager, PERSONA_ID, PLAN_DATE, 0, lives[0])
    assert manager.get_persona_cache_override(PERSONA_ID) == {"enabled": True, "ttl": "5m"}

    day_plan._handle_life_end(manager, PERSONA_ID, PLAN_DATE, 0, lives[0])
    day_plan._clear_life_ttl_override(manager, PERSONA_ID)
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
    runtime.session_lifecycle.load_anchors = lambda p: (
        anchors if anchors is not None else _live_anchors(anchor_id="a1")
    )
    runtime._prepare_context = (
        lambda p, b, u, *a, **k: list(messages or [{"role": "user", "content": "履歴"}])
    )
    runtime._select_llm_client = lambda node_def, p, **k: client
    touched: List[Any] = []
    runtime.session_lifecycle.touch_anchor_after_llm_call = lambda p, usage: touched.append(usage)
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
# 統合: DaySimulator でライフ境界を実発火させ、終端の全挙動を通しで確認
# ---------------------------------------------------------------------------


def test_life_boundary_simulation_end_behavior(manager):
    """even モードのライフ開始・終了処理を通しで確認:
    - 終端で anchor は不変 (惜しい谷でキャッシュヒット再開できる)
    - 終端直後は TTL override が残り、遅延解除予約がある
    - 遅延経過後 (DaySimulator で予約を発火) に override が global 既定へ戻る

    v0.5 (life.md §11.2): 専用のライフ境界イベント予約 (``schedule_lives``)
    は廃止され、ライフ開始/終了処理は day_open/day_close の発火経路
    (``autonomy_wiring.fire_judgment_point``) 直下で呼ばれる。ここではその
    呼び出し方 (``_handle_life_start``/``_handle_life_end`` を直接呼ぶ) を
    模して統合挙動を確認する — TTL 遅延解除の予約だけは引き続き
    EventScheduler 経由なので DaySimulator で発火させる。
    """
    persona = manager.personas[PERSONA_ID]
    lifecycle = manager.sea_runtime.session_lifecycle
    anchors = _live_anchors(updated_at=BASE + timedelta(hours=9, minutes=30))
    lifecycle.save_anchors(persona, anchors)

    clock.enable_virtual(BASE + timedelta(hours=9))
    lives = _save_life(manager, start="09:00", end="09:40", budget=2, mode="even")
    day_plan._handle_life_start(manager, PERSONA_ID, PLAN_DATE, 0, lives[0])
    assert manager.get_persona_cache_override(PERSONA_ID) == LIFE_SET_OVERRIDE

    clock.advance_to(BASE + timedelta(hours=9, minutes=40))
    day_plan._handle_life_end(manager, PERSONA_ID, PLAN_DATE, 0, lives[0])

    # anchor は不変 — 惜しい谷の再訪は生きたキャッシュで再開できる
    assert lifecycle.load_anchors(persona) == anchors
    # TTL override はまだ残り、遅延解除予約が立っている
    assert manager.get_persona_cache_override(PERSONA_ID) == LIFE_SET_OVERRIDE
    assert manager.event_scheduler.has_key(TTL_CLEAR_KEY)

    # 遅延経過後 (一日の終わりまで) 進めると override は解除される
    DaySimulator(
        manager.event_scheduler,
        start=BASE + timedelta(hours=9, minutes=40), end=BASE + timedelta(hours=24),
    ).run()
    assert manager.get_persona_cache_override(PERSONA_ID) is None
    # anchor は依然不変 (解除は override の話で、anchor には触らない)
    assert lifecycle.load_anchors(persona) == anchors
