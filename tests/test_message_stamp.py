"""書き込み時の機械刻印 (前駆刻印 + トークン三つ組) の回帰。

正典: docs/intent/autonomous_behavior_v3.md §7.1 /
docs/issues/memory_continuity_graph.md 決着節 ① / sea/message_stamp.py。

ここで固定するのは 4 つ:

1. 生成メッセージ (role='assistant') の metadata に、その生成が実際に見ていた
   最後のメッセージ ID (``predecessor_message_id``) が乗ること。
2. 同じ欄にトークン三つ組 (``llm_tokens`` = input / cached_input / output) が
   乗ること。
3. **材料が無いときは刻まないこと** — 提示ゼロの生成には前駆が無く、使用量を
   返さなかったコールには三つ組が無い。0 と書いて埋めない。
4. 割り込みで提示の末尾が物理的な最新行とズレたとき、刻印は **見ていた方** を
   指すこと (「並びでは後ろだが見ていない」の記録)。

刻印の消費 (ズレをどう扱うか) はまだ定義しない (2026-08-19 まはー裁定) ので、
ここでも読み手側の挙動はテストしない。
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any, Dict, Optional
from unittest.mock import Mock, patch

import pytest

from sea.message_stamp import (
    CALL_TOKENS_STATE_KEY,
    PREDECESSOR_META_KEY,
    PRESENTED_IDS_STATE_KEY,
    TOKENS_META_KEY,
    append_presented_message_id,
    build_generation_stamp,
    clear_call_tokens,
    normalize_token_triple,
    record_call_tokens,
    record_presented_message_ids,
    stamp_generation_metadata,
)


def _usage(**overrides) -> SimpleNamespace:
    base = dict(
        model="test-model",
        input_tokens=1200,
        output_tokens=340,
        cached_tokens=900,
        cache_write_tokens=0,
        cache_ttl="",
        timestamp=0.0,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


# ---------------------------------------------------------------------------
# 1. 材料の収集 (state に載せるところ)
# ---------------------------------------------------------------------------


class TestRecordCallTokens:
    def test_normalizes_into_the_triple(self):
        state: Dict[str, Any] = {}
        record_call_tokens(state, _usage())
        assert state[CALL_TOKENS_STATE_KEY] == {
            "input": 1200, "cached_input": 900, "output": 340,
        }

    def test_missing_usage_clears_the_previous_call(self):
        """usage が取れないコールが、前のコールの数字を借りないこと。"""
        state: Dict[str, Any] = {}
        record_call_tokens(state, _usage())
        record_call_tokens(state, None)
        assert CALL_TOKENS_STATE_KEY not in state

    def test_unreadable_field_yields_no_triple(self):
        assert normalize_token_triple(_usage(input_tokens=None)) is None
        assert normalize_token_triple(_usage(output_tokens="120")) is None
        assert normalize_token_triple(_usage(cached_tokens=-1)) is None
        assert normalize_token_triple(None) is None

    def test_zero_cached_is_a_real_zero(self):
        """キャッシュが効かなかった 0 は欠測ではないので刻む。"""
        state: Dict[str, Any] = {}
        record_call_tokens(state, _usage(cached_tokens=0))
        assert state[CALL_TOKENS_STATE_KEY]["cached_input"] == 0


class TestRecordPresentedMessageIds:
    def test_carries_the_id_list(self):
        state: Dict[str, Any] = {}
        record_presented_message_ids(state, {"presented_message_ids": ["m1", "m2"]})
        assert state[PRESENTED_IDS_STATE_KEY] == ["m1", "m2"]

    def test_missing_key_is_history_build_failure(self):
        """キー不在 = 履歴組成の失敗 (束 3 の契約) → 刻印材料なしに倒す。"""
        state: Dict[str, Any] = {PRESENTED_IDS_STATE_KEY: ["stale"]}
        record_presented_message_ids(state, {})
        assert PRESENTED_IDS_STATE_KEY not in state

    def test_empty_list_is_a_zero_presentation_generation(self):
        state: Dict[str, Any] = {PRESENTED_IDS_STATE_KEY: ["stale"]}
        record_presented_message_ids(state, {"presented_message_ids": []})
        assert PRESENTED_IDS_STATE_KEY not in state


class TestAppendPresentedMessageId:
    def test_appends_to_the_tail(self):
        """同じ Beat の中で永続化した行が「見た列」の末尾になる。"""
        state: Dict[str, Any] = {PRESENTED_IDS_STATE_KEY: ["m1", "m2"]}
        append_presented_message_id(state, "round-1")
        assert state[PRESENTED_IDS_STATE_KEY] == ["m1", "m2", "round-1"]

    def test_starts_a_list_when_there_was_none(self):
        state: Dict[str, Any] = {}
        append_presented_message_id(state, "round-1")
        assert state[PRESENTED_IDS_STATE_KEY] == ["round-1"]

    def test_ignores_missing_id(self):
        """永続 ID の無い挿入物 (スペル結果行など) は足さない。"""
        state: Dict[str, Any] = {PRESENTED_IDS_STATE_KEY: ["m1"]}
        append_presented_message_id(state, "")
        append_presented_message_id(state, None)
        assert state[PRESENTED_IDS_STATE_KEY] == ["m1"]


class TestClearCallTokens:
    def test_drops_the_triple_and_keeps_the_predecessor(self):
        """作り手が変わった行では三つ組だけを落とす (前駆の器は共有のまま)。"""
        state: Dict[str, Any] = {PRESENTED_IDS_STATE_KEY: ["m1"]}
        record_call_tokens(state, _usage())
        clear_call_tokens(state)
        assert CALL_TOKENS_STATE_KEY not in state
        assert state[PRESENTED_IDS_STATE_KEY] == ["m1"]


class TestBuildGenerationStamp:
    def test_both_marks(self):
        state = {PRESENTED_IDS_STATE_KEY: ["a", "b"]}
        record_call_tokens(state, _usage())
        stamp = build_generation_stamp(state)
        assert stamp[PREDECESSOR_META_KEY] == "b"
        assert stamp[TOKENS_META_KEY]["cached_input"] == 900

    def test_no_material_no_stamp(self):
        assert build_generation_stamp({}) == {}
        assert build_generation_stamp(None) == {}

    def test_stamp_metadata_keeps_none_as_none(self):
        """刻むものが無い呼び出しを空 dict に変えない (既存挙動を動かさない)。"""
        assert stamp_generation_metadata(None, {}) is None

    def test_stamp_metadata_does_not_mutate_the_input(self):
        original = {"reasoning": "..."}
        state = {PRESENTED_IDS_STATE_KEY: ["a"]}
        merged = stamp_generation_metadata(original, state)
        assert original == {"reasoning": "..."}
        assert merged[PREDECESSOR_META_KEY] == "a"


# ---------------------------------------------------------------------------
# 2. 実 memory.db への刻印 (_store_memory)
# ---------------------------------------------------------------------------


class _DummyEmbedder:
    def __init__(self, model=None, **kwargs):
        self.model_name = model

    def embed(self, texts, **kwargs):
        return [[0.0] * 3 for _ in texts]


@pytest.fixture
def stamp_env(tmp_path, monkeypatch):
    """実 SAIMemoryAdapter + 実 SEARuntime._store_memory。

    ``SessionLocal=None`` にして出来事 (episode) 参照を素通しさせ、刻印だけを
    見る。流儀は tests/test_episodes_wiring.py の layer0_env と同じ。
    """
    monkeypatch.setenv("SAIMEMORY_MEMORY", "1")
    persona_dir = tmp_path / "personas" / "tester"
    persona_dir.mkdir(parents=True, exist_ok=True)

    with patch("saiverse_memory.adapter.Embedder", _DummyEmbedder):
        from saiverse_memory import SAIMemoryAdapter
        from sea.runtime import SEARuntime

        adapter = SAIMemoryAdapter(
            "tester", persona_dir=persona_dir, resource_id="tester",
        )
        manager = SimpleNamespace(building_histories={}, SessionLocal=None)
        runtime = SEARuntime(manager)
        persona = SimpleNamespace(
            persona_id="tester",
            sai_memory=adapter,
            current_building_id=None,
        )
        try:
            yield runtime, persona, adapter
        finally:
            adapter.close()


def _metadata_of(adapter, message_id: str) -> Optional[Dict[str, Any]]:
    with adapter._db_lock:
        row = adapter.conn.execute(
            "SELECT metadata FROM messages WHERE id = ?", (message_id,)
        ).fetchone()
    assert row is not None, f"message {message_id} not found"
    return json.loads(row[0]) if row[0] else None


def _store_user_line(runtime, persona, text: str) -> str:
    """ユーザー発言 1 行を積んで ID を返す (刻印の対象外)。"""
    mid = runtime._store_memory(
        persona, text, role="user", return_message_id=True,
    )
    assert mid
    return str(mid)


class TestStoreMemoryStamping:
    def test_generation_carries_both_marks(self, stamp_env):
        runtime, persona, adapter = stamp_env
        seen = _store_user_line(runtime, persona, "おはよう。")

        state: Dict[str, Any] = {PRESENTED_IDS_STATE_KEY: [seen]}
        record_call_tokens(state, _usage())
        mid = runtime._store_memory(
            persona, "おはよう、まはー。", role="assistant",
            return_message_id=True, beat_state=state,
        )
        meta = _metadata_of(adapter, mid)
        assert meta[PREDECESSOR_META_KEY] == seen
        assert meta[TOKENS_META_KEY] == {
            "input": 1200, "cached_input": 900, "output": 340,
        }

    def test_zero_presentation_generation_has_no_predecessor(self, stamp_env):
        """履歴なしの初手 — 見ていたものが無いので前駆は刻まない。"""
        runtime, persona, adapter = stamp_env
        state: Dict[str, Any] = {}
        record_call_tokens(state, _usage())
        mid = runtime._store_memory(
            persona, "はじめまして。", role="assistant",
            return_message_id=True, beat_state=state,
        )
        meta = _metadata_of(adapter, mid)
        assert PREDECESSOR_META_KEY not in (meta or {})
        assert meta[TOKENS_META_KEY]["input"] == 1200

    def test_call_without_usage_has_no_token_mark(self, stamp_env):
        """使用量を返さないプロバイダの生成 — 欠落を 0 と偽らない。"""
        runtime, persona, adapter = stamp_env
        seen = _store_user_line(runtime, persona, "調子はどう?")

        state: Dict[str, Any] = {}
        record_presented_message_ids(state, {"presented_message_ids": [seen]})
        record_call_tokens(state, None)
        mid = runtime._store_memory(
            persona, "ぼちぼちだよ。", role="assistant",
            return_message_id=True, beat_state=state,
        )
        meta = _metadata_of(adapter, mid)
        assert meta[PREDECESSOR_META_KEY] == seen
        assert TOKENS_META_KEY not in meta

    def test_non_generation_rows_are_not_stamped(self, stamp_env):
        """システム記録・スペル結果・ユーザー発言は「生成」ではない。"""
        runtime, persona, adapter = stamp_env
        state: Dict[str, Any] = {PRESENTED_IDS_STATE_KEY: ["m-seen"]}
        record_call_tokens(state, _usage())
        for role in ("system", "user"):
            mid = runtime._store_memory(
                persona, f"[{role}] なにか", role=role,
                return_message_id=True, beat_state=state,
            )
            meta = _metadata_of(adapter, mid) or {}
            assert PREDECESSOR_META_KEY not in meta
            assert TOKENS_META_KEY not in meta

    def test_unwired_caller_degrades_silently(self, stamp_env):
        """beat_state を渡さない呼び出しは刻印なし = 導入前と同じ姿。"""
        runtime, persona, adapter = stamp_env
        _store_user_line(runtime, persona, "ねえ。")
        mid = runtime._store_memory(
            persona, "うん。", role="assistant", return_message_id=True,
        )
        meta = _metadata_of(adapter, mid) or {}
        assert PREDECESSOR_META_KEY not in meta
        assert TOKENS_META_KEY not in meta

    def test_interrupted_generation_points_at_what_it_saw(self, stamp_env):
        """割り込みで提示の末尾が物理的な最新行とズレたとき。

        ティックが A まで見て走り出した後にユーザーの B が割り込むと、ティック
        の発話は B より後ろに並ぶが B を見ていない。刻印は A を指す。
        """
        runtime, persona, adapter = stamp_env
        seen = _store_user_line(runtime, persona, "A: ティックが見た最後の行")

        # ティックの生成が始まった時点の提示 (末尾 = A)
        state: Dict[str, Any] = {}
        record_presented_message_ids(state, {"presented_message_ids": [seen]})
        record_call_tokens(state, _usage())

        # 生成中に割り込んだユーザー発言 B
        interrupt = _store_user_line(runtime, persona, "B: 生成中に割り込んだ発言")
        assert interrupt != seen

        mid = runtime._store_memory(
            persona, "ティックの独白 (B は見ていない)", role="assistant",
            return_message_id=True, beat_state=state,
        )
        meta = _metadata_of(adapter, mid)
        # 並びでは B のほうが手前だが、刻印は「実際に見ていた」A を指す。
        assert meta[PREDECESSOR_META_KEY] == seen
        assert meta[PREDECESSOR_META_KEY] != interrupt

    def test_explicit_metadata_wins(self, stamp_env):
        """呼び出し元が明示した値は上書きしない。"""
        runtime, persona, adapter = stamp_env
        state: Dict[str, Any] = {PRESENTED_IDS_STATE_KEY: ["auto"]}
        mid = runtime._store_memory(
            persona, "移送した古い行", role="assistant",
            metadata={PREDECESSOR_META_KEY: "explicit"},
            return_message_id=True, beat_state=state,
        )
        meta = _metadata_of(adapter, mid)
        assert meta[PREDECESSOR_META_KEY] == "explicit"


# ---------------------------------------------------------------------------
# 3. 配線 (材料が実経路で state に載るか)
# ---------------------------------------------------------------------------


class TestWiring:
    def test_record_llm_usage_leaves_the_triple_on_state(self):
        """sea/runtime_llm._record_llm_usage が三つ組を state へ残す。"""
        from sea.runtime_llm import _record_llm_usage

        runtime = SimpleNamespace(
            _accumulate_usage=Mock(),
            session_lifecycle=SimpleNamespace(touch_anchor_after_llm_call=Mock()),
        )
        client = SimpleNamespace(consume_usage=Mock(return_value=_usage()))
        state: Dict[str, Any] = {}
        with patch("sea.runtime_llm.get_usage_tracker"), \
                patch("sea.runtime_llm._maybe_record_cache_storage"):
            _record_llm_usage(
                runtime, client, SimpleNamespace(persona_id="p"), "b1",
                "pb", "llm", state,
            )
        assert state[CALL_TOKENS_STATE_KEY] == {
            "input": 1200, "cached_input": 900, "output": 340,
        }

    def test_record_llm_usage_clears_the_triple_when_usage_is_absent(self):
        from sea.runtime_llm import _record_llm_usage

        runtime = SimpleNamespace(
            _accumulate_usage=Mock(),
            session_lifecycle=SimpleNamespace(touch_anchor_after_llm_call=Mock()),
        )
        client = SimpleNamespace(consume_usage=Mock(return_value=None))
        state: Dict[str, Any] = {CALL_TOKENS_STATE_KEY: {"input": 1}}
        _record_llm_usage(
            runtime, client, SimpleNamespace(persona_id="p"), "b1",
            "pb", "llm", state,
        )
        assert CALL_TOKENS_STATE_KEY not in state

    def test_speak_node_passes_the_stamp_to_the_emitter(self):
        """SPEAK ノード (emit_speak → memory.db) にも同じ刻印が乗る。"""
        from sea.runtime import SEARuntime

        manager = SimpleNamespace(building_histories={"b1": []}, occupants={})
        runtime = SEARuntime(manager)
        captured: Dict[str, Any] = {}

        def _fake_speak(persona, building_id, text, pulse_id=None, extra_metadata=None):
            captured["metadata"] = extra_metadata
            return {"message_id": "bm-1"}

        runtime._runtime_engine.emitters["speak"] = _fake_speak
        runtime._effective_building_id = Mock(return_value="b1")

        persona = SimpleNamespace(persona_id="p", persona_name="P")
        playbook = SimpleNamespace(name="pb", display_name="pb")
        state: Dict[str, Any] = {
            "last": "こんにちは。",
            PRESENTED_IDS_STATE_KEY: ["m-seen"],
        }
        record_call_tokens(state, _usage())
        runtime._runtime_engine.lg_speak_node(state, persona, "b1", playbook)

        assert captured["metadata"][PREDECESSOR_META_KEY] == "m-seen"
        assert captured["metadata"][TOKENS_META_KEY]["cached_input"] == 900

    def test_think_node_stamps_the_monologue(self, stamp_env):
        """THINK ノードの独白も生成メッセージ (memory.db に 1 行残る)。"""
        runtime, persona, adapter = stamp_env
        seen = _store_user_line(runtime, persona, "きっかけになった行")

        state: Dict[str, Any] = {
            "last": "……あれ、どうだったっけ。",
            "_pulse_id": "pulse-1",
            PRESENTED_IDS_STATE_KEY: [seen],
        }
        record_call_tokens(state, _usage())
        runtime._lg_think_node(state, persona, SimpleNamespace(name="pb"))

        with adapter._db_lock:
            row = adapter.conn.execute(
                "SELECT metadata FROM messages WHERE content = ?",
                ("……あれ、どうだったっけ。",),
            ).fetchone()
        assert row is not None
        meta = json.loads(row[0])
        assert meta["tags"] == ["internal"]
        assert meta[PREDECESSOR_META_KEY] == seen
        assert meta[TOKENS_META_KEY]["output"] == 340


# ---------------------------------------------------------------------------
# 4. 材料の帰属 — 1 ノードの中で作り手が変わる 3 経路
#
# ひとつの LLM ノードの中で LLM コールが 2 回以上走ったり、他所 (子 Playbook)
# が書いた本文が流れ込んだりする経路がある。刻印の材料は「直近のコール」を
# 指す call-local な器なので、放っておくと後の行に前のコールの数字が乗る。
# ここで固定するのは「他人のコールの数字を自分の事実として刻まない」こと。
# ---------------------------------------------------------------------------


class _StampSpy:
    """``_store_memory`` を呼ばれた瞬間の刻印ごと記録するフェイク。

    刻印材料は同じ state dict を書き換えていくので、呼び出し後に state を
    覗いても「そのとき何が刻まれたか」は分からない。呼ばれた時点で
    ``build_generation_stamp`` を評価して固める。
    """

    def __init__(self):
        self.rows: list = []
        self._seq = 0

    def __call__(self, persona, text, **kwargs):
        self._seq += 1
        message_id = f"row-{self._seq}"
        beat_state = kwargs.get("beat_state")
        self.rows.append({
            "id": message_id,
            "role": kwargs.get("role", "assistant"),
            "text": text,
            "stamp": build_generation_stamp(beat_state) if beat_state else {},
        })
        return message_id if kwargs.get("return_message_id") else True

    def assistant_rows(self) -> list:
        return [r for r in self.rows if r["role"] == "assistant"]


class _StreamingClient:
    """``generate_stream`` を返すだけの mock。usage はコールごとに切り替える。"""

    def __init__(self, chunk_scripts, usages):
        self.chunk_scripts = list(chunk_scripts)
        self.usages = list(usages)
        self.prompts: list = []

    def generate_stream(self, messages, tools=None, temperature=None, **kwargs):
        self.prompts.append(list(messages))
        chunks = self.chunk_scripts.pop(0)
        return iter(chunks)

    def consume_usage(self):
        return self.usages.pop(0) if self.usages else None


def _usage_recording_runtime(store_memory):
    runtime = SimpleNamespace(
        _store_memory=store_memory,
        _default_temperature=lambda persona: None,
        _get_cache_kwargs=lambda persona_id=None: {},
        _accumulate_usage=Mock(),
        _emit_say=Mock(return_value={"message_id": "bm-cont"}),
        session_lifecycle=SimpleNamespace(touch_anchor_after_llm_call=Mock()),
    )
    return runtime


class TestStreamTimeoutContinuation:
    """504 中断 → 部分保存 → 継続生成 の 2 コールを取り違えないこと。

    正典の指摘: 継続ストリームが ``consume_usage`` を通っていなかったため、
    最終の memorize が **初回コールの三つ組** を継続文に刻んでいた。
    """

    def _run(self, cont_chunks=("続きです。",)):
        from sea import runtime_llm

        spy = _StampSpy()
        runtime = _usage_recording_runtime(spy)
        client = _StreamingClient(
            chunk_scripts=[list(cont_chunks)],
            usages=[_usage(
                model="cont-model", input_tokens=1500,
                output_tokens=60, cached_tokens=1400,
            )],
        )
        state: Dict[str, Any] = {
            "_pulse_id": "pulse-1",
            PRESENTED_IDS_STATE_KEY: ["hist-1", "hist-2"],
        }
        # 初回 (中断した) コールの三つ組
        record_call_tokens(state, _usage())

        with patch.object(runtime_llm, "get_usage_tracker"), \
                patch.object(runtime_llm, "_maybe_record_cache_storage"):
            result = runtime_llm._respeak_after_stream_timeout(
                runtime=runtime,
                llm_client=client,
                persona=SimpleNamespace(persona_id="p1"),
                building_id="b1",
                playbook=SimpleNamespace(name="pb"),
                node_def=SimpleNamespace(id="llm"),
                state=state,
                messages=[{"role": "user", "content": "やあ"}],
                partial_text="途中まで書い",
                eff_bid="b1",
                pulse_id="pulse-1",
                stream_error={"code": 504, "message": "Deadline expired"},
                event_callback=None,
            )
        return result, state, spy, runtime, client

    def test_partial_keeps_the_interrupted_calls_triple(self):
        """部分文には中断した初回コールの三つ組が刻まれる。"""
        _result, _state, spy, _runtime, _client = self._run()
        partial = spy.assistant_rows()[0]
        assert partial["text"] == "途中まで書い"
        assert partial["stamp"][TOKENS_META_KEY] == {
            "input": 1200, "cached_input": 900, "output": 340,
        }
        assert partial["stamp"][PREDECESSOR_META_KEY] == "hist-2"

    def test_continuation_call_replaces_the_triple_on_state(self):
        """下流の memorize が継続文に刻むのは継続コールの三つ組。"""
        result, state, _spy, _runtime, _client = self._run()
        assert result == "続きです。"
        assert state[CALL_TOKENS_STATE_KEY] == {
            "input": 1500, "cached_input": 1400, "output": 60,
        }

    def test_continuation_predecessor_points_at_the_stored_partial(self):
        """継続コールが実際に見た最後の永続行は、いま保存した部分文。"""
        _result, state, spy, _runtime, _client = self._run()
        partial_id = spy.assistant_rows()[0]["id"]
        assert build_generation_stamp(state)[PREDECESSOR_META_KEY] == partial_id

    def test_continuation_building_row_carries_its_own_usage(self):
        """UI のドット: 継続文の Building 履歴にも使用量 metadata が乗る。"""
        _result, _state, _spy, runtime, _client = self._run()
        runtime._emit_say.assert_called_once()
        metadata = runtime._emit_say.call_args.kwargs["metadata"]
        assert metadata["llm_usage"]["input_tokens"] == 1500
        assert metadata["llm_usage"]["output_tokens"] == 60

    def test_empty_continuation_still_clears_the_stale_triple(self):
        """継続が空でもトークンは消費されている。初回の数字を居残らせない。"""
        result, state, _spy, _runtime, _client = self._run(cont_chunks=("",))
        assert result == "途中まで書い"
        # 継続コールの usage が state を上書きしている (初回の 1200 ではない)
        assert state[CALL_TOKENS_STATE_KEY]["input"] == 1500

    def test_continuation_without_usage_leaves_no_triple(self):
        """使用量を返さなかった継続コール — 初回の数字を借りない。"""
        from sea import runtime_llm

        spy = _StampSpy()
        runtime = _usage_recording_runtime(spy)
        client = _StreamingClient(chunk_scripts=[["続き"]], usages=[None])
        state: Dict[str, Any] = {"_pulse_id": "pulse-1"}
        record_call_tokens(state, _usage())

        with patch.object(runtime_llm, "get_usage_tracker"), \
                patch.object(runtime_llm, "_maybe_record_cache_storage"):
            runtime_llm._respeak_after_stream_timeout(
                runtime=runtime, llm_client=client,
                persona=SimpleNamespace(persona_id="p1"), building_id="b1",
                playbook=SimpleNamespace(name="pb"),
                node_def=SimpleNamespace(id="llm"), state=state,
                messages=[], partial_text="途中まで",
                eff_bid="b1", pulse_id="pulse-1",
                stream_error={"code": 504}, event_callback=None,
            )
        assert CALL_TOKENS_STATE_KEY not in state


class TestSpellLoopPredecessor:
    """スペルの各ラウンドが、直前ラウンドの行を見て書かれた事実を刻むこと。

    正典の指摘: ラウンドの assistant 行は ``messages`` に積まれて次のコールの
    プロンプトに入るのに ``_presented_message_ids`` が動かず、ラウンド 2 以降の
    前駆刻印が ``_prepare_context`` 時点の履歴末尾を指し続けていた。
    """

    SPELL = "note_add"

    def _run_two_rounds(self):
        import asyncio

        from sea import runtime_llm

        spy = _StampSpy()
        runtime = SimpleNamespace(
            _store_memory=spy,
            manager=SimpleNamespace(),
            session_lifecycle=SimpleNamespace(
                touch_anchor_after_llm_call=lambda *a, **k: None,
            ),
            _default_temperature=lambda persona: None,
            _get_cache_kwargs=lambda persona_id=None: {},
            _dump_llm_io=lambda *a, **k: None,
            _accumulate_usage=lambda *a, **k: None,
        )

        # ラウンド 1 の retry はもう一度 spell を唱える → ラウンド 2 へ
        scripted = [
            f"もう一回。\n/spell name='{self.SPELL}' args={{}}",
            "終わりました。",
        ]

        class _Client:
            def generate(self, messages, tools=None, temperature=None, **kwargs):
                return scripted.pop(0)

            def consume_usage(self):
                return None

        async def _fake_spell(tool_name, tool_args, persona, state, playbook_name,
                              event_callback, messages=None):
            return ("記録しました", None, True)

        state: Dict[str, Any] = {
            "_pulse_id": "pulse-1",
            "_pulse_context": None,
            "_cancellation_token": None,
            PRESENTED_IDS_STATE_KEY: ["hist-1", "hist-2"],
        }
        with patch.object(runtime_llm, "SPELL_TOOL_NAMES", {self.SPELL}), \
                patch.object(runtime_llm, "_run_spell_tool_async", new=_fake_spell):
            asyncio.run(runtime_llm._run_spell_loop(
                text=f"やるぞ。\n/spell name='{self.SPELL}' args={{}}",
                spell_enabled=True,
                llm_client=_Client(),
                runtime=runtime,
                persona=SimpleNamespace(persona_id="p1"),
                building_id="b1",
                state=state,
                messages=[],
                playbook=SimpleNamespace(name="pb"),
                event_callback=None,
                node_def=SimpleNamespace(memorize=None, speak=False),
            ))
        return state, spy

    def test_round_two_points_at_round_one(self):
        _state, spy = self._run_two_rounds()
        rows = spy.assistant_rows()
        assert len(rows) == 2, [r["text"] for r in rows]
        # ラウンド 1 は組成時点の履歴末尾を見ている
        assert rows[0]["stamp"][PREDECESSOR_META_KEY] == "hist-2"
        # ラウンド 2 が実際に見た最後の永続行はラウンド 1 の行
        assert rows[1]["stamp"][PREDECESSOR_META_KEY] == rows[0]["id"]

    def test_final_continuation_points_at_the_last_round(self):
        """ループを抜けた後の最終発言も、最後のラウンドの行を見て書かれた。"""
        state, spy = self._run_two_rounds()
        last_round_id = spy.assistant_rows()[-1]["id"]
        assert build_generation_stamp(state)[PREDECESSOR_META_KEY] == last_round_id


class TestSubPlaybookRelayIsNotStamped:
    """子 Playbook の本文を親が中継した行に、親のコールの数字を刻まないこと。

    正典の指摘: 子の ``_last_call_tokens`` は子 state に閉じていて親へ戻らない
    のに、``lg_subplay_node`` は子の出力本文を親の ``state["last"]`` に置く。
    親の SPEAK / MEMORIZE がそこから刻むと、子の LLM が書いた本文に親の直前
    コールの三つ組が乗る。帰属が確定できないので刻印なしに倒す (方式 b)。
    """

    def _parent_after_subplay(self):
        import asyncio

        from sea.runtime import SEARuntime
        from sea.runtime_nodes import lg_subplay_node

        manager = SimpleNamespace(building_histories={"b1": []}, occupants={})
        runtime = SEARuntime(manager)
        runtime._effective_building_id = lambda persona, building_id: "b1"
        runtime._start_subagent_thread = (
            lambda persona, label, pulse_context=None: (None, None)
        )
        runtime._load_playbook_for = lambda name, p, b: SimpleNamespace(
            name=name, output_schema=[],
        )
        runtime._run_playbook = Mock(return_value=["子が書いた本文。"])

        persona = SimpleNamespace(persona_id="p1", persona_name="P")
        node_def = SimpleNamespace(
            id="call_sub", playbook="child_pb", action=None, line="main",
            execution="inline", isolate_pulse_context=False, args=None,
            propagate_output=False, subagent_chronicle=False,
        )
        state: Dict[str, Any] = {
            "_messages": [],
            "_pulse_context": None,
            "_pulse_id": "pulse-1",
            "_cancellation_token": None,
            PRESENTED_IDS_STATE_KEY: ["hist-1"],
        }
        # 親の直前 LLM コールの三つ組 (この本文の出どころではない)
        record_call_tokens(state, _usage())

        node_fn = lg_subplay_node(
            runtime, node_def, persona, "b1",
            SimpleNamespace(name="parent_pb"), False, [], None,
        )
        asyncio.run(node_fn(state))
        return runtime, persona, state

    def test_relay_drops_the_parents_triple(self):
        _runtime, _persona, state = self._parent_after_subplay()
        assert state["last"] == "子が書いた本文。"
        assert CALL_TOKENS_STATE_KEY not in state

    def test_parent_speak_emits_the_relay_without_a_token_mark(self):
        """親 SPEAK が中継本文を出しても、三つ組は metadata に載らない。"""
        runtime, persona, state = self._parent_after_subplay()
        captured: Dict[str, Any] = {}

        def _fake_speak(p, building_id, text, pulse_id=None, extra_metadata=None):
            captured["text"] = text
            captured["metadata"] = extra_metadata
            return {"message_id": "bm-1"}

        runtime._runtime_engine.emitters["speak"] = _fake_speak
        runtime._runtime_engine.lg_speak_node(
            state, persona, "b1", SimpleNamespace(name="pb", display_name="pb"),
        )

        assert captured["text"] == "子が書いた本文。"
        assert TOKENS_META_KEY not in (captured["metadata"] or {})

    def test_parent_next_llm_call_restores_the_triple(self):
        """親が自前の LLM ノードを回せば刻印は復活する (恒久的な欠落ではない)。"""
        from sea.runtime_llm import _record_llm_usage

        runtime, _persona, state = self._parent_after_subplay()
        runtime._accumulate_usage = Mock()
        runtime.session_lifecycle = SimpleNamespace(
            touch_anchor_after_llm_call=Mock(),
        )
        client = SimpleNamespace(consume_usage=Mock(return_value=_usage()))
        with patch("sea.runtime_llm.get_usage_tracker"), \
                patch("sea.runtime_llm._maybe_record_cache_storage"):
            _record_llm_usage(
                runtime, client, SimpleNamespace(persona_id="p1"), "b1",
                "pb", "llm", state,
            )
        assert state[CALL_TOKENS_STATE_KEY]["input"] == 1200
