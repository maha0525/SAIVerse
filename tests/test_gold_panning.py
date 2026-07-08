"""gold_panning (砂金採り) のユニットテスト。

- ops 適用 (add / update / remove / add_scene) が実 SAIMemory (temp DB) に届くこと
- 採取ありは committed / 採取なしは discardable で判断ターンが記録されること
- scene のファジー照合 (表記揺れ許容 / 照合失敗の明示)
- LLM 例外が呼び出し側 (run_metabolism) の try/except で隔離されること
- defer-to-hot: anchor 冷で pending が立ち metabolism がスキップ / 圧力弁で実行

LLM はモック。SAIMemory は temp DB (test_core_memory_section と同じ Embedder patch)。
Windows の teardown OSError は許容する。
"""
from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sea import gold_panning


class DummyEmbedder:
    def __init__(self, model=None, **kwargs) -> None:
        self.model_name = model

    def embed(self, texts, **kwargs):
        return [[0.0] * 3 for _ in texts]


class FakeUsage(SimpleNamespace):
    pass


class FakeLLMClient:
    """generate が固定 dict/str を返す (または例外を投げる) 最小クライアント。"""

    def __init__(self, result, usage=None):
        self.result = result
        self._usage = usage
        self.calls = []

    def generate(self, messages, tools=None, response_schema=None, *, temperature=None, **kwargs):
        self.calls.append({
            "messages": list(messages),
            "response_schema": response_schema,
            "kwargs": kwargs,
        })
        if isinstance(self.result, Exception):
            raise self.result
        return self.result

    def consume_usage(self):
        return self._usage


class FakeRuntime:
    """run_gold_panning が触る SEARuntime の最小フェイク。"""

    def __init__(self, client):
        self.client = client
        self.touched = []

    def _prepare_context(self, persona, building_id, user_input, *args, **kwargs):
        return [{"role": "system", "content": "HEAD"}]

    def _select_llm_client(self, node_def, persona, needs_structured_output=False, state=None):
        return self.client

    def _default_temperature(self, persona):
        return 0.7

    def _get_cache_kwargs(self, persona_id=None):
        return {"enable_cache": True, "cache_ttl": "5m"}

    def _touch_anchor_after_llm_call(self, persona, usage):
        self.touched.append(usage)


def _read_gold_record(adapter):
    """gold_panning タグの判断ターンを 1 件読む (content, scope, line_role)。"""
    row = adapter.conn.execute(
        "SELECT content, scope, line_role FROM messages "
        "WHERE metadata LIKE '%gold_panning%' ORDER BY rowid DESC LIMIT 1"
    ).fetchone()
    return row


def _read_gold_record_full(adapter):
    """gold_panning タグの判断ターンを 1 件読む (role, content, metadata)。"""
    row = adapter.conn.execute(
        "SELECT role, content, metadata FROM messages "
        "WHERE metadata LIKE '%gold_panning%' ORDER BY rowid DESC LIMIT 1"
    ).fetchone()
    return row


