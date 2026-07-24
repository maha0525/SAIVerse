"""MetaLayer unit tests.

判断経路は Playbook path (``_run_judgment_via_playbook`` →
``submit_meta_judgment``) に一本化されている。旧 legacy direct-LLM スペル
ループ (``_run_judgment``) は撤去済み
(docs/issues/archive/meta_judgment_legacy_path_lossy_and_unreachable.md, 2026-07-24)。

検証項目:
- alert / periodic tick observer エントリ → dispatch 起動 (per-persona Lock 直列化)
- 状況テキスト構築 (``_build_situation_text``: alert marker / preempt_collision)
- meta_judgment_log の永続化と過去ログ注入 (``_record_judgment_log`` /
  ``_build_recent_judgments_block``)
- Playbook path の retry 挙動 (``_FakePulseController`` 経由)
"""
import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from database.models import AI, Base, City, User
from saiverse.meta_layer import MetaLayer
from saiverse.track_manager import TrackManager


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

class FakeLLMClient:
    """順次応答を返す Fake LLM。"""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []  # 各呼び出しの messages を記録

    def generate(self, messages, tools=None, response_schema=None, **kwargs):
        self.calls.append({
            "messages": messages,
            "tools": tools,
            "response_schema": response_schema,
        })
        if not self._responses:
            return ""  # デフォルトで終了
        return self._responses.pop(0)


class FakePersona:
    """最小限のペルソナスタブ。"""

    def __init__(self, persona_id, llm_client, system_prompt=""):
        self.persona_id = persona_id
        self._llm = llm_client
        self.system_prompt = system_prompt
        self.persona_log_path = None
        self.manager_ref = None

    @property
    def llm_client(self):
        return self._llm


class FakeManager:
    """SAIVerseManager の MetaLayer 依存部分だけ持つスタブ。"""

    def __init__(self, track_manager, personas):
        self.track_manager = track_manager
        self.personas = personas
        self.SessionLocal = None  # meta_judgment_log を検証するテストだけ差し替える
        # Playbook path のテスト用 (retry 検証等)。dispatch テストでは
        # _run_judgment_via_playbook を monkeypatch するので触らない。
        self.sea_runtime = None


@pytest.fixture
def session_factory():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    yield Session
    engine.dispose()


@pytest.fixture
def db_persona(session_factory):
    db = session_factory()
    try:
        db.add(User(USERID=1, PASSWORD="x", USERNAME="tester"))
        db.flush()
        city = City(USERID=1, CITYNAME="test_city", UI_PORT=3001, API_PORT=8001)
        db.add(city)
        db.flush()
        db.add(AI(AIID="alice", HOME_CITYID=city.CITYID, AINAME="Alice"))
        db.commit()
    finally:
        db.close()
    return "alice"


@pytest.fixture
def tm(session_factory):
    return TrackManager(session_factory=session_factory)


def _make_meta_layer(tm, persona_id, llm_responses):
    llm = FakeLLMClient(llm_responses)
    persona = FakePersona(persona_id, llm)
    manager = FakeManager(tm, {persona_id: persona})
    return MetaLayer(manager), llm, persona


# ---------------------------------------------------------------------------
# observer エントリ
# ---------------------------------------------------------------------------

def test_unknown_persona_skips_silently(tm, db_persona, caplog):
    """persona が見つからない場合は警告ログだけ、例外は出ない。"""
    meta, llm, _persona = _make_meta_layer(tm, db_persona, [])
    meta.on_track_alert("unknown_persona", "fake_track", {})
    # LLM は呼ばれない
    assert llm.calls == []


# ---------------------------------------------------------------------------
# 状況テキストの内容 (_build_situation_text: Playbook path が judge に渡す材料)
# ---------------------------------------------------------------------------

