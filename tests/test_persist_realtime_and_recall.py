"""リアルタイム情報と自動想起を残して送り続ける運用 (モデルごとの設定) の回帰。

正典: docs/intent/persist_realtime_and_recall.md

最も重く見るのは不変条件 1 — 「ON のモデルで、あるターンの最初の LLM 呼び出しで
送った列は、返事を挟んだ次のターンの最初の呼び出しの列の先頭と一字一句同じに
なる」。ここでは組み立て関数の単体比較ではなく、実際の経路を通して LLM
クライアントに渡った列を捕まえて比べる:

    ユーザー発言の保存 (HistoryManager → 実 SAIMemoryAdapter)
      → Pulse 頭の知覚の消費 (adapter.flush_perception_buffer)
      → run_playbook → prepare_context (実物)
      → LangGraph の LLM ノード (実物: リアルタイムスペル → 偽の LLM 呼び出し →
        残す処理 → スペルループ → 返事の memorize)
      → 次のターンも同じ道

替え玉にしているのは、head の描画 (固定の system 一行)・Metabolism の帳簿
(水位なし = スライディングウィンドウ)・ペルソナ設定の DB 読み・建物への発言の
書き込み・LLM クライアント・スペルの実行・埋め込み検索の候補 (unified_recall)
だけ。会話の保存・知覚の台帳・残した行の保存と提示・並べ方は全部実物。
"""
from __future__ import annotations

import copy
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional
from unittest.mock import patch

import pytest

from sai_memory.unified_recall import RecallHit

PERSONA_ID = "tester"
BUILDING_ID = "b1"
ON_MODEL = "test-persist-on"
OFF_MODEL = "test-persist-off"
REALTIME_SPELL = "see_view"
LOOP_SPELL = "note_add"


# ---------------------------------------------------------------------------
# 替え玉
# ---------------------------------------------------------------------------


class _DummyEmbedder:
    def __init__(self, model=None, **kwargs):
        self.model_name = model

    def embed(self, texts, **kwargs):
        return [[0.0] * 3 for _ in texts]


from llm_clients.base import LLMClient  # noqa: E402