class GoldPanningRunTest(unittest.TestCase):
    """実 SAIMemory (temp DB) 経由で ops 適用と判断ターン記録を実証する。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.persona_path = Path(self._tmp.name) / "personas" / "tester"
        self.persona_path.mkdir(parents=True, exist_ok=True)
        os.environ["SAIMEMORY_MEMORY"] = "1"
        self.addCleanup(self._cleanup_temp)

        patcher = patch("saiverse_memory.adapter.Embedder", DummyEmbedder)
        self.addCleanup(patcher.stop)
        patcher.start()

        from saiverse_memory import SAIMemoryAdapter
        self.adapter = SAIMemoryAdapter("tester", persona_dir=self.persona_path, resource_id="tester")
        self.addCleanup(self._close_adapter)

    def _close_adapter(self):
        try:
            self.adapter.close()
        except Exception:
            pass

    def _cleanup_temp(self):
        import gc
        gc.collect()
        try:
            self._tmp.cleanup()
        except (PermissionError, OSError):
            pass

    def tearDown(self):
        os.environ.pop("SAIMEMORY_MEMORY", None)

    def _persona(self):
        return SimpleNamespace(
            persona_id="tester", persona_name="エア", model="claude-x",
            sai_memory=self.adapter,
        )

    def _run(self, result, current_messages=None, evict_count=0, event_callback=None):
        client = FakeLLMClient(result)
        lifecycle = SimpleNamespace(runtime=FakeRuntime(client))
        return gold_panning.run_gold_panning(
            lifecycle, self._persona(), "b",
            current_messages or [], evict_count, event_callback,
        ), client

    def _list_core(self):
        from sai_memory.core_memory import list_core_memories
        with self.adapter._db_lock:
            return list_core_memories(self.adapter.conn)

    def _add_conversation(self, pairs):
        """(role, content) の列を persona スレッドに投入し、id 付き dict 列を返す。"""
        msgs = []
        for role, content in pairs:
            mid = self.adapter.append_persona_message({
                "role": role,
                "content": content,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })
            msgs.append({"id": mid, "role": role, "content": content})
        return msgs

    # -- case 1: add op --------------------------------------------------

    def test_add_op_writes_core_memory_and_committed_record(self):
        result = {
            "reflection": "赴任のことを覚えておく",
            "ops": [{"op": "add", "content": "2026年6月頃〜 まはーは海外赴任中（9月帰国予定）"}],
        }
        summary, client = self._run(result)
        self.assertEqual(summary["ops_applied"], 1)
        self.assertEqual(summary["ops_failed"], 0)
        self.assertFalse(summary["skipped"])

        # structured output 用に response_schema が渡っている
        self.assertIsNotNone(client.calls[0]["response_schema"])

        cores = self._list_core()
        self.assertEqual(len(cores), 1)
        self.assertIn("海外赴任中", cores[0].content)

        row = _read_gold_record(self.adapter)
        self.assertIsNotNone(row)
        content, scope, line_role = row
        self.assertEqual(scope, "committed")
        self.assertEqual(line_role, "main_line")
        self.assertIn("赴任のことを覚えておく", content)
        self.assertIn(f"c:{cores[0].id}", content)

    # -- case 1b: 記録は event_message 形式のシステム通知 (role=user) ------

    def test_record_is_event_message_system_narration(self):
        import json as _json

        result = {
            "reflection": "赴任のことを覚えておく",
            "ops": [{"op": "add", "content": "2026年6月頃〜 まはーは海外赴任中"}],
        }
        self._run(result)

        role, content, metadata = _read_gold_record_full(self.adapter)
        # プロンプト無し assistant 発話ではなく <system> 包みのナレーション。
        self.assertEqual(role, "user")
        self.assertTrue(content.startswith("<system>"))
        self.assertTrue(content.rstrip().endswith("</system>"))
        self.assertIn("記憶整理の節目 — コア記憶の採取判断:", content)
        # reflection は persona_name プレフィックス付きで載る (全文、省略なし)。
        self.assertIn("エアの判断: 赴任のことを覚えておく", content)
        # タグは internal / event_message / gold_panning の 3 つ。
        tags = _json.loads(metadata)["tags"]
        self.assertIn("internal", tags)
        self.assertIn("event_message", tags)
        self.assertIn("gold_panning", tags)

    # -- case 2: empty ops -> discardable --------------------------------

    def test_empty_ops_writes_nothing_and_discardable_record(self):
        result = {"reflection": "今回は採取なし", "ops": []}
        summary, _ = self._run(result)
        self.assertEqual(summary["ops_applied"], 0)
        self.assertEqual(self._list_core(), [])

        row = _read_gold_record(self.adapter)
        self.assertIsNotNone(row)
        _content, scope, _line_role = row
        self.assertEqual(scope, "discardable")

    # -- case 3a: update op ----------------------------------------------

    def test_update_op(self):
        from sai_memory.core_memory import add_core_memory
        with self.adapter._db_lock:
            mid = add_core_memory(self.adapter.conn, "旧: 赴任は3月まで")

        result = {"reflection": "更新", "ops": [
            {"op": "update", "memory_id": mid, "content": "新: 赴任は9月まで"},
        ]}
        summary, _ = self._run(result)
        self.assertEqual(summary["ops_applied"], 1)
        cores = self._list_core()
        self.assertEqual(len(cores), 1)
        self.assertEqual(cores[0].content, "新: 赴任は9月まで")

    def test_update_op_missing_target_is_failure(self):
        result = {"reflection": "x", "ops": [
            {"op": "update", "memory_id": 999, "content": "存在しない対象"},
        ]}
        summary, _ = self._run(result)
        self.assertEqual(summary["ops_applied"], 0)
        self.assertEqual(summary["ops_failed"], 1)
        row = _read_gold_record(self.adapter)
        self.assertIn("update 失敗", row[0])
        self.assertEqual(row[1], "discardable")  # 採取なし

    # -- case 3b: remove op ----------------------------------------------

    def test_remove_op(self):
        from sai_memory.core_memory import add_core_memory
        with self.adapter._db_lock:
            mid = add_core_memory(self.adapter.conn, "消す予定のメモ")

        result = {"reflection": "整理", "ops": [{"op": "remove", "memory_id": mid}]}
        summary, _ = self._run(result)
        self.assertEqual(summary["ops_applied"], 1)
        self.assertEqual(self._list_core(), [])

    # -- case 4: add_scene (match success / miss failure) ----------------

    def test_add_scene_exact_match_success(self):
        msgs = self._add_conversation([
            ("user", "海外赴任は九月まで続くんだ、時差があるから気をつけて"),
            ("model", "うん、体調に気をつけてね"),
        ])
        result = {"reflection": "この場面を残す", "ops": [
            {"op": "add_scene", "quote": "海外赴任は九月まで続くんだ、時差があるから気をつけて", "rounds": 1},
        ]}
        summary, _ = self._run(result, current_messages=msgs, evict_count=2)
        self.assertEqual(summary["ops_applied"], 1)

        cores = self._list_core()
        scene = [c for c in cores if c.kind == "scene"]
        self.assertEqual(len(scene), 1)

        row = _read_gold_record(self.adapter)
        self.assertEqual(row[1], "committed")
        self.assertIn("会話の記憶", row[0])

    def test_add_scene_missing_quote_is_failure_and_explicit(self):
        msgs = self._add_conversation([
            ("user", "今日はいい天気だね"),
            ("model", "そうだね、散歩日和だ"),
        ])
        result = {"reflection": "残したい", "ops": [
            {"op": "add_scene", "quote": "この発言はどこにも存在しない架空の引用文です", "rounds": 1},
        ]}
        summary, _ = self._run(result, current_messages=msgs, evict_count=2)
        self.assertEqual(summary["ops_applied"], 0)
        self.assertEqual(summary["ops_failed"], 1)
        # scene の失敗は黙って捨てず記録テキストに明示される (intent §5-4)
        row = _read_gold_record(self.adapter)
        self.assertIn("scene 照合失敗", row[0])

    # -- case: str fallback (parse failure) ------------------------------

    def test_str_result_non_json_is_no_op_with_reflection(self):
        summary, _ = self._run("これはJSONではない自由文の応答")
        self.assertEqual(summary["ops_applied"], 0)
        self.assertEqual(self._list_core(), [])
        row = _read_gold_record(self.adapter)
        self.assertIn("これはJSONではない", row[0])
        self.assertEqual(row[1], "discardable")

    # -- case: usage recording + anchor touch ----------------------------

    def test_usage_triggers_anchor_touch(self):
        result = {"reflection": "x", "ops": []}
        usage = FakeUsage(
            model="claude-x", input_tokens=100, output_tokens=5,
            cached_tokens=90, cache_write_tokens=10, cache_ttl="5m",
        )
        client = FakeLLMClient(result, usage=usage)
        runtime = FakeRuntime(client)
        lifecycle = SimpleNamespace(runtime=runtime)
        with patch("saiverse.usage_tracker.get_usage_tracker") as get_tracker:
            gold_panning.run_gold_panning(lifecycle, self._persona(), "b", [], 0)
        get_tracker.return_value.record_usage.assert_called_once()
        self.assertEqual(len(runtime.touched), 1)

    # -- case: disabled toggle -------------------------------------------

    def test_disabled_toggle_skips(self):
        with patch.dict(os.environ, {"SAIVERSE_GOLD_PANNING_ENABLED": "0"}):
            summary, client = self._run({"reflection": "x", "ops": [
                {"op": "add", "content": "刻まれないはず"},
            ]})
        self.assertTrue(summary["skipped"])
        self.assertEqual(summary["reason"], "disabled")
        self.assertEqual(client.calls, [])  # LLM 呼び出しすら起きない
        self.assertEqual(self._list_core(), [])


class ResolveQuoteTest(unittest.TestCase):
    """scene のファジー照合 (_resolve_quote) 単体。"""

    def _msgs(self):
        return [
            {"id": "m1", "role": "user", "content": "ABCの前置き"},
            {"id": "m2", "role": "user", "content": "海外赴任は九月まで続くのだと彼は言った"},
            {"id": "m3", "role": "model", "content": "了解、気をつけてね"},
        ]

    def test_stage1_substring_with_whitespace_and_width_variation(self):
        # 全角スペース + 半角/全角の揺れが NFKC + 空白圧縮で吸収され、第1段一致する。
        quote = "海外赴任は九月　まで続くのだ"
        mid = gold_panning._resolve_quote(quote, self._msgs(), min_quote_chars=10)
        self.assertEqual(mid, "m2")

    def test_stage1_latest_wins_on_multiple_hits(self):
        msgs = [
            {"id": "a", "content": "共通のフレーズを含む文章その1"},
            {"id": "b", "content": "共通のフレーズを含む文章その2"},
        ]
        mid = gold_panning._resolve_quote("共通のフレーズを含む", msgs, min_quote_chars=5)
        self.assertEqual(mid, "b")  # 最後 (最新) を採用

    def test_short_quote_is_rejected(self):
        msgs = [{"id": "m1", "content": "短い"}]
        self.assertIsNone(gold_panning._resolve_quote("短い", msgs, min_quote_chars=10))

    def test_no_match_returns_none(self):
        self.assertIsNone(
            gold_panning._resolve_quote(
                "どこにも存在しない完全に無関係な長い文字列", self._msgs(), min_quote_chars=10,
            )
        )

    def test_stage2_fuzzy_partial_match(self):
        # 1 文字だけ違う (誤字) を第2段の部分一致比率で拾う。
        msgs = [{"id": "x", "content": "本日の会議は午後三時から開始する予定です"}]
        quote = "本日の会議は午後三時から開始する予定でず"  # 末尾 1 字違い
        mid = gold_panning._resolve_quote(quote, msgs, min_quote_chars=10)
        self.assertEqual(mid, "x")


class DeferToHotTest(unittest.TestCase):
    """SessionLifecycle.maybe_run_metabolism の defer-to-hot をスタブ化して検証する。"""

    def _make_lifecycle(self, *, hot, messages_count):
        from sea.session_lifecycle import SessionLifecycle

        manager = SimpleNamespace(metabolism_enabled=True, max_history_messages_override=None,
                                  metabolism_keep_messages_override=None)
        runtime = SimpleNamespace()
        lifecycle = SessionLifecycle(runtime, manager)

        history_mgr = SimpleNamespace(metabolism_anchor_message_id="anchor")
        messages = [{"id": f"m{i}", "content": "x"} for i in range(messages_count)]
        history_mgr.get_history_from_anchor = (
            lambda anchor, required_line_roles=None, required_scopes=None: messages
        )
        persona = SimpleNamespace(persona_id="tester", model="claude-x", history_manager=history_mgr)

        # 依存メソッドをスタブ (watermark / hot 判定 / 実行)。
        lifecycle.get_high_watermark = lambda p: 20
        lifecycle.get_low_watermark = lambda p: 10
        lifecycle._is_cache_hot = lambda p: hot
        ran = []
        lifecycle.run_metabolism = lambda p, b, msgs, keep, cb=None: ran.append((len(msgs), keep))
        return lifecycle, persona, ran

    def test_cold_defers_and_sets_pending(self):
        # cold + high_wm(20) < count(25) <= cap(20*1.5=30) → 繰り延べ
        lifecycle, persona, ran = self._make_lifecycle(hot=False, messages_count=25)
        lifecycle.maybe_run_metabolism(persona, "b", None)
        self.assertEqual(ran, [])  # metabolism は走らない
        self.assertTrue(getattr(persona, "_metabolism_pending", False))

    def test_pressure_valve_runs_cold(self):
        # cold だが count(40) > cap(30) → 圧力弁でコールド実行
        lifecycle, persona, ran = self._make_lifecycle(hot=False, messages_count=40)
        lifecycle.maybe_run_metabolism(persona, "b", None)
        self.assertEqual(len(ran), 1)
        self.assertFalse(getattr(persona, "_metabolism_pending", False))

    def test_hot_runs_immediately(self):
        lifecycle, persona, ran = self._make_lifecycle(hot=True, messages_count=25)
        lifecycle.maybe_run_metabolism(persona, "b", None)
        self.assertEqual(len(ran), 1)

    def test_pending_flag_resumes_run(self):
        # 熱くなった後、pending が should_run を立てて消化される
        lifecycle, persona, ran = self._make_lifecycle(hot=True, messages_count=25)
        persona._metabolism_pending = True
        lifecycle.maybe_run_metabolism(persona, "b", None)
        self.assertEqual(len(ran), 1)
        self.assertFalse(persona._metabolism_pending)


class LlmFailureIsolationTest(unittest.TestCase):
    """LLM 例外が run_metabolism の try/except で隔離され、アンカー更新が続くこと。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.persona_path = Path(self._tmp.name) / "personas" / "tester"
        self.persona_path.mkdir(parents=True, exist_ok=True)
        os.environ["SAIMEMORY_MEMORY"] = "1"
        os.environ.pop("ENABLE_MEMORY_WEAVE_CONTEXT", None)
        self.addCleanup(self._cleanup_temp)

        patcher = patch("saiverse_memory.adapter.Embedder", DummyEmbedder)
        self.addCleanup(patcher.stop)
        patcher.start()

        from saiverse_memory import SAIMemoryAdapter
        self.adapter = SAIMemoryAdapter("tester", persona_dir=self.persona_path, resource_id="tester")
        self.addCleanup(self._close_adapter)

    def _close_adapter(self):
        try:
            self.adapter.close()
        except Exception:
            pass

    def _cleanup_temp(self):
        import gc
        gc.collect()
        try:
            self._tmp.cleanup()
        except (PermissionError, OSError):
            pass

    def tearDown(self):
        os.environ.pop("SAIMEMORY_MEMORY", None)

    def test_llm_exception_propagates_out_of_run_gold_panning(self):
        # 隔離は run_gold_panning ではなく呼び出し側 (run_metabolism) の責務。
        client = FakeLLMClient(RuntimeError("boom"))
        lifecycle = SimpleNamespace(runtime=FakeRuntime(client))
        persona = SimpleNamespace(
            persona_id="tester", persona_name="エア", model="claude-x", sai_memory=self.adapter,
        )
        with self.assertRaises(RuntimeError):
            gold_panning.run_gold_panning(lifecycle, persona, "b", [], 0)

    def test_run_metabolism_isolates_llm_failure_and_updates_anchor(self):
        from sea.session_lifecycle import SessionLifecycle

        client = FakeLLMClient(RuntimeError("boom"))
        runtime = FakeRuntime(client)
        manager = SimpleNamespace()
        lifecycle = SessionLifecycle(runtime, manager)

        # 重い経路をスタブ (Chronicle / embedding / anchor 永続化 / dynamic state)。
        lifecycle.is_chronicle_enabled_for_persona = lambda p: False
        lifecycle.ensure_recall_embeddings = lambda p: None
        anchor_updates = []
        lifecycle.update_anchor_for_model = lambda p, m, aid, ttl=None: anchor_updates.append(aid)

        history_mgr = SimpleNamespace(metabolism_anchor_message_id="old")
        persona = SimpleNamespace(
            persona_id="tester", persona_name="エア", model="claude-x",
            sai_memory=self.adapter, history_manager=history_mgr,
        )

        current_messages = [{"id": f"m{i}", "content": "x"} for i in range(5)]
        # keep_count=2 → evict_count=3 → new anchor = m3
        with patch("saiverse.dynamic_state.DynamicStateManager.on_metabolism", lambda *a, **k: None):
            lifecycle.run_metabolism(persona, "b", current_messages, 2, None)

        # gold_panning が LLM 例外で死んでも anchor 更新は実行される (失敗隔離)。
        self.assertEqual(history_mgr.metabolism_anchor_message_id, "m3")
        self.assertEqual(anchor_updates, ["m3"])