def test_state_message_includes_alert_marker(tm, db_persona):
    """状況テキストに alert Track のマーカーが含まれる。"""
    track_id = tm.create(
        db_persona, "user_conversation", title="対 user1", is_persistent=True
    )
    tm.activate(track_id)
    tm.pause(track_id)
    tm.set_alert(track_id)

    meta, _llm, _persona = _make_meta_layer(tm, db_persona, [])
    msg = meta._build_situation_text(
        db_persona, track_id, {"trigger": "user_utterance"}
    )
    assert "対 user1" in msg
    # メタ判断 v2 状況分類で alert_present として描画される
    assert "alert 状態" in msg


# ---------------------------------------------------------------------------
# 自律先制と外部 alert のレース (Phase 2.6, 2026-05-01)
# ---------------------------------------------------------------------------

def test_set_alert_on_running_track_still_notifies_observer(tm, db_persona):
    """既 running の Track に set_alert したとき、状態は no-op だが
    observer には target_already_running=True 付きで通知される。"""
    track_id = tm.create(db_persona, "user_conversation", title="対 user1", is_persistent=True)
    tm.activate(track_id)  # running に

    captured = []
    tm.add_alert_observer(lambda pid, tid, ctx: captured.append((pid, tid, ctx)))

    tm.set_alert(track_id, context={"trigger": "user_utterance"})

    assert len(captured) == 1
    pid, tid, ctx = captured[0]
    assert pid == db_persona
    assert tid == track_id
    assert ctx.get("target_already_running") is True
    assert ctx.get("target_track_title") == "対 user1"
    assert ctx.get("target_track_type") == "user_conversation"
    assert ctx.get("trigger") == "user_utterance"  # 元の context は維持


def test_set_alert_on_already_alert_does_not_notify(tm, db_persona):
    """既 alert の Track に set_alert しても重複通知は走らない。"""
    track_id = tm.create(db_persona, "user_conversation", is_persistent=True)
    tm.activate(track_id)
    tm.pause(track_id)
    tm.set_alert(track_id)  # alert に遷移、ここで 1 回通知

    captured = []
    tm.add_alert_observer(lambda pid, tid, ctx: captured.append(ctx))

    tm.set_alert(track_id)  # 既に alert なので no-op

    assert captured == []  # 重複通知なし


def test_state_message_explains_target_already_running(tm, db_persona):
    """_build_situation_text は target_already_running を自然言語で説明する。"""
    track_id = tm.create(db_persona, "user_conversation", title="対 user1", is_persistent=True)
    tm.activate(track_id)
    tm.pause(track_id)
    tm.set_alert(track_id)

    meta, _llm, _persona = _make_meta_layer(tm, db_persona, [])

    msg = meta._build_situation_text(
        db_persona, track_id,
        context={
            "trigger": "user_utterance",
            "target_already_running": True,
            "target_track_title": "対 user1",
        },
    )
    # メタ判断 v2 状況分類で preempt_collision として描画される
    assert "先制起動済み" in msg
    assert "対 user1" in msg
    assert "メインライン側で応答処理" in msg


def test_state_message_omits_race_block_when_not_already_running(tm, db_persona):
    """通常の alert (state 遷移ありの場合) は target_already_running 説明を出さない。"""
    track_id = tm.create(db_persona, "user_conversation", is_persistent=True)
    tm.activate(track_id)
    tm.pause(track_id)
    tm.set_alert(track_id)

    meta, _llm, _persona = _make_meta_layer(tm, db_persona, [])
    msg = meta._build_situation_text(
        db_persona, track_id, context={"trigger": "user_utterance"}
    )
    assert "既に running 状態" not in msg


# ---------------------------------------------------------------------------
# life.md §7 案 Y (2026-07-13): wait_response タイムアウトの自己ゲート化回避
#
# TrackManager がタイムアウトで Track を pause しなくなったため、social 等
# (user_conversation 以外) の wait_response Track は自分自身のタイムアウトで
# on_periodic_tick を呼んだ時点でもまだ running のまま。on_periodic_tick の
# 「running Track が wait_response 型なら抑止する」ゲートが素朴だと、この
# 自分自身の呼び出しまでブロックしてしまう (社交 Track の判断が二度と
# 起動できなくなる自己ゲート化)。context の trigger/track_id が「自分自身の
# タイムアウト」を示す場合だけ例外的にゲートを通す。
# ---------------------------------------------------------------------------

