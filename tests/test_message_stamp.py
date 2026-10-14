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
    (lg_subplay_node) がその区間で、両方を落とすことで刻印なしへ倒れる。
    """

    def test_nothing_is_stamped_after_both_are_cleared(self):
        state: Dict[str, Any] = {PRESENTED_IDS_STATE_KEY: ["parent-tail"]}
        record_call_tokens(state, _usage())
        # 中継の場で対にして落とす (lg_subplay_node と同じ順)
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
    プロンプトに入るのに ``_presented_message_ids`` が動かず、ラウンド 2 以降の
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

        後で材料を送る列から作り直す回 (子 Playbook の中継の後) に、周の発言が
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
# 子 Playbook の中継 (lg_subplay_node) は三つ組と前駆の材料を対で落とす (上の
# TestSubPlaybookRelayIsNotStamped)。三つ組は親の次の LLM コールで
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
    """本物の lg_subplay_node。子 (メインライン) は Mock だが、本物の子と同じく
    親 state へ自分の提示列を書き、自分の返事を記録に残す。"""
    import asyncio

    from sea.runtime_nodes import lg_subplay_node

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
    node_def = SimpleNamespace(
        id="call_sub", playbook="child_pb", action=None, line="main",
        execution="inline", isolate_pulse_context=False, args=None,
        propagate_output=False, subagent_chronicle=False,
    )
    node_fn = lg_subplay_node(
        runtime, node_def, persona, "b1",
        SimpleNamespace(name="parent_pb"), False, [], None,
    )
    asyncio.run(node_fn(state))


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
        # 子の行は記録の最新だが、親の送る列には入っていない
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


class TestPredecessorAcrossConsecutiveParentNodes:
    """子 Playbook が無くても、親の LLM ノード A (memorize あり) の後に親の LLM
    ノード B が続くとき、B の前駆は B が実際に見た A を指すこと。

    材料が残っている回は作り直さず、B が送る直前に、送る列で材料の末尾より
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
    """材料を送る列に合わせる (sea/message_stamp.sync_presented_message_ids_with_sent)。"""

    SENT = [
        {"role": "system", "content": "head"},
        {"id": "h1", "role": "user", "content": "h1"},
        {"role": "user", "content": "<system>部屋</system>",
         "metadata": {"__consumed_perception__": True}},
        {"id": "a1", "role": "assistant", "content": "a1"},
        {"role": "user", "content": "<system>action</system>"},
    ]

    def test_existing_material_is_never_overwritten(self):
        from sea.message_stamp import sync_presented_message_ids_with_sent

        state: Dict[str, Any] = {PRESENTED_IDS_STATE_KEY: ["hist-1", "round-1"]}
        sync_presented_message_ids_with_sent(state, self.SENT)
        assert state[PRESENTED_IDS_STATE_KEY] == ["hist-1", "round-1"]

    def test_rows_after_the_tail_are_appended_without_rewriting(self):
        """材料の末尾が送る列にあれば、その後ろの記録の行だけを足す。"""
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
        # 材料の中身 (先頭の hist-0 は送る列に無い) は変えず、h2 より後ろだけ足す
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

    def test_head_and_perception_are_not_counted_even_with_an_id(self):
        """head と知覚のまとめは、仮に id を持っても記録の行として数えない。"""
        from sea.eviction_plan import CONSUMED_PERCEPTION_KEY
        from sea.runtime_context import sent_message_ids

        sent = [
            {"id": "sys", "role": "system", "content": "head"},
            {"id": "perc", "role": "user", "content": "<system>部屋</system>",
             "metadata": {CONSUMED_PERCEPTION_KEY: True}},
            {"id": "h1", "role": "user", "content": "h1"},
        ]
        assert sent_message_ids(sent) == ["h1"]


class TestFinalizeBeatCarriesTheStoredRowId:
    """_finalize_beat が、記憶へ書いた返事の行 id を送る列の返事に載せること。"""

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
