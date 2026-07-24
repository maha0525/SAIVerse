"""runtime_llm.py Phase 0 で抽出した共有ヘルパの回帰固定。

分割設計書 `docs/issues/runtime_llm_node_split_design.md` §4 Phase 0。
巨大閉包 `node` の 4 生成経路に散っていた重複（使用量記帳 / reasoning 回収 /
message metadata 組み立て / _emit_say + message_id 捕捉）を関数化したもの。

ここで固定するのは **経路ごとの現状差**（llm_usage_total を載せない経路、
reasoning を載せない経路など）。Phase 1 以降の分割で経路が動いても、この差が
勝手に消えたり増えたりしないことを保証する。統一するかどうかは別判断。
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from sea.runtime_llm import (
    _build_say_metadata,
    _consume_reasoning,
    _emit_say_and_capture,
    _record_llm_usage,
    _store_reasoning_in_state,
)


def _fake_usage(**overrides):
    base = dict(
        model="test-model",
        input_tokens=100,
        output_tokens=20,
        cached_tokens=5,
        cache_write_tokens=3,
        cache_ttl="",
        cache_storage_tokens=0,
        cache_storage_ttl_seconds=0,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class BuildSayMetadataTest(unittest.TestCase):
    """metadata のキー構成と挿入順（= JSON 上の並び）を固定する。"""

    def _state(self):
        return {
            "_activity_trace": [{"kind": "tool", "name": "x"}],
            "_pulse_usage_accumulator": {"cost_usd": 0.5},
            "_auto_recall_text": "recalled",
        }

    def test_full_shape_key_order(self):
        """base → llm_usage → reasoning → reasoning_details → activity_trace
        → llm_usage_total → auto_recall の順（B-stream / B-sync / A-stream text）。"""
        meta = _build_say_metadata(
            self._state(),
            base_metadata={"media": ["a.png"]},
            llm_usage_metadata={"model": "test-model"},
            reasoning_text="thinking",
            reasoning_details=[{"t": 1}],
        )
        self.assertEqual(
            list(meta.keys()),
            [
                "media",
                "llm_usage",
                "reasoning",
                "reasoning_details",
                "activity_trace",
                "llm_usage_total",
                "auto_recall",
            ],
        )
        self.assertEqual(meta["activity_trace"], [{"kind": "tool", "name": "x"}])
        self.assertEqual(meta["llm_usage_total"], {"cost_usd": 0.5})

    def test_activity_trace_and_total_are_copied_not_shared(self):
        """state の可変オブジェクトを metadata がそのまま握らない（後段の追記で
        既存メッセージの metadata が変わってしまうのを防ぐ）。"""
        state = self._state()
        meta = _build_say_metadata(state)
        state["_activity_trace"].append({"kind": "tool", "name": "y"})
        state["_pulse_usage_accumulator"]["cost_usd"] = 99.0
        self.assertEqual(len(meta["activity_trace"]), 1)
        self.assertEqual(meta["llm_usage_total"], {"cost_usd": 0.5})

    def test_both_response_omits_usage_total(self):
        """A-stream の 'both' 経路だけ llm_usage_total を載せない（既存挙動）。"""
        meta = _build_say_metadata(
            self._state(),
            base_metadata={"media": ["a.png"]},
            llm_usage_metadata={"model": "test-model"},
            reasoning_text="thinking",
            include_total=False,
        )
        self.assertNotIn("llm_usage_total", meta)
        self.assertIn("llm_usage", meta)

    def test_tool_mode_spell_emit_shape(self):
        """A-common の spell emit は activity_trace + auto_recall だけ。

        A-sync 経由でもこの経路に来るため llm_usage_metadata を参照できない。"""
        meta = _build_say_metadata(self._state(), include_total=False)
        self.assertEqual(list(meta.keys()), ["activity_trace", "auto_recall"])

    def test_plain_stream_spell_emit_shape(self):
        """B-stream の spell emit は llm_usage を載せるが reasoning は載せない。"""
        meta = _build_say_metadata(
            self._state(), llm_usage_metadata={"model": "test-model"},
        )
        self.assertEqual(
            list(meta.keys()),
            ["llm_usage", "activity_trace", "llm_usage_total", "auto_recall"],
        )

    def test_falsy_values_are_omitted(self):
        meta = _build_say_metadata(
            {}, base_metadata=None, llm_usage_metadata=None, reasoning_text="",
        )
        self.assertEqual(meta, {})

    def test_reasoning_details_included_even_when_falsy(self):
        """reasoning_details は ``is not None`` 判定（空リストも載せる）。"""
        meta = _build_say_metadata({}, reasoning_details=[])
        self.assertEqual(meta, {"reasoning_details": []})

    def test_base_metadata_must_be_dict(self):
        meta = _build_say_metadata({}, base_metadata="not-a-dict")
        self.assertEqual(meta, {})

    def test_auto_recall_is_consumed(self):
        """auto_recall は pop。同一ノード実行内の 2 回目には付かない（既存挙動）。"""
        state = self._state()
        first = _build_say_metadata(state)
        second = _build_say_metadata(state)
        self.assertEqual(first["auto_recall"], "recalled")
        self.assertNotIn("auto_recall", second)
        self.assertNotIn("_auto_recall_text", state)


class ConsumeReasoningTest(unittest.TestCase):
    def _client(self, entries, details=None):
        client = MagicMock()
        client.consume_reasoning.return_value = entries
        client.consume_reasoning_details.return_value = details
        return client

    def test_joins_entries_with_blank_line(self):
        client = self._client([{"text": "a"}, {"text": ""}, {"text": "b"}])
        text, details = _consume_reasoning(client)
        self.assertEqual(text, "a\n\nb")
        self.assertIsNone(details)

    def test_empty_entries_yield_empty_text(self):
        self.assertEqual(_consume_reasoning(self._client(None))[0], "")
        self.assertEqual(_consume_reasoning(self._client([]))[0], "")

    def test_state_write_is_opt_in(self):
        """tool モードの 2 経路は即時格納、ツールなしは Spell ループ後に格納。"""
        state = {}
        _consume_reasoning(self._client([{"text": "a"}], details=[{"d": 1}]))
        self.assertEqual(state, {})

        _consume_reasoning(self._client([{"text": "a"}], details=[{"d": 1}]), state)
        self.assertEqual(state["_reasoning_text"], "a")
        self.assertEqual(state["_reasoning_details"], [{"d": 1}])

    def test_store_skips_empty_text_and_none_details(self):
        state = {}
        _store_reasoning_in_state(state, "", None)
        self.assertEqual(state, {})
        _store_reasoning_in_state(state, "x", [])
        self.assertEqual(state, {"_reasoning_text": "x", "_reasoning_details": []})


class RecordLlmUsageTest(unittest.TestCase):
    def setUp(self):
        self.runtime = MagicMock()
        self.persona = SimpleNamespace(persona_id="p1")
        self.state = {"_prefix_anchor_id": "msg-42"}

    def _client(self, usage):
        client = MagicMock()
        client.consume_usage.return_value = usage
        return client

    def test_no_usage_returns_none_without_side_effects(self):
        result = _record_llm_usage(
            self.runtime, self._client(None), self.persona, "b1",
            "pb", "llm", self.state,
        )
        self.assertIsNone(result)
        self.runtime._accumulate_usage.assert_not_called()
        self.runtime.session_lifecycle.touch_anchor_after_llm_call.assert_not_called()

    def test_records_accumulates_and_touches_anchor(self):
        usage = _fake_usage()
        with patch("sea.runtime_llm.get_usage_tracker") as get_tracker, \
                patch("saiverse.model_configs.calculate_cost", return_value=0.25), \
                patch("saiverse.model_configs.get_model_display_name", return_value="Test Model"):
            tracker = get_tracker.return_value
            meta = _record_llm_usage(
                self.runtime, self._client(usage), self.persona, "b1",
                "pb", "llm_tool_stream", self.state,
            )

        kwargs = tracker.record_usage.call_args.kwargs
        self.assertEqual(kwargs["model_id"], "test-model")
        self.assertEqual(kwargs["node_type"], "llm_tool_stream")
        self.assertEqual(kwargs["playbook_name"], "pb")
        self.assertEqual(kwargs["persona_id"], "p1")
        self.assertEqual(kwargs["building_id"], "b1")
        self.assertEqual(kwargs["category"], "persona_speak")

        self.assertEqual(meta, {
            "model": "test-model",
            "model_display_name": "Test Model",
            "input_tokens": 100,
            "output_tokens": 20,
            "cached_tokens": 5,
            "cache_write_tokens": 3,
            "cost_usd": 0.25,
        })

        self.runtime._accumulate_usage.assert_called_once_with(
            self.state, "test-model", 100, 20, 0.25, 5, 3,
        )
        # 不変条件 9: anchor touch は LLM 成功後、anchor は call-local
        self.runtime.session_lifecycle.touch_anchor_after_llm_call.assert_called_once_with(
            self.persona, usage, anchor_id="msg-42",
        )

    def test_anchor_touch_happens_after_usage_record(self):
        """記帳より先に anchor を進めない（prepare_context 側の先行 touch に戻さない）。"""
        calls = []
        self.runtime._accumulate_usage.side_effect = lambda *a, **k: calls.append("accumulate")
        self.runtime.session_lifecycle.touch_anchor_after_llm_call.side_effect = (
            lambda *a, **k: calls.append("touch")
        )
        with patch("sea.runtime_llm.get_usage_tracker") as get_tracker, \
                patch("saiverse.model_configs.calculate_cost", return_value=0.0), \
                patch("saiverse.model_configs.get_model_display_name", return_value="m"):
            get_tracker.return_value.record_usage.side_effect = (
                lambda *a, **k: calls.append("record")
            )
            _record_llm_usage(
                self.runtime, self._client(_fake_usage()), self.persona, "b1",
                "pb", "llm", self.state,
            )
        self.assertEqual(calls, ["record", "accumulate", "touch"])

    def test_cache_storage_recorded_when_present(self):
        usage = _fake_usage(cache_storage_tokens=1000, cache_storage_ttl_seconds=3600)
        with patch("sea.runtime_llm.get_usage_tracker") as get_tracker, \
                patch("saiverse.model_configs.calculate_cost", return_value=0.0), \
                patch("saiverse.model_configs.get_model_display_name", return_value="m"):
            tracker = get_tracker.return_value
            _record_llm_usage(
                self.runtime, self._client(usage), self.persona, "b1",
                "pb", "llm", self.state,
            )
        tracker.record_cache_storage.assert_called_once_with(
            model_id="test-model",
            cached_tokens=1000,
            ttl_seconds=3600,
            persona_id="p1",
            building_id="b1",
        )


class EmitSayAndCaptureTest(unittest.TestCase):
    def setUp(self):
        self.runtime = MagicMock()
        self.persona = SimpleNamespace(persona_id="p1")

    def test_captures_message_id(self):
        self.runtime._emit_say.return_value = {"message_id": 77}
        state = {}
        _emit_say_and_capture(
            self.runtime, self.persona, "b1", "hello", state,
            pulse_id="pulse-1", metadata={"k": "v"},
        )
        self.assertEqual(state["_last_message_id"], "77")
        self.runtime._emit_say.assert_called_once_with(
            self.persona, "b1", "hello", pulse_id="pulse-1", metadata={"k": "v"},
        )

    def test_empty_metadata_becomes_none(self):
        self.runtime._emit_say.return_value = None
        _emit_say_and_capture(
            self.runtime, self.persona, "b1", "hello", {},
            pulse_id=None, metadata={},
        )
        self.assertIsNone(self.runtime._emit_say.call_args.kwargs["metadata"])

    def test_non_dict_or_missing_id_leaves_state_untouched(self):
        for return_value in (None, "not-a-dict", {}, {"message_id": None}):
            with self.subTest(return_value=return_value):
                self.runtime._emit_say.return_value = return_value
                state = {}
                _emit_say_and_capture(
                    self.runtime, self.persona, "b1", "hello", state, pulse_id=None,
                )
                self.assertNotIn("_last_message_id", state)


if __name__ == "__main__":
    unittest.main()