class _FakeLLM(LLMClient):
    """渡された messages を呼び出しごとに写し取る偽の LLM クライアント。

    思考・使用量・署名などの読み出しは基底クラスの既定 (空) をそのまま使う。
    """

    def __init__(self):
        super().__init__(supports_images=True)
        self.calls: List[List[Dict[str, Any]]] = []
        self.script: List[str] = []
        self.fail_next = False
        #: tools 付きノードの全文一括経路は dict ({"type", "content"}) を受け取る。
        self.tool_dict = False
        #: 次のストリームの後に一度だけ返す「サーバーが切った」申告 (504 等)。
        self.stream_error: Optional[Dict[str, Any]] = None
        #: ユーザーが止める (CancellationToken)。ストリームでは最初の chunk の後、
        #: 全文一括では応答を返す直前 (生成の途中で止められた) に止める。
        self.cancel_token = None
        #: 何回目の呼び出しで止めるか (1 始まり。スペルの繰り返しの周で止める用)。
        self.cancel_at_call = 1

    def _next_text(self, messages) -> str:
        self.calls.append(copy.deepcopy(list(messages)))
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("fake LLM failure")
        if self.script:
            return self.script.pop(0)
        return f"返事{len(self.calls)}です。"

    def _maybe_cancel(self) -> None:
        if self.cancel_token is not None and len(self.calls) >= self.cancel_at_call:
            self.cancel_token.cancel()

    def generate(self, messages, tools=None, temperature=None, response_schema=None, **kwargs):
        text = self._next_text(messages)
        # 全文一括の経路でも、生成の途中でユーザーが止めることはある — 応答は
        # 受け取り終えてから戻ってくる。
        self._maybe_cancel()
        if response_schema is not None:
            return {"answer": text}
        if self.tool_dict:
            return {"type": "text", "content": text}
        return text

    def generate_stream(self, messages, tools=None, temperature=None, **kwargs):
        text = self._next_text(messages)
        if not text:
            return
        half = max(1, len(text) // 2)
        yield text[:half]
        self._maybe_cancel()
        yield text[half:]

    def consume_stream_error(self):
        err, self.stream_error = self.stream_error, None
        return err


class _FakeRecall:
    """unified_recall の替え玉。exclude_keys を本物と同じく収集段階で効かせる。"""

    def __init__(self):
        self.hits: List[RecallHit] = []
        self.calls: List[Dict[str, Any]] = []

    def __call__(self, conn, embedder, query, **kwargs):
        self.calls.append(dict(kwargs))
        excluded = {
            (str(k[0]), str(k[1])) for k in (kwargs.get("exclude_keys") or set())
        }
        return [
            copy.copy(h) for h in self.hits
            if (h.source_type, str(h.source_id)) not in excluded
        ]


def _hit(page_id: str, title: str) -> RecallHit:
    return RecallHit(
        source_type="memopedia", source_id=page_id, title=title,
        content=f"{title}の要約", score=0.5, uri=f"saiverse://memopedia/{page_id}",
        embed_score=0.99,
    )


class _Lifecycle:
    """Metabolism の帳簿の替え玉 (水位なし = スライディングウィンドウで読む)。"""

    def get_metabolism_watermarks(self, persona, model_key=None):
        return None

    def apply_window_folds(self, persona, model_key, recent):
        return recent

    def touch_anchor_after_llm_call(self, *args, **kwargs):
        return None

    def is_chronicle_enabled_for_persona(self, persona):
        return True


@pytest.fixture(autouse=True)
def _no_image_summary_llm(monkeypatch):
    """画像の要約 (saiverse/media_summary.ensure_image_summary) を固定の文字にする。

    Gemini の送信形式への変換 (_gemini_dump) は画像ごとに要約を引き、無ければ
    要約用の LLM を呼ぶ。テストから実 LLM に触らないよう、固定の文字を返す。
    """
    def _summary(path, mime_type):
        return "画像の要約"

    monkeypatch.setattr("saiverse.media_summary.ensure_image_summary", _summary)
    monkeypatch.setattr("llm_clients.gemini.ensure_image_summary", _summary)


@pytest.fixture
def env(request, tmp_path, monkeypatch):
    """実 SAIMemoryAdapter + 実 HistoryManager + 実 SEARuntime。

    ``indirect`` の引数で LLM の呼び方を選ぶ: True (既定 — 本番の既定の
    ストリーミング経路) / False (全文一括の経路)。
    """
    streaming = getattr(request, "param", True)
    monkeypatch.setenv("SAIVERSE_LLM_STREAMING", "true" if streaming else "false")
    monkeypatch.setenv("SAIMEMORY_MEMORY", "1")
    persona_dir = tmp_path / "personas" / PERSONA_ID
    persona_dir.mkdir(parents=True, exist_ok=True)
    image_path = tmp_path / "view.png"
    image_path.write_bytes(b"\x89PNG\r\n\x1a\n")

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from database.models import RealtimeSpellBinding

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    RealtimeSpellBinding.__table__.create(engine)
    SessionLocal = sessionmaker(bind=engine)

    from saiverse import model_configs

    monkeypatch.setitem(model_configs.MODEL_CONFIGS, ON_MODEL, {
        "model": ON_MODEL, "provider": "gemini",
        "cache": {"type": "implicit", "persist_realtime_and_recall": True},
    })
    monkeypatch.setitem(model_configs.MODEL_CONFIGS, OFF_MODEL, {
        "model": OFF_MODEL, "provider": "gemini", "cache": {"type": "implicit"},
    })

    with patch("saiverse_memory.adapter.Embedder", _DummyEmbedder):
        from persona.history_manager import HistoryManager
        from saiverse_memory import SAIMemoryAdapter
        from sea import runtime_llm
        from sea.runtime import SEARuntime

        adapter = SAIMemoryAdapter(
            PERSONA_ID, persona_dir=persona_dir, resource_id=PERSONA_ID,
        )
        history = HistoryManager(
            PERSONA_ID, persona_dir / "log.json", {},
            memory_adapter=adapter, db_session_factory=None,
        )
        manager = SimpleNamespace(
            building_histories={}, SessionLocal=SessionLocal, occupants={},
        )
        runtime = SEARuntime(manager)
        runtime.session_lifecycle = _Lifecycle()
        persona = SimpleNamespace(
            persona_id=PERSONA_ID,
            persona_name="テスター",
            sai_memory=adapter,
            history_manager=history,
            current_building_id=BUILDING_ID,
            model=ON_MODEL,
            lightweight_model=ON_MODEL,
            context_length=200_000,
            timezone=timezone.utc,
            execution_state={},
        )

        llm = _FakeLLM()
        recall = _FakeRecall()
        flags = {"auto_recall": True, "spell": True, "realtime": (True, False)}

        monkeypatch.setattr(runtime, "_is_auto_recall_enabled_for_persona",
                            lambda p: flags["auto_recall"])
        monkeypatch.setattr(runtime, "_is_auto_recall_enhanced_for_persona", lambda p: False)
        monkeypatch.setattr(runtime, "_is_spell_enabled_for_persona", lambda p: flags["spell"])
        monkeypatch.setattr(runtime, "_realtime_info_flags_for_persona",
                            lambda p: flags["realtime"])
        # 建物への発言の書き込み (この設計の外)。返事の記憶への保存 (memorize) は実物。
        from sea.runtime_emitters import SpeakFinalizeResult

        monkeypatch.setattr(runtime, "_emit_say", lambda *a, **k: {"message_id": "bm-1"})
        monkeypatch.setattr(runtime, "_emit_speak_start", lambda *a, **k: "bm-1")
        monkeypatch.setattr(runtime, "_emit_sub_speak", lambda *a, **k: None)
        monkeypatch.setattr(
            runtime, "_emit_speak_finalize",
            lambda *a, **k: SpeakFinalizeResult(
                status="saved", building_msg={"message_id": "bm-1", "content": "x"},
            ),
        )
        monkeypatch.setattr(
            runtime, "select_llm_client",
            lambda node_def, persona, **kw: (llm, persona.model),
        )
        monkeypatch.setattr(
            "sea.head_pipeline.render_head_messages",
            lambda *a, **k: [{"role": "system", "content": "HEAD"}],
        )
        monkeypatch.setattr("sai_memory.unified_recall.unified_recall", recall)

        async def _fake_spell(tool_name, tool_args, persona, state, playbook_name,
                              event_callback, messages=None):
            if tool_name == REALTIME_SPELL:
                return ("目の前の光景を見た。", {
                    "media": [{"path": str(image_path), "mime_type": "image/png"}],
                }, True)
            return ("記録しました", None, True)

        # 本物のスペル実行 (persona_context を張ってツールを呼ぶ) は、実際の
        # run_playbook スペルの入口を通すテストが使う。
        real_run_spell = runtime_llm._run_spell_tool_async
        monkeypatch.setattr(runtime_llm, "_run_spell_tool_async", _fake_spell)
        monkeypatch.setattr(
            runtime_llm, "SPELL_TOOL_NAMES",
            set(runtime_llm.SPELL_TOOL_NAMES) | {REALTIME_SPELL, LOOP_SPELL},
        )

        from sea.auto_recall import reset_ledger
        reset_ledger(PERSONA_ID)
        try:
            yield SimpleNamespace(
                runtime=runtime, persona=persona, adapter=adapter, llm=llm,
                recall=recall, flags=flags, SessionLocal=SessionLocal,
                image_path=str(image_path), manager=manager,
                real_run_spell=real_run_spell,
            )
        finally:
            reset_ledger(PERSONA_ID)
            adapter.close()


def _bind_realtime_spell(env) -> None:
    from database.models import RealtimeSpellBinding

    db = env.SessionLocal()
    try:
        db.add(RealtimeSpellBinding(
            OWNER_KIND="persona", OWNER_ID=PERSONA_ID, SPELL_NAME=REALTIME_SPELL,
            SPELL_ARGS_JSON="{}", LABEL="視界", ENABLED=True, PRIORITY=0,
        ))
        db.commit()
    finally:
        db.close()


def _playbook():
    from sea.playbook_models import PlaybookSchema

    data = json.loads(Path("builtin_data/playbooks/public/track_user_conversation.json")
                      .read_text(encoding="utf-8"))
    return PlaybookSchema(**data)


def _run_turn(
    env, user_text: Optional[str], *, pulse_type: str = "user", playbook=None,
    cancellation_token=None, user_metadata: Optional[Dict[str, Any]] = None,
    pre_spells: Optional[List[str]] = None,
) -> List[List[Dict[str, Any]]]:
    """ユーザー発言を保存して 1 Pulse を実際の経路で走らせ、この Pulse の呼び出し列を返す。

    ``user_metadata`` は発言に付ける metadata (画像など)、``pre_spells`` は
    チャット画面の「ツール指定」で渡る事前スペル。
    """
    from sea.pulse_context import aspect_from_pulse_type

    if user_text is not None:
        # 発言の時刻は ``time.time()`` から刻む。本番では HistoryManager が
        # ``datetime.now()`` で刻み、返事などの他の行は ``time.time()`` で刻む —
        # どちらも同じ実時計なので、後から保存した行が前の行より古い時刻を
        # 持つことはない。テストが ``patch("time.time")`` で時計を止めたとき、
        # 発言だけが実時計のまま進むと、次の発言がその返事より新しい秒を持つ
        # (返事が発言の前に並ぶ) という本番では起きない並びができてしまう。
        stamp = datetime.fromtimestamp(time.time(), timezone.utc).isoformat()
        user_msg: Dict[str, Any] = {"role": "user", "content": user_text, "timestamp": stamp}
        if user_metadata is not None:
            user_msg["metadata"] = user_metadata
        env.persona.history_manager.add_to_persona_only(user_msg)
    env.adapter.flush_perception_buffer()
    before = len(env.llm.calls)
    env.runtime._run_playbook(
        playbook or _playbook(), env.persona, BUILDING_ID, user_text or "",
        auto_mode=False,
        pulse_type=pulse_type,
        pulse_line_aspect=aspect_from_pulse_type(pulse_type),
        cancellation_token=cancellation_token,
        pre_spells=pre_spells,
    )
    return env.llm.calls[before:]


def _custom_playbook(nodes: List[Dict[str, Any]], name: str = "test_pb"):
    from sea.playbook_models import PlaybookSchema

    return PlaybookSchema(
        name=name, description="test",
        input_schema=[{"name": "input", "description": "User input"}],
        nodes=nodes, start_node=nodes[0]["id"],
    )


def _push_perception(env, text: str) -> None:
    from sai_memory.perception_buffer import push_perception

    with env.adapter._db_lock:
        push_perception(env.adapter.conn, "world_state", text)


def _sent(messages: List[Dict[str, Any]]) -> List[tuple]:
    """LLM に届く中身の写し (役割・本文・画像)。metadata の内部印は API へ送られない。"""
    out = []
    for m in messages:
        media = (m.get("metadata") or {}).get("media") or []
        out.append((
            m.get("role"), m.get("content"),
            tuple((x.get("path"), x.get("mime_type")) for x in media if isinstance(x, dict)),
        ))
    return out


def _kept_rows(env) -> List[tuple]:
    with env.adapter._db_lock:
        return env.adapter.conn.execute(
            "SELECT id, kind, rendered_text, media, recalled_json, boundary_rowid "
            "FROM perception_batches WHERE kind = 'realtime_and_recall' ORDER BY id"
        ).fetchall()


def _assert_prefix(first: List[Dict[str, Any]], second: List[Dict[str, Any]]) -> None:
    a, b = _sent(first), _sent(second)
    assert len(b) > len(a)
    assert b[: len(a)] == a, (
        "前回の送信が今回の送信の先頭と一致しない:\n"
        + "\n".join(f"{i}: {x!r}\n   {y!r}" for i, (x, y) in enumerate(zip(a, b)) if x != y)
    )


def _assert_turn_prefix(
    prev_turn: List[List[Dict[str, Any]]], next_turn: List[List[Dict[str, Any]]],
) -> None:
    """不変条件 1 と、ターン内の往復の一致の回帰をまとめて確かめる。

    - 不変条件 1 (この設計の主張): あるターンの**最初の** LLM 呼び出しの列が、
      次のターンの最初の呼び出しの列の先頭と一字一句同じ。
    - 回帰 (不変条件 1 の外): 最後の呼び出し (スペルの繰り返しの周) の列も
      次のターンの先頭と一致する。ターン内の二回目以降の呼び出しの形は不変条件の
      範囲外 (2026-10-14 の Argon の実測で、最初の呼び出しで保持された範囲は
      二回目を挟んでも次のターンに効いた — intent §2)。ここで見ているのは、
      スペルの結果の文面を復元形に揃えた直しと、ON の自律パルスの周内の発言を
      復元形で送る直しが、往復の分の一致を保っていること。native tool の往復と
      自律の通常ノード間は揃えていない (issue
      ``in_pulse_message_forms_differ_from_restored_history.md``)。
    """
    _assert_first_call_prefix(prev_turn, next_turn)
    _assert_prefix(prev_turn[-1], next_turn[0])


def _assert_first_call_prefix(
    prev_turn: List[List[Dict[str, Any]]], next_turn: List[List[Dict[str, Any]]],
) -> None:
    """不変条件 1: あるターンの最初の LLM 呼び出しの列が、次のターンの最初の
    呼び出しの列の先頭と一字一句同じ。"""
    _assert_prefix(prev_turn[0], next_turn[0])


def _gemini_dump(messages: List[Dict[str, Any]]):
    """Gemini の送信形式 (system_instruction, contents) へ変換した写し。"""
    from unittest.mock import MagicMock, patch

    from llm_clients.gemini import GeminiClient
    from saiverse.model_configs import get_model_config

    with patch(
        "llm_clients.gemini.build_gemini_clients",
        return_value=(MagicMock(), MagicMock(), MagicMock()),
    ):
        client = GeminiClient(
            "gemini-4-argon", config=get_model_config("gemini-4-argon"),
            supports_images=True,
        )
    system, contents = client._convert_messages(messages)
    return system, [c.model_dump(mode="json") for c in contents]


def _assert_gemini_prefix(first: List[Dict[str, Any]], second: List[Dict[str, Any]]) -> None:
    sys1, dumped1 = _gemini_dump(first)
    sys2, dumped2 = _gemini_dump(second)
    assert sys1 == sys2
    assert len(dumped2) > len(dumped1)
    assert dumped2[: len(dumped1)] == dumped1


# ---------------------------------------------------------------------------
# 不変条件 1 — 前回の送信は今回の送信の先頭そのもの (実際の経路)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("env", [True, False], indirect=True, ids=["stream", "sync"])
class TestPrefixInvariantOnTheRealPath:
    def test_realtime_spell_with_image_and_new_recall(self, env):
        _bind_realtime_spell(env)
        env.recall.hits = [_hit("page-a", "海辺の町")]
        turn1 = _run_turn(env, "海の話をしよう。")
        env.recall.hits = [_hit("page-a", "海辺の町"), _hit("page-b", "灯台")]
        turn2 = _run_turn(env, "灯台もあったね。")

        _assert_turn_prefix(turn1, turn2)

        # 一ターン目のまとまりは列の一番後ろで、スペルの結果と画像を持っていた
        last1 = turn1[0][-1]
        assert "## リアルタイム情報" in last1["content"]
        assert "視界: 目の前の光景を見た。" in last1["content"]
        assert "ふと浮かんだ記憶:" in last1["content"]
        assert last1["metadata"]["media"][0]["path"] == env.image_path
        # 二ターン目の想起は新しく浮かんだ灯台だけ (海辺の町は候補の段階で外れた)
        last2 = turn2[0][-1]
        assert "灯台" in last2["content"]
        assert "海辺の町" not in last2["content"]
        assert ("memopedia", "page-a") in env.recall.calls[-1]["exclude_keys"]
        rows = _kept_rows(env)
        assert len(rows) == 2  # 一ターン一行
        assert json.loads(rows[0][4]) == [["memopedia", "page-a"]]

    def test_prefix_holds_after_gemini_conversion(self, env):
        """Gemini の送信形式 (system_instruction + contents) に変換した後も一致する。

        SAIVerse の中の列が一致していても、クライアントが印の付いたメッセージを
        別扱いすれば Google へ届く中身はずれる。Argon の設定で作ったクライアントで
        二ターン分を変換し、system_instruction が同じで、一ターン目の contents が
        二ターン目の contents の先頭と一致することを確かめる。
        """
        from unittest.mock import MagicMock, patch

        from llm_clients.gemini import GeminiClient
        from saiverse.model_configs import get_model_config

        _bind_realtime_spell(env)
        env.recall.hits = [_hit("page-a", "海辺の町")]
        turn1 = _run_turn(env, "海の話をしよう。")
        env.recall.hits = [_hit("page-a", "海辺の町"), _hit("page-b", "灯台")]
        turn2 = _run_turn(env, "灯台もあったね。")

        with patch(
            "llm_clients.gemini.build_gemini_clients",
            return_value=(MagicMock(), MagicMock(), MagicMock()),
        ):
            client = GeminiClient(
                "gemini-4-argon", config=get_model_config("gemini-4-argon"),
                supports_images=True,
            )
        sys1, contents1 = client._convert_messages(turn1[0])
        sys2, contents2 = client._convert_messages(turn2[0])

        assert sys1 == sys2
        dumped1 = [c.model_dump(mode="json") for c in contents1]
        dumped2 = [c.model_dump(mode="json") for c in contents2]
        assert len(dumped2) > len(dumped1)
        assert dumped2[: len(dumped1)] == dumped1
        # 一ターン目の最後の Content には、スペルの画像が埋め込まれて届いている
        assert any(
            p.get("inline_data") for p in dumped1[-1].get("parts") or []
        )

    def test_no_new_recall_keeps_only_the_realtime_info(self, env):
        env.recall.hits = [_hit("page-a", "海辺の町")]
        turn1 = _run_turn(env, "海の話をしよう。")
        turn2 = _run_turn(env, "まだ海の話。")  # 同じ記憶しか浮かばない

        _assert_turn_prefix(turn1, turn2)
        last2 = turn2[0][-1]
        assert "## リアルタイム情報" in last2["content"]
        assert "ふと浮かんだ記憶" not in last2["content"]
        rows = _kept_rows(env)
        assert [r[4] for r in rows] == ['[["memopedia", "page-a"]]', None]

    def test_three_turns_with_spell_loop(self, env):
        """スペルの繰り返しがあるターン — まとまりは最初の位置に据え置き、残すのは一回。"""
        env.recall.hits = [_hit("page-a", "海辺の町")]
        env.llm.script = [
            f"メモしておく。\n/spell name='{LOOP_SPELL}' args={{}}",
            "メモしたよ。",
        ]
        turn1 = _run_turn(env, "これ覚えておいて。")
        assert len(turn1) == 2  # 最初の呼び出し + スペルの周
        # 二回目の呼び出しでもまとまりの位置は動かず、往復はその後ろに積まれる
        idx_first = next(
            i for i, m in enumerate(turn1[0])
            if "## リアルタイム情報" in str(m.get("content"))
        )
        assert _sent(turn1[1])[: idx_first + 1] == _sent(turn1[0])[: idx_first + 1]
        assert len(_kept_rows(env)) == 1

        turn2 = _run_turn(env, "ありがとう。")
        turn3 = _run_turn(env, "またね。")
        # 不変条件 1 は最初の呼び出しどうし (_assert_turn_prefix の前半)。加えて、
        # 一ターン目の最後の呼び出し (スペルの結果を受けた周) の列も、二ターン目の
        # 最初の呼び出しの列の先頭と一致する (スペルの結果は復元形で送る —
        # 往復の一致を増やした直しの回帰)
        _assert_turn_prefix(turn1, turn2)
        _assert_turn_prefix(turn2, turn3)
        assert len(_kept_rows(env)) == 3

    def test_last_call_with_image_spell_loop_matches_next_turn(self, env):
        """スペルの繰り返しあり・画像付きのスペル結果あり — 不変条件 1 (最初の
        呼び出しどうし) に加え、最後の呼び出しの列も次のターンの最初の呼び出しの
        列の先頭と一致する (SAIVerse の列と Gemini の送信形式の両方。往復の一致を
        増やした直しの回帰で、不変条件 1 の外)。スペルの結果の本文は復元形
        (``<system>\\n…\\n</system>``) で送られ、画像も記録に残って次のターンに
        同じく付く。"""
        _bind_realtime_spell(env)
        env.recall.hits = [_hit("page-a", "海辺の町")]
        env.llm.script = [
            f"見てみる。\n/spell name='{REALTIME_SPELL}' args={{}}",
            "見えたよ。",
        ]
        turn1 = _run_turn(env, "外を見て。")
        assert len(turn1) == 2
        result_msg = turn1[-1][-1]
        assert result_msg["content"].startswith("<system>\n[Spell Result:")
        assert result_msg["content"].endswith("\n</system>")
        assert result_msg["metadata"]["media"][0]["path"] == env.image_path
        env.recall.hits = [_hit("page-a", "海辺の町"), _hit("page-b", "灯台")]
        turn2 = _run_turn(env, "何が見えた?")

        _assert_turn_prefix(turn1, turn2)
        _assert_gemini_prefix(turn1[-1], turn2[0])
        # 最後の呼び出しの Gemini の Content には、スペルの結果の画像が入っている
        _, dumped_last = _gemini_dump(turn1[-1])
        assert any(p.get("inline_data") for p in dumped_last[-1].get("parts") or [])
        assert len(_kept_rows(env)) == 2

    def test_kept_spell_image_does_not_push_older_images_out(self, env):
        """Codex 四巡目: 画像がすでに上限 (既定 4 枚) まである会話で、スペルの
        繰り返しが画像を 1 枚返し、次の発言は文字だけ。ON のモデルで残した
        スペルの結果の画像が上限の数に入ると、次のターンで一番古い画像が文字に
        置き換わり、Gemini の送信形式で前回の最初の呼び出しと先頭が一致しなく
        なる。残した画像は上限の数に入れない (送るときも、復元したときも)。"""
        for i in range(4):
            env.persona.history_manager.add_to_persona_only({
                "role": "user", "content": f"写真{i}です。",
                "metadata": {"media": [
                    {"type": "image", "path": env.image_path, "mime_type": "image/png"},
                ]},
            })
            env.persona.history_manager.add_to_persona_only(
                {"role": "assistant", "content": f"写真{i}、見たよ。"},
            )
        env.llm.script = [
            f"見てみる。\n/spell name='{REALTIME_SPELL}' args={{}}",
            "見えたよ。",
        ]
        turn1 = _run_turn(env, "外を見て。")
        assert len(turn1) == 2
        turn2 = _run_turn(env, "何が見えた?")

        _assert_first_call_prefix(turn1, turn2)
        _assert_gemini_prefix(turn1[0], turn2[0])
        _, dumped1 = _gemini_dump(turn1[0])
        _, dumped2 = _gemini_dump(turn2[0])
        images1 = sum(
            1 for c in dumped1 for p in (c.get("parts") or []) if p.get("inline_data")
        )
        images2 = sum(
            1 for c in dumped2 for p in (c.get("parts") or []) if p.get("inline_data")
        )
        # 一ターン目の 4 枚は二ターン目でも画像のまま。残したスペルの画像が 1 枚増える。
        assert images1 == 4
        assert images2 == 5

    def test_utterance_and_keeping_in_the_same_second(self, env):
        """発言と残す時刻が同じ秒に並んでも、位置は発言で決まる。"""
        fixed = time.time()
        with patch("time.time", return_value=fixed):
            turn1 = _run_turn(env, "同じ秒です。")
            turn2 = _run_turn(env, "これも同じ秒。")
        _assert_turn_prefix(turn1, turn2)

    def test_perception_reply_and_next_utterance_in_the_same_second(self, env):
        """発言・知覚のまとめ・返事・次の発言・次の知覚が同じ秒に並んでも、知覚は
        返事の後ろへ移らない (Codex 一巡目 #1)。ON のモデルの送る列では、知覚の
        まとめも境界の発言で位置を決める。測る側 (勘定・退場計画) も同じ並び。"""
        from sea.session_lifecycle import SessionLifecycle

        fixed = time.time()
        with patch("time.time", return_value=fixed):
            _push_perception(env, "雨が降ってきた")
            turn1 = _run_turn(env, "同じ秒です。")
            _push_perception(env, "雷が鳴った")
            turn2 = _run_turn(env, "これも同じ秒。")
        _assert_turn_prefix(turn1, turn2)
        contents = [str(m.get("content")) for m in turn2[0]]
        i_u1 = contents.index("同じ秒です。")
        assert "雨が降ってきた" in contents[i_u1 + 1]  # 知覚は発言の直後
        assert "## リアルタイム情報" in contents[i_u1 + 2]  # 残した行はその後ろ
        assert turn2[0][i_u1 + 3]["role"] == "assistant"  # 返事はその後ろ

        recent = env.persona.history_manager.get_recent_history(
            100_000, required_line_roles=["main_line"], required_scopes=["committed"],
        )
        lifecycle = SessionLifecycle(runtime=env.runtime, manager_ref=env.runtime.manager)
        measured = lifecycle.presented_with_perceptions(
            env.persona, recent, model_key=ON_MODEL,
        )
        sent_history = turn2[0][2:-1]
        assert _sent(measured)[: len(sent_history)] == _sent(sent_history)

    def test_room_reseat_stays_at_the_front(self, env):
        """部屋の様子の機構の置き直しは、境界 (可視性の判定用) ではなく最古に
        した consumed_at で先頭に置く — ON のモデルの送る側でも測る側でも、
        最新の発言の直後へ移らない (Codex 二巡目 #5)。"""
        from sea.session_lifecycle import SessionLifecycle

        bundle = {
            "building_id": BUILDING_ID, "building_name": "工房",
            "packages": [{
                "key": "item:1", "family": "item", "label": "[item:1] 覚え書き",
                "lines": ["[item:1] 覚え書き"], "media": [], "state": "open",
            }],
        }
        base = time.time()
        with patch("time.time", return_value=base):
            _run_turn(env, "一つ目。")
        with patch("time.time", return_value=base + 100):
            _run_turn(env, "二つ目。")
        with patch("time.time", return_value=base + 200):
            assert env.adapter.reseat_room_state(bundle) is not None
        with patch("time.time", return_value=base + 300):
            turn3 = _run_turn(env, "三つ目。")

        def _reseat_index(msgs):
            idx = [
                i for i, m in enumerate(msgs)
                if (m.get("metadata") or {}).get("__room_state__")
            ]
            assert len(idx) == 1
            return idx[0]

        sent = turn3[0]
        contents = [m.get("content") for m in sent]
        i_reseat = _reseat_index(sent)
        assert "工房" in sent[i_reseat]["content"]
        assert i_reseat < contents.index("一つ目。")

        recent = env.persona.history_manager.get_recent_history(
            100_000, required_line_roles=["main_line"], required_scopes=["committed"],
        )
        lifecycle = SessionLifecycle(runtime=env.runtime, manager_ref=env.runtime.manager)
        measured = lifecycle.presented_with_perceptions(
            env.persona, recent, model_key=ON_MODEL,
        )
        measured_contents = [m.get("content") for m in measured]
        assert _reseat_index(measured) < measured_contents.index("一つ目。")
        # 測る側と送る側は同じ並び (送った列の履歴部分が測った列の先頭)
        sent_history = sent[2:-1]
        assert _sent(measured)[: len(sent_history)] == _sent(sent_history)

    def test_auto_pulse_without_user_utterance(self, env):
        turn0 = _run_turn(env, "こんにちは。")
        turn1 = _run_turn(env, None, pulse_type="auto")
        turn2 = _run_turn(env, "おかえり。")
        _assert_turn_prefix(turn0, turn1)
        _assert_turn_prefix(turn1, turn2)
        assert len(_kept_rows(env)) == 3

    def test_auto_pulse_with_spell_loop(self, env):
        """自律パルスのスペルの繰り返し — 周内の発言は、次のターンの履歴の復元
        (自律の発言を user ロールの自律記録にする) と同じ形で送る。不変条件 1
        (最初の呼び出しどうし) に加え、最後の呼び出しの列も次のターンの最初の
        呼び出しの列の先頭と一致する (SAIVerse の列と Gemini の送信形式の両方。
        Codex 二巡目 #4 の直しの回帰で、不変条件 1 の外)。保存する元の発言は
        assistant のまま。"""
        turn0 = _run_turn(env, "こんにちは。")
        env.llm.script = [
            f"散歩のメモを残そう。\n/spell name='{LOOP_SPELL}' args={{}}",
            "メモした。",
        ]
        turn1 = _run_turn(env, None, pulse_type="auto")
        assert len(turn1) == 2
        round_msg = next(
            m for m in turn1[-1] if "散歩のメモを残そう。" in str(m.get("content"))
        )
        assert round_msg["role"] == "user"
        assert round_msg["content"].startswith("<system>[自律行動の記録]")
        turn2 = _run_turn(env, "おかえり。")
        _assert_turn_prefix(turn0, turn1)
        _assert_turn_prefix(turn1, turn2)
        _assert_gemini_prefix(turn1[-1], turn2[0])
        # 記録に残る元の発言は assistant ロールのまま (変換は送るときだけ)
        with env.adapter._db_lock:
            stored = env.adapter.conn.execute(
                "SELECT role FROM messages WHERE content LIKE '散歩のメモを残そう。%'"
            ).fetchall()
        assert [r[0] for r in stored] == ["assistant"]


# ---------------------------------------------------------------------------
# 残さない処理・失敗したターン
# ---------------------------------------------------------------------------


class TestWhatIsNotKept:
    @pytest.mark.parametrize("pulse_type", ["work_session", "tell", "meta_judgment", None])
    def test_non_conversation_pulses_do_not_keep(self, env, pulse_type):
        from sea.runtime_context import build_turn_realtime_and_recall_message
        from sea.runtime_llm import _persist_turn_realtime_and_recall

        turn_msg = build_turn_realtime_and_recall_message(
            {"content": "<system>\n## リアルタイム情報\n- 現在時刻: x\n</system>"}, None,
        )
        state = {"_pulse_type": pulse_type, "_messages": [turn_msg]}
        _persist_turn_realtime_and_recall(env.runtime, env.persona, state)
        assert _kept_rows(env) == []

    def test_failed_llm_call_keeps_nothing(self, env):
        env.llm.fail_next = True
        with pytest.raises(Exception):
            _run_turn(env, "失敗するターン。")
        assert _kept_rows(env) == []

    def test_keepalive_does_not_keep(self, env, monkeypatch):
        """keepalive は prepare_context を通って ON の並びで送るが、行は増えない。"""
        _run_turn(env, "こんにちは。")
        before = len(_kept_rows(env))
        sent = env.runtime._prepare_context(
            env.persona, BUILDING_ID, None, model_key=ON_MODEL,
            persist_anchor_advance=False,
        )
        # 残した行は送る列に入る (§4.2「すでに残してある差し込みは含める」)
        assert any(
            (m.get("metadata") or {}).get("__persisted_realtime_and_recall__")
            for m in sent
        )
        assert len(_kept_rows(env)) == before

    def test_nothing_to_show_keeps_nothing(self, env):
        env.flags["realtime"] = (False, False)
        env.recall.hits = []
        turn1 = _run_turn(env, "何も出ないターン。")
        assert _kept_rows(env) == []
        assert not any(
            "## リアルタイム情報" in str(m.get("content")) for m in turn1[0]
        )


# ---------------------------------------------------------------------------
# 残すのは生成が正常に完了した後だけ (Codex 一巡目 #3) — 通常と tools 付き
# ---------------------------------------------------------------------------


def _tools_playbook():
    return _custom_playbook([{
        "id": "main_line_response", "type": "llm", "action": None,
        "speak": True, "memorize": True, "metadata_key": "metadata",
        "available_tools": [LOOP_SPELL],
        "output_keys": [{"text": "speak_content"}], "next": None,
    }], name="track_user_conversation_tools")


@pytest.mark.parametrize("env", [True, False], indirect=True, ids=["stream", "sync"])
@pytest.mark.parametrize("node", ["normal", "tools"])
class TestNotKeptWithoutCompletedGeneration:
    """空応答のやり直しの打ち切り・本文の無い中断 (504)・ユーザー停止では残さない。

    使用量の記録 (_record_llm_usage) はこれらの後にも来るので、そこに保存を
    相乗りさせていた形では、返事の無いターンに差し込みだけが残っていた。"""

    def _setup(self, env, node, monkeypatch):
        if node == "tools":
            monkeypatch.setattr(env.runtime, "_build_tools_spec", lambda *a, **k: [])
            env.llm.tool_dict = True
            return _tools_playbook()
        return None

    def _run(self, env, playbook, **kw):
        try:
            _run_turn(env, "返事の無いターン。", playbook=playbook, **kw)
        except Exception:
            pass  # 止まり方は経路ごとに違う (ここで見るのは残したかどうかだけ)

    def test_empty_response_after_retries(self, env, node, monkeypatch):
        playbook = self._setup(env, node, monkeypatch)
        env.llm.script = ["", "", ""]
        self._run(env, playbook)
        assert len(env.llm.calls) >= 1
        assert _kept_rows(env) == []

    def test_server_cut_without_text(self, env, node, monkeypatch):
        playbook = self._setup(env, node, monkeypatch)
        env.llm.script = ["", "", ""]
        env.llm.stream_error = {"code": 504, "status": "DEADLINE_EXCEEDED"}
        self._run(env, playbook)
        assert _kept_rows(env) == []

    def test_user_stop(self, env, node, monkeypatch):
        """ストリームの途中・全文一括の生成の途中で止められたターン (全文一括は
        Codex 二巡目 #2 — 本文を得たことだけを成功と見ていた)。"""
        from sea.cancellation import CancellationToken

        playbook = self._setup(env, node, monkeypatch)
        token = CancellationToken()
        # ストリームは最初の chunk の後、全文一括は応答が戻る直前に止める
        env.llm.cancel_token = token
        env.llm.script = ["止められた返事の途中までです。"]
        self._run(env, playbook, cancellation_token=token)
        assert len(env.llm.calls) == 1  # 送ってから止まった
        assert _kept_rows(env) == []

    def test_completed_generation_is_kept(self, env, node, monkeypatch):
        """対照: 同じ経路で本文を得たターンは残す。"""
        playbook = self._setup(env, node, monkeypatch)
        turn = _run_turn(env, "返事のあるターン。", playbook=playbook)
        rows = _kept_rows(env)
        assert len(rows) == 1
        assert rows[0][2] == turn[0][-1]["content"]


def _structured_playbook():
    return _custom_playbook([{
        "id": "main_line_response", "type": "llm", "action": None,
        "speak": False, "memorize": False,
        "response_schema": {
            "type": "object",
            "properties": {"answer": {"type": "string"}},
            "required": ["answer"],
        },
        "output_key": "decision", "next": None,
    }], name="track_user_conversation_structured")


class TestStopDuringFullTextGeneration:
    """全文一括の経路で生成の途中に止められたら、保存も保存の書き直しもしない
    (Codex 二巡目 #2)。通常・tools 付きは TestNotKeptWithoutCompletedGeneration
    の test_user_stop (sync) が受け持つ。ここは構造化出力とスペルの繰り返し。"""

    @pytest.mark.parametrize("env", [True, False], indirect=True, ids=["stream", "sync"])
    def test_structured_output(self, env):
        from sea.cancellation import CancellationToken

        token = CancellationToken()
        env.llm.cancel_token = token
        try:
            _run_turn(env, "決めて。", playbook=_structured_playbook(),
                      cancellation_token=token)
        except Exception:
            pass
        assert len(env.llm.calls) == 1  # 構造化出力は全文一括で送った
        assert _kept_rows(env) == []

    @pytest.mark.parametrize("env", [True, False], indirect=True, ids=["stream", "sync"])
    def test_structured_output_completed_is_kept(self, env):
        """対照: 止められなかった構造化出力のターンは残す。"""
        turn = _run_turn(env, "決めて。", playbook=_structured_playbook())
        rows = _kept_rows(env)
        assert len(rows) == 1
        assert rows[0][2] == turn[0][-1]["content"]

    @pytest.mark.parametrize("env", [False], indirect=True, ids=["sync"])
    def test_spell_retry_stopped_does_not_rewrite(self, env, monkeypatch):
        """最初の周の保存が失敗し、スペルの後の続き (全文一括) が止められた —
        書き直しは止められた後に走らない。"""
        from sea.cancellation import CancellationToken

        attempts: List[Dict[str, Any]] = []

        def _failing(**kwargs):
            attempts.append(kwargs)
            return None

        monkeypatch.setattr(env.adapter, "record_realtime_and_recall", _failing)
        token = CancellationToken()
        env.llm.cancel_token = token
        env.llm.cancel_at_call = 2  # 続きの生成の途中で止める
        env.llm.script = [
            f"メモしておく。\n/spell name='{LOOP_SPELL}' args={{}}",
            "メモしたよ。",
        ]
        try:
            _run_turn(env, "これ覚えておいて。", cancellation_token=token)
        except Exception:
            pass
        assert len(env.llm.calls) == 2
        assert len(attempts) == 1  # 最初の周の完了での一回だけ
        assert _kept_rows(env) == []


def _is_streaming(env) -> bool:
    import os

    return os.environ.get("SAIVERSE_LLM_STREAMING") == "true"


# ---------------------------------------------------------------------------
# 保存待ちと保存済み (Codex 一巡目 #7) / 送ったまとまりは不変・子の Playbook (#2)
# ---------------------------------------------------------------------------


class TestPendingAndImmutableBlock:
    def test_failed_write_is_retried_at_the_next_completion(self, env, monkeypatch):
        """初回だけ失敗する DB — 同じパルスの次の成功地点で、初めて送ったときに
        固定した文面・境界で書き直す。"""
        original = env.adapter.record_realtime_and_recall
        attempts: List[Dict[str, Any]] = []

        def _flaky(**kwargs):
            attempts.append(copy.deepcopy(kwargs))
            if len(attempts) == 1:
                return None
            return original(**kwargs)

        monkeypatch.setattr(env.adapter, "record_realtime_and_recall", _flaky)
        env.recall.hits = [_hit("page-a", "海辺の町")]
        env.llm.script = [
            f"メモしておく。\n/spell name='{LOOP_SPELL}' args={{}}",
            "メモしたよ。",
        ]
        turn1 = _run_turn(env, "これ覚えておいて。")
        assert len(turn1) == 2
        assert len(attempts) == 2
        assert attempts[0] == attempts[1]  # 固定済みの同じ写し
        rows = _kept_rows(env)
        assert len(rows) == 1
        assert rows[0][2] == turn1[0][-1]["content"]
        turn2 = _run_turn(env, "ありがとう。")
        _assert_turn_prefix(turn1, turn2)

    def test_sent_block_is_not_appended_to(self, env):
        """一度送ったまとまりには足さない。リアルタイムスペルが二度目に走る経路が
        残っても、結果は別の末尾のメッセージに置かれ、保存されない。"""
        import asyncio

        from sea.runtime_context import (
            add_realtime_spell_lines_to_turn_message,
            build_turn_realtime_and_recall_message,
            freeze_turn_realtime_and_recall_for_send,
        )
        from sea.runtime_llm import (
            _execute_realtime_spells,
            _persist_turn_realtime_and_recall,
        )

        _bind_realtime_spell(env)
        turn_msg = build_turn_realtime_and_recall_message(
            {"content": "<system>\n## リアルタイム情報\n- 現在時刻: x\n</system>"}, None,
        )
        messages = [{"role": "user", "content": "発言", "id": "m-1"}, turn_msg]
        freeze_turn_realtime_and_recall_for_send(messages)
        sent_content = turn_msg["content"]
        assert add_realtime_spell_lines_to_turn_message(turn_msg, "- 追記") is False
        state = {
            "_messages": messages, "_persist_realtime_and_recall": True,
            "_pulse_type": "user", "_pulse_id": "pulse-x",
        }
        asyncio.run(_execute_realtime_spells(
            env.runtime, env.persona, BUILDING_ID, state, None,
        ))
        assert turn_msg["content"] == sent_content
        assert "media" not in turn_msg["metadata"]
        tail = messages[-1]
        assert tail is not turn_msg
        assert "視界: 目の前の光景を見た。" in tail["content"]
        assert "__turn_realtime_and_recall__" not in (tail.get("metadata") or {})
        _persist_turn_realtime_and_recall(env.runtime, env.persona, state)
        rows = _kept_rows(env)
        assert [r[2] for r in rows] == [sent_content]

    def test_child_copy_does_not_share_the_parent_block(self, env):
        from sea.runtime_context import (
            add_realtime_spell_lines_to_turn_message,
            build_turn_realtime_and_recall_message,
            copy_turn_realtime_and_recall_for_child,
        )
        from sea.runtime_llm import _persist_turn_realtime_and_recall

        turn_msg = build_turn_realtime_and_recall_message(
            {"content": "<system>\n## リアルタイム情報\n- 現在時刻: x\n</system>",
             "metadata": {"media": [{"path": "a.png", "mime_type": "image/png"}]}},
            None,
        )
        parent = [{"role": "user", "content": "発言", "id": "m-1"}, turn_msg]
        child = copy_turn_realtime_and_recall_for_child(parent)
        assert child[0] is parent[0]  # ほかのメッセージは共有のまま
        assert child[1] is not turn_msg
        before = turn_msg["content"]
        assert add_realtime_spell_lines_to_turn_message(child[1], "- 子の追記")
        child[1]["metadata"]["media"].append({"path": "b.png"})
        assert turn_msg["content"] == before
        assert len(turn_msg["metadata"]["media"]) == 1
        # 子の写しは (送っても) 保存の対象にならない
        from sea.runtime_context import freeze_turn_realtime_and_recall_for_send

        freeze_turn_realtime_and_recall_for_send(child)
        _persist_turn_realtime_and_recall(
            env.runtime, env.persona,
            {"_messages": child, "_pulse_type": "user", "_pulse_id": "p-child"},
        )
        assert _kept_rows(env) == []


def _child_playbook():
    return _custom_playbook([{
        "id": "child_llm", "type": "llm", "action": None, "speak": False,
        "next": None,
    }], name="child_pb")


def _parent_with_child(order: str, line: str = "sub"):
    talk = {
        "id": "talk", "type": "llm", "action": None, "speak": True,
        "memorize": True, "metadata_key": "metadata",
        "output_keys": [{"text": "speak_content"}], "next": None,
    }
    child = {
        "id": "child", "type": "subplay", "playbook": "child_pb", "line": line,
        "next": None,
    }
    if order == "parent_first":
        talk["next"] = "child"
        return _custom_playbook([talk, child], name="parent_pb")
    child["next"] = "talk"
    return _custom_playbook([child, talk], name="parent_pb")


#: リアルタイムスペルの結果の行 (送る列の中で、結果を運んでいるメッセージの目印)。
_SPELL_LINE = "視界: 目の前の光景を見た。"


def _run_parent_with_child(env, monkeypatch, order: str, line: str):
    """親と子の Playbook を一ターン走らせ、(親の送信, 子の送信, スペルの実行回数) を返す。"""
    from sea import runtime_llm

    _bind_realtime_spell(env)
    real_spell = runtime_llm._run_spell_tool_async
    spell_calls: List[str] = []

    async def _counting(tool_name, *args, **kwargs):
        spell_calls.append(tool_name)
        return await real_spell(tool_name, *args, **kwargs)

    monkeypatch.setattr(runtime_llm, "_run_spell_tool_async", _counting)
    real_load = env.runtime._load_playbook_for
    monkeypatch.setattr(
        env.runtime, "_load_playbook_for",
        lambda name, persona, building_id: (
            _child_playbook() if name == "child_pb" else real_load(name, persona, building_id)
        ),
    )
    turn1 = _run_turn(env, "外の様子はどう?", playbook=_parent_with_child(order, line))
    assert len(turn1) == 2
    parent_call = turn1[0] if order == "parent_first" else turn1[1]
    child_call = turn1[1] if order == "parent_first" else turn1[0]
    return parent_call, child_call, spell_calls.count(REALTIME_SPELL)


def _carriers(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """送る列の中で、リアルタイムスペルの結果を運んでいるメッセージ。"""
    return [m for m in messages if _SPELL_LINE in str(m.get("content"))]


@pytest.mark.parametrize("line", ["main", "sub"])
@pytest.mark.parametrize("order", ["parent_first", "child_first"])
def test_on_child_playbooks_get_the_same_realtime_spell_result(env, monkeypatch, order, line):
    """ON のモデル: リアルタイムスペルは同じ Pulse の中で一度だけ実行し、その
    結果 (本文と画像) を、文脈を組み直す子 (line='main') にも親の列を写す子
    (line='sub') にも、親が先でも子が先でも、そのターンのまとまりに入れて届ける。
    保存される一行と、パルスの中の各送信で結果を運ぶまとまりは同じ文面・画像
    (Codex 二巡目 #3 — 一巡目の修正は実行済みの印だけを共有し、結果を渡して
    いなかった)。"""
    parent_call, child_call, spell_count = _run_parent_with_child(
        env, monkeypatch, order, line,
    )
    assert spell_count == 1
    rows = _kept_rows(env)
    assert len(rows) == 1
    saved_text = rows[0][2]
    saved_media = [m.get("path") for m in json.loads(rows[0][3])]
    assert _SPELL_LINE in saved_text
    assert saved_media == [env.image_path]
    for call in (parent_call, child_call):
        carriers = _carriers(call)
        assert len(carriers) == 1, [m.get("content") for m in carriers]
        assert carriers[0]["content"] == saved_text
        assert [
            m.get("path") for m in (carriers[0].get("metadata") or {}).get("media") or []
        ] == saved_media
    # 親の送信のまとまりは列の一番後ろ (残した一行そのもの)
    assert parent_call[-1]["content"] == saved_text
    turn2 = _run_turn(env, "ありがとう。")
    _assert_prefix(parent_call, turn2[0])


@pytest.mark.parametrize("line", ["main", "sub"])
@pytest.mark.parametrize("order", ["parent_first", "child_first"])
def test_off_child_playbooks_run_realtime_spells_as_before(env, monkeypatch, order, line):
    """OFF のモデル: 実行の仕方は一巡目の修正前 (9c8314cc) と同じ — 器を共有せず、
    親も子も自分で実行する (不変条件 2)。各送信にはリアルタイムスペルの結果が
    載り、何も残さない。

    期待値は 9c8314cc の実装で同じ手順を走らせて取った送信の形 (結果を運ぶ
    メッセージの数と、その中の結果の行の数):

    - 親の列を写す子 (line='sub') は、リアルタイム情報のメッセージの辞書を親と
      共有するので、後から実行した側の送信では結果の行が二つになる (既存の形)。
    - 文脈を組み直す子 (line='main') は自分のメッセージを組むので、各一つ。
    """
    env.persona.model = OFF_MODEL
    env.persona.lightweight_model = OFF_MODEL
    parent_call, child_call, spell_count = _run_parent_with_child(
        env, monkeypatch, order, line,
    )
    assert spell_count == 2
    assert _kept_rows(env) == []
    first_call, second_call = (
        (parent_call, child_call) if order == "parent_first" else (child_call, parent_call)
    )
    for call, expected_lines in ((first_call, 1), (second_call, 1 if line == "main" else 2)):
        carriers = _carriers(call)
        assert len(carriers) == 1
        assert carriers[0]["metadata"].get("__realtime_context__")
        assert carriers[0]["content"].count(_SPELL_LINE) == expected_lines
        # 今までどおり最新の発言の直前 (OFF の並び)
        idx = call.index(carriers[0])
        assert call[idx + 1]["content"] == "外の様子はどう?"


def _run_parent_with_run_playbook_spell(env, monkeypatch):
    """親 (会話の既定の Playbook) が実際の ``/run_playbook`` スペルで子を起動する
    一ターンを走らせ、(親の一回目, 子, 親の二回目の送信, スペルの実行回数) を返す。

    スペルの入口は本物 — スペルループ → ``_run_spell_tool_async`` (persona_context
    を張る) → ``builtin_data/tools/run_playbook.py`` → ``_run_playbook(line='sub')``。
    替え玉にしているのは、子の Playbook の読み込みと、許可の判定だけ。
    """
    import importlib.util

    from sea import runtime_llm
    from tools import TOOL_REGISTRY

    _bind_realtime_spell(env)
    spec = importlib.util.spec_from_file_location(
        "run_playbook_tool_for_persist_test",
        str(Path("builtin_data/tools/run_playbook.py").resolve()),
    )
    tool_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool_module)
    monkeypatch.setattr(tool_module, "_check_playbook_permission", lambda *a, **k: None)
    monkeypatch.setitem(TOOL_REGISTRY, "run_playbook", tool_module.run_playbook)
    monkeypatch.setattr(
        runtime_llm, "SPELL_TOOL_NAMES",
        set(runtime_llm.SPELL_TOOL_NAMES) | {"run_playbook"},
    )
    env.manager.sea_runtime = env.runtime
    env.manager.personas = {PERSONA_ID: env.persona}
    env.persona.manager_ref = env.manager

    fake_spell = runtime_llm._run_spell_tool_async
    spell_calls: List[str] = []

    async def _spell(tool_name, tool_args, persona, state, playbook_name,
                     event_callback, messages=None, **kwargs):
        spell_calls.append(tool_name)
        if tool_name == "run_playbook":
            return await env.real_run_spell(
                tool_name, tool_args, persona, state, playbook_name,
                event_callback, messages=messages, **kwargs,
            )
        return await fake_spell(
            tool_name, tool_args, persona, state, playbook_name,
            event_callback, messages=messages,
        )

    monkeypatch.setattr(runtime_llm, "_run_spell_tool_async", _spell)
    child = _child_playbook()
    child.router_callable = True
    real_load = env.runtime._load_playbook_for
    monkeypatch.setattr(
        env.runtime, "_load_playbook_for",
        lambda name, persona, building_id: (
            child if name == "child_pb" else real_load(name, persona, building_id)
        ),
    )
    env.llm.script = [
        "外を調べてくる。\n/spell name='run_playbook' args={\"name\": \"child_pb\"}",
        "子の調べ。",
        "調べ終わった。",
    ]
    turn1 = _run_turn(env, "外の様子はどう?")
    assert spell_calls.count("run_playbook") == 1
    assert len(turn1) == 3  # 親の一回目 → 子 → 親の二回目 (スペルの結果を受けた周)
    parent_first, child_call, parent_last = turn1
    return parent_first, child_call, parent_last, spell_calls.count(REALTIME_SPELL)


def test_on_run_playbook_spell_does_not_rerun_realtime_spells(env, monkeypatch):
    """ON のモデル: 実際の ``/run_playbook`` スペルで起動した子 (親の列を写す
    サブライン) は、親が実行済みのリアルタイムスペルを再実行せず、結果を二度
    載せない。パルスの各送信で結果を運ぶまとまりは一つで、保存した一行と同じ
    文面・画像 (Codex 三巡目 — 旧コードでは子が再実行し、子の送信に結果が二件
    載っていた)。"""
    parent_first, child_call, parent_last, spell_count = (
        _run_parent_with_run_playbook_spell(env, monkeypatch)
    )
    assert spell_count == 1
    rows = _kept_rows(env)
    assert len(rows) == 1
    saved_text = rows[0][2]
    saved_media = [m.get("path") for m in json.loads(rows[0][3])]
    assert _SPELL_LINE in saved_text
    assert saved_media == [env.image_path]
    for call in (parent_first, child_call, parent_last):
        carriers = _carriers(call)
        assert len(carriers) == 1, [m.get("content") for m in carriers]
        assert carriers[0]["content"] == saved_text
        assert carriers[0]["content"].count(_SPELL_LINE) == 1
        assert [
            m.get("path") for m in (carriers[0].get("metadata") or {}).get("media") or []
        ] == saved_media
    # 親の一回目の送信のまとまりは列の一番後ろ (残した一行そのもの)
    assert parent_first[-1]["content"] == saved_text
    # 不変条件 1: このターンの最初の呼び出しの列は、次のターンの最初の呼び出しの
    # 列の先頭と一字一句同じ
    turn2 = _run_turn(env, "ありがとう。")
    _assert_prefix(parent_first, turn2[0])


def test_off_run_playbook_spell_runs_realtime_spells_as_before(env, monkeypatch):
    """OFF のモデル: 実際の ``/run_playbook`` スペルでも、実行の仕方は 9c8314cc と
    同じ — 子は自分でリアルタイムスペルを実行する (不変条件 2)。何も残さない。

    期待値は 9c8314cc の実装で同じ手順を走らせて取った形。子の列は親の列の
    浅い写しなので、リアルタイム情報のメッセージの辞書を親と共有し、子の実行で
    足した行は親の二回目の送信にも出る (既存の形)。
    """
    env.persona.model = OFF_MODEL
    env.persona.lightweight_model = OFF_MODEL
    parent_first, child_call, parent_last, spell_count = (
        _run_parent_with_run_playbook_spell(env, monkeypatch)
    )
    assert spell_count == 2
    assert _kept_rows(env) == []
    for call, expected_lines in ((parent_first, 1), (child_call, 2), (parent_last, 2)):
        carriers = _carriers(call)
        assert len(carriers) == 1
        assert carriers[0]["metadata"].get("__realtime_context__")
        assert carriers[0]["content"].count(_SPELL_LINE) == expected_lines


# ---------------------------------------------------------------------------
# 不変条件 2 — OFF のモデルは何も変わらない / 切り替え
# ---------------------------------------------------------------------------


class TestOffModelAndSwitching:
    def test_off_model_order_is_unchanged(self, env):
        env.persona.model = OFF_MODEL
        env.recall.hits = [_hit("page-a", "海辺の町")]
        turn1 = _run_turn(env, "海の話をしよう。")
        msgs = turn1[0]
        # 今までどおり: リアルタイム情報 → 想起 → 最新の発言 (末尾)
        assert msgs[-1]["content"] == "海の話をしよう。"
        assert "## リアルタイム情報" in msgs[-3]["content"]
        assert msgs[-3]["metadata"].get("__realtime_context__")
        assert msgs[-2]["metadata"].get("__auto_recall__")
        assert _kept_rows(env) == []
        # 粘着: 次のターンも同じ記憶が出る (OFF の挙動は変えない)
        env.recall.hits = []
        turn2 = _run_turn(env, "続き。")
        assert any("海辺の町" in str(m.get("content")) for m in turn2[0])

    def test_off_model_auto_spell_loop_keeps_the_assistant_line(self, env):
        """OFF のモデルの自律パルスのスペルの繰り返しは今までどおり — 周内の
        発言は assistant ロールのまま次の呼び出しへ積む (不変条件 2)。"""
        env.persona.model = OFF_MODEL
        env.persona.lightweight_model = OFF_MODEL
        _run_turn(env, "こんにちは。")
        env.llm.script = [
            f"散歩のメモを残そう。\n/spell name='{LOOP_SPELL}' args={{}}",
            "メモした。",
        ]
        turn1 = _run_turn(env, None, pulse_type="auto")
        assert len(turn1) == 2
        round_msg = next(
            m for m in turn1[-1] if "散歩のメモを残そう。" in str(m.get("content"))
        )
        assert round_msg == {
            "role": "assistant",
            "content": f"散歩のメモを残そう。\n/spell name='{LOOP_SPELL}' args={{}}",
        }

    def test_off_model_perceptions_stay_in_seconds_order(self, env):
        """知覚のまとめを境界の発言で並べるのは ON のモデルの送る列だけ。OFF の
        モデルへ送る (測る) 列のブロックには位置を決める発言を付けず、今までの
        秒の比較のまま並ぶ。"""
        from sea.runtime_context import (
            BOUNDARY_ANCHOR_ID_KEY,
            list_presented_perception_blocks,
            merge_perception_blocks,
        )

        _push_perception(env, "雨が降ってきた")
        _run_turn(env, "こんにちは。")
        recent = env.persona.history_manager.get_recent_history(
            100_000, required_line_roles=["main_line"], required_scopes=["committed"],
        )
        off_blocks = list_presented_perception_blocks(
            env.runtime, env.persona, recent, model_key=OFF_MODEL,
        )
        perception = [b for b in off_blocks if "雨が降ってきた" in b["content"]]
        assert len(perception) == 1
        assert BOUNDARY_ANCHOR_ID_KEY not in perception[0]["metadata"]
        on_blocks = list_presented_perception_blocks(
            env.runtime, env.persona, recent, model_key=ON_MODEL,
        )
        on_perception = [b for b in on_blocks if "雨が降ってきた" in b["content"]]
        assert on_perception[0]["metadata"][BOUNDARY_ANCHOR_ID_KEY]
        # OFF の列は秒の比較で並ぶ: 知覚は「自分の時刻以下の最後の発言」の直後
        merged = merge_perception_blocks(recent, off_blocks)
        i = next(k for k, m in enumerate(merged) if "雨が降ってきた" in str(m.get("content")))
        assert all(
            (m.get("created_at") or 0) <= perception[0]["created_at"]
            for m in merged[:i] if m.get("id")
        )

    def test_on_off_on(self, env):
        env.recall.hits = [_hit("page-a", "海辺の町")]
        on1 = _run_turn(env, "一つ目。")
        kept_text = _kept_rows(env)[0][2]

        env.persona.model = OFF_MODEL
        off = _run_turn(env, "二つ目。")
        assert not any(m.get("content") == kept_text for m in off[0])
        assert len(_kept_rows(env)) == 1  # OFF のターンは残さない

        env.persona.model = ON_MODEL
        on2 = _run_turn(env, "三つ目。")
        # 残した行は元の位置 (一つ目の発言の直後) に戻ってくる
        contents = [m.get("content") for m in on2[0]]
        i = contents.index("一つ目。")
        assert contents[i + 1] == kept_text
        _assert_prefix(on1[0], on2[0])


# ---------------------------------------------------------------------------
# 位置を決める発言が送る列に無いとき
# ---------------------------------------------------------------------------


class TestBoundaryOutsideTheList:
    def test_kept_row_is_not_sent_without_its_boundary(self, env):
        from sai_memory.perception_buffer import insert_realtime_and_recall_batch
        from sea.runtime_context import list_presented_perception_blocks

        _run_turn(env, "こんにちは。")
        with env.adapter._db_lock:
            row = env.adapter.conn.execute(
                "SELECT created_at, rowid FROM messages ORDER BY rowid LIMIT 1"
            ).fetchone()
            insert_realtime_and_recall_batch(
                env.adapter.conn, consumed_at=int(time.time()),
                rendered_text="<system>別スレッドの差し込み</system>",
                boundary_created_at=int(row[0]), boundary_rowid=int(row[1]),
            )
        recent_without_boundary = [
            m for m in env.persona.history_manager.get_recent_history(
                100_000, required_line_roles=["main_line"], required_scopes=["committed"],
            )
            if m.get("content") != "こんにちは。"
        ]
        blocks = list_presented_perception_blocks(
            env.runtime, env.persona, recent_without_boundary, model_key=ON_MODEL,
        )
        assert not any("別スレッドの差し込み" in b["content"] for b in blocks)
        # 同じ列でも OFF のモデルへは残した行を一つも出さない
        full = env.persona.history_manager.get_recent_history(
            100_000, required_line_roles=["main_line"], required_scopes=["committed"],
        )
        off_blocks = list_presented_perception_blocks(
            env.runtime, env.persona, full, model_key=OFF_MODEL,
        )
        assert not any(
            (b.get("metadata") or {}).get("__persisted_realtime_and_recall__")
            for b in off_blocks
        )


# ---------------------------------------------------------------------------
# 測る側 (勘定・退場計画) は送る側と同じ一枚を見る
# ---------------------------------------------------------------------------


class TestMeasureSideSeesTheSameList:
    def test_presented_with_perceptions_matches_the_sent_history(self, env):
        from sea.eviction_plan import message_chars
        from sea.session_lifecycle import SessionLifecycle

        env.recall.hits = [_hit("page-a", "海辺の町")]
        _run_turn(env, "一つ目。")
        turn2 = _run_turn(env, "二つ目。")

        recent = env.persona.history_manager.get_recent_history(
            100_000, required_line_roles=["main_line"], required_scopes=["committed"],
        )
        lifecycle = SessionLifecycle(runtime=env.runtime, manager_ref=env.runtime.manager)
        measured = lifecycle.presented_with_perceptions(
            env.persona, recent, model_key=ON_MODEL,
        )
        # 送った列の履歴部分 = head・時刻の目印の後ろから、そのターンのまとまりの前まで。
        # 測った時点では二ターン目のまとまりと返事も保存済みなので、その二つが後ろに付く。
        sent_history = turn2[0][2:-1]
        assert _sent(measured)[: len(sent_history)] == _sent(sent_history)
        tail = measured[len(sent_history):]
        assert len(tail) == 2
        assert tail[0]["content"] == turn2[0][-1]["content"]  # 残した二ターン目のまとまり
        assert (tail[0].get("metadata") or {}).get("__persisted_realtime_and_recall__")
        assert tail[1]["role"] == "assistant"
        # 残した行の字数も勘定に入る (知覚のブロックと同じ扱い)
        assert message_chars(measured) == (
            message_chars(sent_history) + message_chars(tail)
        )
        assert message_chars(tail[:1]) == len(turn2[0][-1]["content"])


# ---------------------------------------------------------------------------
# 保存先の表 (種類の列) と知覚のキーワード検索
# ---------------------------------------------------------------------------


class TestStorage:
    def test_old_table_gets_the_kind_column_with_perception_default(self):
        import sqlite3

        from sai_memory.perception_buffer import (
            init_perception_buffer_table,
            list_unannexed_batches,
        )

        conn = sqlite3.connect(":memory:")
        conn.execute(
            "CREATE TABLE perception_batches (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "consumed_at INTEGER NOT NULL, pulse_id TEXT, episode_id TEXT, "
            "rendered_text TEXT NOT NULL, media TEXT, annexed_entry_id TEXT, "
            "boundary_created_at INTEGER, boundary_rowid INTEGER, room_state_json TEXT)"
        )
        conn.execute(
            "INSERT INTO perception_batches (consumed_at, rendered_text) VALUES (1, '既存')"
        )
        conn.commit()
        init_perception_buffer_table(conn)
        init_perception_buffer_table(conn)  # 二度目も無害
        [batch] = list_unannexed_batches(conn)
        assert batch.kind == "perception"
        assert not batch.is_realtime_and_recall
        assert batch.recalled_json is None

    def test_old_database_opened_by_the_cli_path_keeps_its_material(
        self, tmp_path, monkeypatch,
    ):
        """新しい列の無い旧 DB を CLI と同じ経路 (init_db → init_arasuji_tables —
        SAIMemoryAdapter の初期化を通らない) で開いても、付記の材料集め・再生成・
        知覚のキーワード検索が台帳を読める (Codex 一巡目 #6 — 列不足を「台帳
        なし」と取り違えて材料ゼロになっていた)。"""
        import sqlite3
        from unittest.mock import patch as _patch

        from saiverse_memory import SAIMemoryAdapter
        from sai_memory.arasuji import init_arasuji_tables
        from sai_memory.arasuji.executor import collect_annex_items
        from sai_memory.arasuji.storage import create_entry, regenerate_entry
        from sai_memory.memory.storage import init_db
        from sai_memory.unified_recall import unified_recall

        monkeypatch.setenv("SAIMEMORY_MEMORY", "1")
        monkeypatch.setattr("saiverse_memory.adapter.Embedder", _DummyEmbedder)
        persona_dir = tmp_path / "old_persona"
        persona_dir.mkdir()
        old = SAIMemoryAdapter("old", persona_dir=persona_dir, resource_id="old")
        ids = [
            old.append_persona_message({"role": "user", "content": f"話 {i}"})
            for i in range(2)
        ]
        init_arasuji_tables(old.conn)
        entry = create_entry(
            old.conn, level=1, content="旧あらすじ", source_ids=ids,
            start_time=1, end_time=2, source_count=2, message_count=2,
        )
        old.close()

        db_path = persona_dir / "memory.db"
        raw = sqlite3.connect(str(db_path))
        raw.execute("DROP TABLE perception_batches")
        raw.execute(
            "CREATE TABLE perception_batches (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "consumed_at INTEGER NOT NULL, pulse_id TEXT, episode_id TEXT, "
            "rendered_text TEXT NOT NULL, media TEXT, annexed_entry_id TEXT)"
        )
        raw.execute(
            "INSERT INTO perception_batches (consumed_at, rendered_text) "
            "VALUES (100, '[システム通知]\n未付記の灯台の知らせ')"
        )
        raw.execute(
            "INSERT INTO perception_batches (consumed_at, rendered_text, "
            "annexed_entry_id) VALUES (50, '[システム通知]\n付記済みの雷', ?)",
            (entry.id,),
        )
        raw.commit()
        raw.close()

        conn = init_db(str(db_path), check_same_thread=False)
        try:
            init_arasuji_tables(conn)
            items, batch_ids = collect_annex_items(conn, 0, 1_000)
            assert batch_ids == [1]
            assert "未付記の灯台の知らせ" in items[0]["text"]

            captured: Dict[str, Any] = {}

            def _fake_generate(conn, messages, model_name, persona_id=None,
                               extra_items=None):
                captured["extra_items"] = extra_items
                return create_entry(
                    conn, level=1, content="新あらすじ",
                    source_ids=[m.id for m in messages], start_time=1, end_time=2,
                    source_count=len(messages), message_count=len(messages),
                )

            with _patch(
                "scripts.arasuji.build_arasuji_core.regenerate_entry_from_messages",
                _fake_generate,
            ):
                assert regenerate_entry(conn, entry.id) is not None
            assert ["付記済みの雷" in i["text"] for i in captured["extra_items"]] == [True]

            embedder = SimpleNamespace(embed=lambda texts, **kw: [[0.0, 0.0, 0.0] for _ in texts])
            hits = unified_recall(
                conn, embedder, "灯台",
                search_chronicle=False, search_memopedia=False,
                search_fragments=False, search_messages=False,
                search_perceptions=True,
            )
            assert [h.source_id for h in hits] == ["perception:1"]
            cols = {r[1] for r in conn.execute("PRAGMA table_info(perception_batches)")}
            assert {"kind", "recalled_json", "boundary_rowid", "room_state_json"} <= cols
        finally:
            conn.close()

    def test_insert_does_not_touch_the_ledger(self):
        import sqlite3

        from sai_memory.perception_buffer import (
            init_perception_buffer_table,
            insert_realtime_and_recall_batch,
            list_pending,
            list_unannexed_batches,
            push_perception,
        )

        conn = sqlite3.connect(":memory:")
        init_perception_buffer_table(conn)
        push_perception(conn, "world_state", "未消費の知覚")
        insert_realtime_and_recall_batch(
            conn, consumed_at=10, rendered_text="<system>x</system>",
            recalled_keys=[("memopedia", "p1")], boundary_created_at=9,
            boundary_rowid=3,
        )
        assert len(list_pending(conn)) == 1  # 消費を伴わない
        [batch] = list_unannexed_batches(conn)
        assert batch.is_realtime_and_recall
        assert batch.recalled_keys() == [("memopedia", "p1")]
        assert (batch.boundary_created_at, batch.boundary_rowid) == (9, 3)

    def test_keyword_search_of_perceptions_skips_kept_rows(self):
        import sqlite3

        from sai_memory.perception_buffer import (
            create_consumption_batch,
            init_perception_buffer_table,
            insert_realtime_and_recall_batch,
            push_perception,
        )
        from sai_memory.unified_recall import unified_recall

        conn = sqlite3.connect(":memory:")
        init_perception_buffer_table(conn)
        item = push_perception(conn, "feed", "灯台の記事")
        create_consumption_batch(
            conn, [item], consumed_at=5, rendered_text="[フィード]\n灯台の記事",
        )
        insert_realtime_and_recall_batch(
            conn, consumed_at=6,
            rendered_text="<system>ふと浮かんだ記憶:\n- 灯台の思い出</system>",
            recalled_keys=[("memopedia", "p1")],
        )
        embedder = SimpleNamespace(embed=lambda texts, **kw: [[0.0, 0.0, 0.0] for _ in texts])
        hits = unified_recall(
            conn, embedder, "灯台",
            search_chronicle=False, search_memopedia=False,
            search_fragments=False, search_messages=False,
            search_perceptions=True,
        )
        assert len(hits) == 1
        assert "記事" in hits[0].content


# ---------------------------------------------------------------------------
# 自動想起: 既出の記憶は候補の段階で外れる / 残した行はきっかけにならない
# ---------------------------------------------------------------------------


class TestRecallExclusion:
    def _memopedia_env(self):
        from sai_memory.memopedia import init_memopedia_tables
        from sai_memory.memopedia.storage import create_page
        from sai_memory.memory.storage import init_db
        from sai_memory.unified_recall import embed_memopedia_pages

        class _Same:
            model_name = "test"

            def embed(self, texts, *, is_query=False):
                return [[1.0, 0.0] for _ in texts]

        conn = init_db(":memory:")
        init_memopedia_tables(conn)
        a = create_page(conn, parent_id="root_terms", title="海辺の町",
                        summary="海", content="", category="terms")
        b = create_page(conn, parent_id="root_terms", title="灯台",
                        summary="光", content="", category="terms")
        embedder = _Same()
        embed_memopedia_pages(conn, embedder)
        return conn, embedder, a, b

    def test_excluded_key_frees_the_slot_at_collection(self):
        from sai_memory.unified_recall import unified_recall

        conn, embedder, a, b = self._memopedia_env()
        kwargs = dict(
            search_chronicle=False, search_memopedia=True,
            search_fragments=False, search_messages=False, keywords=[],
        )
        before = unified_recall(conn, embedder, "全く別の語", **kwargs)
        assert len(before) == 1  # memopedia の枠は 1 席
        occupier = before[0].source_id
        other = b.id if occupier == a.id else a.id
        after = unified_recall(
            conn, embedder, "全く別の語",
            exclude_keys={("memopedia", occupier)}, **kwargs,
        )
        assert [h.source_id for h in after] == [other]

    @pytest.mark.parametrize(
        "source", ["chronicle", "memopedia", "fragment", "message", "perception"],
    )
    def test_exclusion_frees_the_slot_for_every_source(self, source):
        """exclude_keys はどのソースでも候補を集める段階で効く — 枠 1 席の
        ソースで、席を占めていた候補を除外すると、もう一方が入る (Codex 一巡目
        #8 — perception だけ効いていなかった)。"""
        from sai_memory.arasuji import init_arasuji_tables
        from sai_memory.arasuji.storage import create_entry
        from sai_memory.memopedia import init_memopedia_tables
        from sai_memory.memopedia.storage import create_fragment, create_page
        from sai_memory.memory.storage import (
            add_message,
            init_db,
            replace_message_embeddings,
        )
        from sai_memory.perception_buffer import (
            create_consumption_batch,
            init_perception_buffer_table,
            push_perception,
        )
        from sai_memory.unified_recall import (
            embed_chronicle_entries,
            embed_memopedia_fragments,
            embed_memopedia_pages,
            unified_recall,
        )

        class _Same:
            model_name = "test"

            def embed(self, texts, *, is_query=False):
                return [[1.0, 0.0] for _ in texts]

        embedder = _Same()
        conn = init_db(":memory:")
        init_arasuji_tables(conn)
        init_memopedia_tables(conn)
        init_perception_buffer_table(conn)
        word = "灯台"
        if source == "chronicle":
            for i in range(2):
                create_entry(
                    conn, level=1, content=f"{word}の話 {i}", source_ids=[],
                    start_time=1000 + i, end_time=2000 + i,
                    source_count=1, message_count=1,
                )
            embed_chronicle_entries(conn, embedder, level=1)
        elif source == "memopedia":
            for i in range(2):
                create_page(conn, parent_id="root_terms", title=f"{word}{i}",
                            summary=word, content="", category="terms")
            embed_memopedia_pages(conn, embedder)
        elif source == "fragment":
            page = create_page(conn, parent_id="root_terms", title="港",
                               summary="港", content="", category="terms")
            for i in range(2):
                create_fragment(conn, entity_id=page.id, content=f"{word}の記憶 {i}",
                                source_date="2026-10-14")
            embed_memopedia_fragments(conn, embedder)
        elif source == "message":
            for i in range(2):
                mid = add_message(
                    conn, thread_id="t1", role="user", content=f"{word}の話 {i}",
                    line_role="main_line", scope="committed",
                )
                replace_message_embeddings(conn, mid, [[1.0, 0.0]])
        else:
            for i in range(2):
                item = push_perception(conn, "feed", f"{word}の記事 {i}")
                create_consumption_batch(
                    conn, [item], consumed_at=10 + i, rendered_text=f"[フィード]\n{word}の記事 {i}",
                )
        kwargs = {
            "search_chronicle": source == "chronicle",
            "search_memopedia": source == "memopedia",
            "search_fragments": source == "fragment",
            "search_messages": source == "message",
            "search_perceptions": source == "perception",
            "source_allocations": {source: 1},
        }
        try:
            before = [h for h in unified_recall(conn, embedder, word, **kwargs)
                      if h.source_type == source]
            assert len(before) == 1
            occupier = before[0].source_id
            after = [
                h for h in unified_recall(
                    conn, embedder, word, exclude_keys={(source, occupier)}, **kwargs,
                )
                if h.source_type == source
            ]
            assert len(after) == 1
            assert after[0].source_id != occupier
        finally:
            conn.close()

    def test_run_auto_recall_new_only_and_entity_trigger_exclusion(self):
        from sea.auto_recall import reset_ledger, run_auto_recall

        conn, embedder, a, b = self._memopedia_env()
        reset_ledger("p-ex")
        messages = [{"role": "user", "content": "海辺の町と灯台の話"}]
        try:
            first = run_auto_recall(
                conn, embedder, messages, persona_id="p-ex", thread_id="t",
                exclude_keys=set(), new_only=True,
            )
            assert first.injected
            shown = set(first.recalled_keys)
            second = run_auto_recall(
                conn, embedder, messages, persona_id="p-ex", thread_id="t",
                exclude_keys=shown, new_only=True,
            )
            # 既出 (残した行にある) 記憶はエンティティトリガーでも浮かばない
            assert not (set(second.recalled_keys) & shown)
            third = run_auto_recall(
                conn, embedder, messages, persona_id="p-ex", thread_id="t",
                exclude_keys={("memopedia", a.id), ("memopedia", b.id)},
                new_only=True,
            )
            # 新しく浮かんだ記憶が無いターンは出さない (台帳に粘着が残っていても)
            assert not third.injected
            assert third.recalled_keys == []
        finally:
            reset_ledger("p-ex")

    def test_kept_rows_are_not_query_seeds_or_observations(self):
        from sea.auto_recall import (
            _fresh_observation_texts,
            _latest_user_message,
            build_query,
        )
        from sea.eviction_plan import (
            CONSUMED_PERCEPTION_KEY,
            PERSISTED_REALTIME_AND_RECALL_KEY,
        )

        kept = {
            "role": "user",
            "content": "<system>ふと浮かんだ記憶:\n- 古い記憶</system>",
            "metadata": {
                CONSUMED_PERCEPTION_KEY: True,
                PERSISTED_REALTIME_AND_RECALL_KEY: True,
            },
        }
        perception = {
            "role": "user", "content": "<system>[システム通知]\n雨が降ってきた</system>",
            "metadata": {CONSUMED_PERCEPTION_KEY: True},
        }
        # 自律のターン (ユーザーの発言が無い): 末尾の知覚は種になるが、残した行はならない
        messages = [
            {"role": "assistant", "content": "前の返事"},
            perception,
            kept,
        ]
        assert _fresh_observation_texts(messages) == ["[システム通知]\n雨が降ってきた"]
        assert "古い記憶" not in build_query(messages)
        assert _latest_user_message(messages) is None
        # 残した行が最後尾でなくても同じ
        assert _fresh_observation_texts([messages[0], kept, perception]) == [
            "[システム通知]\n雨が降ってきた",
        ]


# ---------------------------------------------------------------------------
# あらすじの材料
# ---------------------------------------------------------------------------


class TestArasujiMaterial:
    def _conn_with_batches(self):
        import sqlite3

        from sai_memory.perception_buffer import (
            create_consumption_batch,
            init_perception_buffer_table,
            insert_realtime_and_recall_batch,
            push_perception,
        )

        conn = sqlite3.connect(":memory:")
        init_perception_buffer_table(conn)
        item = push_perception(conn, "world_state", "雨が降ってきた")
        create_consumption_batch(conn, [item], consumed_at=100,
                                 rendered_text="[システム通知]\n雨が降ってきた")
        insert_realtime_and_recall_batch(
            conn, consumed_at=101,
            rendered_text=(
                "<system>\n## リアルタイム情報\n- 現在時刻: 10:00\n</system>\n"
                "<system>ふと浮かんだ記憶:\n- 海辺の町</system>"
            ),
            recalled_keys=[("memopedia", "p1")],
        )
        insert_realtime_and_recall_batch(
            conn, consumed_at=102,
            rendered_text="<system>\n## リアルタイム情報\n- 現在時刻: 10:01\n</system>",
        )
        insert_realtime_and_recall_batch(
            conn, consumed_at=103,
            rendered_text="<system>\n## リアルタイム情報\n- 視界: " + "長" * 600 + "\n</system>",
        )
        return conn

    def test_annex_items_carry_labels(self):
        from sai_memory.arasuji.executor import collect_annex_items
        from sai_memory.arasuji.generator import (
            _format_messages_for_prompt,
            has_recall_material,
        )

        conn = self._conn_with_batches()
        items, batch_ids = collect_annex_items(conn, 0, 1000)
        assert len(batch_ids) == 4
        assert [i.get("label") for i in items] == [None, "recall", "notice", "notice"]
        assert "<system>" not in items[1]["text"]
        assert has_recall_material([], items)
        prompt = _format_messages_for_prompt([], include_timestamp=False, extra_items=items)
        i_perception = prompt.index("【知覚】: [システム通知]")
        i_recall = prompt.index("【想起 (過去の再提示)】: ## リアルタイム情報")
        i_notice = prompt.index("【通知】: ## リアルタイム情報\n- 現在時刻: 10:01")
        assert i_perception < i_recall < i_notice
        assert "海辺の町" in prompt[i_recall:i_notice]
        # 長さは機構名義の行と同じ規則 (閾値を超えたら機械的な一行)
        tail = prompt[i_notice:].split("\n\n", 1)[1]
        assert tail.startswith("【通知】") and "字)" in tail
        assert "長" * 600 not in prompt

    def test_notice_only_material_does_not_trigger_the_recall_note(self):
        from sai_memory.arasuji.generator import has_recall_material

        assert not has_recall_material(
            [], [{"at": 1, "text": "現在時刻: 10:00", "label": "notice"}],
        )


# ---------------------------------------------------------------------------
# 画像の枚数の上限 — 残した行の画像は数に入れない (全プロバイダ共通の判定)
# ---------------------------------------------------------------------------

_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4z8AAAAMBAQDJ/pLvAAAAAElFTkSuQmCC"
)


