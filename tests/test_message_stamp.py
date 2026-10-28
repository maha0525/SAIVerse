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
    clear_presented_message_ids,
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
        """同じ Beat の中で永続化した行が`_presented_message_ids`の末尾になる。"""
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
    def test_drops_only_the_triple(self):
        """三つ組だけを落とす (前駆は clear_presented_message_ids の担当)。

        ⚠ このテストは以前「前駆の器は共有のまま」を**正しい振る舞いとして**
        固定していた。そのため「作り手が変わった区間で前駆だけ残る」欠陥が
        テストを通り抜けていた (2026-08-22 掃討フェーズ 束 5 指摘 1)。関数
        単体の責務としては三つ組だけを落とすのが正しく、**対で呼ぶ責任は
        呼び出し側にある** — その対を固定するのが下の TestClearedInPairs。
        """
        state: Dict[str, Any] = {PRESENTED_IDS_STATE_KEY: ["m1"]}
        record_call_tokens(state, _usage())
        clear_call_tokens(state)
        assert CALL_TOKENS_STATE_KEY not in state
        assert state[PRESENTED_IDS_STATE_KEY] == ["m1"]


class TestClearPresentedMessageIds:
    def test_drops_the_precursor(self):
        state: Dict[str, Any] = {PRESENTED_IDS_STATE_KEY: ["m1", "m2"]}
        clear_presented_message_ids(state)
        assert PRESENTED_IDS_STATE_KEY not in state

    def test_a_missing_key_is_not_an_error(self):
        state: Dict[str, Any] = {}
        clear_presented_message_ids(state)
        assert state == {}

    def test_a_non_dict_state_is_ignored(self):
        clear_presented_message_ids(None)


class TestClearedInPairs:
    """作り手が変わった区間では、三つ組と前駆が**両方**落ちる。

    片方だけ落とすと「この本文を書いた LLM が一度も見ていないメッセージ」を
    前駆として刻む = 欠落ではなく捏造になる。子 Playbook の中継
    (lg_subplay_node) がその区間で、`compile_playbook` が
    `drop_stamp_material` で両方を落とすことで刻印なしへ倒れる。
    """

    def test_nothing_is_stamped_after_both_are_cleared(self):
        state: Dict[str, Any] = {PRESENTED_IDS_STATE_KEY: ["parent-tail"]}
        record_call_tokens(state, _usage())
        # drop_stamp_material と同じく対にして落とす
        clear_call_tokens(state)
        clear_presented_message_ids(state)

        assert build_generation_stamp(state) == {}

    def test_clearing_only_the_triple_still_fabricates_a_precursor(self):
        """⭐ 片方だけでは足りないことの実証 (この欠陥そのものの再現)。"""
        state: Dict[str, Any] = {PRESENTED_IDS_STATE_KEY: ["parent-tail"]}
        record_call_tokens(state, _usage())
        clear_call_tokens(state)

        stamp = build_generation_stamp(state)
        # 三つ組は消えたのに、親が見せた列の末尾が前駆として残ってしまう
        assert stamp.get(PREDECESSOR_META_KEY) == "parent-tail"


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

        def _fake_speak(persona, building_id, text, pulse_id=None, extra_metadata=None, event_callback=None):
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


class TestSpellLoopPredecessor:
    """スペルの各ラウンドが、直前ラウンドの行を見て書かれた事実を刻むこと。

    正典の指摘: ラウンドの assistant 行は ``messages`` に積まれて次のコールの
    プロンプトに入るのに ` `_presented_message_ids`` が動かず、ラウンド 2 以降の
    前駆刻印が ``_prepare_context`` 時点の履歴末尾を指し続けていた。
    """

    SPELL = "note_add"

    def _run_two_rounds(self, messages=None):
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
            _effective_building_id=lambda persona, fallback: fallback,
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

            # スペルループは周ごとに思考も回収する (破壊的読み取り)。
            def consume_reasoning(self):
                return []

            def consume_reasoning_details(self):
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
                messages=messages if messages is not None else [],
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

    def test_round_messages_in_the_list_carry_their_row_ids(self):
        """列に積んだ周の発言が、記録へ書いた行の id を持つこと。

        後で材料を送る `messages`から作り直す回 (子 Playbook の中継の後) に、周の発言が
        記録の行として数えられるための印。追記の列と同じ並びになる。
        """
        from sea.runtime_context import sent_message_ids

        messages: list = []
        _state, spy = self._run_two_rounds(messages)
        round_ids = [r["id"] for r in spy.assistant_rows()]
        assert sent_message_ids(messages) == round_ids


class TestSubPlaybookRelayIsNotStamped:
    """子 Playbook の本文を親が中継した行に、親のコールの数字を刻まないこと。

    正典の指摘: 子の ``_last_call_tokens`` は子 state に閉じていて親へ戻らない
    のに、``lg_subplay_node`` は子の出力本文を親の ``state["last"]`` に置く。
    親の SPEAK / MEMORIZE がそこから刻むと、子の LLM が書いた本文に親の直前
    コールの三つ組が乗る。帰属が確定できないので刻印なしに倒す (方式 b)。

    落とすのは `compile_playbook` が SUBPLAY のノード関数を包んだ処理
    (`sea/message_stamp.drop_stamp_material`) なので、`compile_playbook` から走らせる。
    """

    def _parent_after_subplay(self):
        from sea.runtime import SEARuntime

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
        state: Dict[str, Any] = {
            "_messages": [],
            "_pulse_context": None,
            "_pulse_id": "pulse-1",
            "_cancellation_token": None,
            PRESENTED_IDS_STATE_KEY: ["hist-1"],
        }
        # 親の直前 LLM コールの三つ組 (この本文の出どころではない)
        record_call_tokens(state, _usage())

        _run_compiled(runtime, persona, [
            {"id": "call_sub", "type": "subplay", "playbook": "child_pb", "next": None},
        ], state)
        return runtime, persona, state

    def test_relay_drops_the_parents_triple(self):
        _runtime, _persona, state = self._parent_after_subplay()
        assert state["last"] == "子が書いた本文。"
        assert CALL_TOKENS_STATE_KEY not in state

    def test_relay_drops_the_parents_precursor(self):
        """⭐ 前駆も落ちる — 子の LLM が見ていない列を刻ませない。

        三つ組と同じ理由で落とす。親 state に残っているのは**親が見せた列**の
        末尾 (hist-1) で、この本文を書いた子の LLM は一度も見ていない。残すと
        欠落ではなく捏造になる (2026-08-22 掃討フェーズ 束 5 指摘 1)。
        """
        _runtime, _persona, state = self._parent_after_subplay()
        assert PRESENTED_IDS_STATE_KEY not in state

    def test_relay_leaves_nothing_to_stamp(self):
        """中継の直後に刻もうとしても、材料が無いので印は付かない。"""
        _runtime, _persona, state = self._parent_after_subplay()
        assert build_generation_stamp(state) == {}

    def test_parent_speak_emits_the_relay_without_a_token_mark(self):
        """親 SPEAK が中継本文を出しても、三つ組は metadata に載らない。"""
        runtime, persona, state = self._parent_after_subplay()
        captured: Dict[str, Any] = {}

        def _fake_speak(p, building_id, text, pulse_id=None, extra_metadata=None, event_callback=None):
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


# ---------------------------------------------------------------------------
# 5. 子 Playbook の後に、親が自前の LLM ノードで生成した返事の前駆
#
# 子 Playbook の中継 (lg_subplay_node) の後で、compile_playbook が三つ組と前駆の
# 材料を対で落とす (上の TestSubPlaybookRelayIsNotStamped)。三つ組は親の次の LLM コールで
# _record_llm_usage が載せ直すが、前駆の材料には載せ直す口が無く、子の後に
# 親が自前の LLM ノードで生成して保存した返事に前駆が付かなかった。
#
# ここでは lg_subplay_node と lg_llm_node を本物のまま連結し、偽 LLM が
# **実際に受け取った列**の中の最後の記録の行 id と、実 SAIMemory に保存された
# 返事の前駆を照合する (期待値を手で書かない)。
# ---------------------------------------------------------------------------


class _RecordingClient:
    """受け取った列を写しで残す偽 LLM (非ストリーミング・ツールなし)。"""

    def __init__(self):
        self.replies: list = []
        self.sent: list = []

    def generate(self, messages, tools=None, temperature=None, response_schema=None, **kwargs):
        self.sent.append([dict(m) for m in messages])
        return self.replies.pop(0)

    def consume_usage(self):
        return None

    def consume_reasoning(self):
        return []

    def consume_reasoning_details(self):
        return None

    def consume_thought_signature(self):
        return None


def _last_recorded_id_in(adapter, sent: list) -> Optional[str]:
    """送った列の中で、記録 (messages 表) の行にあたる最後のものの行 id。

    行にあたるかどうかは列の dict が持つ ``id`` に頼らず、記録の側から判定する
    (役割と本文が一致する行)。``id`` を持たせる修正そのものを検査に使うと、
    「id を持たない記録の行」を見落とす欠陥が検査ごと素通りするため。
    """
    with adapter._db_lock:
        rows = adapter.conn.execute("SELECT id, role, content FROM messages").fetchall()
    by_id = {str(r[0]) for r in rows}
    by_body = {(r[1], r[2]): str(r[0]) for r in rows}
    last = None
    for msg in sent:
        if not isinstance(msg, dict):
            continue
        mid = msg.get("id")
        if mid and str(mid) in by_id:
            last = str(mid)
            continue
        hit = by_body.get((msg.get("role"), msg.get("content")))
        if hit:
            last = hit
    return last


