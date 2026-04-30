"""MetaLayer unit tests (Phase C-1).

LLM クライアントは Fake を使い、実 API を叩かずにスペルループを検証する。

検証項目:
- alert observer エントリ → 判断ループ起動
- スペルなし応答での自然停止
- スペル抽出 (許可セット外は無視)
- スペル実行 (TOOL_REGISTRY 経由)
- ループ最大回数の安全網
"""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from database.models import AI, Base, City, User
from saiverse.meta_layer import MetaLayer
from saiverse.note_manager import NoteManager
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

    def __init__(self, track_manager, note_manager, personas):
        self.track_manager = track_manager
        self.note_manager = note_manager
        self.personas = personas
        self.SessionLocal = None  # MetaLayer は使わないが念のため


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


@pytest.fixture
def nm(session_factory):
    return NoteManager(session_factory=session_factory)


def _make_meta_layer(tm, nm, persona_id, llm_responses):
    llm = FakeLLMClient(llm_responses)
    persona = FakePersona(persona_id, llm)
    manager = FakeManager(tm, nm, {persona_id: persona})
    return MetaLayer(manager), llm, persona


# ---------------------------------------------------------------------------
# 自然停止
# ---------------------------------------------------------------------------

def test_no_spell_response_stops_immediately(tm, nm, db_persona):
    """LLM が最初からスペルなしで応答 → 1 回の LLM 呼び出しで終了。"""
    meta, llm, _persona = _make_meta_layer(
        tm, nm, db_persona, ["継続して問題なし。何も操作しない。"]
    )
    track_id = tm.create(db_persona, "user_conversation", is_persistent=True)
    tm.activate(track_id)
    tm.pause(track_id)
    tm.set_alert(track_id)  # alert に遷移

    meta.on_track_alert(db_persona, track_id, {"trigger": "test"})

    assert len(llm.calls) == 1


def test_unknown_persona_skips_silently(tm, nm, db_persona, caplog):
    """persona が見つからない場合は警告ログだけ、例外は出ない。"""
    meta, llm, _persona = _make_meta_layer(tm, nm, db_persona, [])
    meta.on_track_alert("unknown_persona", "fake_track", {})
    # LLM は呼ばれない
    assert llm.calls == []


# ---------------------------------------------------------------------------
# スペル実行
# ---------------------------------------------------------------------------

def test_spell_response_triggers_second_llm_call(tm, nm, db_persona):
    """スペル含む応答 → 実行 (成否によらず) → 次ターン LLM 呼び出し → スペルなしで停止。

    Note: 実スペル (track_activate) は production の SessionLocal を見るため、
    テスト中の in-memory DB の Track は見えず、エラー結果が返る。MetaLayer は
    エラー結果を受けても「次の LLM 呼び出し」に進むことを確認する
    (スペル実行成否は track_activate 自身のテストでカバー済み)。
    """
    track_id = tm.create(db_persona, "user_conversation", is_persistent=True)
    tm.activate(track_id)
    tm.pause(track_id)
    tm.set_alert(track_id)

    spell_response = (
        f"対応する。\n/spell name='track_activate' args={{'track_id': '{track_id}'}}"
    )
    meta, llm, _persona = _make_meta_layer(
        tm, nm, db_persona, [spell_response, "判断完了。"]
    )

    meta.on_track_alert(db_persona, track_id, {"trigger": "test"})

    # 2 回 LLM 呼ばれる (1: スペル発行 / 2: 結果を見て自然停止)
    assert len(llm.calls) == 2
    # 2 ターン目には assistant 応答 + ツール結果が積まれている
    second_messages = llm.calls[1]["messages"]
    assert len(second_messages) >= 4  # system + user + assistant + user(results)
    assert second_messages[2]["role"] == "assistant"
    assert "track_activate" in second_messages[3]["content"]