class TestAttachmentLimit:
    """ON のモデルの列には画像の枚数の上限を使わない (列ごとの印、Codex 五巡目 #1)。

    印は列の先頭 (たいてい system のメッセージ) に一つ。どのクライアントも、
    system を抜き出す前の列で判定し、印があれば全部の画像を画像のまま送る。
    印の無い列 (OFF のモデル) は今までどおり上限で古い画像を文字に置き換える。
    """

    def _messages(self, tmp_path, *, off: bool, n_images: int = 3):
        import base64

        from llm_clients.utils import mark_attachment_limit_off

        msgs: List[Dict[str, Any]] = [{"role": "system", "content": "HEAD"}]
        for i in range(n_images):
            path = tmp_path / f"img{i}.png"
            path.write_bytes(base64.b64decode(_PNG_B64))
            msgs.append({"role": "user", "content": f"写真{i}", "metadata": {
                "media": [{"path": str(path), "mime_type": "image/png", "type": "image"}],
                "__skip_image_summary__": True,
            }})
            msgs.append({"role": "assistant", "content": f"見たよ{i}"})
        if off:
            mark_attachment_limit_off(msgs)
        return msgs

    def test_shared_predicate(self):
        from llm_clients.utils import (
            ATTACHMENT_LIMIT_OFF_KEY,
            attachment_limit_is_off,
            mark_attachment_limit_off,
        )

        original = {"role": "system", "content": "HEAD"}
        msgs = [original, {"role": "user", "content": "x"}]
        assert not attachment_limit_is_off(msgs)
        mark_attachment_limit_off(msgs)
        assert attachment_limit_is_off(msgs)
        assert msgs[0]["metadata"] == {ATTACHMENT_LIMIT_OFF_KEY: True}
        assert "metadata" not in original  # 元の dict は書き換えない
        assert not attachment_limit_is_off([])
        assert not attachment_limit_is_off(None)

    @pytest.mark.parametrize("off,expected", [(True, {1, 3, 5}), (False, {5})])
    def test_openai(self, tmp_path, off, expected):
        from llm_clients.openai_message_preparer import scan_message_metadata

        msgs = self._messages(tmp_path, off=off)
        *_rest, allowed = scan_message_metadata(msgs, max_image_embeds_override=1)
        assert (allowed is None) == off
        if allowed is not None:
            assert {m for m, _ in allowed} == expected

    @pytest.mark.parametrize("off,expected", [(True, 3), (False, 1)])
    def test_anthropic_reads_the_mark_before_dropping_system(self, tmp_path, off, expected):
        """Anthropic は system を抜いた列で画像を数えるので、印は抜く前に読む。"""
        from llm_clients.anthropic_request_builder import build_request_params

        built = build_request_params(
            self._messages(tmp_path, off=off), tools=None, response_schema=None,
            temperature=None, enable_cache=False, cache_ttl="5m",
            model="claude-test", max_tokens=100, extra_params={},
            thinking_config=None, thinking_effort=None, supports_images=True,
            max_image_bytes=None, max_image_embeds=1,
        )
        images = [
            block for msg in built["request_params"]["messages"]
            for block in (msg["content"] if isinstance(msg["content"], list) else [])
            if isinstance(block, dict) and block.get("type") == "image"
        ]
        assert len(images) == expected

    @pytest.mark.parametrize("off,expected", [(True, 3), (False, 1)])
    def test_gemini(self, tmp_path, monkeypatch, off, expected):
        from unittest.mock import MagicMock

        from llm_clients.gemini import GeminiClient

        monkeypatch.setenv("SAIVERSE_GEMINI_ATTACHMENT_LIMIT", "1")
        with patch(
            "llm_clients.gemini.build_gemini_clients",
            return_value=(MagicMock(), MagicMock(), MagicMock()),
        ):
            client = GeminiClient("gemini-4-argon", config={"supports_images": True})
        client.supports_images = True
        _, contents = client._convert_messages(self._messages(tmp_path, off=off))
        inline = [
            p for c in contents for p in (c.parts or [])
            if getattr(p, "inline_data", None) is not None
        ]
        assert len(inline) == expected

    @pytest.mark.parametrize("off,expected", [(True, 3), (False, 1)])
    def test_xai(self, tmp_path, monkeypatch, off, expected):
        from llm_clients.xai import _build_xai_messages

        monkeypatch.setenv("SAIVERSE_XAI_ATTACHMENT_LIMIT", "1")
        built = _build_xai_messages(
            self._messages(tmp_path, off=off), supports_images=True,
        )
        dumped = "\n".join(str(m) for m in built)
        assert dumped.count("data:image/") == expected

    def test_sluice_estimate_follows_the_same_mark(self, tmp_path, monkeypatch):
        from sea import sluice

        monkeypatch.setattr(
            sluice, "_media_rules",
            lambda model: sluice._MediaRules(
                image_limit=1,
                image_limit_roles=frozenset({"user"}),
                image_embed_roles=frozenset({"user"}),
                image_note_roles=frozenset(),
                audio_note_roles=frozenset(),
                video_note_roles=frozenset(),
            ),
        )
        estimate = sluice._estimate_input_tokens
        off_total = estimate(self._messages(tmp_path, off=True), "gemini-4-argon")
        on_total = estimate(self._messages(tmp_path, off=False), "gemini-4-argon")
        # 上限を使わない列では、枠の外だった 2 枚も埋め込みとして数える
        from saiverse.token_estimator import estimate_image_tokens

        from saiverse.model_configs import get_model_config

        per_image = estimate_image_tokens(
            str(get_model_config("gemini-4-argon").get("provider", "ollama")),
        )
        assert off_total - on_total == 2 * per_image