def _row_by_content(adapter, content: str) -> tuple:
    with adapter._db_lock:
        row = adapter.conn.execute(
            "SELECT id, metadata FROM messages WHERE content = ? AND role = ?",
            (content, "assistant"),
        ).fetchone()
    assert row is not None, f"assistant row {content!r} not found"
    return str(row[0]), (json.loads(row[1]) if row[1] else {})


@pytest.fixture
def node_env(stamp_env, monkeypatch, tmp_path):
    """実 SEARuntime + 実 SAIMemory の上で、本物の lg_subplay_node / lg_llm_node を回す器。

    外界へ出る口 (LLM・ログファイル・モデル解決) だけを差し替える。
    """
    from sea import runtime_llm, runtime_nodes

    monkeypatch.setenv("SAIVERSE_HOME", str(tmp_path / "home"))
    runtime, persona, adapter = stamp_env
    monkeypatch.setattr(runtime_llm, "log_sea_trace", lambda *a, **k: None)
    monkeypatch.setattr(runtime_nodes, "log_sea_trace", lambda *a, **k: None)
    monkeypatch.setattr(
        runtime_llm, "resolve_execution_context",
        lambda persona, pulse_context, state=None: SimpleNamespace(model_key="model-a"),
    )
    client = _RecordingClient()
    runtime.select_llm_client = lambda *a, **k: (client, "model-a")
    runtime._dump_llm_io = lambda *a, **k: None
    runtime._default_temperature = lambda persona: None
    runtime._get_cache_kwargs = lambda persona_id=None: {}
    runtime._effective_building_id = lambda persona, building_id: "b1"
    runtime._load_playbook_for = lambda name, p, b: SimpleNamespace(
        name=name, output_schema=[],
    )
    return runtime, persona, adapter, client


def _seed_history(runtime, persona) -> tuple:
    """prepare_context が組んだのと同じ形の列 (head + 履歴 + 知覚) と提示 ID 列。"""
    from sea.eviction_plan import CONSUMED_PERCEPTION_KEY

    h1 = _store_user_line(runtime, persona, "h1: おはよう。")
    h2 = runtime._store_memory(
        persona, "h2: おはよう、まはー。", role="assistant", return_message_id=True,
    )
    assert h2
    h2 = str(h2)
    messages = [
        {"role": "system", "content": "head"},
        {"id": h1, "role": "user", "content": "h1: おはよう。"},
        {"id": h2, "role": "assistant", "content": "h2: おはよう、まはー。"},
        {
            "role": "user", "content": "<system>部屋の様子</system>",
            "metadata": {CONSUMED_PERCEPTION_KEY: True},
        },
    ]
    return h1, h2, messages


def _llm_node_def(node_id: str):
    return SimpleNamespace(
        id=node_id, action=None, memorize={"tags": ["conversation"]},
        speak=False, important=False, available_tools=None,
        response_schema=None, output_key=None, output_keys=None,
        label=None, metadata_key=None,
    )


def _run_llm_node(runtime, persona, state, node_id: str) -> None:
    import asyncio

    from sea.runtime_llm import lg_llm_node

    node_fn = lg_llm_node(
        runtime, _llm_node_def(node_id), persona, "b1",
        SimpleNamespace(name="parent_pb", display_name="parent_pb"), None,
    )
    asyncio.run(node_fn(state))


def _run_subplay(runtime, persona, state, *, child_row_text: str) -> None:
    """本物の lg_subplay_node を `compile_playbook` から走らせる (材料を落とすのは
    `compile_playbook` が包んだ処理)。子 (メインライン) は Mock だが、本物の子と
    同じく親 state へ自分の `_presented_message_ids` を書き、自分の返事を記録に残す。"""

    def _child(sub_pb, p, bid, sub_input, auto_mode, record_history, parent_state,
               event_callback, **kwargs):
        child_row = runtime._store_memory(
            p, child_row_text, role="assistant", return_message_id=True,
        )
        # 本物の run_playbook (line='main') は親 state へ子の提示列を書く。
        child_presented = [*(parent_state.get(PRESENTED_IDS_STATE_KEY) or []), str(child_row)]
        record_presented_message_ids(
            parent_state, {"presented_message_ids": child_presented},
        )
        return [child_row_text]

    runtime._run_playbook = Mock(side_effect=_child)
    _run_compiled(runtime, persona, [
        {"id": "call_sub", "type": "subplay", "playbook": "child_pb", "next": None},
    ], state)


def _base_state(messages, presented) -> Dict[str, Any]:
    return {
        "_messages": messages,
        "_pulse_context": None,
        "_pulse_id": "pulse-1",
        "_cancellation_token": None,
        "_spell_enabled": False,
        PRESENTED_IDS_STATE_KEY: list(presented),
    }


class TestPredecessorAfterSubPlaybook:
    """子 Playbook の後に、親の LLM ノードが保存した返事にも前駆が付くこと。"""

    def test_child_first_then_parent_reply(self, node_env):
        """子が先: 履歴 h1,h2 → SUBPLAY → 親の返事 B。"""
        runtime, persona, adapter, client = node_env
        h1, h2, messages = _seed_history(runtime, persona)
        state = _base_state(messages, [h1, h2])

        _run_subplay(runtime, persona, state, child_row_text="子が書いた本文。")
        client.replies.append("親の返事 B。")
        _run_llm_node(runtime, persona, state, "reply_b")

        _b_id, meta = _row_by_content(adapter, "親の返事 B。")
        expected = _last_recorded_id_in(adapter, client.sent[-1])
        # 子の行は記録の最新だが、親の送る `messages`には入っていない
        assert expected == h2
        assert meta.get(PREDECESSOR_META_KEY) == expected

    def test_parent_first_then_child_then_parent(self, node_env):
        """親が先: 親の返事 A → SUBPLAY → 親の返事 B。B は A を見て書かれた。"""
        runtime, persona, adapter, client = node_env
        h1, h2, messages = _seed_history(runtime, persona)
        state = _base_state(messages, [h1, h2])

        client.replies.append("親の返事 A。")
        _run_llm_node(runtime, persona, state, "reply_a")
        a_id, a_meta = _row_by_content(adapter, "親の返事 A。")
        assert a_meta.get(PREDECESSOR_META_KEY) == h2

        _run_subplay(runtime, persona, state, child_row_text="子が書いた本文。")
        client.replies.append("親の返事 B。")
        _run_llm_node(runtime, persona, state, "reply_b")

        _b_id, b_meta = _row_by_content(adapter, "親の返事 B。")
        expected = _last_recorded_id_in(adapter, client.sent[-1])
        assert expected == a_id
        assert b_meta.get(PREDECESSOR_META_KEY) == expected

    def test_relay_row_still_has_no_predecessor(self, node_env):
        """作り直しは親の LLM ノードの送信の直前だけ — 中継の行は刻印なしのまま。"""
        runtime, persona, adapter, client = node_env
        h1, h2, messages = _seed_history(runtime, persona)
        state = _base_state(messages, [h1, h2])

        _run_subplay(runtime, persona, state, child_row_text="子が書いた本文。")
        # 中継の直後に親の MEMORIZE 相当が state["last"] を記録へ書く
        mid = runtime._store_memory(
            persona, state["last"], role="assistant",
            return_message_id=True, beat_state=state,
        )
        assert PREDECESSOR_META_KEY not in (_metadata_of(adapter, mid) or {})


class TestPredecessorAfterExec:
    """EXEC で走った子 Playbook の `_presented_message_ids`を、親の次の返事の前駆に使わないこと。

    子は親の state をそのまま受け取って走るので、子の _prepare_context が子の
    提示列で親の材料を上書きする (sea/runtime_runner.py)。EXEC は子の本文を
    親の state["last"] に中継する点も SUBPLAY と同じ。Codex 2026-10-14 high:
    以前は EXEC が材料を落とさず、親の次の返事が親の見ていない子の行を前駆に
    刻みえた。
    """

    def _run_exec(self, runtime, persona, state, *, child_row_text: str) -> str:
        """本物の lg_exec_node を `compile_playbook` から走らせる (材料を落とすのは
        `compile_playbook` が包んだ処理)。"""
        child_ids: Dict[str, str] = {}

        def _child(sub_pb, p, bid, sub_input, auto_mode, record_history,
                   parent_state, event_callback, **kwargs):
            # 親の組成の後に記録へ届いた行 X を、子の再組成が拾った形
            x_id = runtime._store_memory(
                p, child_row_text, role="assistant", return_message_id=True,
            )
            child_ids["x"] = str(x_id)
            record_presented_message_ids(
                parent_state, {"presented_message_ids": [str(x_id)]},
            )
            return [child_row_text]

        runtime._run_playbook = Mock(side_effect=_child)
        runtime.decide_playbook_permission = Mock(return_value=(True, None))
        runtime._append_tool_result_message = Mock()
        state["selected_playbook"] = "child_pb"
        _run_compiled(runtime, persona, [{"id": "exec", "type": "exec", "next": None}], state)
        return child_ids["x"]

    def test_exec_drops_the_childs_material(self, node_env, monkeypatch):
        from sea import runtime_engine

        monkeypatch.setattr(runtime_engine, "log_sea_trace", lambda *a, **k: None)
        runtime, persona, _adapter, _client = node_env
        h1, h2, messages = _seed_history(runtime, persona)
        state = _base_state(messages, [h1, h2])
        record_call_tokens(state, _usage())

        self._run_exec(runtime, persona, state, child_row_text="子が EXEC で書いた本文。")

        assert state["last"] == "子が EXEC で書いた本文。"
        assert build_generation_stamp(state) == {}

    def test_parent_reply_after_exec_points_at_what_the_parent_sent(
        self, node_env, monkeypatch,
    ):
        from sea import runtime_engine

        monkeypatch.setattr(runtime_engine, "log_sea_trace", lambda *a, **k: None)
        runtime, persona, adapter, client = node_env
        h1, h2, messages = _seed_history(runtime, persona)
        state = _base_state(messages, [h1, h2])

        x_id = self._run_exec(runtime, persona, state, child_row_text="子が EXEC で書いた本文。")
        client.replies.append("EXEC の後の親の返事。")
        _run_llm_node(runtime, persona, state, "reply_after_exec")

        _id, meta = _row_by_content(adapter, "EXEC の後の親の返事。")
        expected = _last_recorded_id_in(adapter, client.sent[-1])
        assert expected == h2
        assert meta.get(PREDECESSOR_META_KEY) == expected
        assert meta.get(PREDECESSOR_META_KEY) != x_id