def test_unknown_spell_is_skipped(tm, nm, db_persona):
    """メタレイヤー許可セット外のスペルは実行されない。"""
    track_id = tm.create(db_persona, "user_conversation", is_persistent=True)
    tm.activate(track_id)
    tm.pause(track_id)
    tm.set_alert(track_id)

    # 許可セット外スペル ("calculator") を含む応答 → 実行されないので結果ターンに進まず終了
    response = "/spell name='calculator' args={'expression': '1+1'}"
    meta, llm, _persona = _make_meta_layer(
        tm, nm, db_persona, [response]
    )

    meta.on_track_alert(db_persona, track_id, {"trigger": "test"})

    # 許可セット外なので結果ターンに進まず、1 回の LLM 呼び出しで終了
    assert len(llm.calls) == 1


def test_llm_failure_is_caught(tm, nm, db_persona):
    """LLM が例外を投げても MetaLayer が落ちない。"""
    track_id = tm.create(db_persona, "user_conversation", is_persistent=True)
    tm.activate(track_id)
    tm.pause(track_id)
    tm.set_alert(track_id)

    class RaisingLLM:
        def generate(self, *args, **kwargs):
            raise RuntimeError("LLM down")

    persona = FakePersona(db_persona, RaisingLLM())
    manager = FakeManager(tm, nm, {db_persona: persona})
    meta = MetaLayer(manager)

    # 例外が伝播しないこと
    meta.on_track_alert(db_persona, track_id, {"trigger": "test"})


# ---------------------------------------------------------------------------
# LLM 呼び出し時に tools / response_schema を渡さない
# ---------------------------------------------------------------------------

def test_llm_called_without_tools_or_schema(tm, nm, db_persona):
    """重要: tools と response_schema を一切渡さないこと (キャッシュ汚染防止)。"""
    track_id = tm.create(db_persona, "user_conversation", is_persistent=True)
    tm.activate(track_id)
    tm.pause(track_id)
    tm.set_alert(track_id)

    meta, llm, _persona = _make_meta_layer(tm, nm, db_persona, ["終了。"])
    meta.on_track_alert(db_persona, track_id, {"trigger": "test"})

    assert len(llm.calls) == 1
    call = llm.calls[0]
    assert call["tools"] is None
    assert call["response_schema"] is None


# ---------------------------------------------------------------------------
# 状態プロンプトの内容
# ---------------------------------------------------------------------------

def test_state_message_includes_alert_marker(tm, nm, db_persona):
    """状態プロンプトに alert Track のマーカーが含まれる。"""
    track_id = tm.create(
        db_persona, "user_conversation", title="対 user1", is_persistent=True
    )
    tm.activate(track_id)
    tm.pause(track_id)
    tm.set_alert(track_id)

    meta, llm, _persona = _make_meta_layer(tm, nm, db_persona, ["停止"])
    meta.on_track_alert(db_persona, track_id, {"trigger": "user_utterance"})

    user_msg = llm.calls[0]["messages"][1]["content"]
    assert "対 user1" in user_msg
    assert "★今回のトリガー" in user_msg


# ---------------------------------------------------------------------------
# Deferred Track ops via PulseContext (Intent A v0.14, Intent B v0.11 — case ii)
# ---------------------------------------------------------------------------

def test_metalayer_applies_deferred_track_ops_at_judgment_end(
    tm, nm, db_persona, monkeypatch
):
    """判断ループ終了時に _apply_deferred_track_ops が PulseContext を渡して呼ばれる。

    MetaLayer は通常の Playbook ランタイムを通らないため、自前で PulseContext を
    作って Track 操作スペルを deferred 化し、判断ループの finally で flush する
    (Intent A v0.14 / Intent B v0.11 の deferred 機構を MetaLayer 経由でも動かす
    ための短期パッチ。Phase 1 で MetaLayer 自体を Playbook 化したらこの配線は
    不要になる)。
    """
    track_id = tm.create(db_persona, "user_conversation", is_persistent=True)
    tm.activate(track_id)
    tm.pause(track_id)
    tm.set_alert(track_id)

    apply_calls = []

    def fake_apply(state, persona):
        apply_calls.append((state, persona))

    # MetaLayer は finally 内で関数を import するため、import 元の名前空間に patch する
    monkeypatch.setattr(
        "sea.runtime_runner._apply_deferred_track_ops", fake_apply
    )

    meta, llm, _persona = _make_meta_layer(
        tm, nm, db_persona, ["継続して問題なし。"]
    )
    meta.on_track_alert(db_persona, track_id, {"trigger": "test"})

    # 判断ループが完走 → finally で apply が 1 回呼ばれる
    assert len(apply_calls) == 1
    state, _ = apply_calls[0]
    pulse_ctx = state["_pulse_context"]
    assert pulse_ctx is not None
    # PulseContext のインターフェースを持っていることを確認
    assert hasattr(pulse_ctx, "deferred_track_ops")
    assert hasattr(pulse_ctx, "enqueue_track_op")