@pytest.mark.parametrize("env", [False], indirect=True, ids=["sync"])
@pytest.mark.parametrize("writer", ["record_realtime_and_recall", "reseat_room_state"])
def test_failed_write_rolls_back_inside_the_lock(env, monkeypatch, writer):
    """保存に失敗したときの rollback は錠前の内側で行う — 錠前を手放した後に
    共有接続を rollback すると、その隙に別のスレッドが同じ接続で始めた書き込み
    (まだ commit していない) を消す (Codex 二巡目 #1)。

    失敗の後始末の rollback の直前で別のスレッドに書き込みを始めさせ、rollback
    の後に commit させる。錠前の内側で rollback していれば、別のスレッドは
    錠前を待ってから書くので、その書き込みは生き残る。"""
    import threading

    if writer == "record_realtime_and_recall":
        def _boom(*a, **k):
            raise RuntimeError("disk full")

        monkeypatch.setattr(
            "sai_memory.perception_buffer.insert_realtime_and_recall_batch", _boom,
        )

        def _call():
            return env.adapter.record_realtime_and_recall(
                content="<system>x</system>", pulse_id="pulse-fail",
            )
    else:
        def _boom(*a, **k):
            raise RuntimeError("disk full")

        monkeypatch.setattr("sai_memory.room_state.reseat_current_room", _boom)

        def _call():
            return env.adapter.reseat_room_state({"building_id": BUILDING_ID})

    real_conn = env.adapter.conn
    started = threading.Event()
    release = threading.Event()
    other_errors: List[BaseException] = []

    def _other_writer():
        try:
            with env.adapter._db_lock:
                real_conn.execute(
                    "INSERT INTO working_memory (persona_id, data, updated_at) "
                    "VALUES ('other-writer', 'survives', 0)"
                )
                started.set()
                release.wait(5)
                real_conn.commit()
        except BaseException as exc:  # pragma: no cover - 失敗の報告用
            other_errors.append(exc)
            started.set()

    threads: List[threading.Thread] = []

    class _ConnWithRollbackHook:
        """共有接続そのもの。rollback の直前にだけ、別のスレッドを走らせる。"""

        def __getattr__(self, name):
            return getattr(real_conn, name)

        def rollback(self):
            if not threads:
                t = threading.Thread(target=_other_writer)
                threads.append(t)
                t.start()
                # 錠前を持ったままなら別のスレッドは書き始められない (待つだけ)。
                # 錠前の外なら、ここで別のスレッドの未確定の書き込みが始まる。
                started.wait(0.5)
            return real_conn.rollback()

    monkeypatch.setattr(env.adapter, "conn", _ConnWithRollbackHook())
    assert _call() is None
    monkeypatch.setattr(env.adapter, "conn", real_conn)
    release.set()
    for t in threads:
        t.join(5)
    assert threads, "失敗の後始末 (rollback) を通っていない"
    assert not other_errors
    with env.adapter._db_lock:
        rows = env.adapter.conn.execute(
            "SELECT data FROM working_memory WHERE persona_id = 'other-writer'"
        ).fetchall()
    assert rows == [("survives",)]