class TestPredecessorAcrossConsecutiveParentNodes:
    """子 Playbook が無くても、親の LLM ノード A (memorize あり) の後に親の LLM
    ノード B が続くとき、B の前駆は B が実際に見た A を指すこと。

    材料が残っている回は作り直さず、B が送る直前に、送る `messages`で材料の末尾より
    後ろにある記録の行 (A) を足すことで成立させる。足さないと B の前駆は
    prepare_context の提示列の末尾 (h2) のままになる (2026-10-14 に同族の既存
    欠陥として発見し、同じ変更で修正)。

    足すのは**送る時点だけ**で、保存の時点ではない。A と B の間に挟まる
    MEMORIZE / SPEAK / THINK (A の本文を役割 assistant で書き直す) の前駆は、
    A が書く前に見ていた行のまま — A 自身の行を指さない。
    """

    def test_rewrite_of_a_between_nodes_points_at_what_a_saw(self, node_env):
        """A の直後に A の本文を書き直す MEMORIZE 相当は、A 自身の行を前駆にしない。

        例: schedule_management の prepare_add (LLM + memorize) → TOOL →
        record_add_params (MEMORIZE、役割 assistant、本文は A の出力)。
        """
        runtime, persona, adapter, client = node_env
        h1, h2, messages = _seed_history(runtime, persona)
        state = _base_state(messages, [h1, h2])

        client.replies.append("親の返事 A。")
        _run_llm_node(runtime, persona, state, "reply_a")
        a_id, a_meta = _row_by_content(adapter, "親の返事 A。")
        seen_by_a = _last_recorded_id_in(adapter, client.sent[-1])
        assert a_meta.get(PREDECESSOR_META_KEY) == seen_by_a

        mid = runtime._store_memory(
            persona, "書き直された A の本文。", role="assistant",
            return_message_id=True, beat_state=state,
        )
        rewritten = (_metadata_of(adapter, mid) or {}).get(PREDECESSOR_META_KEY)
        assert rewritten == seen_by_a
        assert rewritten != a_id

    def test_second_parent_node_points_at_the_first_reply(self, node_env):
        runtime, persona, adapter, client = node_env
        h1, h2, messages = _seed_history(runtime, persona)
        state = _base_state(messages, [h1, h2])

        client.replies.append("親の返事 A。")
        _run_llm_node(runtime, persona, state, "reply_a")
        a_id, _a_meta = _row_by_content(adapter, "親の返事 A。")

        client.replies.append("親の返事 B。")
        _run_llm_node(runtime, persona, state, "reply_b")

        _b_id, b_meta = _row_by_content(adapter, "親の返事 B。")
        expected = _last_recorded_id_in(adapter, client.sent[-1])
        assert expected == a_id
        assert b_meta.get(PREDECESSOR_META_KEY) == expected


class TestSyncPresentedMessageIdsWithSent:
    """材料を送る `messages`に合わせる (sea/message_stamp.sync_presented_message_ids_with_sent)。"""

    SENT = [
        {"role": "system", "content": "head"},
        {"id": "h1", "role": "user", "content": "h1"},
        {"role": "user", "content": "<system>部屋</system>",
         "metadata": {"__consumed_perception__": True}},
        {"id": "a1", "role": "assistant", "content": "a1"},
        {"role": "user", "content": "<system>action</system>"},
    ]

    def test_material_whose_tail_is_not_sent_is_rebuilt(self):
        """材料の末尾が送る `messages`に無いなら、材料はこの生成が見るものではない。

        残すと見ていない行 (round-1) を前駆に刻むので、送る `messages`から作り直す。
        """
        from sea.message_stamp import sync_presented_message_ids_with_sent

        state: Dict[str, Any] = {PRESENTED_IDS_STATE_KEY: ["hist-1", "round-1"]}
        sync_presented_message_ids_with_sent(state, self.SENT)
        assert state[PRESENTED_IDS_STATE_KEY] == ["h1", "a1"]

    def test_material_is_not_rewritten_when_the_tail_is_sent(self):
        """材料の末尾が送る `messages`にあれば、末尾までの中身は変えない (足すだけ)。"""
        from sea.message_stamp import sync_presented_message_ids_with_sent

        state: Dict[str, Any] = {PRESENTED_IDS_STATE_KEY: ["not-sent-0", "h1"]}
        sync_presented_message_ids_with_sent(state, self.SENT)
        assert state[PRESENTED_IDS_STATE_KEY] == ["not-sent-0", "h1", "a1"]

    def test_rows_after_the_tail_are_appended_without_rewriting(self):
        """材料の末尾が送る `messages`にあれば、その後ろの記録の行だけを足す。"""
        from sea.message_stamp import sync_presented_message_ids_with_sent

        sent = [
            {"role": "system", "content": "head"},
            {"id": "h1", "role": "user", "content": "h1"},
            {"id": "h2", "role": "assistant", "content": "h2"},
            {"role": "user", "content": "<system>action</system>"},
            {"id": "a1", "role": "assistant", "content": "a1"},
            {"role": "user", "content": "<system>tool result</system>"},
            {"id": "a2", "role": "assistant", "content": "a2"},
        ]
        # 材料の中身 (先頭の hist-0 は送る `messages`に無い) は変えず、h2 より後ろだけ足す
        state: Dict[str, Any] = {PRESENTED_IDS_STATE_KEY: ["hist-0", "h2"]}
        sync_presented_message_ids_with_sent(state, sent)
        assert state[PRESENTED_IDS_STATE_KEY] == ["hist-0", "h2", "a1", "a2"]
        assert build_generation_stamp(state)[PREDECESSOR_META_KEY] == "a2"

    def test_tail_already_last_is_left_alone(self):
        from sea.message_stamp import sync_presented_message_ids_with_sent

        state: Dict[str, Any] = {PRESENTED_IDS_STATE_KEY: ["h1", "a1"]}
        sync_presented_message_ids_with_sent(state, self.SENT)
        assert state[PRESENTED_IDS_STATE_KEY] == ["h1", "a1"]

    @pytest.mark.parametrize("initial", ["absent", [], None, "not-a-list"])
    def test_missing_material_is_rebuilt_from_the_sent_list(self, initial):
        from sea.message_stamp import sync_presented_message_ids_with_sent

        state: Dict[str, Any] = {}
        if initial != "absent":
            state[PRESENTED_IDS_STATE_KEY] = initial
        sync_presented_message_ids_with_sent(state, self.SENT)
        assert state[PRESENTED_IDS_STATE_KEY] == ["h1", "a1"]
        assert build_generation_stamp(state)[PREDECESSOR_META_KEY] == "a1"

    def test_a_list_without_record_rows_leaves_no_material(self):
        """提示ゼロの生成は刻印なしのまま (0 件の列を置かない)。"""
        from sea.message_stamp import sync_presented_message_ids_with_sent

        state: Dict[str, Any] = {}
        sync_presented_message_ids_with_sent(
            state, [{"role": "system", "content": "head"},
                    {"role": "user", "content": "<system>x</system>"}],
        )
        assert PRESENTED_IDS_STATE_KEY not in state
        sync_presented_message_ids_with_sent(state, [])
        assert PRESENTED_IDS_STATE_KEY not in state

    @pytest.mark.parametrize("messages", [
        [], None,
        [{"role": "system", "content": "head"},
         {"role": "user", "content": "<system>x</system>"}],
    ])
    def test_existing_material_is_dropped_when_messages_have_no_record_rows(self, messages):
        """Codex 四巡目 3: `messages` に記録の行 id が一つも無ければ、既存の
        `_presented_message_ids` を消す (残すと送っていない行を前駆に刻む)。"""
        from sea.message_stamp import sync_presented_message_ids_with_sent

        state: Dict[str, Any] = {PRESENTED_IDS_STATE_KEY: ["h1", "a1"]}
        sync_presented_message_ids_with_sent(state, messages)
        assert PRESENTED_IDS_STATE_KEY not in state
        assert PREDECESSOR_META_KEY not in build_generation_stamp(state)

    def test_head_and_perception_are_not_counted_even_with_an_id(self):
        """head と知覚のまとめは、仮に id を持っても記録の行として数えない。

        head は role ではなく head の印で見分ける — 会話の途中のシステム通知
        (role=system、印なし) は記録の行として数える
        (docs/intent/system_notice_delivery.md 不変条件 5)。
        """
        from sai_memory.memory.storage import HEAD_MESSAGE_KEY
        from sea.eviction_plan import CONSUMED_PERCEPTION_KEY
        from sea.runtime_context import sent_message_ids

        sent = [
            {"id": "sys", "role": "system", "content": "head",
             "metadata": {HEAD_MESSAGE_KEY: True}},
            {"id": "perc", "role": "user", "content": "<system>部屋</system>",
             "metadata": {CONSUMED_PERCEPTION_KEY: True}},
            {"id": "n1", "role": "system", "content": "予定の時刻です"},
            {"id": "h1", "role": "user", "content": "h1"},
        ]
        assert sent_message_ids(sent) == ["n1", "h1"]