def test_metalayer_apply_runs_even_on_llm_error(
    tm, nm, db_persona, monkeypatch
):
    """LLM 呼び出しが例外を投げても finally で deferred ops が apply される。"""
    track_id = tm.create(db_persona, "user_conversation", is_persistent=True)
    tm.activate(track_id)
    tm.pause(track_id)
    tm.set_alert(track_id)

    apply_calls = []

    def fake_apply(state, persona):
        apply_calls.append((state, persona))

    monkeypatch.setattr(
        "sea.runtime_runner._apply_deferred_track_ops", fake_apply
    )

    class RaisingLLM:
        def generate(self, *args, **kwargs):
            raise RuntimeError("LLM down")

    persona = FakePersona(db_persona, RaisingLLM())
    manager = FakeManager(tm, nm, {db_persona: persona})
    meta = MetaLayer(manager)

    meta.on_track_alert(db_persona, track_id, {"trigger": "test"})

    # LLM エラーで早期 return しても finally は通るので apply は走る
    assert len(apply_calls) == 1


def test_metalayer_execute_spells_forwards_pulse_context(
    tm, nm, db_persona, monkeypatch
):
    """_execute_spells が persona_context に pulse_context を渡している。"""
    from tools import context as tools_context_mod

    captured_pulse_contexts = []
    real_persona_context = tools_context_mod.persona_context

    def capturing_persona_context(*args, **kwargs):
        captured_pulse_contexts.append(kwargs.get("pulse_context"))
        return real_persona_context(*args, **kwargs)

    # MetaLayer は関数内で `from tools.context import persona_context` するため、
    # import 元の名前空間に patch する
    monkeypatch.setattr(
        tools_context_mod, "persona_context", capturing_persona_context
    )

    track_id = tm.create(db_persona, "user_conversation", is_persistent=True)
    tm.activate(track_id)
    tm.pause(track_id)
    tm.set_alert(track_id)

    spell_response = (
        f"対応する。\n/spell name='track_activate' args={{'track_id': '{track_id}'}}"
    )
    meta, _llm, _persona = _make_meta_layer(
        tm, nm, db_persona, [spell_response, "判断完了。"]
    )

    meta.on_track_alert(db_persona, track_id, {"trigger": "test"})

    # 1 回はスペル実行で persona_context が呼ばれている
    assert len(captured_pulse_contexts) >= 1
    # 渡された pulse_context は PulseContext 互換オブジェクト
    pulse_ctx = captured_pulse_contexts[0]
    assert pulse_ctx is not None
    assert hasattr(pulse_ctx, "enqueue_track_op")


# ---------------------------------------------------------------------------
# Per-persona 直列化 Lock (handoff_2026-04-30 Part 1)
# ---------------------------------------------------------------------------

def test_alert_and_periodic_tick_are_serialized_per_persona(
    tm, nm, db_persona, monkeypatch
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

    # ACTIVITY_STATE が 'Active' でないと periodic_tick は skip される。
    # 判断本体スタブがその先で動くよう FakePersona に属性を生やす。
    persona = FakePersona(db_persona, FakeLLMClient(["dummy"]))
    persona.activity_state = "Active"
    manager = FakeManager(tm, nm, {db_persona: persona})
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


def test_locks_are_independent_per_persona(tm, nm, session_factory, monkeypatch):
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
    manager = FakeManager(tm, nm, {"alice": alice, "bob": bob})
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
