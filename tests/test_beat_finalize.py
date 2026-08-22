"""Phase 1 で抽出した ④確定部（`_finalize_beat`）と `BeatExecution` の回帰固定。

分割設計書 `docs/issues/runtime_llm_node_split_design.md` §4 Phase 1。
生成 4 経路（tools あり・なし × streaming・sync）はすべてここへ合流するため、
ここが壊れると「喋ったのに記憶に残らない」「thought_signature が落ちて次ターンで
Gemini が落ちる」といった、実行時まで見えない事故になる。

固定するのは主に不変条件（設計書 §5）:
- 記録へ行くのは continuation（最終発言）だけ — merged 全文ではない
- thought_signature は assistant message / memorize / dual-write の 3 箇所へ流れる
- memorize と important dual-write は排他（二重保存しない）
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from sea.runtime_llm import (
    _ALREADY_STORED_STATE_KEY,
    BeatExecution,
    _finalize_beat,
)


def _playbook(name="pb", display_name="表示名"):
    return SimpleNamespace(name=name, display_name=display_name)


def _node_def(**kw):
    base = dict(id="n1", memorize=None, important=False, label=None, output_key=None)
    base.update(kw)
    return SimpleNamespace(**base)


def _beat(state=None, node_def=None, **kw):
    params = dict(
        persona=SimpleNamespace(persona_id="p1", persona_name="Persona"),
        node_def=node_def or _node_def(),
        node_id="n1",
        playbook=_playbook(),
        state=state if state is not None else {},
        event_callback=None,
        messages=[{"role": "user", "content": "hi"}],
        prompt="action prompt",
        continuation="response",
        schema_consumed=False,
    )
    params.update(kw)
    return BeatExecution.from_node_locals(**params)


class FinalizeTestBase(unittest.TestCase):
    """``_finalize_beat`` を呼ぶテストの共通土台。

    ``log_sea_trace`` は実ファイル (``~/.saiverse/user_data/logs/<session>/
    sea_trace.log``) を掴むため、必ずモックする。しないとテスト実行のたびに
    まはーの実データ領域へセッションログのディレクトリが増え、書き込み不可の
    環境では PermissionError で落ちる (Codex レビュー指摘 2026-07-23、実測で確認)。
    """

    def setUp(self):
        patcher = patch("sea.runtime_llm.log_sea_trace")
        self.log_sea_trace = patcher.start()
        self.addCleanup(patcher.stop)
        self.runtime = MagicMock()
        self.runtime._store_memory.return_value = "mid-1"


class FromNodeLocalsTest(unittest.TestCase):
    def test_reasoning_is_lifted_from_state(self):
        """4 経路がいずれも state に置いてから来るため、器へ移すのは 1 回だけ。"""
        beat = _beat(state={"_reasoning_text": "thinking", "_reasoning_details": [{"d": 1}]})
        self.assertEqual(beat.reasoning_text, "thinking")
        self.assertEqual(beat.reasoning_details, [{"d": 1}])

    def test_reasoning_defaults(self):
        beat = _beat(state={})
        self.assertEqual(beat.reasoning_text, "")
        self.assertIsNone(beat.reasoning_details)


class FinalizeStateAndMessagesTest(FinalizeTestBase):
    def test_sets_last_and_text_only_assistant_message(self):
        state = {}
        _finalize_beat(self.runtime, _beat(state=state))
        self.assertEqual(state["last"], "response")
        self.assertEqual(state["_messages"], [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "response"},
        ])

    def test_dict_continuation_is_serialised_for_message_content(self):
        """structured output は dict。後続の LLM 呼び出しに渡せる形へ落とす。"""
        state = {}
        _finalize_beat(self.runtime, _beat(state=state, continuation={"a": "あ"}, schema_consumed=True))
        self.assertEqual(state["last"], {"a": "あ"})
        self.assertEqual(state["_messages"][-1]["content"], '{"a": "あ"}')

    def test_tool_call_assistant_message(self):
        state = {
            "tool_called": True,
            "_last_tool_call_id": "call-1",
            "_last_tool_name": "calculator",
            "_last_tool_args_json": '{"expr": "1+1"}',
            "has_speak_content": True,
        }
        _finalize_beat(self.runtime, _beat(state=state))
        msg = state["_messages"][-1]
        self.assertEqual(msg["content"], "response")
        self.assertEqual(msg["tool_calls"][0]["id"], "call-1")
        self.assertEqual(msg["tool_calls"][0]["function"]["name"], "calculator")

    def test_tool_call_without_speak_content_has_empty_content(self):
        state = {
            "tool_called": True,
            "_last_tool_call_id": "call-1",
            "_last_tool_name": "calculator",
            "_last_tool_args_json": "{}",
        }
        _finalize_beat(self.runtime, _beat(state=state))
        self.assertEqual(state["_messages"][-1]["content"], "")

    def test_original_messages_list_is_not_mutated(self):
        messages = [{"role": "user", "content": "hi"}]
        _finalize_beat(self.runtime, _beat(state={}, messages=messages))
        self.assertEqual(len(messages), 1)


class ThoughtSignatureTest(FinalizeTestBase):
    """thought_signature は 3 箇所へ流れる（intent: thought_signature_persistence.md）。"""

    def test_flows_into_text_only_assistant_message(self):
        state = {"_last_thought_signature": b"sig"}
        _finalize_beat(self.runtime, _beat(state=state))
        self.assertEqual(state["_messages"][-1]["thought_signature"], b"sig")

    def test_flows_into_tool_call_entry_and_pulse_context(self):
        pulse_ctx = MagicMock()
        state = {
            "_last_thought_signature": b"sig",
            "tool_called": True,
            "_last_tool_call_id": "call-1",
            "_last_tool_name": "t",
            "_last_tool_args_json": "{}",
            "_pulse_context": pulse_ctx,
        }
        _finalize_beat(self.runtime, _beat(state=state))
        self.assertEqual(
            state["_messages"][-1]["tool_calls"][0]["thought_signature"], b"sig",
        )
        assistant_entry = pulse_ctx.append.call_args_list[-1].args[0]
        self.assertEqual(assistant_entry.tool_calls[0]["thought_signature"], b"sig")

    def test_flows_into_memorize(self):
        state = {"_last_thought_signature": b"sig"}
        _finalize_beat(
            self.runtime, _beat(state=state, node_def=_node_def(memorize=True)),
        )
        self.assertEqual(
            self.runtime._store_memory.call_args.kwargs["thought_signature"], b"sig",
        )

    def test_flows_into_important_dual_write(self):
        state = {"_last_thought_signature": b"sig"}
        _finalize_beat(
            self.runtime, _beat(state=state, node_def=_node_def(important=True)),
        )
        self.assertEqual(
            self.runtime._store_memory.call_args.kwargs["thought_signature"], b"sig",
        )

    def test_absent_signature_adds_no_key(self):
        state = {}
        _finalize_beat(self.runtime, _beat(state=state))
        self.assertNotIn("thought_signature", state["_messages"][-1])


class PulseContextTest(FinalizeTestBase):
    def test_prompt_and_response_are_both_appended(self):
        pulse_ctx = MagicMock()
        _finalize_beat(self.runtime, _beat(state={"_pulse_context": pulse_ctx}))
        roles = [c.args[0].role for c in pulse_ctx.append.call_args_list]
        self.assertEqual(roles, ["user", "assistant"])
        self.assertEqual(pulse_ctx.append.call_args_list[0].args[0].content, "action prompt")
        self.assertEqual(pulse_ctx.append.call_args_list[1].args[0].content, "response")

    def test_no_prompt_appends_response_only(self):
        pulse_ctx = MagicMock()
        _finalize_beat(
            self.runtime, _beat(state={"_pulse_context": pulse_ctx}, prompt=None),
        )
        roles = [c.args[0].role for c in pulse_ctx.append.call_args_list]
        self.assertEqual(roles, ["assistant"])

    def test_important_flag_is_carried_to_entry(self):
        pulse_ctx = MagicMock()
        _finalize_beat(self.runtime, _beat(
            state={"_pulse_context": pulse_ctx}, node_def=_node_def(important=True),
        ))
        self.assertTrue(pulse_ctx.append.call_args_list[-1].args[0].important)


class MemorizeTest(FinalizeTestBase):
    def test_no_memorize_config_stores_nothing(self):
        _finalize_beat(self.runtime, _beat(state={}))
        self.runtime._store_memory.assert_not_called()

    def test_stores_continuation_not_merged_text(self):
        """不変条件 3: SAIMemory へ行くのは最終発言のみ。

        merged 全文（spellResult の HTML 入り）を保存すると Building 履歴と
        内容重複 + HTML 混入になる（issue: spell_html_leak_into_saimemory）。"""
        _finalize_beat(self.runtime, _beat(
            state={}, node_def=_node_def(memorize=True), continuation="最終発言だけ",
        ))
        self.assertEqual(self.runtime._store_memory.call_args.args[1], "最終発言だけ")

    def test_tags_scope_and_line_role_from_dict_config(self):
        node_def = _node_def(memorize={
            "tags": ["internal"], "scope": "discardable", "line_role": "meta_judgment",
        })
        _finalize_beat(self.runtime, _beat(state={}, node_def=node_def))
        kwargs = self.runtime._store_memory.call_args.kwargs
        self.assertEqual(kwargs["tags"], ["internal"])
        self.assertEqual(kwargs["scope"], "discardable")
        self.assertEqual(kwargs["line_role"], "meta_judgment")

    def test_paired_action_text_dropped_when_spell_loop_ran(self):
        """spell 由来の記録は origin 側が action を持つので、ここでは付けない。"""
        _finalize_beat(self.runtime, _beat(
            state={"_spell_loop_origin_id": "origin-1", "_spell_loop_count": 2},
            node_def=_node_def(memorize=True),
        ))
        kwargs = self.runtime._store_memory.call_args.kwargs
        self.assertIsNone(kwargs["paired_action_text"])
        self.assertEqual(kwargs["spell_origin_id"], "origin-1")
        self.assertEqual(kwargs["spell_seq"], 3)

    def test_paired_action_text_kept_without_spell(self):
        _finalize_beat(self.runtime, _beat(state={}, node_def=_node_def(memorize=True)))
        kwargs = self.runtime._store_memory.call_args.kwargs
        self.assertEqual(kwargs["paired_action_text"], "action prompt")
        self.assertIsNone(kwargs["spell_origin_id"])
        self.assertIsNone(kwargs["spell_seq"])

    def test_reasoning_goes_into_memorize_metadata(self):
        _finalize_beat(self.runtime, _beat(
            state={"_reasoning_text": "考えた", "_reasoning_details": [{"d": 1}]},
            node_def=_node_def(memorize=True),
        ))
        self.assertEqual(self.runtime._store_memory.call_args.kwargs["metadata"], {
            "reasoning": "考えた", "reasoning_details": [{"d": 1}],
        })

    def test_structured_output_saved_as_indented_json(self):
        _finalize_beat(self.runtime, _beat(
            state={}, node_def=_node_def(memorize=True),
            continuation={"a": "あ"}, schema_consumed=True,
        ))
        self.assertEqual(self.runtime._store_memory.call_args.args[1], '{\n  "a": "あ"\n}')

    def test_error_placeholder_is_not_memorized(self):
        _finalize_beat(self.runtime, _beat(
            state={}, node_def=_node_def(memorize=True), continuation="(error in llm node)",
        ))
        self.runtime._store_memory.assert_not_called()

    def test_store_failure_emits_warning_event(self):
        self.runtime._store_memory.return_value = None
        events = []
        _finalize_beat(self.runtime, _beat(
            state={}, node_def=_node_def(memorize=True), event_callback=events.append,
        ))
        warnings = [e for e in events if e.get("type") == "warning"]
        self.assertEqual(warnings[0]["warning_code"], "memorize_failed")

    def test_activity_trace_appended_for_normal_playbooks(self):
        state = {"_activity_trace": []}
        _finalize_beat(self.runtime, _beat(state=state, node_def=_node_def(memorize=True)))
        self.assertEqual(state["_activity_trace"], [
            {"action": "memorize", "name": "n1", "playbook": "表示名"},
        ])

    def test_activity_trace_suppressed_for_meta_and_sub_playbooks(self):
        for pb_name in ("meta_judgment_running", "sub_worker"):
            with self.subTest(playbook=pb_name):
                state = {"_activity_trace": []}
                _finalize_beat(self.runtime, _beat(
                    state=state, node_def=_node_def(memorize=True),
                    playbook=_playbook(name=pb_name),
                ))
                self.assertEqual(state["_activity_trace"], [])


class ImportantDualWriteTest(FinalizeTestBase):
    def test_writes_conversation_tagged_message(self):
        _finalize_beat(self.runtime, _beat(state={}, node_def=_node_def(important=True)))
        self.assertEqual(self.runtime._store_memory.call_args.kwargs["tags"], ["conversation"])

    def test_skipped_when_memorize_already_ran(self):
        """memorize と dual-write は排他（同じ発言を 2 レコード残さない）。"""
        _finalize_beat(self.runtime, _beat(
            state={}, node_def=_node_def(important=True, memorize=True),
        ))
        self.assertEqual(self.runtime._store_memory.call_count, 1)
        self.assertEqual(self.runtime._store_memory.call_args.kwargs["tags"], [])

    def test_skipped_for_error_placeholder(self):
        _finalize_beat(self.runtime, _beat(
            state={}, node_def=_node_def(important=True),
            continuation="(error in llm node)",
        ))
        self.runtime._store_memory.assert_not_called()


class AlreadyStoredResponseTest(FinalizeTestBase):
    """既に保存済みの本文で来た Beat は、同じ行をもう一度書かない。

    出自: docs/issues/stamp_empty_continuation_double_save.md (2026-08-22 裁定)。
    504 で切れた応答の続きが空だった回、部分文は
    ``_respeak_after_stream_timeout`` の中で保存済みで、戻り値としても返る
    (本人が実際に言ったのは部分文だから)。印が無いと下流がもう一度保存し、
    本人が同じ言葉を二度言ったことになる。
    """

    def test_memorize_is_skipped_for_the_already_stored_text(self):
        state = {_ALREADY_STORED_STATE_KEY: "response"}
        _finalize_beat(self.runtime, _beat(
            state=state, node_def=_node_def(memorize=True),
        ))
        self.runtime._store_memory.assert_not_called()

    def test_important_dual_write_is_skipped_too(self):
        """memorize と同じ本文を書く隣の経路にも同じ歯止めが効く。"""
        state = {_ALREADY_STORED_STATE_KEY: "response"}
        _finalize_beat(self.runtime, _beat(
            state=state, node_def=_node_def(important=True),
        ))
        self.runtime._store_memory.assert_not_called()

    def test_a_different_text_is_still_saved(self):
        """印が古い / 本文が差し替わった回は通常どおり保存する (取りこぼさない)。"""
        state = {_ALREADY_STORED_STATE_KEY: "途中まで書い"}
        _finalize_beat(self.runtime, _beat(
            state=state, node_def=_node_def(memorize=True),
            continuation="別のノードの発言",
        ))
        self.runtime._store_memory.assert_called_once()

    def test_the_mark_is_consumed_so_the_next_beat_saves(self):
        """印は必ず消費する — 残すと次の Beat の保存を黙って止める。"""
        state = {_ALREADY_STORED_STATE_KEY: "response"}
        _finalize_beat(self.runtime, _beat(
            state=state, node_def=_node_def(memorize=True),
        ))
        self.assertNotIn(_ALREADY_STORED_STATE_KEY, state)

        self.runtime._store_memory.reset_mock()
        _finalize_beat(self.runtime, _beat(
            state=state, node_def=_node_def(memorize=True),
        ))
        self.runtime._store_memory.assert_called_once()


if __name__ == "__main__":
    unittest.main()