class TestFinalizeBeatCarriesTheStoredRowId:
    """_finalize_beat が、記憶へ書いた返事の行 id を送る `messages`の返事に載せること。"""

    @pytest.fixture(autouse=True)
    def _no_trace(self, monkeypatch):
        from sea import runtime_llm

        monkeypatch.setattr(runtime_llm, "log_sea_trace", lambda *a, **k: None)

    def _finalize(self, state, *, store_return="row-9", **node_kw):
        from unittest.mock import MagicMock

        from sea.runtime_llm import BeatExecution, _finalize_beat

        node = dict(id="n1", memorize=None, important=False, label=None, output_key=None)
        node.update(node_kw)
        runtime = MagicMock()
        runtime._store_memory.return_value = store_return
        beat = BeatExecution.from_node_locals(
            persona=SimpleNamespace(persona_id="p1", persona_name="P"),
            node_def=SimpleNamespace(**node), node_id="n1",
            playbook=SimpleNamespace(name="pb", display_name="pb"),
            state=state, event_callback=None,
            messages=[{"id": "h1", "role": "user", "content": "hi"}],
            prompt=None, continuation="response", schema_consumed=False,
        )
        _finalize_beat(runtime, beat)
        return runtime

    def test_memorize_puts_the_row_id_on_the_reply(self):
        state: Dict[str, Any] = {}
        self._finalize(state, memorize=True)
        assert state["_messages"][-1] == {
            "role": "assistant", "content": "response", "id": "row-9",
        }

    def test_important_dual_write_puts_the_row_id_on_the_reply(self):
        state: Dict[str, Any] = {}
        runtime = self._finalize(state, important=True)
        assert runtime._store_memory.call_args.kwargs["return_message_id"] is True
        assert state["_messages"][-1]["id"] == "row-9"

    def test_settle_row_id_is_used_when_memorize_is_skipped(self):
        """止まった生成の保存 (settle) が書いた回は、その行 id を載せる。"""
        from sea.runtime_llm import BEAT_MEMORY_ID_KEY

        state: Dict[str, Any] = {"_beat_memorized": True, BEAT_MEMORY_ID_KEY: "settle-1"}
        runtime = self._finalize(state, memorize=True)
        runtime._store_memory.assert_not_called()
        assert state["_messages"][-1]["id"] == "settle-1"
        assert BEAT_MEMORY_ID_KEY not in state  # この Beat で消費する

    def test_a_reply_that_was_not_stored_carries_no_id(self):
        state: Dict[str, Any] = {}
        self._finalize(state)  # memorize も important も無い
        assert "id" not in state["_messages"][-1]
        state2: Dict[str, Any] = {}
        self._finalize(state2, memorize=True, store_return="")  # 保存に失敗
        assert "id" not in state2["_messages"][-1]

    def test_tool_call_only_reply_carries_the_row_id(self):
        """本文が空の tool_calls だけの返事にも載せる (Codex 三巡目 3)。

        この dict は SAIVerse の `messages` に入っているので、
        `sync_presented_message_ids_with_sent` は送る先のクライアントに関わらず
        数える (Codex 四巡目 2)。Anthropic の `_prepare_anthropic_messages` は
        この dict を送る前に落とすが、predecessor_message_id は SAIVerse の
        `messages` の段階で決まる。
        """
        from llm_clients.anthropic_request_builder import _prepare_anthropic_messages
        from sea.message_stamp import sync_presented_message_ids_with_sent

        state: Dict[str, Any] = {
            "tool_called": True, "_last_tool_call_id": "call-1",
            "_last_tool_name": "calc", "_last_tool_args_json": "{}",
            "has_speak_content": False,
        }
        self._finalize(state, memorize=True)
        msg = state["_messages"][-1]
        assert msg["content"] == ""
        assert msg["id"] == "row-9"
        # この返事が `_presented_message_ids` の末尾になる
        sent_state: Dict[str, Any] = {}
        sync_presented_message_ids_with_sent(sent_state, state["_messages"])
        assert sent_state[PRESENTED_IDS_STATE_KEY] == ["h1", "row-9"]
        # Anthropic へ渡すメッセージにはこの返事が無いが、数え方は変わらない
        prepared = _prepare_anthropic_messages(state["_messages"])
        assert all(m.get("role") != "assistant" for m in prepared)

    def test_tool_call_reply_also_carries_the_row_id(self):
        state: Dict[str, Any] = {
            "tool_called": True, "_last_tool_call_id": "call-1",
            "_last_tool_name": "calc", "_last_tool_args_json": "{}",
            "has_speak_content": True,
        }
        self._finalize(state, memorize=True)
        msg = state["_messages"][-1]
        assert msg["id"] == "row-9"
        assert msg["tool_calls"][0]["id"] == "call-1"

    # ---- `_unstored_reply` (後ろのノードが保存したときに返事の dict へ `id` を載せるための state のキー) ----

    def test_reply_that_was_not_stored_is_noted(self):
        from sea.message_stamp import UNSTORED_REPLY_STATE_KEY

        state: Dict[str, Any] = {}
        self._finalize(state)  # memorize も important も無い
        assert state[UNSTORED_REPLY_STATE_KEY] is state["_messages"][-1]

    def test_stored_reply_drops_a_stale_note(self):
        from sea.message_stamp import UNSTORED_REPLY_STATE_KEY

        state: Dict[str, Any] = {UNSTORED_REPLY_STATE_KEY: {"role": "assistant", "content": "old"}}
        self._finalize(state, memorize=True)
        assert state["_messages"][-1]["id"] == "row-9"
        assert UNSTORED_REPLY_STATE_KEY not in state

    def test_reply_memorized_without_an_id_is_not_noted(self):
        """この LLM ノードが保存した (`_beat_memorized`) が id を受け取れなかった回は
        `_unstored_reply` に書かない — 後ろのノードが同じ本文をもう一度書いた行を、この返事の行として
        載せない。"""
        from sea.message_stamp import UNSTORED_REPLY_STATE_KEY

        state: Dict[str, Any] = {}
        self._finalize(state, memorize=True, store_return=True)
        assert "id" not in state["_messages"][-1]
        assert UNSTORED_REPLY_STATE_KEY not in state

    def test_tool_call_only_reply_is_not_noted(self):
        from sea.message_stamp import UNSTORED_REPLY_STATE_KEY

        state: Dict[str, Any] = {
            "tool_called": True, "_last_tool_call_id": "call-1",
            "_last_tool_name": "calc", "_last_tool_args_json": "{}",
            "has_speak_content": False,
            UNSTORED_REPLY_STATE_KEY: {"role": "assistant", "content": "old"},
        }
        self._finalize(state)
        assert UNSTORED_REPLY_STATE_KEY not in state


class TestEmittersReturnTheMemoryRowId:
    """SPEAK / THINK ノードが、記憶に書いた行の id を受け取れること。"""

    def test_emit_speak_returns_the_memory_row_id_on_a_copy(self):
        from sea.runtime_emitters import SPEAK_MEMORY_MESSAGE_ID_KEY, RuntimeEmitters

        building_row = {"message_id": "b1:7", "content": "こんにちは。"}
        history = Mock()
        history.add_to_persona_only.return_value = ("synced", "mem-3")
        history.add_to_building_only.return_value = building_row
        manager = SimpleNamespace(
            occupants={}, user_presence_status="offline",
            gateway_handle_ai_replies=Mock(),
        )
        emitters = RuntimeEmitters(runtime=SimpleNamespace(manager=manager))
        persona = SimpleNamespace(persona_id="p1", history_manager=history)
        with patch("saiverse.addon_hooks.dispatch_hook"):
            out = emitters.emit_speak(persona, "b1", "こんにちは。")
        assert out[SPEAK_MEMORY_MESSAGE_ID_KEY] == "mem-3"
        assert out["message_id"] == "b1:7"
        assert SPEAK_MEMORY_MESSAGE_ID_KEY not in building_row

    @pytest.mark.parametrize("persona_saved", [("failed", None), ("skipped", None), None])
    def test_emit_speak_without_a_memory_row_adds_nothing(self, persona_saved):
        from sea.runtime_emitters import SPEAK_MEMORY_MESSAGE_ID_KEY, RuntimeEmitters

        history = Mock()
        history.add_to_persona_only.return_value = persona_saved
        history.add_to_building_only.return_value = {"message_id": "b1:7", "content": "x"}
        manager = SimpleNamespace(
            occupants={}, user_presence_status="offline",
            gateway_handle_ai_replies=Mock(),
        )
        emitters = RuntimeEmitters(runtime=SimpleNamespace(manager=manager))
        persona = SimpleNamespace(persona_id="p1", history_manager=history)
        with patch("saiverse.addon_hooks.dispatch_hook"):
            out = emitters.emit_speak(persona, "b1", "x")
        assert SPEAK_MEMORY_MESSAGE_ID_KEY not in out

    def test_emit_think_returns_the_row_id(self, stamp_env):
        runtime, persona, adapter = stamp_env
        mid = runtime._emit_think(persona, "pulse-1", "ひとりごと。")
        assert isinstance(mid, str) and mid
        assert _metadata_of(adapter, mid)["tags"] == ["internal"]
        assert runtime._emit_think(persona, "pulse-1", "x", record_history=False) is None


