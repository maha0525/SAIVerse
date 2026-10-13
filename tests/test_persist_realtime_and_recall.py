"""リアルタイム情報と自動想起を残して送り続ける運用 (モデルごとの設定) の回帰。

正典: docs/intent/persist_realtime_and_recall.md

最も重く見るのは不変条件 1 — 「ON のモデルで、あるターンに送った列は、返事を
挟んだ次のターンの列の先頭と一字一句同じになる」。ここでは組み立て関数の単体
比較ではなく、実際の経路を通して LLM クライアントに渡った列を捕まえて比べる:

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

    def generate(self, messages, tools=None, temperature=None, response_schema=None, **kwargs):
        self.calls.append(copy.deepcopy(list(messages)))
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("fake LLM failure")
        if self.script:
            return self.script.pop(0)
        return f"返事{len(self.calls)}です。"

    def generate_stream(self, messages, tools=None, temperature=None, **kwargs):
        text = self.generate(messages, tools=tools, temperature=temperature, **kwargs)
        half = max(1, len(text) // 2)
        yield text[:half]
        yield text[half:]

    def consume_stream_error(self):
        return None


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
            timezone=__import__("datetime").timezone.utc,
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
                image_path=str(image_path),
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


def _run_turn(env, user_text: Optional[str], *, pulse_type: str = "user") -> List[List[Dict[str, Any]]]:
    """ユーザー発言を保存して 1 Pulse を実際の経路で走らせ、この Pulse の呼び出し列を返す。"""
    from sea.pulse_context import aspect_from_pulse_type

    if user_text is not None:
        env.persona.history_manager.add_to_persona_only(
            {"role": "user", "content": user_text},
        )
    env.adapter.flush_perception_buffer()
    before = len(env.llm.calls)
    env.runtime._run_playbook(
        _playbook(), env.persona, BUILDING_ID, user_text or "", auto_mode=False,
        pulse_type=pulse_type,
        pulse_line_aspect=aspect_from_pulse_type(pulse_type),
    )
    return env.llm.calls[before:]


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

        _assert_prefix(turn1[0], turn2[0])

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

        _assert_prefix(turn1[0], turn2[0])
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
        _assert_prefix(turn1[0], turn2[0])
        _assert_prefix(turn2[0], turn3[0])
        assert len(_kept_rows(env)) == 3

    def test_utterance_and_keeping_in_the_same_second(self, env):
        """発言と残す時刻が同じ秒に並んでも、位置は発言で決まる。"""
        fixed = time.time()
        with patch("time.time", return_value=fixed):
            turn1 = _run_turn(env, "同じ秒です。")
            turn2 = _run_turn(env, "これも同じ秒。")
        _assert_prefix(turn1[0], turn2[0])

    def test_auto_pulse_without_user_utterance(self, env):
        turn0 = _run_turn(env, "こんにちは。")
        turn1 = _run_turn(env, None, pulse_type="auto")
        turn2 = _run_turn(env, "おかえり。")
        _assert_prefix(turn0[0], turn1[0])
        _assert_prefix(turn1[0], turn2[0])
        assert len(_kept_rows(env)) == 3


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
    def _messages(self, tmp_path, *, exempt: bool):
        import base64

        from llm_clients.utils import ATTACHMENT_LIMIT_EXEMPT_KEY

        old = tmp_path / "old.png"
        new = tmp_path / "new.png"
        old.write_bytes(base64.b64decode(_PNG_B64))
        new.write_bytes(base64.b64decode(_PNG_B64))
        kept_meta: Dict[str, Any] = {
            "media": [{"path": str(new), "mime_type": "image/png", "type": "image"}],
            "__skip_image_summary__": True,
        }
        if exempt:
            kept_meta[ATTACHMENT_LIMIT_EXEMPT_KEY] = True
        return [
            {"role": "user", "content": "古い写真", "metadata": {
                "media": [{"path": str(old), "mime_type": "image/png", "type": "image"}],
                "__skip_image_summary__": True,
            }},
            {"role": "assistant", "content": "見たよ"},
            {"role": "user", "content": "<system>残した行</system>", "metadata": kept_meta},
        ]

    def test_shared_predicate(self):
        from llm_clients.utils import ATTACHMENT_LIMIT_EXEMPT_KEY, is_attachment_limit_exempt

        assert is_attachment_limit_exempt({"__visual_context__": True})
        assert is_attachment_limit_exempt({ATTACHMENT_LIMIT_EXEMPT_KEY: True})
        assert not is_attachment_limit_exempt({"media": []})
        assert not is_attachment_limit_exempt(None)

    @pytest.mark.parametrize("exempt,expected", [(True, {0, 2}), (False, {2})])
    def test_openai_and_anthropic(self, tmp_path, exempt, expected):
        from llm_clients.anthropic_request_builder import _collect_attachment_state
        from llm_clients.openai_message_preparer import scan_message_metadata

        msgs = self._messages(tmp_path, exempt=exempt)
        *_rest, allowed = scan_message_metadata(msgs, max_image_embeds_override=1)
        assert {m for m, _ in allowed} == expected
        *_rest, allowed_a = _collect_attachment_state(msgs, max_image_embeds=1)
        assert {m for m, _ in allowed_a} == expected

    @pytest.mark.parametrize("exempt,expected", [(True, 2), (False, 1)])
    def test_gemini(self, tmp_path, monkeypatch, exempt, expected):
        from unittest.mock import MagicMock

        from llm_clients.gemini import GeminiClient

        monkeypatch.setenv("SAIVERSE_GEMINI_ATTACHMENT_LIMIT", "1")
        with patch(
            "llm_clients.gemini.build_gemini_clients",
            return_value=(MagicMock(), MagicMock(), MagicMock()),
        ):
            client = GeminiClient("gemini-4-argon", config={"supports_images": True})
        client.supports_images = True
        _, contents = client._convert_messages(self._messages(tmp_path, exempt=exempt))
        inline = [
            p for c in contents for p in (c.parts or [])
            if getattr(p, "inline_data", None) is not None
        ]
        assert len(inline) == expected

    def test_sluice_estimate_uses_the_same_predicate(self, tmp_path, monkeypatch):
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
        exempt_total = estimate(self._messages(tmp_path, exempt=True), "gemini-4-argon")
        counted_total = estimate(self._messages(tmp_path, exempt=False), "gemini-4-argon")
        # 枠を使わない画像が一枚増えて埋め込まれる分だけ多い
        assert exempt_total > counted_total


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