@pytest.mark.parametrize("env", [False], indirect=True, ids=["sync"])
def test_same_pulse_is_kept_only_once_even_without_the_message_mark(env):
    """一ターン一行の二重の守り: まとまりの印が写しで失われても、同じパルスの行は一行。"""
    first = env.adapter.record_realtime_and_recall(
        content="<system>\n## リアルタイム情報\n- 現在時刻: x\n</system>",
        pulse_id="pulse-dup",
    )
    second = env.adapter.record_realtime_and_recall(
        content="<system>\n## リアルタイム情報\n- 現在時刻: y\n</system>",
        pulse_id="pulse-dup",
    )
    assert first is not None
    assert second == first
    rows = _kept_rows(env)
    assert len(rows) == 1
    assert "現在時刻: x" in rows[0][2]


# ---------------------------------------------------------------------------
# ON のモデルの列には画像の枚数の上限を使わない (実際の経路、Codex 五巡目 #1)
# ---------------------------------------------------------------------------

CAMERA_TOOL = "fake_camera"


def _image(env) -> Dict[str, Any]:
    return {"type": "image", "path": env.image_path, "mime_type": "image/png"}


def _register_camera(env, monkeypatch, *, fail: bool = False) -> None:
    """TOOL ノードから呼ぶ偽のツール (画像付きの結果、または例外)。"""
    from tools import TOOL_REGISTRY

    def fake_camera(**kwargs):
        if fail:
            raise RuntimeError("カメラが壊れた")
        return ("写真を撮った", None, None, {"media": [_image(env)]})

    monkeypatch.setitem(TOOL_REGISTRY, CAMERA_TOOL, fake_camera)