class TestPredecessorAfterLaterNodeStoresTheReply:
    """LLM ノード X が `memorize` なしで書いた返事を、後ろの MEMORIZE / SPEAK /
    THINK ノードが `state["last"]` から記憶に保存したとき、次の LLM ノード Z の
    返事の `predecessor_message_id` が、その保存した行を指すこと。

    X の返事の dict は `state["_messages"]` に入り、Z が送る `messages` にも
    入る。保存したノードがその dict に `id` を載せないと、
    `sync_presented_message_ids_with_sent` が数えられず、Z の
    `predecessor_message_id` は保存した行より前の行になる。

    載せるのは `_unstored_reply` が残っていて、`state["last"]` が X の返事の
    dict の `content` と値で等しいときだけ。間に `state["last"]` を書き換えうる
    ノード (`compile_playbook` がその後で `_unstored_reply` を消す) や、`{last}`
    以外の文字を含む `action` の MEMORIZE では載せない。
    """

    PB = SimpleNamespace(name="parent_pb", display_name="parent_pb")

    @pytest.fixture(autouse=True)
    def _no_engine_trace(self, monkeypatch):
        from sea import runtime_engine

        monkeypatch.setattr(runtime_engine, "log_sea_trace", lambda *a, **k: None)

    # ---- ノードを回す口 ----

    def _run_llm(self, runtime, persona, state, node_id: str, *, memorize: bool) -> None:
        import asyncio

        from sea.runtime_llm import lg_llm_node

        node_def = _llm_node_def(node_id)
        if not memorize:
            node_def.memorize = None
        asyncio.run(lg_llm_node(runtime, node_def, persona, "b1", self.PB, None)(state))

    def _run_memorize(self, runtime, persona, state, *, action=None) -> None:
        import asyncio

        node_def = SimpleNamespace(
            id="record", action=action, role="assistant", tags=["conversation"],
            line_role=None, scope=None, metadata_key=None, label=None,
        )
        node_fn = runtime._runtime_engine.lg_memorize_node(node_def, persona, self.PB)
        asyncio.run(node_fn(state))

    def _run_speak(self, runtime, persona, state) -> None:
        """本物の lg_speak_node + 本物の emit_speak。

        `history_manager.add_to_persona_only` は本物と同じ形 (`(status, message_id)`)
        を返し、記憶へは本物の adapter に書く。建物側の保存だけ代役。
        """
        from sea.runtime_emitters import forget_saved_utterances

        adapter = persona.sai_memory

        class _History:
            def add_to_persona_only(self, msg, **kwargs):
                mid = adapter.append_persona_message(dict(msg))
                return ("synced", str(mid)) if mid else ("failed", None)

            def add_to_building_only(self, building_id, msg, heard_by=None):
                return {"message_id": f"{building_id}:1", "content": msg["content"]}

        persona.history_manager = _History()
        runtime.manager.occupants = {}
        runtime.manager.user_presence_status = "offline"
        runtime.manager.gateway_handle_ai_replies = lambda *a, **k: None
        try:
            runtime._runtime_engine.lg_speak_node(state, persona, "b1", self.PB)
        finally:
            forget_saved_utterances()

    def _run_think(self, runtime, persona, state) -> None:
        runtime._lg_think_node(state, persona, self.PB)

    def _run_storing_node(self, kind: str, runtime, persona, state) -> None:
        {
            "memorize": lambda: self._run_memorize(runtime, persona, state),
            "speak": lambda: self._run_speak(runtime, persona, state),
            "think": lambda: self._run_think(runtime, persona, state),
        }[kind]()

    @staticmethod
    def _reply_dict(state, content: str) -> Dict[str, Any]:
        hits = [
            m for m in state["_messages"]
            if m.get("role") == "assistant" and m.get("content") == content
        ]
        assert len(hits) == 1, hits
        return hits[0]

    # ---- 本題 ----

    @pytest.mark.parametrize("kind", ["memorize", "speak", "think"])
    def test_next_reply_points_at_the_row_the_later_node_stored(self, node_env, kind):
        runtime, persona, adapter, client = node_env
        h1, h2, messages = _seed_history(runtime, persona)
        state = _base_state(messages, [h1, h2])

        client.replies.append("返事 A。")
        self._run_llm(runtime, persona, state, "reply_a", memorize=False)
        self._run_storing_node(kind, runtime, persona, state)
        r_id, _r_meta = _row_by_content(adapter, "返事 A。")

        client.replies.append("返事 B。")
        self._run_llm(runtime, persona, state, "reply_b", memorize=True)

        _b_id, b_meta = _row_by_content(adapter, "返事 B。")
        expected = _last_recorded_id_in(adapter, client.sent[-1])
        assert expected == r_id
        assert b_meta.get(PREDECESSOR_META_KEY) == expected
        assert self._reply_dict(state, "返事 A。").get("id") == r_id

    @pytest.mark.parametrize("kind", ["memorize", "speak", "think"])
    def test_stored_row_points_at_what_x_sent(self, node_env, kind):
        """保存した行 R 自身の `predecessor_message_id` は、X が送った `messages`
        の最後の記録の行のまま — R 自身を指さない。"""
        runtime, persona, adapter, client = node_env
        h1, h2, messages = _seed_history(runtime, persona)
        state = _base_state(messages, [h1, h2])

        client.replies.append("返事 A。")
        self._run_llm(runtime, persona, state, "reply_a", memorize=False)
        seen_by_x = _last_recorded_id_in(adapter, client.sent[-1])
        self._run_storing_node(kind, runtime, persona, state)

        r_id, r_meta = _row_by_content(adapter, "返事 A。")
        assert seen_by_x == h2
        assert r_meta.get(PREDECESSOR_META_KEY) == seen_by_x
        assert r_meta.get(PREDECESSOR_META_KEY) != r_id

    @pytest.mark.parametrize("same_text", [False, True])
    def test_node_that_rewrites_last_in_between_blocks_the_id(
        self, node_env, monkeypatch, same_text,
    ):
        """X と MEMORIZE の間で TOOL が `state["last"]` を書き換えたら、X の dict
        に `id` は載らない。TOOL の結果が X の本文と同じ文字列でも載らない —
        `compile_playbook` が TOOL の後で `_unstored_reply` を消すため。
        (X・TOOL・MEMORIZE を `compile_playbook` から走らせる。)"""
        import tools

        runtime, persona, adapter, client = node_env
        h1, h2, messages = _seed_history(runtime, persona)
        state = _base_state(messages, [h1, h2])

        result = "".join(["返事", " A。"]) if same_text else "ツールの結果。"
        monkeypatch.setitem(tools.TOOL_REGISTRY, "stamp_test_tool", lambda: result)
        client.replies.append("返事 A。")
        _run_compiled(runtime, persona, [
            _llm_node("reply_a", "tool"),
            {"id": "tool", "type": "tool", "action": "stamp_test_tool", "next": "record"},
            _memorize_node("record"),
        ], state)
        assert state["last"] == result

        assert "id" not in self._reply_dict(state, "返事 A。")

        if not same_text:
            client.replies.append("返事 B。")
            self._run_llm(runtime, persona, state, "reply_b", memorize=True)
            _b_id, b_meta = _row_by_content(adapter, "返事 B。")
            expected = _last_recorded_id_in(adapter, client.sent[-1])
            assert expected == h2
            assert b_meta.get(PREDECESSOR_META_KEY) == expected

    def test_templated_memorize_action_does_not_put_the_id(self, node_env):
        runtime, persona, adapter, client = node_env
        h1, h2, messages = _seed_history(runtime, persona)
        state = _base_state(messages, [h1, h2])

        client.replies.append("返事 A。")
        self._run_llm(runtime, persona, state, "reply_a", memorize=False)
        self._run_memorize(runtime, persona, state, action="前置き: {last}")
        _row_by_content(adapter, "前置き: 返事 A。")

        assert "id" not in self._reply_dict(state, "返事 A。")

        client.replies.append("返事 B。")
        self._run_llm(runtime, persona, state, "reply_b", memorize=True)
        _b_id, b_meta = _row_by_content(adapter, "返事 B。")
        expected = _last_recorded_id_in(adapter, client.sent[-1])
        assert expected == h2
        assert b_meta.get(PREDECESSOR_META_KEY) == expected

    def test_next_llm_node_drops_the_previous_unstored_reply(self, node_env):
        """X (保存なし) → Z (保存なし) → MEMORIZE: `id` が載るのは Z の dict だけ。

        `lg_llm_node` の入り口で `_unstored_reply` を消すので、X の返事を Z の
        返事と取り違えない。
        """
        runtime, persona, adapter, client = node_env
        h1, h2, messages = _seed_history(runtime, persona)
        state = _base_state(messages, [h1, h2])

        client.replies.append("返事 A。")
        self._run_llm(runtime, persona, state, "reply_a", memorize=False)
        client.replies.append("返事 Z。")
        self._run_llm(runtime, persona, state, "reply_z", memorize=False)
        self._run_memorize(runtime, persona, state)

        z_row, _ = _row_by_content(adapter, "返事 Z。")
        assert self._reply_dict(state, "返事 Z。").get("id") == z_row
        assert "id" not in self._reply_dict(state, "返事 A。")