def test_periodic_tick_not_self_gated_by_own_wait_response_timeout(
    tm, db_persona, session_factory, monkeypatch,
):
    """自分自身の wait_response タイムアウト起因の tick は抑止されない。"""
    from types import SimpleNamespace

    track_id = tm.create(
        db_persona, "social", is_persistent=True, initial_status="running",
    )

    persona = FakePersona(db_persona, FakeLLMClient(["dummy"]))
    persona.autonomy_enabled = True
    manager = FakeManager(tm, {db_persona: persona})
    manager.SessionLocal = session_factory
    meta = MetaLayer(manager)

    calls = []
    monkeypatch.setattr(
        meta, "_run_judgment_via_playbook",
        lambda p, atid, ctx: calls.append(ctx),
    )
    monkeypatch.setattr(
        meta, "_get_handler_for_track",
        lambda track: SimpleNamespace(post_complete_behavior="wait_response"),
    )

    meta.on_periodic_tick(
        db_persona,
        context={"trigger": "wait_response_timeout", "track_id": track_id},
    )
    assert len(calls) == 1
    assert calls[0]["trigger"] == "wait_response_timeout"


def test_periodic_tick_still_gated_for_unrelated_trigger(
    tm, db_persona, session_factory, monkeypatch,
):
    """通常の定期 tick (自分自身のタイムアウトでない) は従来どおり抑止される
    (退行防止 — 自己ゲート回避の例外を広げすぎていないことの確認)。"""
    from types import SimpleNamespace

    tm.create(
        db_persona, "social", is_persistent=True, initial_status="running",
    )

    persona = FakePersona(db_persona, FakeLLMClient(["dummy"]))
    persona.autonomy_enabled = True
    manager = FakeManager(tm, {db_persona: persona})
    manager.SessionLocal = session_factory
    meta = MetaLayer(manager)

    calls = []
    monkeypatch.setattr(
        meta, "_run_judgment_via_playbook",
        lambda p, atid, ctx: calls.append(ctx),
    )
    monkeypatch.setattr(
        meta, "_get_handler_for_track",
        lambda track: SimpleNamespace(post_complete_behavior="wait_response"),
    )

    meta.on_periodic_tick(db_persona, context={"cycle_id": "x"})
    assert calls == []


# ---------------------------------------------------------------------------
# Per-persona 直列化 Lock (handoff_2026-04-30 Part 1)
# ---------------------------------------------------------------------------

def test_alert_and_periodic_tick_are_serialized_per_persona(
    tm, db_persona, monkeypatch
):
    """同一ペルソナへの alert と periodic_tick が重ならず直列実行される。

    Intent A v0.9 不変条件 11 ("メタ判断 = ペルソナ自身の思考の流れ 1 本") の保証。
    両入口は別 thread から呼ばれうる:
      - alert: chat thread から `on_track_alert`
      - 定期: AutonomyManager background thread から `on_periodic_tick`
    入口で per-persona Lock を取って直列化する。
    """
    import threading
    import time as time_mod

    track_id = tm.create(db_persona, "user_conversation", is_persistent=True)
    tm.activate(track_id)
    tm.pause(track_id)
    tm.set_alert(track_id)

    # 判断本体 (`_run_judgment_via_playbook`) を「100ms 寝るだけ」のスタブに置換し、
    # 重なりが起きるかを実時間で観察する。playbook 経路は default。
    in_progress = []
    overlaps_observed = []
    body_lock = threading.Lock()

    def stub_judgment(persona, alert_track_id, context):
        with body_lock:
            in_progress.append(context.get("trigger", "?"))
            if len(in_progress) > 1:
                overlaps_observed.append(list(in_progress))
        time_mod.sleep(0.1)
        with body_lock:
            in_progress.remove(context.get("trigger", "?"))

    # AUTONOMY_ENABLED が False だと periodic_tick は skip される。
    # 判断本体スタブがその先で動くよう FakePersona に属性を生やす。
    persona = FakePersona(db_persona, FakeLLMClient(["dummy"]))
    persona.autonomy_enabled = True
    manager = FakeManager(tm, {db_persona: persona})
    meta = MetaLayer(manager)

    monkeypatch.setattr(meta, "_run_judgment_via_playbook", stub_judgment)
    # 走行中 Track 取得は不要 (running は pause 済み): None を返すので wait_response 抑止は走らない

    t1 = threading.Thread(
        target=meta.on_track_alert,
        args=(db_persona, track_id, {"trigger": "user_utterance"}),
    )
    t2 = threading.Thread(
        target=meta.on_periodic_tick,
        args=(db_persona, {"cycle_id": "x"}),
    )

    t1.start()
    t2.start()
    t1.join(timeout=5)
    t2.join(timeout=5)

    # 重なりが一度も観測されない (= 直列化されている)
    assert overlaps_observed == [], (
        f"Meta judgment ran in parallel for the same persona: {overlaps_observed}"
    )