def _tool_then_llm_playbook(*, memorize_image: bool = False):
    """TOOL → (MEMORIZE) → LLM のトップレベルの Playbook。

    ``memorize_image`` なら、ツールの結果の metadata (画像) を MEMORIZE で
    そのまま記録に残す (sea/runtime_engine.lg_memorize_node の metadata_key)。
    """
    talk = {
        "id": "talk", "type": "llm", "action": None, "speak": True,
        "memorize": True, "metadata_key": "metadata",
        "output_keys": [{"text": "speak_content"}], "next": None,
    }
    look = {
        "id": "look", "type": "tool", "action": CAMERA_TOOL,
        "output_keys": ["camera_text", "camera_snippet", "camera_file", "camera_meta"],
        "next": "keep" if memorize_image else "talk",
    }
    nodes = [look]
    if memorize_image:
        nodes.append({
            "id": "keep", "type": "memorize", "action": "{camera_text}",
            "metadata_key": "camera_meta", "next": "talk",
        })
    nodes.append(talk)
    return _custom_playbook(nodes, name="camera_pb")


def _gemini_image_parts(messages: List[Dict[str, Any]]) -> tuple:
    """Gemini の送信形式での (画像のまま届いた枚数, 文字に置き換わった枚数)。"""
    _, dumped = _gemini_dump(messages)
    inline = sum(
        1 for c in dumped for p in (c.get("parts") or []) if p.get("inline_data")
    )
    notes = sum(
        str(p.get("text") or "").count("[画像参照のみ:")
        for c in dumped for p in (c.get("parts") or [])
    )
    return inline, notes