# ---------------------------------------------------------------------------
# 7. `compile_playbook` が、`state["last"]` を生成以外の本文に書き換えうるノード
#    (TOOL / TOOL_CALL / EXEC / SUBPLAY / `"last"` を代入する SET) の後で
#    `_last_call_tokens` / `_presented_message_ids` / `_unstored_reply` を消すこと
#
# ノード関数を直接呼ぶと `compile_playbook` が包んだ処理を通らないので、ここでは
# 実 Playbook (`PlaybookSchema`) を `compile_playbook` から走らせる。
# ---------------------------------------------------------------------------


class _UsageRecordingClient(_RecordingClient):
    """`_RecordingClient` に、コールごとの使用量 (`_usage()`) を返させたもの。"""

    def __init__(self):
        super().__init__()
        self._pending_usage = None

    def generate(self, messages, tools=None, temperature=None, response_schema=None, **kwargs):
        self._pending_usage = _usage()
        return super().generate(
            messages, tools=tools, temperature=temperature,
            response_schema=response_schema, **kwargs,
        )

    def consume_usage(self):
        usage, self._pending_usage = self._pending_usage, None
        return usage


def _run_compiled(runtime, persona, nodes: list, state: Dict[str, Any]) -> None:
    """`nodes` を親の Playbook にして `compile_playbook` から走らせる。

    ノードを作る関数は `sea/runtime_graph.compile_with_langgraph` と同じ
    (runtime の `_lg_*` メソッド)。LangGraph は state の dict をそのまま
    ノードへ渡すので、走った後の `state` で結果を確かめられる。
    """
    import asyncio

    from sea.langgraph_runner import compile_playbook
    from sea.playbook_models import PlaybookSchema

    playbook = PlaybookSchema(
        name="parent_pb", display_name="parent_pb", description="t",
        input_schema=[], start_node=nodes[0]["id"], nodes=nodes,
    )
    outputs: list = []
    compiled = compile_playbook(
        playbook,
        llm_node_factory=lambda nd: runtime._lg_llm_node(nd, persona, "b1", playbook, None),
        tool_node_factory=lambda nd: runtime._lg_tool_node(nd, persona, playbook, None),
        tool_call_node_factory=lambda nd: runtime._lg_tool_call_node(nd, persona, playbook, None),
        speak_node=lambda s: runtime._lg_speak_node(s, persona, "b1", playbook, outputs, None),
        think_node=lambda s: runtime._lg_think_node(s, persona, playbook, outputs, None),
        memorize_node_factory=lambda nd: runtime._lg_memorize_node(nd, persona, playbook, outputs, None),
        exec_node_factory=lambda nd: runtime._lg_exec_node(nd, playbook, persona, "b1", False, outputs, None),
        subplay_node_factory=lambda nd: runtime._lg_subplay_node(nd, persona, "b1", playbook, False, outputs, None),
        set_node_factory=lambda nd: runtime._lg_set_node(nd, playbook, None),
    )
    assert compiled is not None
    asyncio.run(compiled(state, {"recursion_limit": 100}))


def _llm_node(node_id: str, next_id: Optional[str], *, memorize=None) -> Dict[str, Any]:
    return {"id": node_id, "type": "llm", "memorize": memorize, "next": next_id}


def _memorize_node(node_id: str, next_id: Optional[str] = None, *, action="{last}") -> Dict[str, Any]:
    return {
        "id": node_id, "type": "memorize", "action": action, "role": "assistant",
        "tags": ["conversation"], "next": next_id,
    }


def _assistant_rows(adapter, content: str) -> list:
    with adapter._db_lock:
        rows = adapter.conn.execute(
            "SELECT id, metadata FROM messages WHERE content = ? AND role = ? ORDER BY rowid",
            (content, "assistant"),
        ).fetchall()
    return [(str(r[0]), json.loads(r[1]) if r[1] else {}) for r in rows]


def _reply_dicts(state: Dict[str, Any], content: str) -> list:
    return [
        m for m in state["_messages"]
        if m.get("role") == "assistant" and m.get("content") == content
    ]


@pytest.fixture
def compiled_env(node_env, monkeypatch):
    """`node_env` に、使用量を返す偽 LLM と、`compile_playbook` から走らせる準備を足す。"""
    from sea import runtime as sea_runtime
    from sea import runtime_engine, runtime_llm

    runtime, persona, adapter, _client = node_env
    client = _UsageRecordingClient()
    runtime.select_llm_client = lambda *a, **k: (client, "model-a")
    runtime._accumulate_usage = lambda *a, **k: None
    monkeypatch.setattr(runtime_llm, "get_usage_tracker", Mock())
    monkeypatch.setattr(runtime_llm, "_maybe_record_cache_storage", lambda *a, **k: None)
    monkeypatch.setattr(runtime_engine, "log_sea_trace", lambda *a, **k: None)
    monkeypatch.setattr(sea_runtime, "log_sea_trace", lambda *a, **k: None)
    return runtime, persona, adapter, client


def _child_writes_parent_material(runtime, output: str):
    """子 Playbook の代役。本物の子と同じく親 state へ子の `_presented_message_ids` を書く。"""

    def _child(sub_pb, p, bid, sub_input, auto_mode, record_history, parent_state,
               event_callback, **kwargs):
        child_row = runtime._store_memory(
            p, "子が記録に書いた行。", role="assistant", return_message_id=True,
        )
        record_presented_message_ids(
            parent_state,
            {"presented_message_ids": [
                *(parent_state.get(PRESENTED_IDS_STATE_KEY) or []), str(child_row),
            ]},
        )
        return [output]

    return _child


def _middle_node(kind: str, runtime, monkeypatch, state, output: str) -> Dict[str, Any]:
    """`state["last"]` を `output` に書き換えるノード (種類ごと) を用意して返す。"""
    if kind == "subplay":
        runtime._run_playbook = Mock(side_effect=_child_writes_parent_material(runtime, output))
        return {"id": "mid", "type": "subplay", "playbook": "child_pb", "next": "record"}
    if kind == "exec":
        runtime._run_playbook = Mock(side_effect=_child_writes_parent_material(runtime, output))
        runtime.decide_playbook_permission = Mock(return_value=(True, None))
        state["selected_playbook"] = "child_pb"
        return {"id": "mid", "type": "exec", "next": "record"}
    if kind == "tool":
        import tools

        monkeypatch.setitem(tools.TOOL_REGISTRY, "stamp_test_tool", lambda: output)
        return {"id": "mid", "type": "tool", "action": "stamp_test_tool", "next": "record"}
    if kind == "tool_call":
        import tools

        monkeypatch.setitem(tools.TOOL_REGISTRY, "stamp_test_tool", lambda: output)
        state["fc"] = {"name": "stamp_test_tool", "args": {}}
        return {"id": "mid", "type": "tool_call", "next": "record"}
    if kind == "set_last":
        return {"id": "mid", "type": "set", "assignments": {"last": output}, "next": "record"}
    raise AssertionError(kind)