# ---------------------------------------------------------------------------
# meta_judgment_log への永続化 (Phase 2 / handoff Part 2)
# ---------------------------------------------------------------------------

def _make_meta_with_db(tm, session_factory, persona_id, llm_responses):
    """FakeManager に SessionLocal を載せた版。meta_judgment_log の書き込みを検証する用。"""
    llm = FakeLLMClient(llm_responses)
    persona = FakePersona(persona_id, llm)
    manager = FakeManager(tm, {persona_id: persona})
    manager.SessionLocal = session_factory
    return MetaLayer(manager), llm, persona


def test_recent_judgments_block_renders_chronologically(
    tm, db_persona, session_factory
):
    """過去の判断ログが新しい順に整形される。"""
    meta, _llm, _persona = _make_meta_with_db(
        tm, session_factory, db_persona, []
    )

    # 3 件の判断ログを直接書く (古い→新しい順)
    for i, trigger in enumerate(["periodic_tick", "user_utterance", "alert"]):
        meta._record_judgment_log(
            persona_id=db_persona,
            trigger_type=trigger,
            trigger_context=None,
            track_at_judgment_id=None,
            thought_parts=[f"判断 #{i}: 今は {trigger} で問題ない。"],
            spells=[{"name": "track_pause", "args": {}, "result": "ok"}] if i == 1 else [],
            committed_to_main_cache=(i == 1),
        )

    block = meta._build_recent_judgments_block(db_persona)
    assert "[最近のメタ判断ログ" in block
    # 新しい順なので alert が先頭、periodic_tick が末尾
    alert_pos = block.find("alert")
    user_pos = block.find("user_utterance")
    periodic_pos = block.find("periodic_tick")
    assert 0 < alert_pos < user_pos < periodic_pos
    # committed=True の行に [committed] マーカー、スペル行は正準 /spell 形式で出る
    assert "[committed]" in block
    assert "/spell name='track_pause'" in block


def test_recent_judgments_empty_when_no_history(
    tm, db_persona, session_factory
):
    """過去ログがゼロのとき空文字列を返す (プロンプトに余計な改行を入れない)。"""
    meta, _llm, _persona = _make_meta_with_db(
        tm, session_factory, db_persona, []
    )
    assert meta._build_recent_judgments_block(db_persona) == ""