def _images_in(messages: List[Dict[str, Any]]) -> int:
    from saiverse.media_utils import iter_image_media

    return sum(
        len(iter_image_media(m.get("metadata"))) for m in messages
        if isinstance(m, dict) and m.get("role") != "system"
    )


@pytest.mark.parametrize("env", [True, False], indirect=True, ids=["stream", "sync"])
class TestNoImageLimitOnTheRealPath:
    def test_perception_tool_and_spell_images_over_the_limit(self, env, monkeypatch):
        """ユーザーの画像を一枚も使わず、知覚の画像・ツールの画像 (MEMORIZE で
        記録に残したもの)・リアルタイムスペルの画像だけで上限 (既定 4 枚) を
        超える。ON のモデルでは、二ターン目の最初の呼び出しの先頭が一ターン目の
        最初の呼び出しと Gemini の送信形式でも一致し、画像は一枚も文字に
        置き換わらない。"""
        from sai_memory.perception_buffer import push_perception

        _bind_realtime_spell(env)
        _register_camera(env, monkeypatch)
        # ツールの画像は、比べる二ターンより前のターンで記録に残しておく
        # (比べる二ターンの差が画像の上限の扱いだけになるように)。
        _run_turn(
            env, "写真を撮って。", playbook=_tool_then_llm_playbook(memorize_image=True),
        )
        # 知覚の画像は別々のファイルにする (同じ場所の画像は一枚にまとまる)
        media = []
        for i in range(5):
            path = Path(env.image_path).with_name(f"perception{i}.png")
            path.write_bytes(Path(env.image_path).read_bytes())
            media.append({"path": str(path), "mime_type": "image/png", "role": "image"})
        with env.adapter._db_lock:
            push_perception(
                env.adapter.conn, "world_state", "窓の外を見た", media=media[:4],
            )
        turn1 = _run_turn(env, "外はどう?")
        with env.adapter._db_lock:
            push_perception(
                env.adapter.conn, "world_state", "鳥が飛んだ", media=media[4:],
            )
        turn2 = _run_turn(env, "どうだった?")

        _assert_first_call_prefix(turn1, turn2)
        _assert_gemini_prefix(turn1[0], turn2[0])
        assert _images_in(turn1[0]) > 4
        assert _images_in(turn2[0]) > _images_in(turn1[0])
        inline2, notes2 = _gemini_image_parts(turn2[0])
        assert notes2 == 0
        assert inline2 == _images_in(turn2[0])

    def test_fifth_user_image(self, env):
        """ユーザーの画像が 5 枚目に達しても、ON のモデルでは一番古い画像が
        文字に置き換わらず、前回の最初の呼び出しと先頭が一致する。"""
        for i in range(4):
            env.persona.history_manager.add_to_persona_only({
                "role": "user", "content": f"写真{i}です。",
                "metadata": {"media": [_image(env)]},
            })
            env.persona.history_manager.add_to_persona_only(
                {"role": "assistant", "content": f"写真{i}、見たよ。"},
            )
        turn1 = _run_turn(env, "どれが好き?")
        turn2 = _run_turn(env, "これも見て。", user_metadata={"media": [_image(env)]})

        _assert_first_call_prefix(turn1, turn2)
        _assert_gemini_prefix(turn1[0], turn2[0])
        inline2, notes2 = _gemini_image_parts(turn2[0])
        assert (inline2, notes2) == (5, 0)

    def test_off_model_keeps_the_limit(self, env):
        """OFF のモデルは今までどおり: 5 枚目のユーザーの画像で一番古い画像が
        文字に置き換わる (不変条件 2)。"""
        env.persona.model = OFF_MODEL
        env.persona.lightweight_model = OFF_MODEL
        for i in range(4):
            env.persona.history_manager.add_to_persona_only({
                "role": "user", "content": f"写真{i}です。",
                "metadata": {"media": [_image(env)]},
            })
            env.persona.history_manager.add_to_persona_only(
                {"role": "assistant", "content": f"写真{i}、見たよ。"},
            )
        turn = _run_turn(env, "これも見て。", user_metadata={"media": [_image(env)]})
        assert _gemini_image_parts(turn[0]) == (4, 1)


# ---------------------------------------------------------------------------
# 最初の LLM 呼び出しの前の TOOL の結果 (Codex 五巡目 #2) と、記録に残らない
# メッセージの機械検査
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("env", [True, False], indirect=True, ids=["stream", "sync"])
class TestToolResultBeforeTheFirstCall:
    @pytest.mark.parametrize("fail", [False, True], ids=["result", "error"])
    def test_tool_then_llm_and_next_turn(self, env, monkeypatch, fail):
        """ON のトップレベルの user パルスで TOOL → LLM。ツールの結果 (エラーの
        結果も) は記録に残る形 (id 付き・復元形の文面) で送られ、次のターンの
        最初の呼び出しの先頭と一致する (Gemini の送信形式でも)。"""
        _register_camera(env, monkeypatch, fail=fail)
        turn1 = _run_turn(env, "写真を撮って。", playbook=_tool_then_llm_playbook())
        turn2 = _run_turn(env, "どうだった?")

        _assert_first_call_prefix(turn1, turn2)
        _assert_gemini_prefix(turn1[0], turn2[0])
        word = "error" if fail else "result"
        tool_msgs = [
            m for m in turn1[0] if f"tool '{CAMERA_TOOL}' {word}:" in str(m.get("content"))
        ]
        assert len(tool_msgs) == 1
        assert tool_msgs[0]["id"]
        assert tool_msgs[0]["content"].startswith(f"<system>\ntool '{CAMERA_TOOL}' {word}:")
        assert tool_msgs[0]["content"].endswith("\n</system>")
        with env.adapter._db_lock:
            stored = env.adapter.conn.execute(
                "SELECT role FROM messages WHERE id = ?", (tool_msgs[0]["id"],),
            ).fetchall()
        assert [r[0] for r in stored] == ["system"]

    def test_off_model_tool_result_is_not_kept(self, env, monkeypatch):
        """OFF のモデルは今までどおり: 記録に残さず、今までの文面で送る。"""
        env.persona.model = OFF_MODEL
        env.persona.lightweight_model = OFF_MODEL
        _register_camera(env, monkeypatch)
        turn1 = _run_turn(env, "写真を撮って。", playbook=_tool_then_llm_playbook())
        tool_msgs = [
            m for m in turn1[0] if f"tool '{CAMERA_TOOL}' result:" in str(m.get("content"))
        ]
        assert tool_msgs == [{
            "role": "user",
            "content": f"<system>tool '{CAMERA_TOOL}' result:\n写真を撮った</system>",
        }]
        with env.adapter._db_lock:
            stored = env.adapter.conn.execute(
                "SELECT COUNT(*) FROM messages WHERE content LIKE 'tool %'"
            ).fetchone()[0]
        assert stored == 0