class TestCompilePlaybookDropsStampMaterial:
    """親の LLM ノード (保存なし) → `state["last"]` を書き換えるノード → 親の MEMORIZE (`{last}`)。"""

    @pytest.mark.parametrize("kind", ["subplay", "exec", "tool", "tool_call", "set_last"])
    def test_same_short_text_is_not_tied_to_the_old_reply(
        self, compiled_env, monkeypatch, kind,
    ):
        """⭐ Codex 三巡目 1: 返事も書き換えた後の本文も `"1"` (Python では同じ
        オブジェクトになりうる)。MEMORIZE が保存した行の id を親の返事の dict に
        付けず、その行に親の `predecessor_message_id` と `llm_tokens` も付けない。"""
        runtime, persona, adapter, client = compiled_env
        h1, h2, messages = _seed_history(runtime, persona)
        state = _base_state(messages, [h1, h2])

        client.replies.append("1")
        middle = _middle_node(kind, runtime, monkeypatch, state, "1")
        _run_compiled(runtime, persona, [
            _llm_node("reply", "mid"), middle, _memorize_node("record"),
        ], state)

        reply = _reply_dicts(state, "1")
        assert len(reply) == 1
        assert "id" not in reply[0]
        rows = _assistant_rows(adapter, "1")
        assert len(rows) == 1
        _row_id, meta = rows[0]
        assert PREDECESSOR_META_KEY not in meta
        assert TOKENS_META_KEY not in meta

    def test_memorize_right_after_the_reply_still_ties_the_id(self, compiled_env):
        """対照: 間に書き換えるノードが無ければ、MEMORIZE の行の id は返事の dict に載り、
        行には返事の前駆と三つ組が付く。"""
        runtime, persona, adapter, client = compiled_env
        h1, h2, messages = _seed_history(runtime, persona)
        state = _base_state(messages, [h1, h2])

        client.replies.append("1")
        _run_compiled(runtime, persona, [
            _llm_node("reply", "record"), _memorize_node("record"),
        ], state)

        (row_id, meta), = _assistant_rows(adapter, "1")
        assert _reply_dicts(state, "1")[0]["id"] == row_id
        assert meta[PREDECESSOR_META_KEY] == h2
        assert TOKENS_META_KEY in meta

    def test_set_without_last_keeps_the_material(self, compiled_env):
        """`"last"` を代入しない SET は包まない — 材料も `_unstored_reply` も残る。"""
        runtime, persona, adapter, client = compiled_env
        h1, h2, messages = _seed_history(runtime, persona)
        state = _base_state(messages, [h1, h2])

        client.replies.append("1")
        _run_compiled(runtime, persona, [
            _llm_node("reply", "mid"),
            {"id": "mid", "type": "set", "assignments": {"counter": 1}, "next": "record"},
            _memorize_node("record"),
        ], state)

        (row_id, meta), = _assistant_rows(adapter, "1")
        assert _reply_dicts(state, "1")[0]["id"] == row_id
        assert meta[PREDECESSOR_META_KEY] == h2
        assert TOKENS_META_KEY in meta

    def test_templated_memorize_then_speak_does_not_tie_the_id(self, compiled_env):
        """MEMORIZE が `action` のテンプレートで包んだ文を `state["last"]` に入れた後、
        SPEAK がそれを保存しても、返事の dict には id を付けない (本文が違う)。"""
        from sea.runtime_emitters import forget_saved_utterances

        runtime, persona, adapter, client = compiled_env
        h1, h2, messages = _seed_history(runtime, persona)
        state = _base_state(messages, [h1, h2])

        class _History:
            def add_to_persona_only(self, msg, **kwargs):
                mid = adapter.append_persona_message(dict(msg))
                return ("synced", str(mid)) if mid else ("failed", None)

            def add_to_building_only(self, building_id, msg, heard_by=None):
                return {"message_id": f"{building_id}:1", "content": msg["content"]}

        persona.history_manager = _History()
        runtime.manager.occupants = {}
        runtime.manager.user_presence_status = "offline"
        runtime.manager.gateway_handle_ai_replies = lambda *a, **k: None

        client.replies.append("返事 A。")
        try:
            _run_compiled(runtime, persona, [
                _llm_node("reply", "record"),
                _memorize_node("record", "talk", action="前置き: {last}"),
                {"id": "talk", "type": "speak", "next": None},
            ], state)
        finally:
            forget_saved_utterances()

        assert len(_assistant_rows(adapter, "前置き: 返事 A。")) == 2
        assert "id" not in _reply_dicts(state, "返事 A。")[0]

    @staticmethod
    def _speak_ready(runtime, persona, adapter) -> None:
        """SPEAK ノードが記憶に 1 行書けるようにする (建物への保存は偽物)。"""

        class _History:
            def add_to_persona_only(self, msg, **kwargs):
                mid = adapter.append_persona_message(dict(msg))
                return ("synced", str(mid)) if mid else ("failed", None)

            def add_to_building_only(self, building_id, msg, heard_by=None):
                return {"message_id": f"{building_id}:1", "content": msg["content"]}

        persona.history_manager = _History()
        runtime.manager.occupants = {}
        runtime.manager.user_presence_status = "offline"
        runtime.manager.gateway_handle_ai_replies = lambda *a, **k: None

    def test_memorize_from_another_variable_then_speak_does_not_tie_the_id(
        self, compiled_env,
    ):
        """Codex 四巡目 1: 保存しない LLM の返事が `"1"` → MEMORIZE
        (`action="{other}"`、`state["other"] == "1"`) → SPEAK。MEMORIZE が
        `state["last"]` を `{last}` 以外から作り直すので、`compile_playbook` が
        その後で `_unstored_reply` を消し、SPEAK が保存した行の id は返事の dict に
        付かない。MEMORIZE 自身の行にも刻印は付かない — `{last}` 以外から作った
        本文がどの生成の出力かは確かめられない (Codex 五巡目)。"""
        from sea.runtime_emitters import forget_saved_utterances

        runtime, persona, adapter, client = compiled_env
        h1, h2, messages = _seed_history(runtime, persona)
        state = _base_state(messages, [h1, h2])
        state["other"] = "1"
        self._speak_ready(runtime, persona, adapter)

        client.replies.append("1")
        try:
            _run_compiled(runtime, persona, [
                _llm_node("reply", "record"),
                _memorize_node("record", "talk", action="{other}"),
                {"id": "talk", "type": "speak", "next": None},
            ], state)
        finally:
            forget_saved_utterances()

        assert "id" not in _reply_dicts(state, "1")[0]
        rows = _assistant_rows(adapter, "1")
        assert len(rows) == 2
        _memo_id, memo_meta = rows[0]
        assert PREDECESSOR_META_KEY not in memo_meta
        assert TOKENS_META_KEY not in memo_meta

    def test_memorize_of_an_older_reply_gets_no_stamp_of_the_latest_generation(
        self, compiled_env,
    ):
        """Codex 五巡目: LLM A が返事を保存して `old_reply` に入れる → LLM B が
        A の行を見て生成 → MEMORIZE (`action="{old_reply}"`) で A の本文を
        もう一度保存する。この行に B の生成の `predecessor_message_id` (= A の行
        自身) とトークン数を刻まない。"""
        runtime, persona, adapter, client = compiled_env
        h1, h2, messages = _seed_history(runtime, persona)
        state = _base_state(messages, [h1, h2])

        client.replies.extend(["返事 A。", "返事 B。"])
        _run_compiled(runtime, persona, [
            _llm_node("reply_a", "keep_a", memorize={"tags": ["conversation"]}),
            {"id": "keep_a", "type": "set", "assignments": {"old_reply": "{last}"},
             "next": "reply_b"},
            _llm_node("reply_b", "record_old", memorize={"tags": ["conversation"]}),
            _memorize_node("record_old", None, action="{old_reply}"),
        ], state)

        rows = _assistant_rows(adapter, "返事 A。")
        assert len(rows) == 2
        a_id, _a_meta = rows[0]
        _b_id, b_meta = _row_by_content(adapter, "返事 B。")
        assert b_meta.get(PREDECESSOR_META_KEY) == a_id
        _again_id, again_meta = rows[1]
        assert PREDECESSOR_META_KEY not in again_meta
        assert TOKENS_META_KEY not in again_meta

    @pytest.mark.parametrize("action", ["{last}", None])
    def test_memorize_of_last_only_then_speak_still_ties_the_id(self, compiled_env, action):
        """対照: `action` が `{last}` だけ (省略も実行時は `{last}`) の MEMORIZE は
        包まないので、MEMORIZE が保存した行の id が返事の dict に付く。"""
        from sea.runtime_emitters import forget_saved_utterances

        runtime, persona, adapter, client = compiled_env
        h1, h2, messages = _seed_history(runtime, persona)
        state = _base_state(messages, [h1, h2])
        self._speak_ready(runtime, persona, adapter)

        client.replies.append("1")
        try:
            _run_compiled(runtime, persona, [
                _llm_node("reply", "record"),
                _memorize_node("record", "talk", action=action),
                {"id": "talk", "type": "speak", "next": None},
            ], state)
        finally:
            forget_saved_utterances()

        rows = _assistant_rows(adapter, "1")
        assert len(rows) == 2
        memo_id, _memo_meta = rows[0]
        assert _reply_dicts(state, "1")[0]["id"] == memo_id

    @pytest.mark.parametrize("path", [
        "exec_not_found", "exec_denied", "subplay_not_found", "subplay_missing_name",
    ])
    def test_error_text_paths_drop_the_material(self, compiled_env, path):
        """Codex 三巡目 4: `state["last"]` をエラー文に変えて早期 return する経路でも、
        後ろの MEMORIZE がエラー文に直前の LLM の前駆と三つ組を刻まない。"""
        runtime, persona, adapter, client = compiled_env
        h1, h2, messages = _seed_history(runtime, persona)
        state = _base_state(messages, [h1, h2])

        runtime._run_playbook = Mock(side_effect=AssertionError("子は走らない"))
        if path == "exec_not_found":
            runtime._load_playbook_for = lambda name, p, b: None
            state["selected_playbook"] = "missing_pb"
            middle = {"id": "mid", "type": "exec", "next": "record"}
        elif path == "exec_denied":
            runtime.decide_playbook_permission = Mock(return_value=(False, "許可されませんでした。"))
            state["selected_playbook"] = "child_pb"
            middle = {"id": "mid", "type": "exec", "next": "record"}
        elif path == "subplay_not_found":
            runtime._load_playbook_for = lambda name, p, b: None
            middle = {"id": "mid", "type": "subplay", "playbook": "missing_pb", "next": "record"}
        else:
            middle = {"id": "mid", "type": "subplay", "playbook": "", "next": "record"}

        client.replies.append("返事 A。")
        _run_compiled(runtime, persona, [
            _llm_node("reply", "mid"), middle, _memorize_node("record"),
        ], state)

        error_text = state["last"]
        assert error_text != "返事 A。"
        (_row_id, meta), = _assistant_rows(adapter, error_text)
        assert PREDECESSOR_META_KEY not in meta
        assert TOKENS_META_KEY not in meta
        assert "id" not in _reply_dicts(state, "返事 A。")[0]

    @pytest.mark.parametrize("is_async", [False, True])
    def test_sync_and_async_node_returning_a_new_dict(self, is_async):
        """同期のノード関数も async のノード関数も包む。ノードが受け取った dict と
        別の dict を返しても、次のノードへ渡る返した方から消える。"""
        import asyncio
        import inspect

        from sea.langgraph_runner import _drop_stamp_material_after, compile_playbook
        from sea.message_stamp import UNSTORED_REPLY_STATE_KEY
        from sea.playbook_models import PlaybookSchema

        def set_factory(node_def):
            if is_async:
                async def node(state):
                    return {**state, "last": "代入した値"}
            else:
                def node(state):
                    return {**state, "last": "代入した値"}
            assert inspect.iscoroutinefunction(_drop_stamp_material_after(node)) is is_async
            return node

        playbook = PlaybookSchema(
            name="pb", description="t", input_schema=[], start_node="a",
            nodes=[
                {"id": "a", "type": "set", "assignments": {"last": "代入した値"}, "next": "b"},
                {"id": "b", "type": "pass", "next": None},
            ],
        )
        compiled = compile_playbook(
            playbook, llm_node_factory=Mock(), tool_node_factory=Mock(),
            speak_node=Mock(), think_node=Mock(), set_node_factory=set_factory,
        )
        state: Dict[str, Any] = {
            "last": "返事",
            PRESENTED_IDS_STATE_KEY: ["h1"],
            UNSTORED_REPLY_STATE_KEY: {"role": "assistant", "content": "返事"},
        }
        record_call_tokens(state, _usage())
        result = asyncio.run(compiled(state, {"recursion_limit": 10}))

        assert result is not state
        assert result["last"] == "代入した値"
        for key in (CALL_TOKENS_STATE_KEY, PRESENTED_IDS_STATE_KEY, UNSTORED_REPLY_STATE_KEY):
            assert key not in result
            assert key not in state

    def test_node_that_raises_still_drops_the_material(self, compiled_env):
        """包んだノードが例外で終わっても消す (`finally`)。SUBPLAY は子の LLMError を投げ直す。"""
        from llm_clients.exceptions import LLMError
        from sea.message_stamp import UNSTORED_REPLY_STATE_KEY

        runtime, persona, adapter, client = compiled_env
        h1, h2, messages = _seed_history(runtime, persona)
        state = _base_state(messages, [h1, h2])

        runtime._run_playbook = Mock(side_effect=LLMError("子の LLM が失敗した"))
        client.replies.append("返事 A。")
        with pytest.raises(LLMError):
            _run_compiled(runtime, persona, [
                _llm_node("reply", "mid"),
                {"id": "mid", "type": "subplay", "playbook": "child_pb", "next": None},
            ], state)

        assert CALL_TOKENS_STATE_KEY not in state
        assert PRESENTED_IDS_STATE_KEY not in state
        assert UNSTORED_REPLY_STATE_KEY not in state