def test_locks_are_independent_per_persona(tm, session_factory, monkeypatch):
    """別ペルソナの判断は並行できる (Lock は persona ごと独立)。"""
    import threading
    import time as time_mod

    # 2 ペルソナをセットアップ
    db = session_factory()
    try:
        db.add(User(USERID=1, PASSWORD="x", USERNAME="tester"))
        db.flush()
        city = City(USERID=1, CITYNAME="test_city", UI_PORT=3001, API_PORT=8001)
        db.add(city)
        db.flush()
        db.add(AI(AIID="alice", HOME_CITYID=city.CITYID, AINAME="Alice"))
        db.add(AI(AIID="bob", HOME_CITYID=city.CITYID, AINAME="Bob"))
        db.commit()
    finally:
        db.close()

    track_a = tm.create("alice", "user_conversation", is_persistent=True)
    tm.activate(track_a)
    tm.pause(track_a)
    tm.set_alert(track_a)

    track_b = tm.create("bob", "user_conversation", is_persistent=True)
    tm.activate(track_b)
    tm.pause(track_b)
    tm.set_alert(track_b)

    in_progress = []
    parallel_seen = []
    body_lock = threading.Lock()

    def stub_judgment(persona, alert_track_id, context):
        with body_lock:
            in_progress.append(persona.persona_id)
            if len(in_progress) >= 2:
                parallel_seen.append(list(in_progress))
        time_mod.sleep(0.15)
        with body_lock:
            in_progress.remove(persona.persona_id)

    alice = FakePersona("alice", FakeLLMClient(["dummy"]))
    bob = FakePersona("bob", FakeLLMClient(["dummy"]))
    manager = FakeManager(tm, {"alice": alice, "bob": bob})
    meta = MetaLayer(manager)
    monkeypatch.setattr(meta, "_run_judgment_via_playbook", stub_judgment)

    t1 = threading.Thread(
        target=meta.on_track_alert, args=("alice", track_a, {"trigger": "test"})
    )
    t2 = threading.Thread(
        target=meta.on_track_alert, args=("bob", track_b, {"trigger": "test"})
    )
    t1.start()
    t2.start()
    t1.join(timeout=5)
    t2.join(timeout=5)

    # 別ペルソナなので並行実行が観測されている
    assert parallel_seen, "別ペルソナ同士は並行できるはず"


# ---------------------------------------------------------------------------
# Phase 4-e: _run_judgment_via_playbook の retry 挙動
# ---------------------------------------------------------------------------


class _FakeRuntime:
    """sea_runtime.run_meta_user の最小スタブ。

    side_effect が callable ならそれを使う、リストなら順次取り出す。
    """

    def __init__(self, side_effects=None):
        self.side_effects = side_effects or []
        self.calls = 0

    def run_meta_user(self, persona, **kwargs):
        self.calls += 1
        if self.side_effects:
            effect = self.side_effects.pop(0)
            if isinstance(effect, BaseException):
                raise effect
            if callable(effect):
                effect(persona, **kwargs)


class _FakePulseController:
    """pulse_dispatch.md §6.3 案 A 対応: MetaLayer は pulse_controller.submit_meta_judgment 経由で
    runtime.run_meta_user を呼ぶ。テストでは Fake が submit_meta_judgment をそのまま runtime に転送する。
    """

    def __init__(self, runtime, persona):
        self.runtime = runtime
        self.persona = persona

    def submit_meta_judgment(
        self,
        persona_id,
        building_id,
        meta_playbook,
        args=None,
        event_callback=None,
    ):
        self.runtime.run_meta_user(
            self.persona,
            user_input=None,
            building_id=building_id,
            meta_playbook=meta_playbook,
            args=args,
            event_callback=event_callback,
            pulse_type="meta_judgment",
        )


def _make_playbook_meta(tm, db_persona, runtime, judgment_config_override=None):
    """Playbook path テスト用の MetaLayer を組み立てる。"""
    persona = FakePersona(db_persona, FakeLLMClient([]))
    persona.current_building_id = "test_building"
    persona.persona_id = db_persona
    manager = FakeManager(tm, {db_persona: persona})
    manager.sea_runtime = runtime
    # pulse_dispatch.md §6.3: MetaLayer は pulse_controller 経由で起動する
    manager.pulse_controller = _FakePulseController(runtime, persona)
    meta = MetaLayer(manager)
    if judgment_config_override is not None:
        meta._load_judgment_config = lambda _p: judgment_config_override
    # 過去判断ログは空でいい (DB 読みを回避)
    meta._build_recent_judgments_block = lambda _pid: ""
    return meta, persona


