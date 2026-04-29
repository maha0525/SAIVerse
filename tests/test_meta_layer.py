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
    assert "user_utterance" in user_msg