class TestSpeakIdSurvivesBuildingSaveFailure:
    """Codex 三巡目 2: `add_to_persona_only` が記憶に書けた後で建物への保存が
    例外 / None になっても、`emit_speak` は記憶の行の id を返し、`lg_speak_node` は
    それを返事の dict に載せる。"""

    @staticmethod
    def _emitters_with(persona_saved, building):
        from sea.runtime_emitters import RuntimeEmitters

        history = Mock()
        history.add_to_persona_only.return_value = persona_saved
        if isinstance(building, Exception):
            history.add_to_building_only.side_effect = building
        else:
            history.add_to_building_only.return_value = building
        manager = SimpleNamespace(
            occupants={}, user_presence_status="offline",
            gateway_handle_ai_replies=Mock(),
        )
        emitters = RuntimeEmitters(runtime=SimpleNamespace(manager=manager))
        persona = SimpleNamespace(persona_id="p1", history_manager=history)
        return emitters, persona

    @pytest.mark.parametrize("building", [RuntimeError("building_messages の保存に失敗"), None])
    def test_emit_speak_returns_the_memory_row_id(self, building):
        from sea.runtime_emitters import SPEAK_MEMORY_MESSAGE_ID_KEY, SPEAK_PERSISTED_EVENT_TYPE

        emitters, persona = self._emitters_with(("synced", "mem-3"), building)
        events: list = []
        out = emitters.emit_speak(persona, "b1", "こんにちは。", event_callback=events.append)
        assert out == {SPEAK_MEMORY_MESSAGE_ID_KEY: "mem-3"}
        # 建物に保存されなかった回は、保存完了イベントを流さない (従来どおり)
        assert not any(e.get("type") == SPEAK_PERSISTED_EVENT_TYPE for e in events)

    def test_emit_speak_without_either_row_returns_none(self):
        emitters, persona = self._emitters_with(("failed", None), RuntimeError("x"))
        assert emitters.emit_speak(persona, "b1", "x") is None

    def test_speak_node_puts_the_memory_row_id_on_the_reply(self, compiled_env):
        from sea.runtime_emitters import forget_saved_utterances

        runtime, persona, adapter, client = compiled_env
        h1, h2, messages = _seed_history(runtime, persona)
        state = _base_state(messages, [h1, h2])

        class _History:
            def add_to_persona_only(self, msg, **kwargs):
                mid = adapter.append_persona_message(dict(msg))
                return ("synced", str(mid)) if mid else ("failed", None)

            def add_to_building_only(self, building_id, msg, heard_by=None):
                raise RuntimeError("building_messages の保存に失敗")

        persona.history_manager = _History()
        runtime.manager.occupants = {}
        runtime.manager.user_presence_status = "offline"
        runtime.manager.gateway_handle_ai_replies = lambda *a, **k: None

        client.replies.append("返事 A。")
        try:
            _run_compiled(runtime, persona, [
                _llm_node("reply", "talk"), {"id": "talk", "type": "speak", "next": None},
            ], state)
        finally:
            forget_saved_utterances()

        r_id, _meta = _row_by_content(adapter, "返事 A。")
        assert _reply_dicts(state, "返事 A。")[0].get("id") == r_id


class TestEmptyContentRowsCountAtSaiverseMessages:
    """Codex 四巡目 2: predecessor_message_id は SAIVerse が組んだ `messages`
    (クライアントへ渡す前) の段階で決める。本文が空の tool_calls だけの返事も、
    本文が空で画像だけのユーザー発言も、記録の行 id があれば
    `sync_presented_message_ids_with_sent` が数える。各クライアントが送る直前に
    行を落とすかどうかは見ない。"""

    SENT = [
        {"role": "system", "content": "head"},
        {"id": "h1", "role": "user", "content": "h1"},
        {"id": "tc-row", "role": "assistant", "content": "",
         "tool_calls": [{"id": "call-1", "type": "function",
                         "function": {"name": "calc", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "call-1", "name": "calc", "content": "42"},
    ]

    def test_sync_counts_the_tool_call_only_reply(self):
        from sea.message_stamp import sync_presented_message_ids_with_sent

        state: Dict[str, Any] = {}
        sync_presented_message_ids_with_sent(state, self.SENT)
        assert build_generation_stamp(state)[PREDECESSOR_META_KEY] == "tc-row"

    def test_sync_counts_an_image_only_row(self, tmp_path):
        from sea.message_stamp import sync_presented_message_ids_with_sent

        image = tmp_path / "x.png"
        image.write_bytes(b"\x89PNG\r\n\x1a\n")
        sent = [
            {"id": "h1", "role": "user", "content": "h1"},
            {"id": "img", "role": "user", "content": "",
             "metadata": {"media": [{"type": "image", "path": str(image),
                                     "mime_type": "image/png"}]}},
        ]
        state: Dict[str, Any] = {}
        sync_presented_message_ids_with_sent(state, sent)
        assert build_generation_stamp(state)[PREDECESSOR_META_KEY] == "img"

    def test_client_conversions_that_drop_the_row_do_not_change_the_count(self):
        """Anthropic の `_prepare_anthropic_messages` と xAI の `_build_xai_messages` は
        本文の空な tool_calls だけの assistant を送らないが、
        `sync_presented_message_ids_with_sent` は `messages` の段階で数える。"""
        from llm_clients.anthropic_request_builder import _prepare_anthropic_messages
        from llm_clients.xai import _build_xai_messages
        from sea.message_stamp import sync_presented_message_ids_with_sent

        assert all(m.get("role") != "assistant" for m in _prepare_anthropic_messages(self.SENT))
        # system + h1 だけが残る (本文の空な tool_calls の返事と tool の行は送られない)
        assert len(_build_xai_messages(self.SENT, supports_images=False)) == 2

        state: Dict[str, Any] = {}
        sync_presented_message_ids_with_sent(state, self.SENT)
        assert state[PRESENTED_IDS_STATE_KEY] == ["h1", "tc-row"]

    def test_llm_node_counts_the_tool_call_only_reply(self, node_env):
        """`lg_llm_node` が送る直前に `messages` の段階で数える (クライアントを問わない)。"""
        import asyncio

        from sea.runtime_llm import lg_llm_node

        runtime, persona, adapter, client = node_env
        h1, h2, messages = _seed_history(runtime, persona)
        tc_row = runtime._store_memory(
            persona, '{"tool_calls": "calc"}', role="assistant", return_message_id=True,
        )
        messages = [
            *messages,
            {"id": str(tc_row), "role": "assistant", "content": "",
             "tool_calls": [{"id": "call-1", "type": "function",
                             "function": {"name": "calc", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "call-1", "name": "calc", "content": "42"},
        ]
        state = _base_state(messages, [h1, h2])
        client.replies.append("返事 B。")
        pb = SimpleNamespace(name="parent_pb", display_name="parent_pb")
        asyncio.run(lg_llm_node(runtime, _llm_node_def("reply_b"), persona, "b1", pb, None)(state))

        _b_id, b_meta = _row_by_content(adapter, "返事 B。")
        assert b_meta.get(PREDECESSOR_META_KEY) == str(tc_row)