def test_playbook_retries_on_runtime_exception(tm, db_persona):
    """run_meta_user が例外を投げた場合、max_retries 回まで再試行する。"""
    runtime = _FakeRuntime(side_effects=[
        RuntimeError("boom #1"),
        RuntimeError("boom #2"),
        None,  # 3 回目で成功
    ])
    meta, persona = _make_playbook_meta(
        tm, db_persona, runtime,
        judgment_config_override={
            "max_retries": 2,
            "retry_backoff_seconds": 0,
            "cache_threshold_ratio": 0.3,
            "periodic_interval_minutes": 50,
        },
    )
    meta._run_judgment_via_playbook(persona, "track_x", {"trigger": "test"})
    assert runtime.calls == 3, "1 回失敗 + retry 1 失敗 + retry 2 成功 = 3 回"


def test_playbook_no_retry_when_max_retries_zero(tm, db_persona):
    """max_retries=0 ならリトライなしで 1 回だけ呼ばれる (失敗してもそのまま諦める)。"""
    runtime = _FakeRuntime(side_effects=[RuntimeError("boom")])
    meta, persona = _make_playbook_meta(
        tm, db_persona, runtime,
        judgment_config_override={
            "max_retries": 0,
            "retry_backoff_seconds": 0,
            "cache_threshold_ratio": 0.3,
            "periodic_interval_minutes": 50,
        },
    )
    meta._run_judgment_via_playbook(persona, "track_x", {"trigger": "test"})
    assert runtime.calls == 1


def test_playbook_exhausts_retries_and_logs_warning(tm, db_persona, caplog):
    """全試行失敗時は WARN ログ + 例外を呼び出し元に投げない (次回 tick に委ねる)。"""
    runtime = _FakeRuntime(side_effects=[
        RuntimeError("boom #1"),
        RuntimeError("boom #2"),
        RuntimeError("boom #3"),
    ])
    meta, persona = _make_playbook_meta(
        tm, db_persona, runtime,
        judgment_config_override={
            "max_retries": 2,
            "retry_backoff_seconds": 0,
            "cache_threshold_ratio": 0.3,
            "periodic_interval_minutes": 50,
        },
    )
    import logging as _logging
    with caplog.at_level(_logging.WARNING):
        meta._run_judgment_via_playbook(persona, "track_x", {"trigger": "test"})
    assert runtime.calls == 3
    # 最終枯渇ログが出ている
    exhausted_msgs = [
        r for r in caplog.records
        if "exhausted retries" in r.getMessage()
    ]
    assert len(exhausted_msgs) == 1


def test_playbook_retries_on_captured_error_event(tm, db_persona):
    """event_callback が error event を受信した場合も retry 対象。"""
    error_event = {"type": "error", "content": "playbook internal error"}

    call_count = [0]

    def emit_error(persona, **kwargs):
        cb = kwargs.get("event_callback")
        if cb is not None:
            cb(error_event)
        call_count[0] += 1

    def succeed(persona, **kwargs):
        call_count[0] += 1

    runtime = _FakeRuntime(side_effects=[emit_error, succeed])
    meta, persona = _make_playbook_meta(
        tm, db_persona, runtime,
        judgment_config_override={
            "max_retries": 1,
            "retry_backoff_seconds": 0,
            "cache_threshold_ratio": 0.3,
            "periodic_interval_minutes": 50,
        },
    )
    meta._run_judgment_via_playbook(persona, "track_x", {"trigger": "test"})
    assert call_count[0] == 2, "1 回目 error event → retry → 2 回目成功"


def test_playbook_uses_default_config_when_load_fails(tm, db_persona):
    """_load_judgment_config が壊れた値を返しても、defensive parsing で動作する。"""
    runtime = _FakeRuntime(side_effects=[None])
    meta, persona = _make_playbook_meta(
        tm, db_persona, runtime,
        judgment_config_override={
            # 不正値 (string) を渡しても except でデフォルト fallback されること
            "max_retries": "invalid",
            "retry_backoff_seconds": None,
        },
    )
    # 例外なく実行できる
    meta._run_judgment_via_playbook(persona, "track_x", {"trigger": "test"})
    assert runtime.calls == 1
