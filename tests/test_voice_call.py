"""通話モード (saiverse/voice_call.py, api/routes/voice_call.py) の単体テスト。

設計: ``docs/intent/voice_call.md``。

Gemini Live API への接続は :class:`FakeLiveSession` へ差し替え、ネットワークも
課金も一切発生させない。サーバーから届く server message は **google-genai の
実物の型** (``types.LiveServerMessage``) で組む — フィールド名を覚え書きで
書くと、SDK 側の綴りと食い違ったときにテストだけが通ってしまうため。

検証する契約:

1. start → ready のハンドシェイク (エラーは必ず code つき)
2. ブラウザの音声が Live セッションへ届き、Live の音声がブラウザへ返る
3. 文字起こしが発話単位に束ねられ、通話終了時に**実会話と同じ dict 形式**で
   SAIMemory と建物履歴へ追記される
4. 書き戻しは end 経路でも無言切断経路でもちょうど一度
5. interrupted がクライアントへ中継される
6. 記憶に入らなかった行に「取り込み済み」の印を打たない (自動転記の締め出し防止)
7. 記憶のスレッドは通話開始時に確定し、書き戻しの瞬間の状態に引きずられない
8. 通話の舞台はサーバー側 (ペルソナの現在地) が決める
9. 同じペルソナへの 2 本目は拒否される
10. 認証・Origin の検査がルートで効く
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from google.genai import types

from saiverse import app_state, voice_call
from saiverse.voice_call import (
    NO_TRANSCRIPT_NOTICE,
    CallTranscript,
    VoiceCallError,
    VoiceCallSession,
    build_history_turns,
    build_live_config,
    write_back_transcript,
)

PERSONA_ID = "persona_test"
BUILDING_ID = "bldg_test"
SESSION_SENTINEL = object()  # manager.SessionLocal の代わり (呼ばれない)


# ---------------------------------------------------------------------------
# 偽物一式
# ---------------------------------------------------------------------------


class FakeAdapter:
    """SAIMemoryAdapter の最小の偽物 (append_persona_message の引数を記録する)。

    ``thread_sequence`` を渡すと ``get_current_thread()`` が呼ばれるたびに次の
    値を返す (通話中にスレッドが切り替わる状況の再現)。``append_result`` は
    追記の戻り値 — ``None`` は「行が入らなかった」を表す実物の契約と同じ。
    """

    _PERSONA_THREAD_SUFFIX = "__persona__"

    def __init__(
        self,
        current_thread: Optional[str] = f"{PERSONA_ID}:main",
        *,
        thread_sequence: Optional[List[Optional[str]]] = None,
        append_result: Any = "ok",
        append_raises: bool = False,
    ) -> None:
        self.appended: List[tuple] = []
        self._thread = current_thread
        self._thread_sequence = list(thread_sequence) if thread_sequence else None
        self._append_result = append_result
        self._append_raises = append_raises
        self.thread_reads = 0

    def is_ready(self) -> bool:
        return True

    def get_current_thread(self) -> Optional[str]:
        self.thread_reads += 1
        if self._thread_sequence:
            self._thread = self._thread_sequence.pop(0)
        return self._thread

    def set_active_thread(self, thread_id: str) -> bool:
        self._thread = thread_id
        return True

    def append_persona_message(self, message: dict, *, thread_suffix: Optional[str] = None):
        self.appended.append((message, thread_suffix))
        if self._append_raises:
            raise RuntimeError("memory.db is locked")
        if self._append_result == "ok":
            return f"mem-{len(self.appended)}"
        return self._append_result


class FakeLiveSession:
    """``client.aio.live.connect`` が返すセッションの偽物。

    ``receive()`` は**実物と同じく「モデルの発話一巡で終わる」** async iterator
    (SDK の ``AsyncSession.receive`` は turn_complete の message を yield した
    直後に break する — live.py:456)。ここを実物より寛容 (無限に生きる形) に
    すると、一巡で通話を畳んでしまう欠陥がテストを素通りする
    (2026-09-16 の初通話で実際に素通りした)。
    """

    def __init__(self, script: List[types.LiveServerMessage]) -> None:
        self.script = list(script)
        self.audio_in: List[Dict[str, Any]] = []
        self.primed_turns: List[Any] = []
        self.primed_turn_complete: List[bool] = []

    async def send_client_content(self, *, turns=None, turn_complete: bool = True) -> None:
        self.primed_turns.append(turns)
        self.primed_turn_complete.append(turn_complete)

    async def send_realtime_input(self, *, audio=None, **kwargs) -> None:
        if audio is not None:
            self.audio_in.append(audio)

    async def receive(self):
        while self.script:
            message = self.script.pop(0)
            yield message
            content = getattr(message, "server_content", None)
            if content is not None and content.turn_complete:
                return  # 実物と同じ: 一巡で iterator を終える
        # 台本を出し切ったあとは「モデルは黙って繋がったまま」を保つ。
        # ここで即終了すると、クライアントが end を送る前に通話が畳まれる。
        await asyncio.Event().wait()


class _FakeLiveConnect:
    def __init__(self, session: FakeLiveSession) -> None:
        self.session = session
        self.config: Optional[Dict[str, Any]] = None

    async def __aenter__(self) -> FakeLiveSession:
        return self.session

    async def __aexit__(self, *exc_info) -> bool:
        return False


class FakeWebSocket:
    """starlette の WebSocket のうち、セッションが使う部分だけの偽物。

    ``inbox`` に ASGI 形式のイベントを積んでおくと、その順に受け取る。
    """

    def __init__(self, inbox: List[Dict[str, Any]]) -> None:
        self._inbox = list(inbox)
        self.sent_json: List[Dict[str, Any]] = []
        self.sent_bytes: List[bytes] = []

    async def receive(self) -> Dict[str, Any]:
        if not self._inbox:
            return {"type": "websocket.disconnect", "code": 1006}
        return self._inbox.pop(0)

    async def send_json(self, payload: Dict[str, Any]) -> None:
        self.sent_json.append(payload)

    async def send_bytes(self, data: bytes) -> None:
        self.sent_bytes.append(data)


class HoldingWebSocket(FakeWebSocket):
    """``release`` が立つまで切れない客 (通話が続いている状態を作る)。"""

    def __init__(self, release: asyncio.Event) -> None:
        super().__init__([])
        self._release = release

    async def receive(self) -> Dict[str, Any]:
        await self._release.wait()
        return {"type": "websocket.disconnect", "code": 1000}


def make_manager(adapter: Optional[FakeAdapter] = None, *, occupants=None) -> SimpleNamespace:
    persona = SimpleNamespace(
        persona_id=PERSONA_ID,
        persona_name="テスト子",
        current_building_id=BUILDING_ID,
        sai_memory=adapter if adapter is not None else FakeAdapter(),
        language="ja",
        buildings={BUILDING_ID: SimpleNamespace(name="試験場", system_instruction="")},
    )
    return SimpleNamespace(
        personas={PERSONA_ID: persona},
        all_personas={PERSONA_ID: persona},
        occupants={BUILDING_ID: list(occupants) if occupants else [PERSONA_ID]},
        state=SimpleNamespace(user_id=1),
        user_presence_status="online",
        SessionLocal=SESSION_SENTINEL,
    )


# ---------------------------------------------------------------------------
# server message の組み立て (実物の SDK 型を使う)
# ---------------------------------------------------------------------------


def audio_message(data: bytes) -> types.LiveServerMessage:
    return types.LiveServerMessage(
        server_content=types.LiveServerContent(
            model_turn=types.Content(
                role="model",
                parts=[types.Part(inline_data=types.Blob(data=data, mime_type="audio/pcm;rate=24000"))],
            ),
        ),
    )


def transcript_message(*, user: str = "", model: str = "") -> types.LiveServerMessage:
    content = types.LiveServerContent()
    if user:
        content.input_transcription = types.Transcription(text=user)
    if model:
        content.output_transcription = types.Transcription(text=model)
    return types.LiveServerMessage(server_content=content)


def turn_complete_message() -> types.LiveServerMessage:
    return types.LiveServerMessage(server_content=types.LiveServerContent(turn_complete=True))


def interrupted_message() -> types.LiveServerMessage:
    return types.LiveServerMessage(server_content=types.LiveServerContent(interrupted=True))


def usage_message(prompt: int, response: int, total: int) -> types.LiveServerMessage:
    return types.LiveServerMessage(
        usage_metadata=types.UsageMetadata(
            prompt_token_count=prompt,
            response_token_count=response,
            total_token_count=total,
        ),
    )


def go_away_message(time_left: str = "10s") -> types.LiveServerMessage:
    return types.LiveServerMessage(go_away=types.LiveServerGoAway(time_left=time_left))


# ---------------------------------------------------------------------------
# 共通の下ごしらえ
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def no_leftover_calls():
    """テストを跨いで「通話中」の印が残らないようにする。"""
    voice_call._ACTIVE_CALLS.clear()
    yield
    voice_call._ACTIVE_CALLS.clear()


@pytest.fixture
def written_rows(monkeypatch) -> List[tuple]:
    """建物履歴への INSERT を捕まえる (DB には触らない)。"""
    rows: List[tuple] = []

    def _fake_insert(session_factory, building_id, message, _max_retries: int = 5):
        rows.append((session_factory, building_id, message))
        return {**message, "message_id": f"bm-{len(rows)}", "seq": len(rows)}

    import database.building_messages as bm

    monkeypatch.setattr(bm, "insert_building_message", _fake_insert)
    return rows


@pytest.fixture
def stub_context(monkeypatch):
    """人格プロンプトと履歴の組み立てを差し替える (別テストで個別に検証する)。"""
    monkeypatch.setattr(
        voice_call, "build_system_instruction",
        lambda manager, persona, building_id: "あなたはテスト子である。",
    )
    monkeypatch.setattr(
        voice_call, "build_history_turns",
        lambda manager, persona, building_id, **kwargs: [
            {"role": "user", "parts": [{"text": "前回の続きだよ"}]},
        ],
    )


@pytest.fixture
def api_client(monkeypatch, stub_context):
    """``/api/voice/call`` を提供する TestClient を作る。"""
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    from api.routes import voice_call as voice_route

    app = FastAPI()
    app.include_router(voice_route.router, prefix="/api/voice")
    return TestClient(app)


def install_live(monkeypatch, script: List[types.LiveServerMessage]) -> FakeLiveSession:
    live = FakeLiveSession(script)
    monkeypatch.setattr(
        voice_call, "open_live_session",
        lambda model, config, *, api_key: _FakeLiveConnect(live),
    )
    return live


def capture_live_config(monkeypatch, script: List[types.LiveServerMessage]) -> Dict[str, Any]:
    """Live へ渡した config を覗ける形で偽セッションを挿す。"""
    seen: Dict[str, Any] = {}
    live = FakeLiveSession(script)

    def _connect(model, config, *, api_key):
        seen["model"] = model
        seen["config"] = config
        seen["live"] = live
        return _FakeLiveConnect(live)

    monkeypatch.setattr(voice_call, "open_live_session", _connect)
    return seen


def install_manager(monkeypatch, manager) -> None:
    monkeypatch.setattr(app_state, "manager", manager)


def end_call(ws) -> Dict[str, Any]:
    """``end`` を送って ``call_ended`` を受け取る (usage つき)。"""
    ws.send_json({"type": "end"})
    payload = ws.receive_json()
    assert payload["type"] == "call_ended"
    return payload


# ---------------------------------------------------------------------------
# 1. ハンドシェイク
# ---------------------------------------------------------------------------


def test_start_handshake_returns_ready(monkeypatch, api_client, written_rows):
    manager = make_manager()
    install_manager(monkeypatch, manager)
    seen = capture_live_config(monkeypatch, [])

    with api_client.websocket_connect("/api/voice/call") as ws:
        ws.send_json({"type": "start", "persona_id": PERSONA_ID, "building_id": BUILDING_ID})
        assert ws.receive_json() == {"type": "ready"}
        end_call(ws)

    live = seen["live"]
    # 初期履歴は history_config を立てたうえで turn_complete=True で閉じる。
    # SDK の HistoryConfig docstring: この形のときの turn_complete=True は
    # 「返事をしろ」ではなく「初期履歴はここまで」の合図で、これを送るまで
    # realtime_input は処理されない。
    assert seen["config"]["history_config"] == {"initial_history_in_client_content": True}
    assert live.primed_turn_complete == [True]
    assert live.primed_turns == [[{"role": "user", "parts": [{"text": "前回の続きだよ"}]}]]


def test_no_history_means_no_initial_history_handshake(monkeypatch, api_client, written_rows):
    """積む履歴が無い通話は history_config を立てない (待たされないように)。"""
    install_manager(monkeypatch, make_manager())
    monkeypatch.setattr(voice_call, "build_history_turns", lambda *a, **k: [])
    seen = capture_live_config(monkeypatch, [])

    with api_client.websocket_connect("/api/voice/call") as ws:
        ws.send_json({"type": "start", "persona_id": PERSONA_ID})
        assert ws.receive_json() == {"type": "ready"}
        end_call(ws)

    assert "history_config" not in seen["config"]
    assert seen["live"].primed_turns == []


def test_unknown_persona_is_refused_with_a_coded_error_frame(monkeypatch, api_client, written_rows):
    install_manager(monkeypatch, make_manager())
    install_live(monkeypatch, [])

    with api_client.websocket_connect("/api/voice/call") as ws:
        ws.send_json({"type": "start", "persona_id": "no_such_persona"})
        payload = ws.receive_json()

    assert payload["type"] == "error"
    assert payload["code"] == "persona_not_found"


def test_missing_api_key_is_refused_with_a_coded_error_frame(monkeypatch, api_client, written_rows):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_FREE_API_KEY", raising=False)
    install_manager(monkeypatch, make_manager())
    install_live(monkeypatch, [])

    with api_client.websocket_connect("/api/voice/call") as ws:
        ws.send_json({"type": "start", "persona_id": PERSONA_ID})
        payload = ws.receive_json()

    assert payload["type"] == "error"
    assert payload["code"] == "no_api_key"


def test_start_frame_must_be_json(monkeypatch, api_client, written_rows):
    install_manager(monkeypatch, make_manager())
    install_live(monkeypatch, [])

    with api_client.websocket_connect("/api/voice/call") as ws:
        ws.send_text("not json at all")
        payload = json.loads(ws.receive_text())

    assert payload["type"] == "error"
    assert payload["code"] == "bad_start"


def test_start_frame_without_persona_id_is_refused(monkeypatch, api_client, written_rows):
    install_manager(monkeypatch, make_manager())
    install_live(monkeypatch, [])

    with api_client.websocket_connect("/api/voice/call") as ws:
        ws.send_json({"type": "start"})
        payload = ws.receive_json()

    assert payload["code"] == "missing_persona_id"


# ---------------------------------------------------------------------------
# 2. 音声の往復
# ---------------------------------------------------------------------------


def test_audio_is_relayed_in_both_directions(monkeypatch, api_client, written_rows):
    install_manager(monkeypatch, make_manager())
    live = install_live(monkeypatch, [audio_message(b"\x11\x22" * 8)])

    with api_client.websocket_connect("/api/voice/call") as ws:
        ws.send_json({"type": "start", "persona_id": PERSONA_ID})
        assert ws.receive_json() == {"type": "ready"}
        ws.send_bytes(b"\x01\x02" * 16)
        assert ws.receive_bytes() == b"\x11\x22" * 8
        end_call(ws)

    assert live.audio_in == [
        {"data": b"\x01\x02" * 16, "mime_type": "audio/pcm;rate=16000"},
    ]


# ---------------------------------------------------------------------------
# 5. 割り込み / サーバー都合の終了 / 使用量
# ---------------------------------------------------------------------------


def test_call_survives_multiple_model_turns(monkeypatch, api_client, written_rows):
    """一巡目の turn_complete で通話が畳まれない (2026-09-16 初通話の回帰)。

    SDK の receive() は一巡ごとに終わる async iterator なので、受信側が
    張り直さないと「ペルソナが一言喋った時点で call_ended」になる。
    """
    adapter = FakeAdapter()
    install_manager(monkeypatch, make_manager(adapter))
    install_live(monkeypatch, [
        transcript_message(model="一言目だよ。"),
        turn_complete_message(),
        transcript_message(model="まだ切れてないよ。"),
        turn_complete_message(),
    ])

    with api_client.websocket_connect("/api/voice/call") as ws:
        ws.send_json({"type": "start", "persona_id": PERSONA_ID})
        assert ws.receive_json() == {"type": "ready"}
        assert ws.receive_json() == {"type": "output_transcript", "text": "一言目だよ。"}
        assert ws.receive_json() == {"type": "turn_complete"}
        # 一巡目が終わっても通話は生きていて、二巡目がそのまま届く。
        assert ws.receive_json() == {"type": "output_transcript", "text": "まだ切れてないよ。"}
        assert ws.receive_json() == {"type": "turn_complete"}
        ws.send_json({"type": "end"})
        payload = ws.receive_json()
        assert payload["type"] == "call_ended"

    # 発話も二巡ぶん別々に記憶へ残る。
    assert [m["content"] for m, _ in adapter.appended] == ["一言目だよ。", "まだ切れてないよ。"]


def test_interrupted_is_relayed_to_the_client(monkeypatch, api_client, written_rows):
    install_manager(monkeypatch, make_manager())
    install_live(monkeypatch, [interrupted_message()])

    with api_client.websocket_connect("/api/voice/call") as ws:
        ws.send_json({"type": "start", "persona_id": PERSONA_ID})
        assert ws.receive_json() == {"type": "ready"}
        assert ws.receive_json() == {"type": "interrupted"}
        end_call(ws)


def test_go_away_is_reported_as_a_session_ending_error(monkeypatch, api_client, written_rows):
    install_manager(monkeypatch, make_manager())
    install_live(monkeypatch, [go_away_message("12s")])

    with api_client.websocket_connect("/api/voice/call") as ws:
        ws.send_json({"type": "start", "persona_id": PERSONA_ID})
        assert ws.receive_json() == {"type": "ready"}
        payload = ws.receive_json()
        end_call(ws)

    assert payload["type"] == "error"
    assert payload["code"] == "session_ending"
    assert "12s" in payload["message"]


def test_token_usage_is_accumulated_and_returned_with_call_ended(monkeypatch, api_client, written_rows):
    install_manager(monkeypatch, make_manager())
    install_live(monkeypatch, [usage_message(100, 20, 120), usage_message(30, 5, 35)])

    with api_client.websocket_connect("/api/voice/call") as ws:
        ws.send_json({"type": "start", "persona_id": PERSONA_ID})
        assert ws.receive_json() == {"type": "ready"}
        ended = end_call(ws)

    assert ended["usage"] == {"prompt_tokens": 130, "response_tokens": 25, "total_tokens": 155}


# ---------------------------------------------------------------------------
# 3. 書き戻しの形
# ---------------------------------------------------------------------------


def test_transcript_is_written_back_in_the_real_conversation_shape(
    monkeypatch, api_client, written_rows,
):
    adapter = FakeAdapter()
    # 同席している別のペルソナがいる部屋で通話する。
    install_manager(monkeypatch, make_manager(adapter, occupants=[PERSONA_ID, "persona_other"]))
    install_live(monkeypatch, [
        transcript_message(user="おはよう"),
        transcript_message(user="、調子はどう"),
        transcript_message(model="おはよう。"),
        transcript_message(model="上々だよ"),
        turn_complete_message(),
    ])

    with api_client.websocket_connect("/api/voice/call") as ws:
        ws.send_json({"type": "start", "persona_id": PERSONA_ID})
        assert ws.receive_json() == {"type": "ready"}
        assert ws.receive_json() == {"type": "input_transcript", "text": "おはよう"}
        assert ws.receive_json() == {"type": "input_transcript", "text": "、調子はどう"}
        assert ws.receive_json() == {"type": "output_transcript", "text": "おはよう。"}
        assert ws.receive_json() == {"type": "output_transcript", "text": "上々だよ"}
        assert ws.receive_json() == {"type": "turn_complete"}
        end_call(ws)

    # -- SAIMemory: 断片が発話単位に束ねられ、ユーザー → ペルソナの順で追記される
    assert len(adapter.appended) == 2
    user_msg, user_suffix = adapter.appended[0]
    persona_msg, persona_suffix = adapter.appended[1]

    # thread_suffix は SEARuntime._store_memory と同じ規則
    # (get_current_thread() の ":" 以降) で決まる。
    assert user_suffix == "main"
    assert persona_suffix == "main"

    assert user_msg["role"] == "user"
    assert user_msg["content"] == "おはよう、調子はどう"
    assert user_msg["metadata"]["tags"] == ["conversation"]
    assert user_msg["metadata"]["voice_call"] is True
    # ユーザー行は音声の自動文字起こし由来であることを記録に残す。
    assert user_msg["metadata"]["voice_transcript"] is True
    assert "timestamp" in user_msg
    # 層0タグは message dict の top-level に明示する (adapter._append_message)。
    assert user_msg["line_role"] == "main_line"
    assert user_msg["scope"] == "committed"

    assert persona_msg["role"] == "assistant"
    assert persona_msg["content"] == "おはよう。上々だよ"
    # ペルソナ行は emit_speak と同じく persona_id と conversation タグを持つ。
    assert persona_msg["persona_id"] == PERSONA_ID
    assert persona_msg["metadata"]["tags"] == ["conversation"]
    assert persona_msg["metadata"]["voice_call"] is True
    assert "voice_transcript" not in persona_msg["metadata"]
    # with は emit_speak と同じ規則 = 同席者 (自分を除く) + ユーザー。
    assert persona_msg["metadata"]["with"] == ["persona_other", "user"]
    assert persona_msg["line_role"] == "main_line"
    assert persona_msg["scope"] == "committed"

    # -- 建物履歴: 同じ 2 件が通常の会話と同じ形で入る
    assert [row[1] for row in written_rows] == [BUILDING_ID, BUILDING_ID]
    building_user, building_persona = (row[2] for row in written_rows)
    assert building_user["role"] == "user"
    assert building_user["content"] == "おはよう、調子はどう"
    assert building_user["heard_by"] == sorted({PERSONA_ID, "persona_other", "1"})
    # 記憶への書き込みが成功した行だけ「取り込み済み」= 自動転記の二重取り込み防止。
    assert building_user["ingested_by"] == [PERSONA_ID]
    assert building_persona["role"] == "assistant"
    assert building_persona["persona_id"] == PERSONA_ID
    assert building_persona["ingested_by"] == [PERSONA_ID]


# ---------------------------------------------------------------------------
# 6. 記憶に入らなかった行に「取り込み済み」の印を打たない
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "adapter_kwargs",
    [
        {"append_result": None},   # 行が入らなかった (adapter の失敗の契約)
        {"append_raises": True},   # 例外で落ちた
    ],
)
def test_failed_memory_append_leaves_the_building_row_open_for_ingest(
    monkeypatch, written_rows, adapter_kwargs,
):
    """記憶に入らなかった行に ingested_by を刻むと、自動転記から永久に外れる。"""
    adapter = FakeAdapter(**adapter_kwargs)
    manager = make_manager(adapter)

    written = write_back_transcript(
        manager, manager.personas[PERSONA_ID], BUILDING_ID,
        [{"role": "user", "content": "聞こえてる？", "timestamp": "2026-09-16T00:00:00+00:00"}],
        thread_suffix="main",
    )

    assert written == {"memory": 0, "building": 1}
    assert written_rows[0][2]["ingested_by"] == []


def test_write_back_without_memory_still_reaches_the_building(monkeypatch, written_rows):
    """SAIMemory が使えなくても、通話の記録を建物履歴から落とさない。

    そして**取り込み済みの印は打たない** — 記憶へは一度も入っていないので、
    建物履歴からの自動転記がこの行を拾えるままにしておく必要がある。
    """
    manager = make_manager()
    manager.personas[PERSONA_ID].sai_memory = None

    written = write_back_transcript(
        manager, manager.personas[PERSONA_ID], BUILDING_ID,
        [{"role": "user", "content": "聞こえてる？", "timestamp": "2026-09-16T00:00:00+00:00"}],
        thread_suffix=None,
    )

    assert written == {"memory": 0, "building": 1}
    assert written_rows[0][2]["content"] == "聞こえてる？"
    assert written_rows[0][2]["ingested_by"] == []


# ---------------------------------------------------------------------------
# 7. 記憶のスレッドは通話開始時に確定する
# ---------------------------------------------------------------------------


def test_thread_suffix_is_pinned_at_the_start_of_the_call(monkeypatch, written_rows):
    """書き戻しの瞬間にスレッドが切り替わっていても、通話は開始時のスレッドへ入る。

    ここが書き戻し時の読み取りだと、そのとき Pulse が Stelis のサブスレッドへ
    移っていた場合に通話全体が本線の提示列から消える。
    """
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(voice_call, "build_system_instruction", lambda *a, **k: "s")
    monkeypatch.setattr(voice_call, "build_history_turns", lambda *a, **k: [])
    # 1 回目 (通話開始) は main、それ以降は Stelis のサブスレッドを返す。
    adapter = FakeAdapter(thread_sequence=[f"{PERSONA_ID}:main", f"{PERSONA_ID}:stelis_42"])
    install_live(monkeypatch, [transcript_message(user="ひとこと"), turn_complete_message()])

    websocket = FakeWebSocket([{"type": "websocket.disconnect", "code": 1006}])
    session = VoiceCallSession(make_manager(adapter), PERSONA_ID)
    asyncio.run(session.run(websocket))

    assert adapter.thread_reads == 1
    assert [suffix for _msg, suffix in adapter.appended] == ["main"]


# ---------------------------------------------------------------------------
# 4. 書き戻しはちょうど一度
# ---------------------------------------------------------------------------


def test_write_back_runs_exactly_once_on_the_end_path(monkeypatch, api_client, written_rows):
    adapter = FakeAdapter()
    install_manager(monkeypatch, make_manager(adapter))
    install_live(monkeypatch, [transcript_message(user="ひとこと"), turn_complete_message()])

    with api_client.websocket_connect("/api/voice/call") as ws:
        ws.send_json({"type": "start", "persona_id": PERSONA_ID})
        assert ws.receive_json() == {"type": "ready"}
        assert ws.receive_json() == {"type": "input_transcript", "text": "ひとこと"}
        assert ws.receive_json() == {"type": "turn_complete"}
        end_call(ws)

    assert len(adapter.appended) == 1
    assert len(written_rows) == 1


def test_write_back_runs_exactly_once_on_a_silent_disconnect(monkeypatch, written_rows):
    """無言切断でも書き戻しは走り、二度目は呼ばれない。"""
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(
        voice_call, "build_system_instruction", lambda *a, **k: "あなたはテスト子である。",
    )
    monkeypatch.setattr(voice_call, "build_history_turns", lambda *a, **k: [])
    adapter = FakeAdapter()
    manager = make_manager(adapter)
    install_live(monkeypatch, [transcript_message(user="言い残したこと"), turn_complete_message()])

    # 音声を 1 フレーム送った直後に黙って切れるクライアント。
    websocket = FakeWebSocket([
        {"type": "websocket.receive", "bytes": b"\x00\x01"},
        {"type": "websocket.disconnect", "code": 1006},
    ])
    session = VoiceCallSession(manager, PERSONA_ID, BUILDING_ID)
    asyncio.run(session.run(websocket))

    assert len(adapter.appended) == 1
    assert adapter.appended[0][0]["content"] == "言い残したこと"
    assert len(written_rows) == 1

    # finish() をもう一度呼んでも二重には書かない。
    asyncio.run(session.finish())
    assert len(adapter.appended) == 1
    assert len(written_rows) == 1


def test_write_back_keeps_a_turn_that_never_completed(monkeypatch, written_rows):
    """turn_complete が来ないまま切れても、溜まった断片は失われない。"""
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(voice_call, "build_system_instruction", lambda *a, **k: "s")
    monkeypatch.setattr(voice_call, "build_history_turns", lambda *a, **k: [])
    adapter = FakeAdapter()
    install_live(monkeypatch, [transcript_message(user="途中まで")])

    websocket = FakeWebSocket([{"type": "websocket.disconnect", "code": 1006}])
    session = VoiceCallSession(make_manager(adapter), PERSONA_ID, BUILDING_ID)
    asyncio.run(session.run(websocket))

    assert [m["content"] for m, _ in adapter.appended] == ["途中まで"]


def test_a_call_without_any_transcript_still_leaves_a_trace(monkeypatch, written_rows):
    """声は流れたのに一文字も起こせなかった通話も、あった事実だけは残す。"""
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(voice_call, "build_system_instruction", lambda *a, **k: "s")
    monkeypatch.setattr(voice_call, "build_history_turns", lambda *a, **k: [])
    adapter = FakeAdapter()
    install_live(monkeypatch, [])

    websocket = FakeWebSocket([
        {"type": "websocket.receive", "bytes": b"\x00\x01"},
        {"type": "websocket.disconnect", "code": 1006},
    ])
    session = VoiceCallSession(make_manager(adapter), PERSONA_ID)
    asyncio.run(session.run(websocket))

    assert len(adapter.appended) == 1
    notice, _suffix = adapter.appended[0]
    # システム通告は user ロール + <system> タグ (既存の転記経路と同じ流儀)。
    assert notice["role"] == "user"
    assert notice["content"] == f"<system>{NO_TRANSCRIPT_NOTICE}</system>"
    assert notice["metadata"]["tags"] == ["internal", "event_message"]
    assert len(written_rows) == 1
    assert written_rows[0][2]["ingested_by"] == [PERSONA_ID]


def test_a_call_that_never_started_leaves_nothing(monkeypatch, written_rows):
    """音声を一度も中継していない通話は、痕跡の一行も残さない。"""
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(voice_call, "build_system_instruction", lambda *a, **k: "s")
    monkeypatch.setattr(voice_call, "build_history_turns", lambda *a, **k: [])
    adapter = FakeAdapter()
    install_live(monkeypatch, [])

    websocket = FakeWebSocket([{"type": "websocket.disconnect", "code": 1006}])
    session = VoiceCallSession(make_manager(adapter), PERSONA_ID)
    asyncio.run(session.run(websocket))

    assert adapter.appended == []
    assert written_rows == []


# ---------------------------------------------------------------------------
# 8. 通話の舞台はサーバーが決める
# ---------------------------------------------------------------------------


def test_client_supplied_building_id_is_ignored(monkeypatch, written_rows):
    """クライアントの申告ではなく、ペルソナの現在地を使う。

    メニューを開いてから通話を始めるまでの間にペルソナが移動していると、
    申告どおりに書けば「本人のいない部屋に本人名義の行」が立つ。
    """
    manager = make_manager()
    session = VoiceCallSession(manager, PERSONA_ID, "bldg_somewhere_else")
    assert session.building_id == BUILDING_ID


def test_call_is_refused_when_the_persona_has_no_room(monkeypatch, api_client, written_rows):
    manager = make_manager()
    manager.personas[PERSONA_ID].current_building_id = None
    install_manager(monkeypatch, manager)
    install_live(monkeypatch, [])

    with api_client.websocket_connect("/api/voice/call") as ws:
        ws.send_json({"type": "start", "persona_id": PERSONA_ID, "building_id": BUILDING_ID})
        payload = ws.receive_json()

    assert payload["code"] == "persona_location_unknown"


# ---------------------------------------------------------------------------
# 9. 同じペルソナへの 2 本目
# ---------------------------------------------------------------------------


def test_a_second_call_to_the_same_persona_is_refused(monkeypatch, written_rows):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(voice_call, "build_system_instruction", lambda *a, **k: "s")
    monkeypatch.setattr(voice_call, "build_history_turns", lambda *a, **k: [])
    install_live(monkeypatch, [])
    manager = make_manager()

    async def scenario() -> None:
        release = asyncio.Event()
        held = HoldingWebSocket(release)
        first = VoiceCallSession(manager, PERSONA_ID)
        running = asyncio.create_task(first.run(held))
        # ready が出る = 1 本目が「通話中」になっている
        for _ in range(200):
            if held.sent_json:
                break
            await asyncio.sleep(0)
        assert held.sent_json == [{"type": "ready"}]

        second = VoiceCallSession(manager, PERSONA_ID)
        with pytest.raises(VoiceCallError) as refused:
            await second.run(FakeWebSocket([]))
        assert refused.value.code == "already_in_call"

        release.set()
        await running

    asyncio.run(scenario())
    # 通話が終われば印は外れる (次の通話が拒否され続けない)。
    assert voice_call._ACTIVE_CALLS == set()


def test_a_failing_relay_surfaces_instead_of_being_swallowed(monkeypatch, written_rows):
    """中継タスクが落ちた理由を握り潰さない (通話は「静かに終わった」ことにしない)。"""
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(voice_call, "build_system_instruction", lambda *a, **k: "s")
    monkeypatch.setattr(voice_call, "build_history_turns", lambda *a, **k: [])

    class ExplodingLive(FakeLiveSession):
        async def receive(self):
            raise RuntimeError("live socket died")
            yield  # pragma: no cover - ジェネレータにするためだけ

    live = ExplodingLive([])
    monkeypatch.setattr(
        voice_call, "open_live_session",
        lambda model, config, *, api_key: _FakeLiveConnect(live),
    )

    adapter = FakeAdapter()
    session = VoiceCallSession(make_manager(adapter), PERSONA_ID)
    release = asyncio.Event()  # 客は黙って待っている = 先に落ちるのは downlink
    with pytest.raises(RuntimeError, match="live socket died"):
        asyncio.run(session.run(HoldingWebSocket(release)))


def test_invalid_model_is_refused_before_connecting(monkeypatch, api_client, written_rows):
    install_manager(monkeypatch, make_manager())
    install_live(monkeypatch, [])

    with api_client.websocket_connect("/api/voice/call") as ws:
        ws.send_json({"type": "start", "persona_id": PERSONA_ID, "model": "gemini-9-ultra-expensive"})
        payload = ws.receive_json()

    assert payload["code"] == "invalid_model"


def test_invalid_voice_is_refused_before_connecting(monkeypatch, api_client, written_rows):
    install_manager(monkeypatch, make_manager())
    install_live(monkeypatch, [])

    with api_client.websocket_connect("/api/voice/call") as ws:
        ws.send_json({"type": "start", "persona_id": PERSONA_ID, "voice": "K" * 200})
        payload = ws.receive_json()

    assert payload["code"] == "invalid_voice"


# ---------------------------------------------------------------------------
# 10. 認証と Origin
# ---------------------------------------------------------------------------


def _authenticated_app():
    """LAN 公開と同じ構成 (OwnerAuthMiddleware 入り) の app を作る。"""
    from api.owner_auth import OwnerAuthMiddleware
    from api.routes import voice_call as voice_route

    app = FastAPI()
    app.include_router(voice_route.router, prefix="/api/voice")
    app.add_middleware(OwnerAuthMiddleware)
    return app


def test_websocket_is_refused_without_the_owner_token(monkeypatch, stub_context, written_rows):
    """LAN 公開の構成では、鍵を持たない接続を通話の手前で断る。

    WebSocket は BaseHTTPMiddleware を素通りするので、ルート側で同じ検証を
    呼ばない限りこの経路だけ素通しになる。
    """
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("SAIVERSE_OWNER_TOKEN", "secret-token")
    monkeypatch.setenv("SAIVERSE_ALLOWED_ORIGINS", "http://city.local:3000")
    install_manager(monkeypatch, make_manager())
    install_live(monkeypatch, [])

    with TestClient(_authenticated_app()).websocket_connect("/api/voice/call") as ws:
        payload = ws.receive_json()

    assert payload["type"] == "error"
    assert payload["code"] == "unauthorized"


def test_websocket_is_accepted_with_the_owner_bearer_token(monkeypatch, stub_context, written_rows):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("SAIVERSE_OWNER_TOKEN", "secret-token")
    monkeypatch.setenv("SAIVERSE_ALLOWED_ORIGINS", "http://city.local:3000")
    install_manager(monkeypatch, make_manager())
    install_live(monkeypatch, [])

    client = TestClient(_authenticated_app())
    with client.websocket_connect(
        "/api/voice/call", headers={"authorization": "Bearer secret-token"},
    ) as ws:
        ws.send_json({"type": "start", "persona_id": PERSONA_ID})
        assert ws.receive_json() == {"type": "ready"}
        end_call(ws)


def test_websocket_is_refused_from_an_unlisted_origin(monkeypatch, api_client, written_rows):
    """Origin は HTTP の CORS が許す集合と同じ源で突き合わせる。"""
    monkeypatch.delenv("SAIVERSE_ALLOWED_ORIGINS", raising=False)
    install_manager(monkeypatch, make_manager())
    install_live(monkeypatch, [])

    with api_client.websocket_connect(
        "/api/voice/call", headers={"origin": "http://evil.example"},
    ) as ws:
        payload = ws.receive_json()

    assert payload["code"] == "bad_origin"


def test_websocket_accepts_the_frontend_origin(monkeypatch, api_client, written_rows):
    monkeypatch.delenv("SAIVERSE_ALLOWED_ORIGINS", raising=False)
    install_manager(monkeypatch, make_manager())
    install_live(monkeypatch, [])

    with api_client.websocket_connect(
        "/api/voice/call", headers={"origin": "http://localhost:3000"},
    ) as ws:
        ws.send_json({"type": "start", "persona_id": PERSONA_ID})
        assert ws.receive_json() == {"type": "ready"}
        end_call(ws)


def test_authorize_connection_can_be_replaced_by_tests(monkeypatch, api_client, written_rows):
    """検査そのものを差し替えられる (拒否の経路をルート側だけで確かめられる)。"""
    from api.routes import voice_call as voice_route

    install_manager(monkeypatch, make_manager())
    install_live(monkeypatch, [])
    monkeypatch.setattr(voice_route, "authorize_connection", lambda websocket: "unauthorized")

    with api_client.websocket_connect("/api/voice/call") as ws:
        payload = ws.receive_json()

    assert payload["code"] == "unauthorized"


# ---------------------------------------------------------------------------
# 文脈の組み立て (単体)
# ---------------------------------------------------------------------------


def test_history_turns_label_speakers_like_the_memory_transcription(monkeypatch):
    """建物履歴の各 role が、自動転記 (_transcribe_message) と同じ見え方になる。"""
    other = SimpleNamespace(persona_id="persona_other", persona_name="別の子")
    manager = make_manager()
    manager.all_personas["persona_other"] = other

    heard = [PERSONA_ID, "persona_other"]
    history = [
        {"role": "user", "content": "こんにちは", "heard_by": heard},
        {"role": "assistant", "persona_id": PERSONA_ID, "content": "やあ", "heard_by": heard},
        {"role": "assistant", "persona_id": "persona_other", "content": "私もいるよ", "heard_by": heard},
        {
            "role": "assistant", "persona_id": PERSONA_ID,
            "content": '<div class="note-box">告知</div>', "heard_by": heard,
        },
        {
            "role": "host", "content": "<div>誰かが入室しました</div>",
            "metadata": {"event": {"type": "occupancy"}}, "heard_by": heard,
        },
        {"role": "host", "content": "<div>雨が降り始めた</div>", "heard_by": heard},
    ]

    import database.building_messages as bm

    monkeypatch.setattr(bm, "fetch_building_messages", lambda factory, bid, *, limit=None: history)

    turns = build_history_turns(manager, manager.personas[PERSONA_ID], BUILDING_ID)

    assert turns == [
        {"role": "user", "parts": [{"text": "こんにちは"}]},
        {"role": "model", "parts": [{"text": "やあ"}]},
        {
            "role": "user",
            "parts": [{"text": "別の子: 私もいるよ\n<system>[試験場] 雨が降り始めた</system>"}],
        },
    ]


def test_history_turns_skip_what_the_persona_could_not_hear(monkeypatch):
    """heard_by に自分がいない行と、heard_by の無い古い行は積まない。

    建物履歴からペルソナ記憶への転記 (``_ingest_round``) がその二つを候補から
    外しているので、通話の文脈だけがそれを読むと「知らないはずの話」を
    知っていることになる。
    """
    manager = make_manager()
    history = [
        {"role": "user", "content": "聞こえた話", "heard_by": [PERSONA_ID]},
        {"role": "user", "content": "隣の部屋の話", "heard_by": ["persona_other"]},
        {"role": "user", "content": "heard_by の無い古い行"},
    ]

    import database.building_messages as bm

    monkeypatch.setattr(bm, "fetch_building_messages", lambda factory, bid, *, limit=None: history)

    turns = build_history_turns(manager, manager.personas[PERSONA_ID], BUILDING_ID)

    assert turns == [{"role": "user", "parts": [{"text": "聞こえた話"}]}]


def test_history_turns_never_turn_an_unattributed_line_into_the_persona(monkeypatch):
    """persona_id の無い assistant 行は、本人の発話に化けさせない。"""
    manager = make_manager()
    history = [
        {"role": "assistant", "content": "誰の発言か分からない行", "heard_by": [PERSONA_ID]},
        {"role": "assistant", "persona_id": PERSONA_ID, "content": "本人の発言", "heard_by": [PERSONA_ID]},
    ]

    import database.building_messages as bm

    monkeypatch.setattr(bm, "fetch_building_messages", lambda factory, bid, *, limit=None: history)

    turns = build_history_turns(manager, manager.personas[PERSONA_ID], BUILDING_ID)

    assert turns == [{"role": "model", "parts": [{"text": "本人の発言"}]}]


def test_history_turns_drop_legacy_presence_notices(monkeypatch):
    """host 行の「オフラインになりました」等は記憶に入れない (転記経路と同じ)。"""
    manager = make_manager()
    history = [
        {"role": "host", "content": "<div>ユーザーがオフラインになりました</div>", "heard_by": [PERSONA_ID]},
        {"role": "host", "content": "<div>ユーザーがオンラインになりました</div>", "heard_by": [PERSONA_ID]},
        {"role": "host", "content": "<div>雪が積もった</div>", "heard_by": [PERSONA_ID]},
    ]

    import database.building_messages as bm

    monkeypatch.setattr(bm, "fetch_building_messages", lambda factory, bid, *, limit=None: history)

    turns = build_history_turns(manager, manager.personas[PERSONA_ID], BUILDING_ID)

    assert turns == [
        {"role": "user", "parts": [{"text": "<system>[試験場] 雪が積もった</system>"}]},
    ]


def test_live_config_uses_real_sdk_field_names():
    """組んだ dict が実物の LiveConnectConfig として検証を通ることを確かめる。

    フィールド名を思い出しで書くと、SDK 側が黙って無視して「音声が返らない」
    「文字起こしが来ない」という形で後から露見する。ここで型に通しておく。
    """
    config = types.LiveConnectConfig(
        **build_live_config("あなたはテスト子である。", "Kore", prime_history=True)
    )

    assert config.response_modalities == [types.Modality.AUDIO]
    assert config.speech_config.voice_config.prebuilt_voice_config.voice_name == "Kore"
    assert config.input_audio_transcription is not None
    assert config.output_audio_transcription is not None
    assert config.context_window_compression.sliding_window is not None
    assert config.context_window_compression.trigger_tokens == voice_call.COMPRESSION_TRIGGER_TOKENS
    assert config.history_config.initial_history_in_client_content is True


def test_compression_watermarks_fit_under_the_session_limit():
    """畳む水位の主語は「このセッションが保持できるトークンの総量」。

    人格プロンプトと記憶だけで数万トークンあるので、上限に対して十分手前で、
    かつ通話の序盤を巻き込まない位置にいること。
    """
    assert voice_call.COMPRESSION_TARGET_TOKENS < voice_call.COMPRESSION_TRIGGER_TOKENS
    assert voice_call.COMPRESSION_TRIGGER_TOKENS < voice_call.LIVE_SESSION_TOKEN_LIMIT
    assert voice_call.COMPRESSION_TARGET_TOKENS >= 32000


def test_transcript_groups_fragments_per_turn():
    transcript = CallTranscript()
    transcript.add_input("こん")
    transcript.add_input("にちは")
    transcript.add_output("はい、")
    transcript.add_output("どうぞ")
    transcript.flush_turn()
    transcript.add_input("またね")
    transcript.flush_turn()
    transcript.flush_turn()  # 空の flush は何も足さない

    assert [(e["role"], e["content"]) for e in transcript.entries] == [
        ("user", "こんにちは"),
        ("assistant", "はい、どうぞ"),
        ("user", "またね"),
    ]