_UNRECORDED_WARNING = "carries a message that is not kept"
_NOT_SENT_WARNING = "the record has a row that the first send of this turn does not carry"


# ---------------------------------------------------------------------------
# 最初の LLM 呼び出しの直前に、送る列の後ろ側を記録に揃える (Codex 六巡目)
#
# 最初の送信の前に記録に書かれるもの (事前スペル・TOOL の結果・ツールの中で
# 書かれる記録・MEMORIZE …) を差し込み口ごとに送る列へ同期するのをやめ、
# 送る列の「最後の記録済みの発言より後ろ」を、次のターンの prepare_context と
# 同じ取り出し・同じ変換で記録から読み直した並びに揃える
# (sea/runtime_llm._align_first_send_with_record →
#  sea/runtime_context.align_first_send_with_record)。
# ---------------------------------------------------------------------------


SNAPSHOT_TOOL = "get_situation_snapshot"
_CHANGE_MARK = "【状況変化】"


def _use_real_situation_snapshot(env) -> None:
    """本物の get_situation_snapshot (状況の変化を記録に書く) が動く最小の世界。

    ツールは persona_context の manager から自分を引き、記録 (実 SAIMemoryAdapter
    のメモリ内でない一時ファイルの SQLite) の作業メモリと比べて、変化があれば
    ``<system>【状況変化】…</system>`` の行を記録に書く。
    """
    env.manager.all_personas = {PERSONA_ID: env.persona}
    env.manager.user_presence_status = "online"
    env.persona.manager_ref = env.manager
    env.persona.timezone_name = "UTC"
    env.persona.buildings = {}
    env.persona.id_to_name_map = {}


def _situation_snapshot_then_llm_playbook():
    """TOOL (状況の変化を記録する get_situation_snapshot) → LLM。"""
    return _custom_playbook([
        {
            "id": "snap", "type": "tool", "action": SNAPSHOT_TOOL,
            "args_input": {"detect_changes": True},
            "output_keys": ["snapshot_text", "change_info"],
            "next": "talk",
        },
        {
            "id": "talk", "type": "llm", "action": None, "speak": True,
            "memorize": True, "metadata_key": "metadata",
            "output_keys": [{"text": "speak_content"}], "next": None,
        },
    ], name="snapshot_pb")


def _tool_memorize_tool_then_llm_playbook():
    """TOOL → MEMORIZE → TOOL → LLM (MEMORIZE の行が二つの結果の間に記録される)。"""
    return _custom_playbook([
        {
            "id": "look1", "type": "tool", "action": CAMERA_TOOL,
            "output_keys": ["camera_text", "camera_snippet", "camera_file", "camera_meta"],
            "next": "keep",
        },
        {
            "id": "keep", "type": "memorize", "action": "メモ: {camera_text}",
            "next": "look2",
        },
        {
            "id": "look2", "type": "tool", "action": CAMERA_TOOL,
            "output_keys": ["camera_text", "camera_snippet", "camera_file", "camera_meta"],
            "next": "talk",
        },
        {
            "id": "talk", "type": "llm", "action": None, "speak": True,
            "memorize": True, "metadata_key": "metadata",
            "output_keys": [{"text": "speak_content"}], "next": None,
        },
    ], name="camera_memo_pb")


#: 最初の送信の前に記録に書かれるものがある場面 (Codex 六巡目の (a)(b)) と、
#: 既存の場面。
_SCENES = [
    "plain", "pre_spell", "tool", "situation_change", "tool_memorize_tool",
    "realtime_spell", "auto_recall", "perception", "auto",
]


def _prepare_scene(env, monkeypatch, scene: str) -> Dict[str, Any]:
    """場面の準備。返り値は ``_run_turn`` への引数。"""
    kwargs: Dict[str, Any] = {}
    if scene == "pre_spell":
        from sea import runtime_llm

        async def _pre_spell(tool_name, tool_args, persona, state, playbook_name,
                             event_callback, messages=None, **kw):
            return ("記録しました", None, True)

        monkeypatch.setattr(runtime_llm, "_run_spell_tool_async", _pre_spell)
        kwargs["pre_spells"] = [f"/spell name='{LOOP_SPELL}' args={{}}"]
    elif scene == "tool":
        _register_camera(env, monkeypatch)
        kwargs["playbook"] = _tool_then_llm_playbook()
    elif scene == "situation_change":
        _use_real_situation_snapshot(env)
        kwargs["playbook"] = _situation_snapshot_then_llm_playbook()
    elif scene == "tool_memorize_tool":
        _register_camera(env, monkeypatch)
        kwargs["playbook"] = _tool_memorize_tool_then_llm_playbook()
    elif scene == "realtime_spell":
        _bind_realtime_spell(env)
    elif scene == "auto_recall":
        env.recall.hits = [_hit("page-a", "海辺の町")]
    elif scene == "perception":
        _push_perception(env, "雨が降ってきた")
    elif scene == "auto":
        kwargs["pulse_type"] = "auto"
    return kwargs


#: その場面の差し込みが実際に最初の呼び出しに載っていることの目印。
_SCENE_MARKS = {
    "pre_spell": "[Triggered by user]",
    "tool": f"tool '{CAMERA_TOOL}' result:",
    "situation_change": _CHANGE_MARK,
    "tool_memorize_tool": "メモ: 写真を撮った",
    "realtime_spell": _SPELL_LINE,
    "auto_recall": "海辺の町",
    "perception": "雨が降ってきた",
}


def _run_scene_turns(env, monkeypatch, scene: str):
    """一ターン目 (普通の会話) → その場面のターン → 次の普通のターン。"""
    _run_turn(env, "こんにちは。")
    kwargs = _prepare_scene(env, monkeypatch, scene)
    turn1 = _run_turn(env, None if scene == "auto" else "続き。", **kwargs)
    turn2 = _run_turn(env, "それで?")
    return turn1, turn2


@pytest.mark.parametrize("env", [True, False], indirect=True, ids=["stream", "sync"])
@pytest.mark.parametrize("scene", _SCENES)
def test_first_send_matches_the_next_turn(env, monkeypatch, scene):
    """不変条件 1: どの場面でも、ターンの最初の呼び出しの列が次のターンの最初の
    呼び出しの列の先頭と一字一句同じ (SAIVerse の列と Gemini の送信形式の両方)。

    ``situation_change`` (本物の get_situation_snapshot が状況の変化を記録に
    書く) と ``tool_memorize_tool`` (MEMORIZE の行が二つの TOOL の結果の間に
    記録される) は Codex 六巡目の指摘の場面で、送る列を記録に揃える前は
    次のターンの先頭と割れた。"""
    turn1, turn2 = _run_scene_turns(env, monkeypatch, scene)
    mark = _SCENE_MARKS.get(scene)
    if mark:
        assert any(mark in str(m.get("content")) for m in turn1[0])
    _assert_first_call_prefix(turn1, turn2)
    _assert_gemini_prefix(turn1[0], turn2[0])


@pytest.mark.parametrize("env", [True], indirect=True, ids=["stream"])
def test_situation_change_and_memorize_rows_sit_where_the_record_puts_them(
    env, monkeypatch,
):
    """揃えた後の最初の呼び出しでは、ツールの中で書かれた状況変化の記録が
    TOOL の結果の前に、MEMORIZE の行が二つの結果の間に並び、そのターンの
    まとまりはそれらの後ろ (列の一番後ろ) にある。"""
    from sea.runtime_context import find_turn_realtime_and_recall_index

    turn1, _ = _run_scene_turns(env, monkeypatch, "situation_change")
    contents = [str(m.get("content")) for m in turn1[0]]
    change = next(i for i, c in enumerate(contents) if _CHANGE_MARK in c)
    result = next(i for i, c in enumerate(contents) if f"tool '{SNAPSHOT_TOOL}' result:" in c)
    assert change < result
    assert find_turn_realtime_and_recall_index(turn1[0]) == len(turn1[0]) - 1

    turn1, _ = _run_scene_turns(env, monkeypatch, "tool_memorize_tool")
    contents = [str(m.get("content")) for m in turn1[0]]
    results = [i for i, c in enumerate(contents) if f"tool '{CAMERA_TOOL}' result:" in c]
    memo = next(i for i, c in enumerate(contents) if "メモ: 写真を撮った" in c)
    assert len(results) == 2 and results[0] < memo < results[1]
    assert find_turn_realtime_and_recall_index(turn1[0]) == len(turn1[0]) - 1


def _spy_alignment(monkeypatch) -> List[Any]:
    """送る直前の揃えと検査 (align_first_send_with_record) の結果を写し取る。"""
    from sea import runtime_context

    seen: List[Any] = []
    real = runtime_context.align_first_send_with_record

    def _spy(messages, base_messages, source):
        out = real(messages, base_messages, source)
        seen.append(out)
        return out

    monkeypatch.setattr(runtime_context, "align_first_send_with_record", _spy)
    return seen


@pytest.mark.parametrize("env", [False], indirect=True, ids=["sync"])
@pytest.mark.parametrize("scene", _SCENES)
def test_first_send_check_is_clean_after_alignment(env, monkeypatch, caplog, scene):
    """揃えた後の機械検査 (sea/runtime_context.find_unrecorded_messages —
    送る列の境界より後ろと、記録から読み直した並びの比較) は、どの場面でも
    食い違いを返さず、WARN も出ない。揃えはトップレベルの残すパルスで
    ターンに一度だけ走る。"""
    import logging

    seen = _spy_alignment(monkeypatch)
    with caplog.at_level(logging.WARNING, logger="sea.runtime_llm"):
        _run_scene_turns(env, monkeypatch, scene)
    # 三ターン (一ターン目・場面のターン・次のターン) で一度ずつ
    assert seen == [[], [], []]
    assert "persist_realtime_and_recall" not in caplog.text


@pytest.mark.parametrize("env", [False], indirect=True, ids=["sync"])
@pytest.mark.parametrize(
    "scene, expected",
    [
        ("situation_change", _CHANGE_MARK),
        ("tool_memorize_tool", "メモ: 写真を撮った"),
    ],
)
def test_check_finds_the_row_when_the_list_is_not_aligned(
    env, monkeypatch, caplog, scene, expected,
):
    """揃える前のコードと同じ列 (置き換えを止めた列) では、機械検査が
    「記録にはあるのに送る列のその位置に無い行」を見つけて WARN を出し、
    次のターンの先頭と実際に割れる。検査は送った行の保存だけでなく、
    復元時に間へ入る行も見る (Codex 六巡目: 以前の検査はここを空で返した)。"""
    import logging

    from sea import runtime_context

    monkeypatch.setattr(runtime_context, "_align_with_record", lambda *a, **k: None)
    seen = _spy_alignment(monkeypatch)
    with caplog.at_level(logging.WARNING, logger="sea.runtime_llm"):
        turn1, turn2 = _run_scene_turns(env, monkeypatch, scene)
    scene_result = seen[1]
    assert any(
        kind == runtime_context.NOT_SENT and expected in str(m.get("content"))
        for kind, m in scene_result
    )
    assert _NOT_SENT_WARNING in caplog.text
    with pytest.raises(AssertionError):
        _assert_first_call_prefix(turn1, turn2)


@pytest.mark.parametrize("env", [True, False], indirect=True, ids=["stream", "sync"])
def test_first_ever_turn_without_history(env, monkeypatch):
    """履歴が一行も無い最初のパルス (自律) で TOOL → LLM。境界になる発言が
    無いので、head の直後から記録に揃える (時刻の目安も読み直しで組まれる)。
    次のターンの最初の呼び出しの先頭と一致する。"""
    seen = _spy_alignment(monkeypatch)
    _register_camera(env, monkeypatch)
    turn1 = _run_turn(env, None, pulse_type="auto", playbook=_tool_then_llm_playbook())
    turn2 = _run_turn(env, "おはよう。")
    assert any(
        f"tool '{CAMERA_TOOL}' result:" in str(m.get("content")) for m in turn1[0]
    )
    _assert_first_call_prefix(turn1, turn2)
    _assert_gemini_prefix(turn1[0], turn2[0])
    assert seen == [[], []]


@pytest.mark.parametrize("env", [False], indirect=True, ids=["sync"])
def test_first_send_check_reports_an_unrecorded_message(env, monkeypatch, caplog):
    """記録に残らない差し込み (ここでは LLM ノードの action の指示文) は、揃えた
    後もそのターンのまとまりの後ろに残り、送る直前の検査が役割と先頭の文字を
    WARN で出す (送信は止めない)。"""
    import logging

    from sea import runtime_context

    seen = _spy_alignment(monkeypatch)
    playbook = _custom_playbook([{
        "id": "talk", "type": "llm", "action": "手短に答えて。", "speak": True,
        "memorize": True, "metadata_key": "metadata",
        "output_keys": [{"text": "speak_content"}], "next": None,
    }], name="action_pb")
    with caplog.at_level(logging.WARNING, logger="sea.runtime_llm"):
        turn = _run_turn(env, "こんにちは。", playbook=playbook)
    assert [(kind, m["content"]) for kind, m in seen[0]] == [
        (runtime_context.NOT_KEPT, "<system>手短に答えて。</system>"),
    ]
    assert turn[0][-1]["content"] == "<system>手短に答えて。</system>"
    assert _UNRECORDED_WARNING in caplog.text
    assert "手短に答えて。" in caplog.text


@pytest.mark.parametrize("env", [False], indirect=True, ids=["sync"])
def test_off_model_and_nested_playbooks_are_not_aligned(env, monkeypatch):
    """OFF のモデルの列と、``_pulse_type`` を持たない入れ子の Playbook では、
    揃えも検査も走らない (送る列は今までどおり)。"""
    seen = _spy_alignment(monkeypatch)
    env.persona.model = OFF_MODEL
    env.persona.lightweight_model = OFF_MODEL
    _register_camera(env, monkeypatch)
    turn = _run_turn(env, "写真を撮って。", playbook=_tool_then_llm_playbook())
    assert seen == []
    # OFF のモデルでは TOOL の結果は記録に残らない今までの形のまま、最後に並ぶ
    assert turn[0][-1] == {
        "role": "user",
        "content": f"<system>tool '{CAMERA_TOOL}' result:\n写真を撮った</system>",
    }

    env.persona.model = ON_MODEL
    env.persona.lightweight_model = ON_MODEL
    seen.clear()
    _run_parent_with_child(env, monkeypatch, "child_first", "main")
    # 子 (入れ子、先に送る) では走らず、親 (トップレベル) の最初の送信で一度だけ。
    assert len(seen) == 1