class SessionCloseTest(unittest.TestCase):
    """gold_panning.run_session_close (Phase 3) を実 SAIMemory (temp DB) で検証する。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.persona_path = Path(self._tmp.name) / "personas" / "tester"
        self.persona_path.mkdir(parents=True, exist_ok=True)
        os.environ["SAIMEMORY_MEMORY"] = "1"
        os.environ.pop("ENABLE_MEMORY_WEAVE_CONTEXT", None)
        self.addCleanup(self._cleanup_temp)

        patcher = patch("saiverse_memory.adapter.Embedder", DummyEmbedder)
        self.addCleanup(patcher.stop)
        patcher.start()

        from saiverse_memory import SAIMemoryAdapter
        self.adapter = SAIMemoryAdapter("tester", persona_dir=self.persona_path, resource_id="tester")
        self.addCleanup(self._close_adapter)

    def _close_adapter(self):
        try:
            self.adapter.close()
        except Exception:
            pass

    def _cleanup_temp(self):
        import gc
        gc.collect()
        try:
            self._tmp.cleanup()
        except (PermissionError, OSError):
            pass

    def tearDown(self):
        os.environ.pop("SAIMEMORY_MEMORY", None)

    # -- fixtures -------------------------------------------------------

    def _make_lifecycle(self, client, *, hot, chronicle_enabled=True):
        from unittest.mock import MagicMock

        lifecycle = SimpleNamespace(runtime=FakeRuntime(client))
        lifecycle.is_chronicle_enabled_for_persona = lambda p: chronicle_enabled
        lifecycle.generate_chronicle = MagicMock()
        lifecycle.generate_track_chronicle = MagicMock()
        lifecycle.ensure_recall_embeddings = MagicMock()
        lifecycle._is_cache_hot = lambda p: hot
        return lifecycle

    def _make_persona(self, messages, *, last_pan_id=None, building_id="b"):
        history_mgr = SimpleNamespace(metabolism_anchor_message_id="anchor")
        history_mgr.get_history_from_anchor = (
            lambda anchor, required_line_roles=None, required_scopes=None: messages
        )
        persona = SimpleNamespace(
            persona_id="tester", persona_name="エア", model="claude-x",
            sai_memory=self.adapter, history_manager=history_mgr,
            current_building_id=building_id,
        )
        if last_pan_id is not None:
            persona._gold_panning_last_pan_id = last_pan_id
        return persona

    @staticmethod
    def _msgs(n):
        return [{"id": f"m{i}", "role": "user", "content": f"msg {i}"} for i in range(n)]

    # -- case 1: marker guard skips pan but Chronicle runs --------------

    def test_marker_guard_skips_pan_but_chronicle_runs(self):
        msgs = self._msgs(12)
        client = FakeLLMClient({"reflection": "x", "ops": []})
        lifecycle = self._make_lifecycle(client, hot=True)
        # marker が最新 id と一致 → 新規 0 件 → 採取スキップ。
        persona = self._make_persona(msgs, last_pan_id="m11")
        with patch.dict(os.environ, {"ENABLE_MEMORY_WEAVE_CONTEXT": "true"}), \
                patch("sea.gold_panning.run_gold_panning") as pan:
            result = gold_panning.run_session_close(lifecycle, persona)
        pan.assert_not_called()
        lifecycle.generate_chronicle.assert_called_once()
        self.assertFalse(result["panned"])
        self.assertTrue(result["chronicle"])
        self.assertEqual(result["skipped_reason"], "below_min")

    # -- case 2: enough new + hot -> pan runs, marker updates ------------

    def test_new_messages_hot_pans_and_updates_marker(self):
        msgs = self._msgs(5)
        client = FakeLLMClient({"reflection": "採取なし", "ops": []})
        lifecycle = self._make_lifecycle(client, hot=True)
        persona = self._make_persona(msgs, last_pan_id=None)
        with patch.dict(os.environ, {"SAIVERSE_GOLD_PANNING_CLOSE_MIN_MESSAGES": "3"}):
            result = gold_panning.run_session_close(lifecycle, persona)
        self.assertTrue(result["panned"])
        # 実 run_gold_panning が LLM を 1 回呼んでいる。
        self.assertEqual(len(client.calls), 1)
        # マーカーが窓の末尾 id に更新される。
        self.assertEqual(persona._gold_panning_last_pan_id, "m4")

    # -- case 3: cold -> pan skipped, Chronicle still runs --------------

    def test_cold_skips_pan_but_runs_chronicle(self):
        msgs = self._msgs(5)
        client = FakeLLMClient({"reflection": "x", "ops": []})
        lifecycle = self._make_lifecycle(client, hot=False)
        persona = self._make_persona(msgs, last_pan_id=None)
        with patch.dict(os.environ, {
            "ENABLE_MEMORY_WEAVE_CONTEXT": "true",
            "SAIVERSE_GOLD_PANNING_CLOSE_MIN_MESSAGES": "3",
        }), patch("sea.gold_panning.run_gold_panning") as pan:
            result = gold_panning.run_session_close(lifecycle, persona)
        pan.assert_not_called()
        lifecycle.generate_chronicle.assert_called_once()
        self.assertFalse(result["panned"])
        self.assertTrue(result["chronicle"])
        self.assertEqual(result["skipped_reason"], "cold")

    # -- case 4: in-flight guard + flag reset ---------------------------

    def test_inflight_guard_and_flag_reset(self):
        msgs = self._msgs(5)
        client = FakeLLMClient({"reflection": "x", "ops": []})

        # (a) flag が立っていると再入は skip され、何も走らない。
        busy = self._make_persona(msgs, last_pan_id=None)
        busy._gold_panning_close_inflight = True
        lifecycle_a = self._make_lifecycle(client, hot=True)
        with patch.dict(os.environ, {"ENABLE_MEMORY_WEAVE_CONTEXT": "true"}), \
                patch("sea.gold_panning.run_gold_panning") as pan:
            result = gold_panning.run_session_close(lifecycle_a, busy)
        self.assertEqual(result["skipped_reason"], "inflight")
        pan.assert_not_called()
        lifecycle_a.generate_chronicle.assert_not_called()

        # (b) 正常終了後は flag が False に戻る。
        fresh = self._make_persona(msgs, last_pan_id=None)
        lifecycle_b = self._make_lifecycle(client, hot=True)
        with patch.dict(os.environ, {"SAIVERSE_GOLD_PANNING_CLOSE_MIN_MESSAGES": "3"}), \
                patch("sea.gold_panning.run_gold_panning"):
            gold_panning.run_session_close(lifecycle_b, fresh)
        self.assertFalse(getattr(fresh, "_gold_panning_close_inflight", True))

    # -- case 5: chain termination (2nd call pans nothing) --------------

    def test_chain_terminates_on_second_call(self):
        msgs = self._msgs(5)
        client = FakeLLMClient({"reflection": "x", "ops": []})
        lifecycle = self._make_lifecycle(client, hot=True)
        persona = self._make_persona(msgs, last_pan_id=None)
        with patch.dict(os.environ, {"SAIVERSE_GOLD_PANNING_CLOSE_MIN_MESSAGES": "3"}):
            first = gold_panning.run_session_close(lifecycle, persona)
            second = gold_panning.run_session_close(lifecycle, persona)
        self.assertTrue(first["panned"])
        self.assertFalse(second["panned"])
        self.assertEqual(second["skipped_reason"], "below_min")
        # 追加 LLM コールが起きない = 連鎖はちょうど 1 回で止まる。
        self.assertEqual(len(client.calls), 1)

    # -- case 7: weave disabled -> Chronicle not generated --------------

    def test_weave_disabled_skips_chronicle(self):
        msgs = self._msgs(5)
        client = FakeLLMClient({"reflection": "x", "ops": []})
        lifecycle = self._make_lifecycle(client, hot=True)
        persona = self._make_persona(msgs, last_pan_id=None)
        with patch.dict(os.environ, {
            "ENABLE_MEMORY_WEAVE_CONTEXT": "false",
            "SAIVERSE_GOLD_PANNING_CLOSE_MIN_MESSAGES": "3",
        }), patch("sea.gold_panning.run_gold_panning"):
            result = gold_panning.run_session_close(lifecycle, persona)
        lifecycle.generate_chronicle.assert_not_called()
        lifecycle.generate_track_chronicle.assert_not_called()
        self.assertFalse(result["chronicle"])


class KeepaliveSessionCloseHookTest(unittest.TestCase):
    """run_cache_keepalive の not-Active 分岐がセッションクローズを spawn すること。"""

    def test_not_active_branch_spawns_session_close(self):
        from sea.runtime import SEARuntime

        persona = SimpleNamespace(activity_state="Idle")
        rt = SimpleNamespace(manager=SimpleNamespace(personas={"tester": persona}))
        spawned = []
        rt._spawn_session_close = lambda pid: spawned.append(pid)
        rt.run_cache_keepalive = SEARuntime.run_cache_keepalive.__get__(rt)

        result = rt.run_cache_keepalive("tester")

        self.assertFalse(result)
        self.assertEqual(spawned, ["tester"])


class SessionWatchdogScheduleTest(unittest.TestCase):
    """schedule_cache_ttl_pulse の見張り一般化 (非 explicit でも予約する)。

    explicit (Anthropic) は従来どおり keep-alive を予約し、非 explicit
    (gemini_explicit / implicit) はセッション見張りを予約する。
    """

    def _make_lifecycle(self, scheduled, ttl=1200, threshold=0.3):
        from sea.session_lifecycle import SessionLifecycle

        scheduler = SimpleNamespace(
            schedule=lambda fire_at, callback, key: scheduled.append((fire_at, callback, key)),
            cancel=lambda key: scheduled.append(("cancel", key)),
        )
        meta_layer = SimpleNamespace(
            _load_judgment_config=lambda persona: {
                "cache_threshold_ratio": threshold,
                "keep_cache_alive": True,
            }
        )
        manager = SimpleNamespace(event_scheduler=scheduler, meta_layer=meta_layer)
        lc = SimpleNamespace(manager=manager, runtime=SimpleNamespace())
        lc.get_anchor_validity_seconds = lambda model_key, persona_id=None: ttl
        lc._schedule_session_watchdog = SessionLifecycle._schedule_session_watchdog.__get__(lc)
        lc.schedule_cache_ttl_pulse = SessionLifecycle.schedule_cache_ttl_pulse.__get__(lc)
        return lc

    def test_non_explicit_schedules_watchdog(self):
        scheduled = []
        lc = self._make_lifecycle(scheduled, ttl=1200, threshold=0.3)
        persona = SimpleNamespace(persona_id="air", model="gem")
        before = datetime.now()
        lc.schedule_cache_ttl_pulse(persona, "gem", "gemini_explicit")
        self.assertEqual(len(scheduled), 1)
        fire_at, _callback, key = scheduled[0]
        self.assertEqual(key, "ttl:air")
        # anchor validity 1200 × (1 - 0.3) = 840s
        self.assertAlmostEqual((fire_at - before).total_seconds(), 840, delta=5)

    def test_non_explicit_skipped_when_gold_panning_disabled(self):
        scheduled = []
        lc = self._make_lifecycle(scheduled)
        persona = SimpleNamespace(persona_id="air", model="gem")
        with patch.dict(os.environ, {"SAIVERSE_GOLD_PANNING_ENABLED": "0"}):
            lc.schedule_cache_ttl_pulse(persona, "gem", "gemini_explicit")
        self.assertEqual(scheduled, [])

    def test_explicit_schedules_regardless_of_gold_panning_flag(self):
        """explicit (Anthropic) の keep-alive 予約は gold_panning フラグに影響されない。"""
        scheduled = []
        lc = self._make_lifecycle(scheduled, ttl=3600, threshold=0.3)
        persona = SimpleNamespace(persona_id="air", model="claude-x")
        with patch.dict(os.environ, {"SAIVERSE_GOLD_PANNING_ENABLED": "0"}):
            lc.schedule_cache_ttl_pulse(persona, "claude-x", "explicit")
        self.assertEqual(len(scheduled), 1)
        fire_at, _callback, key = scheduled[0]
        self.assertEqual(key, "ttl:air")

    def test_explicit_keep_cache_alive_false_cancels(self):
        """explicit の keep_cache_alive=False ゲートは無変更 (見張りには波及しない)。"""
        from sea.session_lifecycle import SessionLifecycle

        scheduled = []
        scheduler = SimpleNamespace(
            schedule=lambda fire_at, callback, key: scheduled.append(("schedule", key)),
            cancel=lambda key: scheduled.append(("cancel", key)),
        )
        meta_layer = SimpleNamespace(
            _load_judgment_config=lambda persona: {
                "cache_threshold_ratio": 0.3,
                "keep_cache_alive": False,
            }
        )
        manager = SimpleNamespace(event_scheduler=scheduler, meta_layer=meta_layer)
        lc = SimpleNamespace(manager=manager, runtime=SimpleNamespace())
        lc.get_anchor_validity_seconds = lambda model_key, persona_id=None: 3600
        lc.schedule_cache_ttl_pulse = SessionLifecycle.schedule_cache_ttl_pulse.__get__(lc)
        persona = SimpleNamespace(persona_id="air", model="claude-x")
        lc.schedule_cache_ttl_pulse(persona, "claude-x", "explicit")
        self.assertEqual(scheduled, [("cancel", "ttl:air")])


class KeepaliveNonExplicitBranchTest(unittest.TestCase):
    """run_cache_keepalive: Active + 非 explicit は LLM を呼ばず見張りだけ再予約。"""

    def test_active_non_explicit_reschedules_without_llm(self):
        from sea.runtime import SEARuntime

        persona = SimpleNamespace(activity_state="Active", model="gem")
        rescheduled = []
        session_lifecycle = SimpleNamespace(
            schedule_cache_ttl_pulse=lambda p, mk, ct: rescheduled.append((mk, ct)),
        )
        rt = SimpleNamespace(
            manager=SimpleNamespace(personas={"air": persona}),
            session_lifecycle=session_lifecycle,
        )
        rt.run_cache_keepalive = SEARuntime.run_cache_keepalive.__get__(rt)

        # get_cache_config を gemini_explicit にすると LLM 経路 (_prepare_context 等)
        # に入らず見張り再予約で return False するはず。rt にそれらのメソッドを
        # 与えていないので、もし到達したら AttributeError で顕在化する。
        with patch("saiverse.model_configs.get_cache_config", return_value={"type": "gemini_explicit"}):
            result = rt.run_cache_keepalive("air")

        self.assertFalse(result)
        self.assertEqual(rescheduled, [("gem", "gemini_explicit")])

    def test_not_active_non_explicit_spawns_session_close(self):
        """not-Active 分岐は cache 型非依存でクローズ spawn (gemini でも同じ)。"""
        from sea.runtime import SEARuntime

        persona = SimpleNamespace(activity_state="Idle", model="gem")
        rt = SimpleNamespace(manager=SimpleNamespace(personas={"air": persona}))
        spawned = []
        rt._spawn_session_close = lambda pid: spawned.append(pid)
        rt.run_cache_keepalive = SEARuntime.run_cache_keepalive.__get__(rt)

        result = rt.run_cache_keepalive("air")

        self.assertFalse(result)
        self.assertEqual(spawned, ["air"])


class PanMarkerPersistenceTest(unittest.TestCase):
    """pan マーカーが memory.db (embed_metadata KV) に永続化され、属性キャッシュを
    持たない新 persona オブジェクト (プロセス再起動相当) でもガードが効くこと。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.persona_path = Path(self._tmp.name) / "personas" / "tester"
        self.persona_path.mkdir(parents=True, exist_ok=True)
        os.environ["SAIMEMORY_MEMORY"] = "1"
        os.environ.pop("ENABLE_MEMORY_WEAVE_CONTEXT", None)
        self.addCleanup(self._cleanup_temp)

        patcher = patch("saiverse_memory.adapter.Embedder", DummyEmbedder)
        self.addCleanup(patcher.stop)
        patcher.start()

        from saiverse_memory import SAIMemoryAdapter
        self.adapter = SAIMemoryAdapter("tester", persona_dir=self.persona_path, resource_id="tester")
        self.addCleanup(self._close_adapter)

    def _close_adapter(self):
        try:
            self.adapter.close()
        except Exception:
            pass

    def _cleanup_temp(self):
        import gc
        gc.collect()
        try:
            self._tmp.cleanup()
        except (PermissionError, OSError):
            pass

    def tearDown(self):
        os.environ.pop("SAIMEMORY_MEMORY", None)

    def _fresh_persona(self):
        """属性キャッシュ (_gold_panning_last_pan_id) を持たない persona。

        プロセス再起動で in-memory 状態が消えた状態を模す。同じ memory.db を指す
        adapter を共有するため、永続ストアからの read-through を検証できる。
        """
        return SimpleNamespace(
            persona_id="tester", persona_name="エア", model="claude-x",
            sai_memory=self.adapter,
        )

    def _run_pan(self, persona, current_messages, result=None):
        client = FakeLLMClient(result or {"reflection": "採取なし", "ops": []})
        lifecycle = SimpleNamespace(runtime=FakeRuntime(client))
        return gold_panning.run_gold_panning(
            lifecycle, persona, "b", current_messages, 0, None,
        )

    def _read_store(self):
        from sai_memory.memory.storage import get_embed_metadata
        with self.adapter._db_lock:
            return get_embed_metadata(self.adapter.conn, gold_panning._PAN_MARKER_KEY)

    # -- case 1: 永続ストアから read-through で読める (再起動相当) --------

    def test_marker_survives_persona_object_replacement(self):
        msgs = [{"id": f"m{i}", "content": "x"} for i in range(4)]
        self._run_pan(self._fresh_persona(), msgs)

        # 直接ストアにも末尾 id が書かれている。
        self.assertEqual(self._read_store(), "m3")

        # 属性キャッシュを持たない新 persona でも read-through でロードできる。
        reader = self._fresh_persona()
        self.assertIsNone(getattr(reader, "_gold_panning_last_pan_id", None))
        self.assertEqual(gold_panning._load_pan_marker(reader), "m3")
        # ロード後は属性にキャッシュされる。
        self.assertEqual(reader._gold_panning_last_pan_id, "m3")

    # -- case 2: 新 persona で run_session_close のガードが効く -----------

    def test_marker_guard_effective_after_restart(self):
        from unittest.mock import MagicMock

        msgs = [{"id": f"m{i}", "content": "x"} for i in range(12)]
        self._run_pan(self._fresh_persona(), msgs)  # marker → m11

        # 再起動相当の fresh persona (属性キャッシュ無し) で run_session_close。
        history_mgr = SimpleNamespace(metabolism_anchor_message_id="anchor")
        history_mgr.get_history_from_anchor = (
            lambda anchor, required_line_roles=None, required_scopes=None: msgs
        )
        reader = SimpleNamespace(
            persona_id="tester", persona_name="エア", model="claude-x",
            sai_memory=self.adapter, history_manager=history_mgr,
            current_building_id="b",
        )
        self.assertIsNone(getattr(reader, "_gold_panning_last_pan_id", None))

        lifecycle = SimpleNamespace(
            runtime=FakeRuntime(FakeLLMClient({"reflection": "x", "ops": []})),
        )
        lifecycle.is_chronicle_enabled_for_persona = lambda p: False
        lifecycle.generate_chronicle = MagicMock()
        lifecycle.generate_track_chronicle = MagicMock()
        lifecycle.ensure_recall_embeddings = MagicMock()
        lifecycle._is_cache_hot = lambda p: True

        # close_min デフォルト (10) に対し新規 0 件 → below_min で採取スキップ。
        with patch("sea.gold_panning.run_gold_panning") as pan:
            result = gold_panning.run_session_close(lifecycle, reader)
        pan.assert_not_called()
        self.assertFalse(result["panned"])
        self.assertEqual(result["skipped_reason"], "below_min")
        # read-through で永続ストアの marker が属性へ昇格している。
        self.assertEqual(reader._gold_panning_last_pan_id, "m11")

    # -- case 3: ストア書き込み失敗でも採取は完走 (WARNING のみ) ---------

    def test_store_write_failure_does_not_abort_panning(self):
        msgs = [{"id": f"m{i}", "content": "x"} for i in range(4)]
        persona = self._fresh_persona()
        result = {
            "reflection": "赴任を覚える",
            "ops": [{"op": "add", "content": "テスト用のコア記憶"}],
        }
        with patch(
            "sai_memory.memory.storage.set_embed_metadata",
            side_effect=RuntimeError("disk full"),
        ):
            summary = self._run_pan(persona, msgs, result=result)

        # 採取本体は完走している (ops 適用済み・skipped でない)。
        self.assertEqual(summary["ops_applied"], 1)
        self.assertFalse(summary["skipped"])
        # in-memory 属性は更新される (write-through の属性側は失敗前に走る)。
        self.assertEqual(persona._gold_panning_last_pan_id, "m3")
        # 永続ストアには書かれていない (書き込みが失敗したため)。
        self.assertIsNone(self._read_store())
        # コア記憶は実際に追加されている (採取が止まっていない証拠)。
        from sai_memory.core_memory import list_core_memories
        with self.adapter._db_lock:
            cores = list_core_memories(self.adapter.conn)
        self.assertEqual(len(cores), 1)


if __name__ == "__main__":
    unittest.main()
