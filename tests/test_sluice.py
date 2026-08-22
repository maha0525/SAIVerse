"""sluice (スルース) のユニットテスト。旧 gold_panning テストの世代交代先。

- コア記憶 ops 適用 (add / update / remove) が実 SAIMemory (temp DB) に届くこと
- 手帳メモ (want/did) が activities/memos に span・idem 込みで書かれること
- 約束 (promises) がタスク帳 (temp 中央 DB) に書かれること
- 同じ実行 ID (run_id) の再適用が重複しないこと
- 一覧に無い activity_id の要素だけが捨てられ、他は適用されること
- **確実に通るゲート**: スルース失敗で退場 (anchor 前進) が止まり、成功で進むこと
- コンテキスト超過で後退再試行 (§13.5-1) が働くこと
- defer-to-hot: anchor 冷で pending が立ち metabolism がスキップ / 圧力弁で実行

LLM はモック。SAIMemory は temp DB (test_core_memory_section と同じ Embedder patch)。
Windows の teardown OSError は許容する。
"""
from __future__ import annotations

import gc
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sea import sluice
from sea.eviction_plan import Watermarks
from sea.session_window import SessionWindow

#: run_metabolism 検証用の水位。1,000字 × 5 通の窓で、末尾 2,000字 (2 通) を
#: 保護し、残り 3,000字を U=2,500字 で 1 束に畳む → anchor は m3 へ。
_METABOLISM_WATERMARKS = Watermarks(low=2_000, target=2_000, high=4_000)


def _metabolism_messages(count=5, chars=1_000):
    return [
        {"id": f"m{i}", "content": "x" * chars, "created_at": 100 + i}
        for i in range(count)
    ]


def _metabolism_window(messages):
    return SessionWindow(
        anchor_id="m0", raw=list(messages), presented=list(messages), folds=[],
    )


def _sluice_result(**overrides):
    """スキーマ全欄必須 (Codex 第七巡 修正 1) を満たす応答の雛形。

    「空」は明示的な空配列だけ — 欄を省略した応答は fail-closed で棄却される
    ため、正常系のフェイク応答は必ずこの雛形から作る。
    """
    base = {
        "reflection": "x",
        "ops": [],
        "want_memos": [],
        "did_memos": [],
        "promises": [],
    }
    base.update(overrides)
    return base


def _stub_chronicle_refs(_persona, folds):
    """編纂が済んで級 1 エントリが引けた状態を模す。

    実装は「あらすじを持たない fold は退場させない」ので、退役の検証では refs が
    付いた状態を前提にする (chronicle_eviction.md §2)。
    """
    for i, fold in enumerate(folds):
        fold.chronicle_entry_ids = [f"entry-{i}"]
        fold.chronicle_short_ids = [i + 1]


class DummyEmbedder:
    def __init__(self, model=None, **kwargs) -> None:
        self.model_name = model

    def embed(self, texts, **kwargs):
        return [[0.0] * 3 for _ in texts]


class FakeUsage(SimpleNamespace):
    pass


class FakeLLMClient:
    """generate が固定 dict/str を返す (または例外を投げる) 最小クライアント。

    ``result`` に list を渡すと呼び出しごとに順に消費する (超過→成功の再試行
    シーケンス用)。
    """

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
        if isinstance(self.result, list):
            current = self.result[min(len(self.calls) - 1, len(self.result) - 1)]
        else:
            current = self.result
        if isinstance(current, Exception):
            raise current
        return current

    def consume_usage(self):
        return self._usage


class FakeRuntime:
    """run_sluice が触る SEARuntime の最小フェイク。

    ``presented_ids`` は _prepare_context の実入力 ID 列の契約
    (context_meta["presented_message_ids"]) の模擬:

    - "auto" (既定): context_messages のうち id を持つものの ID 列を書く。
    - list: その ID 列をそのまま書く (実入力と窓のズレの模擬)。
    - None: キー自体を書かない (履歴構築失敗の模擬 — sluice は fail-closed)。

    既定の context_messages は id を持つ履歴を 1 通含む — スルースは「1 通も
    見ていない結果」を凍結しない (Codex 第八巡 修正 2) ため、正常系のフェイクは
    見た集合が空にならないようにする。
    """

    def __init__(self, client, context_messages=None, presented_ids="auto"):
        self.client = client
        self.touched = []
        self.context_messages = context_messages or [
            {"role": "system", "content": "HEAD"},
            {"role": "user", "content": "u0", "id": "ctx0"},
        ]
        self.presented_ids = presented_ids

    def _prepare_context(self, persona, building_id, user_input, *args,
                         context_meta=None, **kwargs):
        if context_meta is not None and self.presented_ids is not None:
            if self.presented_ids == "auto":
                ids = [
                    str(m["id"]) for m in self.context_messages
                    if isinstance(m, dict) and m.get("id")
                ]
            else:
                ids = [str(i) for i in self.presented_ids]
            context_meta["presented_message_ids"] = ids
        return list(self.context_messages)

    def _select_llm_client(self, node_def, persona, needs_structured_output=False, state=None):
        return self.client

    def select_llm_client(self, node_def, persona, execution_context=None,
                          needs_structured_output=False, state=None):
        model = execution_context.model_key if execution_context is not None else "fake-model"
        return self.client, model

    def _default_temperature(self, persona):
        return 0.7

    def _get_cache_kwargs(self, persona_id=None):
        return {"enable_cache": True, "cache_ttl": "5m"}

    def touch_anchor_after_llm_call(self, persona, usage, anchor_id=None):
        self.touched.append(usage)


def _read_sluice_record(adapter):
    """sluice タグの判断ターンを 1 件読む (content, scope, line_role)。"""
    row = adapter.conn.execute(
        "SELECT content, scope, line_role FROM messages "
        "WHERE metadata LIKE '%sluice%' ORDER BY rowid DESC LIMIT 1"
    ).fetchone()
    return row


def _read_sluice_record_full(adapter):
    """sluice タグの判断ターンを 1 件読む (role, content, metadata)。"""
    row = adapter.conn.execute(
        "SELECT role, content, metadata FROM messages "
        "WHERE metadata LIKE '%sluice%' ORDER BY rowid DESC LIMIT 1"
    ).fetchone()
    return row


class _AdapterTestBase(unittest.TestCase):
    """実 SAIMemory (temp DB) を持つテストの共通 setup。"""

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
        gc.collect()
        try:
            self._tmp.cleanup()
        except (PermissionError, OSError):
            pass

    def tearDown(self):
        os.environ.pop("SAIMEMORY_MEMORY", None)


class SluiceRunTest(_AdapterTestBase):
    """実 SAIMemory (temp DB) 経由で ops 適用と判断ターン記録を実証する。"""

    def _persona(self):
        return SimpleNamespace(
            persona_id="tester", persona_name="エア", model="claude-x",
            sai_memory=self.adapter,
        )

    def _run(self, result, current_messages=None, evict_count=0, event_callback=None):
        client = FakeLLMClient(result)
        runtime = FakeRuntime(client)
        lifecycle = SimpleNamespace(
            runtime=runtime,
            touch_anchor_after_llm_call=runtime.touch_anchor_after_llm_call,
        )
        return sluice.run_sluice(
            lifecycle, self._persona(), "b",
            current_messages or [], evict_count, event_callback,
        ), client

    def _list_core(self):
        from sai_memory.core_memory import list_core_memories
        with self.adapter._db_lock:
            return list_core_memories(self.adapter.conn)

    # -- case 1: add op --------------------------------------------------

    def test_add_op_writes_core_memory_and_committed_record(self):
        result = _sluice_result(
            reflection="赴任のことを覚えておく",
            ops=[{"op": "add", "content": "2026年6月頃〜 まはーは海外赴任中（9月帰国予定）"}],
        )
        summary, client = self._run(result)
        self.assertEqual(summary["ops_applied"], 1)
        self.assertEqual(summary["ops_failed"], 0)
        self.assertFalse(summary["skipped"])

        # structured output 用に response_schema が渡っている
        self.assertIsNotNone(client.calls[0]["response_schema"])

        cores = self._list_core()
        self.assertEqual(len(cores), 1)
        self.assertIn("海外赴任中", cores[0].content)

        row = _read_sluice_record(self.adapter)
        self.assertIsNotNone(row)
        content, scope, line_role = row
        self.assertEqual(scope, "committed")
        self.assertEqual(line_role, "main_line")
        self.assertIn("赴任のことを覚えておく", content)
        self.assertIn(f"core:{cores[0].id}", content)

    # -- case 1b: 記録は event_message 形式のシステム通知 (role=user) ------

    def test_record_is_event_message_system_narration(self):
        import json as _json

        result = _sluice_result(
            reflection="赴任のことを覚えておく",
            ops=[{"op": "add", "content": "2026年6月頃〜 まはーは海外赴任中"}],
        )
        self._run(result)

        role, content, metadata = _read_sluice_record_full(self.adapter)
        # プロンプト無し assistant 発話ではなく <system> 包みのナレーション。
        self.assertEqual(role, "user")
        self.assertTrue(content.startswith("<system>"))
        self.assertTrue(content.rstrip().endswith("</system>"))
        self.assertIn("記憶整理の節目 — スルースの採取判断:", content)
        # reflection は persona_name プレフィックス付きで載る (全文、省略なし)。
        self.assertIn("エアの判断: 赴任のことを覚えておく", content)
        # タグは internal / event_message / sluice の 3 つ。
        tags = _json.loads(metadata)["tags"]
        self.assertIn("internal", tags)
        self.assertIn("event_message", tags)
        self.assertIn("sluice", tags)

    # -- case 2: empty ops -> discardable --------------------------------

    def test_empty_ops_writes_nothing_and_discardable_record(self):
        result = _sluice_result(reflection="今回は採取なし")
        summary, _ = self._run(result)
        self.assertEqual(summary["ops_applied"], 0)
        self.assertEqual(self._list_core(), [])

        row = _read_sluice_record(self.adapter)
        self.assertIsNotNone(row)
        _content, scope, _line_role = row
        self.assertEqual(scope, "discardable")

    # -- case 3a: update op ----------------------------------------------

    def test_update_op(self):
        from sai_memory.core_memory import add_core_memory
        with self.adapter._db_lock:
            mid = add_core_memory(self.adapter.conn, "旧: 赴任は3月まで")

        result = _sluice_result(reflection="更新", ops=[
            {"op": "update", "memory_id": mid, "content": "新: 赴任は9月まで"},
        ])
        summary, _ = self._run(result)
        self.assertEqual(summary["ops_applied"], 1)
        cores = self._list_core()
        self.assertEqual(len(cores), 1)
        self.assertEqual(cores[0].content, "新: 赴任は9月まで")

    def test_update_op_missing_target_is_failure(self):
        result = _sluice_result(ops=[
            {"op": "update", "memory_id": 999, "content": "存在しない対象"},
        ])
        summary, _ = self._run(result)
        self.assertEqual(summary["ops_applied"], 0)
        self.assertEqual(summary["ops_failed"], 1)
        row = _read_sluice_record(self.adapter)
        self.assertIn("update 失敗", row[0])
        self.assertEqual(row[1], "discardable")  # 採取なし

    # -- 2026-08-22 掃討フェーズ 束 3 指摘 3-1 ---------------------------

    def test_a_long_scene_memory_is_not_overwritten(self):
        """⭐ 場面の記憶 (実会話の写し) を丸ごと書き換えさせない。

        CAS が守るのは「実行中に他人が書き換えていないか」だけで、写しの改変は
        守らない。先頭だけを見たまま update が返ると、誰も書き換えていないので
        CAS は通り、全文がその先頭に潰れる。
        """
        from sai_memory.core_memory import add_core_memory
        from sea.sluice import _SCENE_PREVIEW_CHARS

        full = "あ" * (_SCENE_PREVIEW_CHARS + 200)
        with self.adapter._db_lock:
            mid = add_core_memory(self.adapter.conn, full, kind="scene")

        # 本人が見えていた範囲 (先頭 + 省略記号) をそのまま返してくる
        seen = full[:_SCENE_PREVIEW_CHARS] + "…"
        result = _sluice_result(reflection="整えた", ops=[
            {"op": "update", "memory_id": mid, "content": seen},
        ])
        summary, _ = self._run(result)

        self.assertEqual(summary["ops_applied"], 0)
        self.assertEqual(summary["ops_failed"], 1)
        # 全文が生きている (痩せていない)
        cores = self._list_core()
        self.assertEqual(cores[0].content, full)
        row = _read_sluice_record(self.adapter)
        self.assertIn("場面の記憶", row[0])

    def test_a_short_scene_memory_is_also_rejected(self):
        """⭐ 80 字以下の場面の記憶も書き換えられない (歯止めは長さではなく種類)。

        2026-08-22 裁定で塞いだ穴 (docs/issues/sluice_truncated_scene_update.md)。
        以前ここは「切り詰めずに見せた scene は直せる」を仕様として固定して
        いたが、scene は実会話の写しなので、全文を見せていても本人が書き換える
        ことは捏造にあたる。歯止めを長さで書くと、短い写しだけ改変できる穴が残る。
        """
        from sai_memory.core_memory import add_core_memory
        from sea.sluice import _SCENE_PREVIEW_CHARS

        short = "い" * (_SCENE_PREVIEW_CHARS - 10)
        with self.adapter._db_lock:
            mid = add_core_memory(self.adapter.conn, short, kind="scene")

        result = _sluice_result(reflection="整えた", ops=[
            {"op": "update", "memory_id": mid, "content": "書き直した本文"},
        ])
        summary, _ = self._run(result)

        self.assertEqual(summary["ops_applied"], 0)
        self.assertEqual(summary["ops_failed"], 1)
        cores = self._list_core()
        self.assertEqual(cores[0].content, short)  # 写しは無傷
        row = _read_sluice_record(self.adapter)
        self.assertIn("場面の記憶", row[0])

    def test_a_scene_memory_can_still_be_removed(self):
        """写しを消すことは改変ではない — remove は従来どおり通る。"""
        from sai_memory.core_memory import add_core_memory

        with self.adapter._db_lock:
            mid = add_core_memory(self.adapter.conn, "短い場面", kind="scene")

        result = _sluice_result(reflection="整えた", ops=[
            {"op": "remove", "memory_id": mid},
        ])
        summary, _ = self._run(result)

        self.assertEqual(summary["ops_applied"], 1)
        self.assertEqual(self._list_core(), [])

    def test_scene_is_still_presented_truncated(self):
        """提示側の切り詰めは先頭 80 字のまま (歯止めを種類へ移しても変えない)。

        歯止めと提示が別の規則になったので、提示側が黙って全文提示へ変わって
        いないことをここで固定する (プロンプトの分量が跳ねるのを防ぐ)。
        """
        from sea.sluice import _SCENE_PREVIEW_CHARS, _is_presented_truncated

        class _Mem:
            def __init__(self, kind, content):
                self.kind = kind
                self.content = content

        self.assertTrue(
            _is_presented_truncated(_Mem("scene", "あ" * (_SCENE_PREVIEW_CHARS + 1)))
        )
        self.assertFalse(
            _is_presented_truncated(_Mem("scene", "あ" * _SCENE_PREVIEW_CHARS))
        )
        self.assertFalse(
            _is_presented_truncated(_Mem("note", "あ" * (_SCENE_PREVIEW_CHARS + 1)))
        )

    def test_a_long_note_memory_is_still_updatable(self):
        """長くても scene 種でなければ写しではないので、書き換えられる。"""
        from sai_memory.core_memory import add_core_memory
        from sea.sluice import _SCENE_PREVIEW_CHARS

        long_note = "う" * (_SCENE_PREVIEW_CHARS + 200)
        with self.adapter._db_lock:
            mid = add_core_memory(self.adapter.conn, long_note)  # kind='note'

        result = _sluice_result(reflection="整えた", ops=[
            {"op": "update", "memory_id": mid, "content": "書き直した本文"},
        ])
        summary, _ = self._run(result)

        self.assertEqual(summary["ops_applied"], 1)
        cores = self._list_core()
        self.assertEqual(cores[0].content, "書き直した本文")

    # -- case 3b: remove op ----------------------------------------------

    def test_remove_op(self):
        from sai_memory.core_memory import add_core_memory
        with self.adapter._db_lock:
            mid = add_core_memory(self.adapter.conn, "消す予定のメモ")

        result = _sluice_result(
            reflection="整理", ops=[{"op": "remove", "memory_id": mid}],
        )
        summary, _ = self._run(result)
        self.assertEqual(summary["ops_applied"], 1)
        self.assertEqual(self._list_core(), [])

    # -- case: 壊れた構造化出力は fail-closed (空へ丸めない) ---------------

    def test_non_json_str_result_fails_closed(self):
        """壊れた応答を「採取なし」へ丸めると未採取のままゲートが通る —
        送出して退場停止 → 次回再試行に乗せる。"""
        with self.assertRaises(sluice.SluiceOutputError):
            self._run("これはJSONではない自由文の応答")
        self.assertEqual(self._list_core(), [])
        # 判断ターンの記録も書かれない (失敗した回は痕跡ごと再試行に委ねる)。
        self.assertIsNone(_read_sluice_record(self.adapter))

    def test_non_dict_result_fails_closed(self):
        with self.assertRaises(sluice.SluiceOutputError):
            self._run(42)

    def test_omitted_capture_field_fails_closed(self):
        """採取欄の省略・null は fail-closed (Codex 第七巡 修正 1) — 「空」は
        明示的な空配列だけ。旧形式 ({"reflection", "ops"} のみ) も棄却される。"""
        with self.assertRaises(sluice.SluiceOutputError):
            self._run({"reflection": "x", "ops": []})  # want/did/promises 欠落
        with self.assertRaises(sluice.SluiceOutputError):
            self._run({**_sluice_result(), "promises": None})  # null も不可
        missing_reflection = _sluice_result()
        del missing_reflection["reflection"]
        with self.assertRaises(sluice.SluiceOutputError):
            self._run(missing_reflection)
        self.assertEqual(self._list_core(), [])

    # -- case: コア記憶 CAS (Codex 第七巡 修正 2 — タスク帳 CAS の同族) -----

    def test_concurrent_core_edit_rejects_stale_update(self):
        """LLM 実行中にユーザーが本文を変えたら、スルースの古い update は
        スナップショット (本文ハッシュ) の CAS で棄却され、ユーザー本文が残る。"""
        from sai_memory.core_memory import add_core_memory, update_core_memory
        with self.adapter._db_lock:
            mid = add_core_memory(self.adapter.conn, "旧: 赴任は3月まで")
        adapter = self.adapter

        result = _sluice_result(ops=[
            {"op": "update", "memory_id": mid, "content": "スルースの古い判断"},
        ])

        class EditingDuringCallClient(FakeLLMClient):
            """generate の最中 (= スナップショット後・適用前) にユーザー編集が入る。"""

            def generate(self, *args, **kwargs):
                with adapter._db_lock:
                    update_core_memory(adapter.conn, mid, "ユーザーが直した")
                return super().generate(*args, **kwargs)

        client = EditingDuringCallClient(result)
        runtime = FakeRuntime(client)
        lifecycle = SimpleNamespace(
            runtime=runtime,
            touch_anchor_after_llm_call=runtime.touch_anchor_after_llm_call,
        )
        summary = sluice.run_sluice(lifecycle, self._persona(), "b", [], 0)
        self.assertEqual(summary["ops_applied"], 0)
        self.assertEqual(summary["ops_failed"], 1)
        cores = self._list_core()
        self.assertEqual(cores[0].content, "ユーザーが直した")  # 編集が残る
        record = _read_sluice_record(self.adapter)[0]
        self.assertIn(
            f"記憶 core:{mid} は実行中に変更されたため適用しませんでした", record,
        )

    def test_concurrent_core_edit_rejects_stale_remove(self):
        """remove も同様: 実行中に変更された記憶は消さない。"""
        from sai_memory.core_memory import add_core_memory, update_core_memory
        with self.adapter._db_lock:
            mid = add_core_memory(self.adapter.conn, "消される予定だった")
        adapter = self.adapter

        result = _sluice_result(ops=[{"op": "remove", "memory_id": mid}])

        class EditingDuringCallClient(FakeLLMClient):
            def generate(self, *args, **kwargs):
                with adapter._db_lock:
                    update_core_memory(adapter.conn, mid, "ユーザーが書き直した")
                return super().generate(*args, **kwargs)

        client = EditingDuringCallClient(result)
        runtime = FakeRuntime(client)
        lifecycle = SimpleNamespace(
            runtime=runtime,
            touch_anchor_after_llm_call=runtime.touch_anchor_after_llm_call,
        )
        summary = sluice.run_sluice(lifecycle, self._persona(), "b", [], 0)
        self.assertEqual(summary["ops_applied"], 0)
        self.assertEqual(summary["ops_failed"], 1)
        cores = self._list_core()
        self.assertEqual(len(cores), 1)  # 消されていない
        self.assertEqual(cores[0].content, "ユーザーが書き直した")

    def test_missing_ops_field_fails_closed(self):
        with self.assertRaises(sluice.SluiceOutputError):
            self._run({"reflection": "x"})

    def test_wrong_field_type_fails_closed(self):
        with self.assertRaises(sluice.SluiceOutputError):
            self._run({"ops": "not-a-list"})
        with self.assertRaises(sluice.SluiceOutputError):
            self._run({"ops": [], "want_memos": {"text": "配列でない"}})

    def test_non_object_element_fails_closed(self):
        with self.assertRaises(sluice.SluiceOutputError):
            self._run({"ops": ["add"]})

    # -- case: usage recording + anchor touch ----------------------------

    def test_usage_triggers_anchor_touch(self):
        result = _sluice_result()
        usage = FakeUsage(
            model="claude-x", input_tokens=100, output_tokens=5,
            cached_tokens=90, cache_write_tokens=10, cache_ttl="5m",
        )
        client = FakeLLMClient(result, usage=usage)
        runtime = FakeRuntime(client)
        lifecycle = SimpleNamespace(
            runtime=runtime,
            touch_anchor_after_llm_call=runtime.touch_anchor_after_llm_call,
        )
        with patch("saiverse.usage_tracker.get_usage_tracker") as get_tracker:
            sluice.run_sluice(lifecycle, self._persona(), "b", [], 0)
        get_tracker.return_value.record_usage.assert_called_once()
        self.assertEqual(len(runtime.touched), 1)

    # -- case: disabled toggle -------------------------------------------

    def test_disabled_toggle_skips(self):
        with patch.dict(os.environ, {"SAIVERSE_SLUICE_ENABLED": "0"}):
            summary, client = self._run(_sluice_result(ops=[
                {"op": "add", "content": "刻まれないはず"},
            ]))
        self.assertTrue(summary["skipped"])
        self.assertEqual(summary["reason"], "disabled")
        self.assertEqual(client.calls, [])  # LLM 呼び出しすら起きない
        self.assertEqual(self._list_core(), [])

    # -- case: プロンプトにアクティビティ一覧が同梱される -----------------

    def test_activity_list_failure_is_fail_closed(self):
        """アクティビティ一覧の読み失敗は空一覧へ丸めず送出する (Codex 第八巡
        修正 3)。空へ丸めると「開いている活動は無い」と見せたまま LLM が
        new_activity_name を書き、既存の活動と重複する新 activity が生まれる。"""
        client = FakeLLMClient(_sluice_result())
        runtime = FakeRuntime(client)
        lifecycle = SimpleNamespace(
            runtime=runtime,
            touch_anchor_after_llm_call=runtime.touch_anchor_after_llm_call,
        )
        with patch(
            "sai_memory.memory.pocketbook.list_activities",
            side_effect=RuntimeError("db down"),
        ):
            with self.assertRaises(RuntimeError):
                sluice.run_sluice(lifecycle, self._persona(), "b", [], 0)
        self.assertEqual(client.calls, [])  # LLM 呼び出しに到達しない

    # -- case: 要素内フィールドの型不正 (Codex 第八巡 修正 6) ----------------

    def test_type_invalid_op_field_is_dropped_without_crashing(self):
        """content が文字列でない ops は、pan 全体を落とさずその要素だけ棄却する
        (旧実装は .strip() が要素単位の例外処理の外で AttributeError になった)。"""
        summary, _ = self._run(_sluice_result(
            reflection="型が壊れた採取",
            ops=[{"op": "add", "content": 123}],
        ))
        self.assertFalse(summary["skipped"])
        self.assertEqual(summary["ops_applied"], 0)
        self.assertEqual(summary["ops_failed"], 1)
        self.assertEqual(self._list_core(), [])
        record = _read_sluice_record(self.adapter)[0]
        self.assertIn("コア記憶の操作の1件目を棄却", record)
        self.assertIn("content が文字列ではありません", record)

    def test_type_invalid_memory_id_is_dropped(self):
        """memory_id が整数でない update も要素棄却 (真偽値も整数扱いしない)。"""
        summary, _ = self._run(_sluice_result(ops=[
            {"op": "update", "memory_id": True, "content": "真偽値の id"},
            {"op": "remove", "memory_id": "3"},
        ]))
        self.assertEqual(summary["ops_applied"], 0)
        self.assertEqual(summary["ops_failed"], 2)
        record = _read_sluice_record(self.adapter)[0]
        self.assertIn("memory_id が整数ではありません", record)

    def test_prompt_includes_open_activities(self):
        from sai_memory.memory.pocketbook import add_activity, close_activity
        with self.adapter._db_lock:
            act = add_activity(self.adapter.conn, "小説を書く", "user")
            closed = add_activity(self.adapter.conn, "閉じた活動", "user")
            close_activity(self.adapter.conn, closed.id)

        _summary, client = self._run(_sluice_result())
        prompt = client.calls[0]["messages"][-1]["content"]
        self.assertIn(f"[act:{act.id}] 小説を書く", prompt)
        self.assertNotIn("閉じた活動", prompt)  # 閉語彙は開いているものだけ


class SluiceApplyExtensionTest(_AdapterTestBase):
    """スキーマ拡張 (want/did メモ・約束) の適用を temp DB で検証する。"""

    def setUp(self):
        super().setUp()
        # タスク帳用の temp 中央 DB (tests/test_task_book.py と同じ流儀)。
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from database.models import AI, Base

        self._tb_tmp = tempfile.TemporaryDirectory()
        db_path = str(Path(self._tb_tmp.name) / "central.db")
        self.engine = create_engine(f"sqlite:///{db_path}")
        Base.metadata.create_all(self.engine)
        self.SessionLocal = sessionmaker(bind=self.engine)
        self.manager = SimpleNamespace(SessionLocal=self.SessionLocal)
        db = self.SessionLocal()
        try:
            db.add(AI(AIID="tester", HOME_CITYID=1, AINAME="tester"))
            db.commit()
        finally:
            db.close()
        self.addCleanup(self._cleanup_tb)

    def _cleanup_tb(self):
        self.engine.dispose()
        gc.collect()
        try:
            self._tb_tmp.cleanup()
        except (PermissionError, OSError):
            pass

    def _persona(self):
        return SimpleNamespace(
            persona_id="tester", persona_name="エア", model="claude-x",
            sai_memory=self.adapter,
        )

    def _run(self, result, *, run_id="run-1", current_messages=None, marker=None):
        client = FakeLLMClient(result)
        runtime = FakeRuntime(client)
        lifecycle = SimpleNamespace(
            runtime=runtime,
            manager=self.manager,
            touch_anchor_after_llm_call=runtime.touch_anchor_after_llm_call,
        )
        persona = self._persona()
        if marker is not None:
            persona._sluice_last_pan_id = marker
        msgs = current_messages if current_messages is not None else [
            {"id": f"m{i}", "content": "x"} for i in range(5)
        ]
        summary = sluice.run_sluice(
            lifecycle, persona, "b", msgs, 0, None, run_id=run_id,
        )
        return summary, client

    def _memo_rows(self):
        return self.adapter.conn.execute(
            "SELECT activity_id, date, kind, text, span_start_id, span_end_id, idem_key "
            "FROM memos ORDER BY id ASC"
        ).fetchall()

    def _activities(self):
        from sai_memory.memory.pocketbook import list_activities
        with self.adapter._db_lock:
            return list_activities(self.adapter.conn, include_closed=True)

    def _task_rows(self):
        from saiverse import task_book
        return task_book.list_open(self.manager, "tester")

    # -- 手帳: 新アクティビティ + 既存 id、span・idem・日付 ----------------

    def test_memos_written_with_span_and_idem(self):
        from sai_memory.memory.pocketbook import add_activity
        from saiverse import clock
        with self.adapter._db_lock:
            act = add_activity(self.adapter.conn, "絵の練習", "user")

        result = {
            **_sluice_result(reflection="手帳へ"),
            "want_memos": [
                {"new_activity_name": "小説を書く", "text": "星を拾う話の続きを書きたい"},
            ],
            "did_memos": [
                {"activity_id": act.id, "text": "クロッキーを30分"},
            ],
        }
        summary, _ = self._run(result)
        self.assertEqual(summary["memos_applied"], 2)
        self.assertEqual(summary["memos_failed"], 0)

        rows = self._memo_rows()
        self.assertEqual(len(rows), 2)
        today = clock.now().date().isoformat()
        # want メモ: 新アクティビティが origin='sluice' で立つ。
        want = rows[0]
        acts = {a.name: a for a in self._activities()}
        self.assertIn("小説を書く", acts)
        self.assertEqual(acts["小説を書く"].origin, "sluice")
        self.assertEqual(want[0], acts["小説を書く"].id)
        self.assertEqual(want[1], today)
        self.assertEqual(want[2], "want")
        self.assertEqual(want[3], "星を拾う話の続きを書きたい")
        # span はマーカー無し → 窓の先頭〜末尾 (機械刻印)。
        self.assertEqual(want[4], "m0")
        self.assertEqual(want[5], "m4")
        # 冪等キーは span 由来の安定キー (再試行で不変 — run_id 由来ではない)。
        self.assertEqual(want[6], "sluice:m0..m4:m0")
        # did メモ: 既存アクティビティ参照。
        did = rows[1]
        self.assertEqual(did[0], act.id)
        self.assertEqual(did[2], "did")
        self.assertEqual(did[6], "sluice:m0..m4:m1")
        # 判断ターンは committed (採取あり)。
        row = _read_sluice_record(self.adapter)
        self.assertEqual(row[1], "committed")

    def test_span_starts_after_previous_pan_marker(self):
        result = {
            **_sluice_result(),
            "did_memos": [{"new_activity_name": "散歩", "text": "川沿いを歩いた"}],
        }
        msgs = [{"id": f"m{i}", "content": "x"} for i in range(5)]
        self._run(result, current_messages=msgs, marker="m2")
        rows = self._memo_rows()
        self.assertEqual(rows[0][4], "m3")  # マーカーの次から
        self.assertEqual(rows[0][5], "m4")

    # -- 2026-08-22 掃討フェーズ 束 3: 担当範囲をまたぐ内容重複 ------------

    def test_same_memo_from_a_later_span_is_not_written_twice(self):
        """⭐ 退場が繰り越された次の回に同じメモが返っても、二行目を書かない。

        issue の具体的な並び (docs/issues/sluice_memo_duplicate_across_spans.md):
        Metabolism #1 が m0..m4 を見てメモを書く → 退場は繰り越され、窓には
        採取済みの m0..m4 が残ったまま新着 m5/m6 が積まれる → #2 の担当範囲は
        m5..m6 なので冪等キーが変わり、同じ内容でも通ってしまっていた。
        """
        result = {
            **_sluice_result(),
            "did_memos": [
                {"new_activity_name": "小説を書く", "text": "星を拾う話の続きを書いた"},
            ],
        }
        msgs1 = [{"id": f"m{i}", "content": "x"} for i in range(5)]
        summary1, _ = self._run(result, current_messages=msgs1)
        self.assertEqual(summary1["memos_applied"], 1)
        self.assertEqual(len(self._memo_rows()), 1)

        # 退場が繰り越された回: 窓には採取済みの m0..m4 が残り、m5/m6 が積まれる。
        msgs2 = [{"id": f"m{i}", "content": "x"} for i in range(7)]
        summary2, _ = self._run(result, current_messages=msgs2, run_id="run-2")

        rows = self._memo_rows()
        self.assertEqual(len(rows), 1)  # 二行目は書かれない
        self.assertEqual(rows[0][6], "sluice:m0..m4:m0")  # 一行目 (初回の刻印) が残る
        # スキップは成功扱い (退場停止のゲートに乗せない)。
        self.assertEqual(summary2["memos_failed"], 0)
        record = _read_sluice_record(self.adapter)[0]
        self.assertIn("既に手帳にあるため採りませんでした", record)

    def test_prompt_states_the_span_scope(self):
        """⭐ 手帳の節で、今回の対象範囲を本人へ明示する (重複の供給源を塞ぐ)。"""
        msgs = [{"id": f"m{i}", "content": "x"} for i in range(5)]
        _summary, client = self._run(
            _sluice_result(), current_messages=msgs, marker="m2",
        )
        prompt = client.calls[0]["messages"][-1]["content"]
        self.assertIn("今回の対象は直近 2 通の会話です", prompt)
        self.assertIn("それより前は前回の整理で採取済みです", prompt)

    def test_prompt_says_whole_window_when_the_marker_is_gone(self):
        """マーカーが窓に無い (初回 / 押し出されて消えた) ときは窓全体が対象。"""
        _summary, client = self._run(_sluice_result())
        prompt = client.calls[0]["messages"][-1]["content"]
        self.assertIn("今回は手元の会話全体が対象です", prompt)

    # -- 冪等: 同じ担当範囲 (span) の再適用が重複しない --------------------

    def test_same_span_reapply_is_idempotent(self):
        """部分失敗 → 再試行を模す: マーカーが進んでいない同じ span への再適用は
        (実行台帳が無くても) span 由来の冪等キーで重複しない。"""
        from sai_memory.memory.storage import set_embed_metadata

        result = {
            **_sluice_result(),
            "want_memos": [{"new_activity_name": "小説を書く", "text": "続きを書きたい"}],
            "promises": [{"op": "add", "content": "水曜までに挿絵を渡す", "due": "2026-08-26"}],
        }
        self._run(result)
        # 成功でマーカーが進むので、再試行相当としてマーカーを巻き戻す
        # (失敗した回はマーカーが進まない — その状態の再現)。
        with self.adapter._db_lock:
            set_embed_metadata(self.adapter.conn, sluice._PAN_MARKER_KEY, "")
        self._run(result)

        self.assertEqual(len(self._memo_rows()), 1)
        self.assertEqual(len(self._task_rows()), 1)
        # get-or-create でアクティビティも一本に収束。
        names = [a.name for a in self._activities()]
        self.assertEqual(names.count("小説を書く"), 1)

    # -- 一覧に無い activity_id の要素だけ捨てる ---------------------------

    def test_unknown_activity_id_element_dropped_others_applied(self):
        from sai_memory.memory.pocketbook import add_activity
        with self.adapter._db_lock:
            act = add_activity(self.adapter.conn, "絵の練習", "user")

        result = {
            **_sluice_result(),
            "did_memos": [
                {"activity_id": 999, "text": "発明された id"},
                {"activity_id": act.id, "text": "正しい参照"},
            ],
        }
        summary, _ = self._run(result)
        self.assertEqual(summary["memos_applied"], 1)
        self.assertEqual(summary["memos_failed"], 1)
        rows = self._memo_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][3], "正しい参照")
        # 捨てた事実は判断ターンの記録に残る (黙って捨てない)。
        record = _read_sluice_record(self.adapter)[0]
        self.assertIn("activity_id=999", record)

    # -- 約束: add (期限あり / なし / 解釈不能) と update ------------------

    def test_promise_add_with_and_without_due(self):
        result = {
            **_sluice_result(),
            "promises": [
                {"op": "add", "content": "水曜までに挿絵を渡す", "due": "2026-08-26"},
                {"op": "add", "content": "ずっと一緒にいる"},
            ],
        }
        summary, _ = self._run(result)
        self.assertEqual(summary["promises_applied"], 2)
        rows = {r["content"]: r for r in self._task_rows()}
        dated = rows["水曜までに挿絵を渡す"]
        self.assertIsNotNone(dated["due_at"])
        # 日付のみはその日の終わり (23:59:59 ローカル) と解釈。
        self.assertEqual(
            datetime.fromtimestamp(dated["due_at"]).strftime("%Y-%m-%d %H:%M:%S"),
            "2026-08-26 23:59:59",
        )
        undated = rows["ずっと一緒にいる"]
        self.assertIsNone(undated["due_at"])  # 期限を発明しない
        for r in rows.values():
            self.assertEqual(r["origin"], "sluice")
            self.assertEqual(r["counterpart"], "user")
            self.assertEqual(r["origin_ref"], "m0..m4")  # span 由来の参照

    def test_promise_unparsable_due_saved_without_deadline(self):
        """解釈できない期限でも約束は失わない (まはー裁定 — タスク帳の芯は
        「失くすことが許されない」): 期限なしで保存 + 記録に明記。"""
        result = {
            **_sluice_result(),
            "promises": [
                {"op": "add", "content": "そのうち返事する", "due": "来週の水曜"},
                {"op": "add", "content": "正しい約束", "due": "2026-08-26"},
            ],
        }
        summary, _ = self._run(result)
        self.assertEqual(summary["promises_applied"], 2)
        self.assertEqual(summary["promises_failed"], 0)
        rows = {r["content"]: r for r in self._task_rows()}
        self.assertIsNone(rows["そのうち返事する"]["due_at"])  # 期限なしで保存
        self.assertIsNotNone(rows["正しい約束"]["due_at"])
        record = _read_sluice_record(self.adapter)[0]
        self.assertIn("『来週の水曜』を解釈できなかったため期限なしで登録しました", record)

    def test_promise_unparsable_due_on_update_skips_deadline_only(self):
        """update の解釈不能な期限は期限の変更だけを見送り、content 変更は適用。"""
        from saiverse import task_book
        entry = task_book.add_entry(
            self.manager, "tester", "挿絵を渡す",
            origin="user", counterpart="user", due_at=1_800_000_000,
        )
        result = {
            **_sluice_result(),
            "promises": [{
                "op": "update", "task_ref": entry["task_id"],
                "content": "挿絵を渡す (下書きから)", "due": "来週の水曜",
            }],
        }
        summary, _ = self._run(result)
        self.assertEqual(summary["promises_applied"], 1)
        updated = task_book.get_entry(self.manager, "tester", entry["task_id"])
        self.assertEqual(updated["content"], "挿絵を渡す (下書きから)")  # 適用
        self.assertEqual(updated["due_at"], 1_800_000_000)               # 期限は不変
        record = _read_sluice_record(self.adapter)[0]
        self.assertIn("期限は変更しませんでした", record)

    def test_promise_boundary_dates_saved_without_deadline(self):
        """境界日付 (0001-01-01 / 9999-12-31) は範囲検証と OSError/OverflowError の
        捕捉で「解釈不能」に畳まれ、約束は期限なしで保存されてスルースは完走する
        (Windows の timestamp() は 0001-01-01 で OSError を投げる)。"""
        result = {
            **_sluice_result(),
            "promises": [
                {"op": "add", "content": "紀元の約束", "due": "0001-01-01"},
                {"op": "add", "content": "遠未来の約束", "due": "9999-12-31"},
                {"op": "add", "content": "正しい約束", "due": "2026-08-26"},
            ],
        }
        summary, _ = self._run(result)
        self.assertFalse(summary["skipped"])
        self.assertEqual(summary["promises_applied"], 3)
        self.assertEqual(summary["promises_failed"], 0)
        rows = {r["content"]: r for r in self._task_rows()}
        self.assertEqual(len(rows), 3)
        self.assertIsNone(rows["紀元の約束"]["due_at"])
        self.assertIsNone(rows["遠未来の約束"]["due_at"])
        self.assertIsNotNone(rows["正しい約束"]["due_at"])

    def test_open_task_list_failure_is_fail_closed(self):
        """タスク一覧スナップショットの読み失敗は空一覧へ丸めず送出 — LLM は
        呼ばれず、ゲート失敗 (退場停止) に乗る (Codex 第四巡 修正 2)。"""
        result = _sluice_result()
        with patch(
            "saiverse.task_book.list_open",
            side_effect=RuntimeError("db down"),
        ):
            with self.assertRaises(RuntimeError):
                self._run(result)
        # LLM 呼び出しに到達していないことは、client を捕まえて確かめる。
        client = FakeLLMClient(result)
        runtime = FakeRuntime(client)
        lifecycle = SimpleNamespace(
            runtime=runtime, manager=self.manager,
            touch_anchor_after_llm_call=runtime.touch_anchor_after_llm_call,
        )
        with patch(
            "saiverse.task_book.list_open",
            side_effect=RuntimeError("db down"),
        ):
            with self.assertRaises(RuntimeError):
                sluice.run_sluice(
                    lifecycle, self._persona(), "b",
                    [{"id": f"m{i}", "content": "x"} for i in range(5)], 0, None,
                )
        self.assertEqual(client.calls, [])

    def test_type_invalid_elements_dropped_per_category(self):
        """各欄の型不正はその要素だけ落ち、同じ欄の正しい要素は適用される
        (Codex 第八巡 修正 6)。棄却は欄ごとに数えられ、判断ターンに残る。"""
        from sai_memory.memory.pocketbook import add_activity
        with self.adapter._db_lock:
            act = add_activity(self.adapter.conn, "絵の練習", "user")

        result = {
            **_sluice_result(reflection="型不正が混ざった応答"),
            "ops": [
                {"op": "add", "content": 123},
                {"op": "add", "content": "正しいコア記憶"},
            ],
            "want_memos": [
                {"activity_id": act.id, "text": ["配列は文字列でない"]},
            ],
            "did_memos": [
                {"activity_id": "1", "text": "activity_id が文字列"},
                {"activity_id": act.id, "text": "正しいメモ"},
            ],
            "promises": [
                {"op": "add", "content": "期限が配列", "due": []},
                {"op": "add", "content": "正しい約束", "due": "2026-08-26"},
            ],
        }
        summary, _ = self._run(result)
        self.assertFalse(summary["skipped"])
        self.assertEqual((summary["ops_applied"], summary["ops_failed"]), (1, 1))
        self.assertEqual((summary["memos_applied"], summary["memos_failed"]), (1, 2))
        self.assertEqual(
            (summary["promises_applied"], summary["promises_failed"]), (1, 1),
        )

        from sai_memory.core_memory import list_core_memories
        with self.adapter._db_lock:
            cores = list_core_memories(self.adapter.conn)
        self.assertEqual([c.content for c in cores], ["正しいコア記憶"])
        self.assertEqual([r[3] for r in self._memo_rows()], ["正しいメモ"])
        self.assertEqual([r["content"] for r in self._task_rows()], ["正しい約束"])

        record = _read_sluice_record(self.adapter)[0]
        self.assertIn("コア記憶の操作の1件目を棄却", record)
        self.assertIn("やりたいメモの1件目を棄却", record)
        self.assertIn("やったメモの1件目を棄却", record)
        self.assertIn("約束の1件目を棄却", record)

    def test_promise_update_changes_existing_entry(self):
        from saiverse import task_book
        entry = task_book.add_entry(
            self.manager, "tester", "挿絵を渡す", origin="user", counterpart="user",
        )
        result = {
            **_sluice_result(),
            "promises": [{
                "op": "update", "task_ref": entry["task_id"],
                "content": "挿絵を金曜までに渡す", "due": "2026-08-28",
            }],
        }
        summary, _ = self._run(result)
        self.assertEqual(summary["promises_applied"], 1)
        updated = task_book.get_entry(self.manager, "tester", entry["task_id"])
        self.assertEqual(updated["content"], "挿絵を金曜までに渡す")
        self.assertIsNotNone(updated["due_at"])

    def test_promise_clear_due_removes_deadline(self):
        """clear_due=True の update は期限を外す (期限の撤回)。"""
        from saiverse import task_book
        entry = task_book.add_entry(
            self.manager, "tester", "挿絵を渡す",
            origin="user", counterpart="user", due_at=1_800_000_000,
        )
        result = {
            **_sluice_result(),
            "promises": [{
                "op": "update", "task_ref": entry["task_id"], "clear_due": True,
            }],
        }
        summary, _ = self._run(result)
        self.assertEqual(summary["promises_applied"], 1)
        self.assertEqual(summary["promises_failed"], 0)
        updated = task_book.get_entry(self.manager, "tester", entry["task_id"])
        self.assertIsNone(updated["due_at"])
        record = _read_sluice_record(self.adapter)[0]
        self.assertIn("期限を撤回", record)

    def test_promise_due_and_clear_due_conflict_rejected(self):
        """due と clear_due の同時指定は矛盾 — その要素だけ棄却し他は適用される。"""
        from saiverse import task_book
        entry = task_book.add_entry(
            self.manager, "tester", "挿絵を渡す",
            origin="user", counterpart="user", due_at=1_800_000_000,
        )
        result = {
            **_sluice_result(),
            "promises": [
                {
                    "op": "update", "task_ref": entry["task_id"],
                    "due": "2026-08-28", "clear_due": True,
                },
                {"op": "add", "content": "別の約束", "due": "2026-08-26"},
            ],
        }
        summary, _ = self._run(result)
        self.assertEqual(summary["promises_applied"], 1)
        self.assertEqual(summary["promises_failed"], 1)
        # 矛盾した要素は適用されず、既存の期限は変わらない。
        unchanged = task_book.get_entry(self.manager, "tester", entry["task_id"])
        self.assertEqual(unchanged["due_at"], 1_800_000_000)
        record = _read_sluice_record(self.adapter)[0]
        self.assertIn("同時に指定できません", record)

    def test_promise_update_without_clear_due_keeps_deadline(self):
        """clear_due 省略の update は期限を変更しない (従来どおり)。"""
        from saiverse import task_book
        entry = task_book.add_entry(
            self.manager, "tester", "挿絵を渡す",
            origin="user", counterpart="user", due_at=1_800_000_000,
        )
        result = {
            **_sluice_result(),
            "promises": [{
                "op": "update", "task_ref": entry["task_id"], "content": "挿絵を渡す (下書きから)",
            }],
        }
        summary, _ = self._run(result)
        self.assertEqual(summary["promises_applied"], 1)
        updated = task_book.get_entry(self.manager, "tester", entry["task_id"])
        self.assertEqual(updated["due_at"], 1_800_000_000)
        self.assertEqual(updated["content"], "挿絵を渡す (下書きから)")

    def test_concurrent_user_edit_wins_over_stale_sluice_update(self):
        """LLM 実行中にユーザーが同じタスクを編集したら、スルースの古い判断は
        スナップショット revision の CAS で棄却され、ユーザー編集が残る
        (Codex 第三巡 修正 2)。棄却は判断ターン記録に明記される。"""
        from saiverse import task_book
        entry = task_book.add_entry(
            self.manager, "tester", "挿絵を渡す", origin="user", counterpart="user",
        )
        result = {
            **_sluice_result(),
            "promises": [{
                "op": "update", "task_ref": entry["task_id"],
                "content": "スルースの古い判断",
            }],
        }
        manager = self.manager
        task_id = entry["task_id"]

        class EditingDuringCallClient(FakeLLMClient):
            """generate の最中 (= スナップショット後・適用前) にユーザー編集が入る。"""

            def generate(self, *args, **kwargs):
                task_book.update_entry(
                    manager, "tester", task_id, content="ユーザーが直した",
                )
                return super().generate(*args, **kwargs)

        client = EditingDuringCallClient(result)
        runtime = FakeRuntime(client)
        lifecycle = SimpleNamespace(
            runtime=runtime, manager=self.manager,
            touch_anchor_after_llm_call=runtime.touch_anchor_after_llm_call,
        )
        persona = self._persona()
        msgs = [{"id": f"m{i}", "content": "x"} for i in range(5)]
        summary = sluice.run_sluice(lifecycle, persona, "b", msgs, 0, None)

        self.assertEqual(summary["promises_applied"], 0)
        self.assertEqual(summary["promises_failed"], 1)
        current = task_book.get_entry(self.manager, "tester", task_id)
        self.assertEqual(current["content"], "ユーザーが直した")  # 編集が残る
        record = _read_sluice_record(self.adapter)[0]
        self.assertIn("実行中に変更されたため適用しませんでした", record)

    def test_promise_update_invented_task_ref_fails_element_only(self):
        """LLM が発明した task_ref は同梱一覧の検証で棄却 → その要素だけ失敗し、
        スルース全体 (と他の要素) は成功する。"""
        result = {
            **_sluice_result(),
            "promises": [
                {"op": "update", "task_ref": "no-such-task", "content": "更新"},
                {"op": "add", "content": "本物の約束", "due": "2026-08-26"},
            ],
        }
        summary, _ = self._run(result)
        self.assertEqual(summary["promises_applied"], 1)
        self.assertEqual(summary["promises_failed"], 1)
        rows = self._task_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["content"], "本物の約束")
        # 棄却の事実は判断ターンの記録に残る (黙って捨てない)。
        record = _read_sluice_record(self.adapter)[0]
        self.assertIn("一覧にありません", record)

    # -- プロンプト同梱: open なタスク一覧 (閉語彙・再提案防止) ------------

    def test_prompt_includes_open_tasks(self):
        from saiverse import task_book
        due_epoch = int(datetime(2026, 8, 26, 12, 0).timestamp())
        dated = task_book.add_entry(
            self.manager, "tester", "水曜までに挿絵を渡す",
            origin="user", counterpart="user", due_at=due_epoch,
        )
        undated = task_book.add_entry(
            self.manager, "tester", "ずっと一緒にいる",
            origin="user", counterpart="user",
        )
        # 閉じた一件は一覧に出ない。
        closed = task_book.add_entry(
            self.manager, "tester", "終わった約束",
            origin="user", counterpart="user",
        )
        task_book.complete_entry(
            self.manager, "tester", closed["task_id"], outcome="済",
        )

        _summary, client = self._run(_sluice_result())
        prompt = client.calls[0]["messages"][-1]["content"]
        self.assertIn(
            f"[task:{dated['task_id']}] 水曜までに挿絵を渡す (期限: 2026-08-26)",
            prompt,
        )
        self.assertIn(
            f"[task:{undated['task_id']}] ずっと一緒にいる (期限なし)",
            prompt,
        )
        self.assertNotIn("終わった約束", prompt)
        # 再提案防止の誘導文が載っている。
        self.assertIn("再び\n  add する必要はありません", prompt)

    def test_prompt_with_no_open_tasks_shows_placeholder(self):
        _summary, client = self._run(_sluice_result())
        prompt = client.calls[0]["messages"][-1]["content"]
        self.assertIn("（開いているタスクはありません）", prompt)

    def test_promise_update_to_offered_task_id_succeeds(self):
        """同梱一覧に載る task_id への update は検証を通過して適用される。"""
        from saiverse import task_book
        entry = task_book.add_entry(
            self.manager, "tester", "挿絵を渡す", origin="user", counterpart="user",
        )
        result = {
            **_sluice_result(),
            "promises": [{
                "op": "update", "task_ref": entry["task_id"], "content": "挿絵を月曜までに渡す",
            }],
        }
        summary, client = self._run(result)
        self.assertEqual(summary["promises_applied"], 1)
        self.assertEqual(summary["promises_failed"], 0)
        # 一覧にも同じ id が載っていた (閉語彙の出どころの確認)。
        prompt = client.calls[0]["messages"][-1]["content"]
        self.assertIn(f"[task:{entry['task_id']}]", prompt)
        updated = task_book.get_entry(self.manager, "tester", entry["task_id"])
        self.assertEqual(updated["content"], "挿絵を月曜までに渡す")


class ContextOverflowBackoffTest(_AdapterTestBase):
    """コンテキスト超過の後退方式 (autonomous_behavior_v3.md §13.5-1)。"""

    #: 3 交換ぶんの提示 context。履歴部には ID を振ってある — 後退で全部外れると
    #: 「1 通も見ていない」結果になり、スルースはそれを凍結せず送出する
    #: (Codex 第八巡 修正 2)。ここでは 2 組外しても 1 組残る長さにしてある。
    _CONTEXT = [
        {"role": "system", "content": "HEAD"},
        {"role": "user", "content": "u0", "id": "c1"},
        {"role": "assistant", "content": "a0", "id": "c2"},
        {"role": "user", "content": "u1", "id": "c3"},
        {"role": "assistant", "content": "a1", "id": "c4"},
        {"role": "user", "content": "u2", "id": "c5"},
        {"role": "assistant", "content": "a2", "id": "c6"},
    ]

    def _persona(self):
        return SimpleNamespace(
            persona_id="tester", persona_name="エア", model="claude-x",
            sai_memory=self.adapter,
        )

    def _run(self, results):
        client = FakeLLMClient(results)
        runtime = FakeRuntime(client, context_messages=self._CONTEXT)
        lifecycle = SimpleNamespace(
            runtime=runtime,
            touch_anchor_after_llm_call=runtime.touch_anchor_after_llm_call,
        )
        return sluice.run_sluice(lifecycle, self._persona(), "b", [], 0), client

    def test_overflow_drops_latest_exchange_and_retries(self):
        overflow = RuntimeError("input token count exceeds the maximum context length")
        summary, client = self._run([overflow, _sluice_result()])
        self.assertFalse(summary["skipped"])
        self.assertEqual(len(client.calls), 2)
        # 1 回目: 全 context + 注入プロンプト。
        self.assertEqual(len(client.calls[0]["messages"]), 8)
        # 2 回目: 直近のプロンプト+応答の組 (u2, a2) が外れている。
        second = client.calls[1]["messages"]
        self.assertEqual(len(second), 6)
        contents = [m["content"] for m in second[:-1]]
        self.assertEqual(contents, ["HEAD", "u0", "a0", "u1", "a1"])

    def test_overflow_backs_off_pairwise_until_fit(self):
        overflow = RuntimeError("prompt is too long: context window exceeded")
        summary, client = self._run(
            [overflow, overflow, _sluice_result()],
        )
        self.assertFalse(summary["skipped"])
        self.assertEqual(len(client.calls), 3)
        # 3 回目は 2 組外れ、最初の 1 組だけが残っている。
        third = client.calls[2]["messages"]
        self.assertEqual([m["content"] for m in third[:-1]], ["HEAD", "u0", "a0"])

    def test_overflow_with_nothing_left_to_drop_raises(self):
        overflow = RuntimeError("context length exceeded")
        with self.assertRaises(RuntimeError):
            self._run([overflow, overflow, overflow, overflow])

    def test_all_exchanges_dropped_is_rejected_as_seeing_nothing(self):
        """後退で全交換が外れた回は「1 通も見ていない」— 適用も確定もせず送出する
        (Codex 第八巡 修正 2)。通すと、マーカー据え置きのまま台帳が completed に
        なり、以後は同じ記録が再利用されて末尾の退場が永久に止まる。"""
        overflow = RuntimeError("context length exceeded")
        with self.assertRaises(sluice.SluiceEmptySeenSetError):
            self._run([overflow, overflow, overflow, _sluice_result()])
        # 判断ターンの記録も残らない (失敗した回は痕跡ごと再試行に委ねる)。
        self.assertIsNone(_read_sluice_record(self.adapter))

    def test_non_overflow_error_does_not_trigger_backoff(self):
        boom = RuntimeError("boom")
        with self.assertRaises(RuntimeError):
            self._run([boom, _sluice_result()])

    def test_backoff_advances_marker_only_to_seen_end_and_next_span_covers_rest(self):
        """後退で外した組は「見ていない」— マーカーは実際に LLM に渡した範囲の
        末尾までしか進まず、外した組は次回の担当範囲に自然に入る (ゲートの
        不変条件: 全経験が退場前に一度本人の目を通る)。"""
        # 窓の 6 通 (c1..c6) が提示 context の履歴部 (3 交換) に対応する。
        msgs = [{"id": f"c{i}", "content": "x"} for i in range(1, 7)]
        overflow = RuntimeError("context length exceeded")
        client = FakeLLMClient([overflow, _sluice_result()])
        runtime = FakeRuntime(client, context_messages=self._CONTEXT)
        lifecycle = SimpleNamespace(
            runtime=runtime,
            touch_anchor_after_llm_call=runtime.touch_anchor_after_llm_call,
        )
        persona = self._persona()
        summary = sluice.run_sluice(lifecycle, persona, "b", msgs, 0)
        self.assertFalse(summary["skipped"])
        # 1 組 (u2, a2 = 2 通) 外した → 見た末尾は c4。c5, c6 は未見のまま。
        self.assertEqual(persona._sluice_last_pan_id, "c4")

        # 次のスルース: 担当範囲は c5 から始まり、外した組を回収する。
        client2 = FakeLLMClient({
            **_sluice_result(reflection="回収"),
            "did_memos": [{"new_activity_name": "散歩", "text": "川沿いを歩いた"}],
        })
        runtime2 = FakeRuntime(client2)
        lifecycle2 = SimpleNamespace(
            runtime=runtime2,
            touch_anchor_after_llm_call=runtime2.touch_anchor_after_llm_call,
        )
        sluice.run_sluice(lifecycle2, persona, "b", msgs, 0)
        row = self.adapter.conn.execute(
            "SELECT span_start_id, span_end_id, idem_key FROM memos"
        ).fetchone()
        self.assertEqual((row[0], row[1]), ("c5", "c6"))
        self.assertEqual(row[2], "sluice:c5..c6:m0")
        self.assertEqual(persona._sluice_last_pan_id, "c6")


class LegacyEnvMigrationTest(unittest.TestCase):
    """旧 SAIVERSE_GOLD_PANNING_* キーからの設定移行 (新キー > 旧キー > 既定)。"""

    _KEYS = (
        "SAIVERSE_SLUICE_ENABLED", "SAIVERSE_GOLD_PANNING_ENABLED",
        "SAIVERSE_SLUICE_PENDING_CAP", "SAIVERSE_GOLD_PANNING_PENDING_CAP",
        "SAIVERSE_SLUICE_CLOSE_MIN_MESSAGES",
        "SAIVERSE_GOLD_PANNING_CLOSE_MIN_MESSAGES",
    )

    def setUp(self):
        sluice._LEGACY_ENV_WARNED.clear()
        self._saved = {k: os.environ.pop(k, None) for k in self._KEYS}

    def tearDown(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_legacy_enabled_key_falls_back_with_warning(self):
        """旧 ENABLED=0 の環境が更新後に黙って採取 (課金) を再開しないこと。"""
        os.environ["SAIVERSE_GOLD_PANNING_ENABLED"] = "0"
        with self.assertLogs("sea.sluice", level="WARNING") as logs:
            self.assertFalse(sluice.is_enabled())
        self.assertTrue(any("非推奨" in line for line in logs.output))
        self.assertTrue(any("SAIVERSE_SLUICE_ENABLED" in line for line in logs.output))

    def test_new_key_takes_priority_over_legacy(self):
        os.environ["SAIVERSE_GOLD_PANNING_ENABLED"] = "0"
        os.environ["SAIVERSE_SLUICE_ENABLED"] = "1"
        self.assertTrue(sluice.is_enabled())

    def test_legacy_numeric_keys_fall_back(self):
        os.environ["SAIVERSE_GOLD_PANNING_PENDING_CAP"] = "2.5"
        self.assertEqual(sluice.get_pending_cap(), 2.5)
        os.environ["SAIVERSE_GOLD_PANNING_CLOSE_MIN_MESSAGES"] = "7"
        self.assertEqual(sluice.get_close_min_messages(), 7)

    def test_warning_emitted_once_per_key(self):
        os.environ["SAIVERSE_GOLD_PANNING_ENABLED"] = "0"
        with self.assertLogs("sea.sluice", level="WARNING"):
            sluice.is_enabled()
        with self.assertNoLogs("sea.sluice", level="WARNING"):
            sluice.is_enabled()


class SluiceLedgerRetryTest(_AdapterTestBase):
    """実行台帳 (execution ledger) 統合: 部分失敗 → 再試行の重複を塞ぐこと。

    identity = (persona, span_start_id)。LLM の構造化結果は mark_applied で
    台帳に凍結され、同じ担当範囲の再試行は新しい LLM コールをせず記録済みの
    結果を再利用して適用だけやり直す。
    """

    def setUp(self):
        super().setUp()
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from database.models import AI, Base
        from saiverse.execution_ledger import ExecutionLedger

        self._tb_tmp = tempfile.TemporaryDirectory()
        db_path = str(Path(self._tb_tmp.name) / "central.db")
        self.engine = create_engine(f"sqlite:///{db_path}")
        Base.metadata.create_all(self.engine)
        self.SessionLocal = sessionmaker(bind=self.engine)
        self.ledger = ExecutionLedger(self.SessionLocal)
        self.manager = SimpleNamespace(
            SessionLocal=self.SessionLocal, execution_ledger=self.ledger,
        )
        db = self.SessionLocal()
        try:
            db.add(AI(AIID="tester", HOME_CITYID=1, AINAME="tester"))
            db.commit()
        finally:
            db.close()
        self.addCleanup(self._cleanup_tb)

    def _cleanup_tb(self):
        self.engine.dispose()
        gc.collect()
        try:
            self._tb_tmp.cleanup()
        except (PermissionError, OSError):
            pass

    _MSGS = [{"id": f"m{i}", "content": "x"} for i in range(5)]

    _RESULT = {
        **_sluice_result(reflection="採取"),
        "ops": [{"op": "add", "content": "まはーは海外赴任中"}],
        "want_memos": [{"new_activity_name": "小説を書く", "text": "続きを書きたい"}],
        "promises": [{"op": "add", "content": "水曜までに挿絵を渡す", "due": "2026-08-26"}],
    }

    def _run(self, result, *, finalize=True):
        client = FakeLLMClient(result)
        runtime = FakeRuntime(client)
        lifecycle = SimpleNamespace(
            runtime=runtime,
            manager=self.manager,
            touch_anchor_after_llm_call=runtime.touch_anchor_after_llm_call,
        )
        persona = SimpleNamespace(
            persona_id="tester", persona_name="エア", model="claude-x",
            sai_memory=self.adapter,
        )
        summary = sluice.run_sluice(
            lifecycle, persona, "b", list(self._MSGS), 0, None,
            finalize=finalize,
        )
        return summary, client, persona

    def _list_core(self):
        from sai_memory.core_memory import list_core_memories
        with self.adapter._db_lock:
            return list_core_memories(self.adapter.conn)

    def _memo_rows(self):
        return self.adapter.conn.execute(
            "SELECT text, idem_key FROM memos ORDER BY id ASC"
        ).fetchall()

    def _task_rows(self):
        from saiverse import task_book
        return task_book.list_open(self.manager, "tester")

    def _ledger_row(self):
        return self.ledger.find_execution("sluice.pan", "tester:m0")

    def test_failure_at_memo_stage_then_retry_reuses_result(self):
        """コア記憶適用後・メモ書き込みで障害 → 再試行は LLM を再コールせず、
        コア記憶もメモも約束も重複しない。"""
        with patch(
            "sai_memory.memory.pocketbook.add_memo",
            side_effect=RuntimeError("disk error"),
        ):
            with self.assertRaises(RuntimeError):
                self._run(self._RESULT)
        # 1 回目: コア記憶は適用済み・メモ/約束は未適用・結果は台帳に凍結済み。
        self.assertEqual(len(self._list_core()), 1)
        self.assertEqual(self._memo_rows(), [])
        self.assertEqual(self._task_rows(), [])
        self.assertEqual(self._ledger_row()["status"], "applied")

        # 2 回目 (次回 Metabolism の再処理相当): LLM 再コールなしで再適用。
        summary, client, persona = self._run(self._RESULT)
        self.assertEqual(client.calls, [])  # LLM は呼ばれていない
        self.assertFalse(summary["skipped"])
        self.assertEqual(len(self._list_core()), 1)   # 内容一致ガードで重複しない
        self.assertEqual(len(self._memo_rows()), 1)
        self.assertEqual(len(self._task_rows()), 1)
        self.assertEqual(self._ledger_row()["status"], "completed")
        self.assertEqual(persona._sluice_last_pan_id, "m4")

    def test_failure_at_promise_stage_then_retry_does_not_duplicate(self):
        """メモ commit 後・約束のストレージ障害 → 再試行でメモが二重にならない。"""
        with patch(
            "saiverse.task_book.add_entry",
            side_effect=RuntimeError("db down"),
        ):
            with self.assertRaises(RuntimeError):
                self._run(self._RESULT)
        self.assertEqual(len(self._list_core()), 1)
        self.assertEqual(len(self._memo_rows()), 1)
        self.assertEqual(self._task_rows(), [])
        self.assertEqual(self._ledger_row()["status"], "applied")

        summary, client, _persona = self._run(self._RESULT)
        self.assertEqual(client.calls, [])
        self.assertEqual(len(self._list_core()), 1)
        self.assertEqual(len(self._memo_rows()), 1)  # span 由来 idem キーで冪等
        self.assertEqual(len(self._task_rows()), 1)
        self.assertEqual(self._ledger_row()["status"], "completed")

    def test_failure_at_core_stage_then_retry_reapplies(self):
        """コア記憶のストレージ障害は要素失敗へ丸めず送出 — 台帳は applied のまま
        (completed にならない) 退場が止まり、次回の再適用で記憶操作が果たされる。"""
        with patch(
            "sai_memory.core_memory.add_core_memory",
            side_effect=RuntimeError("disk error"),
        ):
            with self.assertRaises(RuntimeError):
                self._run(self._RESULT)
        self.assertEqual(self._list_core(), [])
        self.assertEqual(self._memo_rows(), [])
        self.assertEqual(self._task_rows(), [])
        self.assertEqual(self._ledger_row()["status"], "applied")

        summary, client, _persona = self._run(self._RESULT)
        self.assertEqual(client.calls, [])  # LLM 再コールなし (記録を再利用)
        self.assertEqual(len(self._list_core()), 1)
        self.assertEqual(len(self._memo_rows()), 1)
        self.assertEqual(len(self._task_rows()), 1)
        self.assertEqual(self._ledger_row()["status"], "completed")

    def test_boundary_due_in_recorded_result_does_not_block_retry(self):
        """applied 凍結された結果に境界日付の期限が入っていても、再適用は同じ
        例外で退場を止め続けない — 期限なし保存で完走する (Codex 第四巡 修正 3 +
        まはー裁定の「約束は失わない」)。"""
        result = {
            **_sluice_result(),
            "want_memos": [{"new_activity_name": "小説を書く", "text": "続き"}],
            "promises": [
                {"op": "add", "content": "紀元の約束", "due": "0001-01-01"},
                {"op": "add", "content": "遠未来の約束", "due": "9999-12-31"},
                {"op": "add", "content": "正しい約束", "due": "2026-08-26"},
            ],
        }
        # 1 回目: メモ書き込み障害で applied 凍結 (境界日付が結果に残る)。
        with patch(
            "sai_memory.memory.pocketbook.add_memo",
            side_effect=RuntimeError("disk error"),
        ):
            with self.assertRaises(RuntimeError):
                self._run(result)
        self.assertEqual(self._ledger_row()["status"], "applied")

        # 2 回目 (再適用): 境界日付は期限なしで保存され、完走して completed。
        summary, client, _persona = self._run(result)
        self.assertEqual(client.calls, [])
        self.assertFalse(summary["skipped"])
        self.assertEqual(summary["promises_applied"], 3)
        self.assertEqual(summary["promises_failed"], 0)
        self.assertEqual(len(self._memo_rows()), 1)
        rows = {r["content"]: r for r in self._task_rows()}
        self.assertEqual(len(rows), 3)
        self.assertIsNone(rows["紀元の約束"]["due_at"])
        self.assertIsNone(rows["遠未来の約束"]["due_at"])
        self.assertIsNotNone(rows["正しい約束"]["due_at"])
        self.assertEqual(self._ledger_row()["status"], "completed")

    def test_llm_failure_marks_failed_and_retry_calls_llm_again(self):
        """LLM 段の失敗は failed (副作用ゼロ) — 再試行は新しい LLM コールになる。"""
        with self.assertRaises(RuntimeError):
            self._run(RuntimeError("boom"))
        self.assertEqual(self._ledger_row()["status"], "failed")

        summary, client, _persona = self._run(_sluice_result())
        self.assertEqual(len(client.calls), 1)  # 記録が無いので LLM を呼ぶ
        self.assertFalse(summary["skipped"])
        self.assertEqual(self._ledger_row()["status"], "completed")

    def test_narration_persist_failure_keeps_applied_then_recovers(self):
        """finalize ① (ナレーション永続) の障害は送出 — 台帳は applied のまま
        (completed にならない) で、再試行が回収して completed に至る
        (Codex 第六巡 修正 2: completed ⇒ ナレーションとマーカーが永続済み)。"""
        with patch.object(
            self.adapter, "append_persona_message",
            side_effect=RuntimeError("disk error"),
        ):
            with self.assertRaises(RuntimeError):
                self._run(self._RESULT)
        self.assertEqual(self._ledger_row()["status"], "applied")
        # マーカーも進んでいない (①で止まった — ②以降は未実行)。
        from sai_memory.memory.storage import get_embed_metadata
        with self.adapter._db_lock:
            self.assertIsNone(
                get_embed_metadata(self.adapter.conn, sluice._PAN_MARKER_KEY),
            )
        # 適用は済んでいる。
        self.assertEqual(len(self._memo_rows()), 1)

        # 再試行: LLM 再コールなしで再適用 (重複なし) → completed + マーカー。
        summary, client, persona = self._run(self._RESULT)
        self.assertEqual(client.calls, [])
        self.assertEqual(len(self._memo_rows()), 1)
        self.assertEqual(len(self._task_rows()), 1)
        self.assertEqual(self._ledger_row()["status"], "completed")
        self.assertEqual(persona._sluice_last_pan_id, "m4")
        self.assertIsNotNone(_read_sluice_record(self.adapter))

    def test_marker_persist_failure_keeps_applied_then_recovers(self):
        """finalize ② (マーカー永続) の障害も送出 — 台帳は applied のまま・
        マーカー据え置きで、再試行が回収して completed に至る。"""
        with patch(
            "sai_memory.memory.storage.set_embed_metadata",
            side_effect=RuntimeError("disk full"),
        ):
            with self.assertRaises(RuntimeError):
                self._run(self._RESULT)
        self.assertEqual(self._ledger_row()["status"], "applied")
        from sai_memory.memory.storage import get_embed_metadata
        with self.adapter._db_lock:
            self.assertIsNone(
                get_embed_metadata(self.adapter.conn, sluice._PAN_MARKER_KEY),
            )

        summary, client, persona = self._run(self._RESULT)
        self.assertEqual(client.calls, [])
        self.assertEqual(len(self._memo_rows()), 1)
        self.assertEqual(len(self._task_rows()), 1)
        self.assertEqual(len(self._list_core()), 1)
        self.assertEqual(self._ledger_row()["status"], "completed")
        self.assertEqual(persona._sluice_last_pan_id, "m4")

    def test_recorded_result_reapply_respects_core_cas(self):
        """記録済み結果の再適用でも CAS が効く: 部分失敗 → ユーザー編集 →
        再適用、の並びで古い update がユーザー本文を上書きしない
        (Codex 第七巡 修正 2)。"""
        from sai_memory.core_memory import add_core_memory, update_core_memory
        with self.adapter._db_lock:
            mid = add_core_memory(self.adapter.conn, "旧: 内容")
        result = {
            **_sluice_result(reflection="更新"),
            "ops": [{"op": "update", "memory_id": mid, "content": "スルースの古い判断"}],
            "want_memos": [{"new_activity_name": "小説を書く", "text": "続き"}],
        }
        # 1 回目: メモ書き込み障害で applied 凍結 (update は適用済み)。
        with patch(
            "sai_memory.memory.pocketbook.add_memo",
            side_effect=RuntimeError("disk error"),
        ):
            with self.assertRaises(RuntimeError):
                self._run(result)
        self.assertEqual(self._ledger_row()["status"], "applied")

        # 失敗中にユーザーが本文を編集する。
        with self.adapter._db_lock:
            update_core_memory(self.adapter.conn, mid, "ユーザーが直した")

        # 2 回目 (再適用): 同じスナップショットで照合され、古い update は棄却。
        summary, client, _persona = self._run(result)
        self.assertEqual(client.calls, [])
        self.assertEqual(summary["ops_applied"], 0)
        self.assertEqual(summary["ops_failed"], 1)
        self.assertEqual(len(self._list_core()), 1)
        self.assertEqual(self._list_core()[0].content, "ユーザーが直した")
        self.assertEqual(len(self._memo_rows()), 1)  # メモ側は回収される
        self.assertEqual(self._ledger_row()["status"], "completed")
        record = _read_sluice_record(self.adapter)[0]
        self.assertIn("実行中に変更されたため適用しませんでした", record)

    def test_recorded_result_without_core_snapshot_rejects_core_writes(self):
        """旧形式の記録 (core_snapshot 無し) の update / remove は再構成せず
        要素棄却する (第五巡の裁定と同族 — 推定で適用しない)。"""
        from sai_memory.core_memory import add_core_memory
        with self.adapter._db_lock:
            mid = add_core_memory(self.adapter.conn, "変わらないはず")
        execution_id, runnable, _status = self.ledger.claim_execution(
            "sluice.pan", "tester:m0", "tester",
        )
        self.assertTrue(runnable)
        self.assertTrue(self.ledger.try_mark_running(execution_id))
        self.ledger.mark_applied(execution_id, result={
            "response": _sluice_result(ops=[
                {"op": "update", "memory_id": mid, "content": "古い判断"},
            ]),
            "span_start_id": "m0", "span_end_id": "m4",
            "seen_ids": [f"m{i}" for i in range(5)],
            "offered_activities": {}, "offered_tasks": {}, "prompt": "p",
            # core_snapshot を意図的に欠落させる (旧形式)。
        })
        summary, client, _persona = self._run(self._RESULT)
        self.assertEqual(client.calls, [])  # 記録の再利用 (LLM なし)
        # 記録の ops (update) は棄却され、本文は不変。
        self.assertEqual(self._list_core()[0].content, "変わらないはず")
        record = _read_sluice_record(self.adapter)[0]
        self.assertIn("スナップショット情報が無いため適用しませんでした", record)
        self.assertEqual(self._ledger_row()["status"], "completed")

    def test_recorded_result_without_seen_ids_fails_closed(self):
        """seen_ids の無い記録は span から再構成しない (Codex 第五巡 修正 1 —
        「別読みの近似」の同族)。送出してゲート失敗 (退場停止) に乗る。"""
        execution_id, runnable, _status = self.ledger.claim_execution(
            "sluice.pan", "tester:m0", "tester",
        )
        self.assertTrue(runnable)
        self.assertTrue(self.ledger.try_mark_running(execution_id))
        self.ledger.mark_applied(execution_id, result={
            "response": _sluice_result(),
            "span_start_id": "m0", "span_end_id": "m4",
            # seen_ids を意図的に欠落させる (旧形式の記録)。
            "offered_activities": {}, "offered_tasks": {}, "prompt": "p",
        })
        with self.assertRaises(sluice.SluiceContextUnavailableError):
            self._run(self._RESULT)

    def test_unfinalized_run_reapplies_idempotently_then_finalizes(self):
        """finalize=False で返った回 (確定保留) の再適用は重複せず、次の確定で
        completed + マーカー前進に至る (Codex 第五巡 修正 2)。"""
        summary1, _client1, persona1 = self._run(self._RESULT, finalize=False)
        self.assertFalse(summary1["skipped"])
        # 適用は済んでいるが確定はしていない: 台帳 applied・マーカー据え置き・
        # 判断ターン記録も未永続。
        self.assertEqual(len(self._list_core()), 1)
        self.assertEqual(len(self._memo_rows()), 1)
        self.assertEqual(len(self._task_rows()), 1)
        self.assertEqual(self._ledger_row()["status"], "applied")
        self.assertIsNone(getattr(persona1, "_sluice_last_pan_id", None))
        self.assertIsNone(_read_sluice_record(self.adapter))

        # 次回相当: 記録を再適用 (LLM 再コールなし・重複なし) → 既定の確定。
        summary2, client2, persona2 = self._run(self._RESULT)
        self.assertEqual(client2.calls, [])
        self.assertEqual(len(self._list_core()), 1)
        self.assertEqual(len(self._memo_rows()), 1)
        self.assertEqual(len(self._task_rows()), 1)
        self.assertEqual(self._ledger_row()["status"], "completed")
        self.assertEqual(persona2._sluice_last_pan_id, "m4")
        self.assertIsNotNone(_read_sluice_record(self.adapter))
        # クロージャの二重呼びは no-op。
        summary2["finalize"]()
        self.assertEqual(self._ledger_row()["status"], "completed")

    # -- 凍結 (mark_applied) 自体の失敗 (Codex 第八巡 修正 1) -----------------

    def test_mark_applied_failure_falls_back_to_failed_and_retries(self):
        """凍結のコミット障害は running のまま残さず failed へ倒す — running の
        まま残ると次回の claim が拒否し (起動時回収で unknown に入っても人裁定
        までブロック)、このペルソナの退場が永久に止まる。凍結より前なので世界側の
        適用はゼロで、次回は新しい LLM コールでやり直せる。"""
        with patch.object(
            self.ledger, "mark_applied", side_effect=RuntimeError("commit failed"),
        ):
            with self.assertRaises(RuntimeError):
                self._run(self._RESULT)
        self.assertEqual(self._ledger_row()["status"], "failed")
        # 世界側は 1 件も書かれていない (適用は凍結の後段)。
        self.assertEqual(self._list_core(), [])
        self.assertEqual(self._memo_rows(), [])
        self.assertEqual(self._task_rows(), [])

        # 次回: claim がキーを退避して新規実行 → 新しい LLM コールで完走。
        summary, client, persona = self._run(self._RESULT)
        self.assertEqual(len(client.calls), 1)
        self.assertFalse(summary["skipped"])
        self.assertEqual(len(self._list_core()), 1)
        self.assertEqual(len(self._memo_rows()), 1)
        self.assertEqual(len(self._task_rows()), 1)
        self.assertEqual(self._ledger_row()["status"], "completed")
        self.assertEqual(persona._sluice_last_pan_id, "m4")

    def test_mark_failed_also_failing_propagates_and_leaves_running(self):
        """台帳そのものが落ちている並びは fail-closed — 送出して pan を失敗させる
        (退場停止)。世界側は書かれない。"""
        with patch.object(
            self.ledger, "mark_applied", side_effect=RuntimeError("commit failed"),
        ), patch.object(
            self.ledger, "mark_failed", side_effect=RuntimeError("ledger down"),
        ):
            with self.assertRaises(RuntimeError):
                self._run(self._RESULT)
        self.assertEqual(self._ledger_row()["status"], "running")
        self.assertEqual(self._list_core(), [])
        self.assertEqual(self._memo_rows(), [])

    def test_ledger_lookup_failure_is_fail_closed(self):
        """台帳の読み出し失敗を「記録なし」へ丸めない (修正 5 の同族)。丸めると
        記録があるのに新しい LLM コールへ進み、失敗の顔が本当の原因から離れる。"""
        client = FakeLLMClient(self._RESULT)
        runtime = FakeRuntime(client)
        lifecycle = SimpleNamespace(
            runtime=runtime, manager=self.manager,
            touch_anchor_after_llm_call=runtime.touch_anchor_after_llm_call,
        )
        persona = SimpleNamespace(
            persona_id="tester", persona_name="エア", model="claude-x",
            sai_memory=self.adapter,
        )
        with patch.object(
            self.ledger, "find_execution", side_effect=RuntimeError("ledger down"),
        ):
            with self.assertRaises(RuntimeError):
                sluice.run_sluice(
                    lifecycle, persona, "b", list(self._MSGS), 0, None,
                )
        self.assertEqual(client.calls, [])  # LLM を呼ぶ前に止まる
        self.assertEqual(self._list_core(), [])

    # -- 見た集合が空の結果は凍結しない (Codex 第八巡 修正 2) ----------------

    def test_empty_seen_set_is_not_frozen_and_retries_with_new_llm_call(self):
        """1 通も見ていない結果は台帳に凍結せず failed にする — 記録が残ると
        同じ結果が永久に再利用され、新しい LLM コールが二度と起きない。"""
        client = FakeLLMClient(self._RESULT)
        runtime = FakeRuntime(client, presented_ids=[])
        lifecycle = SimpleNamespace(
            runtime=runtime, manager=self.manager,
            touch_anchor_after_llm_call=runtime.touch_anchor_after_llm_call,
        )
        persona = SimpleNamespace(
            persona_id="tester", persona_name="エア", model="claude-x",
            sai_memory=self.adapter,
        )
        with self.assertRaises(sluice.SluiceEmptySeenSetError):
            sluice.run_sluice(
                lifecycle, persona, "b", list(self._MSGS), 0, None,
            )
        self.assertEqual(self._ledger_row()["status"], "failed")
        self.assertEqual(self._list_core(), [])
        self.assertIsNone(getattr(persona, "_sluice_last_pan_id", None))

        # 次回は記録の再利用に閉じ込められず、新しい LLM コールで採り直せる。
        summary, client2, persona2 = self._run(self._RESULT)
        self.assertEqual(len(client2.calls), 1)
        self.assertFalse(summary["skipped"])
        self.assertEqual(self._ledger_row()["status"], "completed")
        self.assertEqual(persona2._sluice_last_pan_id, "m4")

    # -- 旧形式の記録は task の CAS を無効化しない (Codex 第八巡 修正 4) -------

    def test_legacy_record_without_task_revisions_rejects_updates(self):
        """revision を持たない旧形式 (offered_task_ids) の記録から復元した update は
        棄却する。None で渡すと update_entry が「現在値を読み直して CAS」に落ち、
        どんな現在値にも当たって古い判断がユーザー編集を上書きする。"""
        from saiverse import task_book
        entry = task_book.add_entry(
            self.manager, "tester", "挿絵を渡す", origin="user", counterpart="user",
        )
        execution_id, runnable, _status = self.ledger.claim_execution(
            "sluice.pan", "tester:m0", "tester",
        )
        self.assertTrue(runnable)
        self.assertTrue(self.ledger.try_mark_running(execution_id))
        self.ledger.mark_applied(execution_id, result={
            "response": _sluice_result(promises=[{
                "op": "update", "task_ref": entry["task_id"],
                "content": "スルースの古い判断",
            }]),
            "span_start_id": "m0", "span_end_id": "m4",
            "seen_ids": [f"m{i}" for i in range(5)],
            "offered_activities": {}, "core_snapshot": {}, "prompt": "p",
            # 旧形式: revision を持たない ID 列だけ。
            "offered_task_ids": [entry["task_id"]],
        })

        summary, client, _persona = self._run(self._RESULT)
        self.assertEqual(client.calls, [])  # 記録の再利用 (LLM なし)
        self.assertEqual(summary["promises_applied"], 0)
        self.assertEqual(summary["promises_failed"], 1)
        current = task_book.get_entry(self.manager, "tester", entry["task_id"])
        self.assertEqual(current["content"], "挿絵を渡す")  # 上書きされていない
        record = _read_sluice_record(self.adapter)[0]
        self.assertIn("スナップショット情報が無いため適用しませんでした", record)
        self.assertEqual(self._ledger_row()["status"], "completed")

    def test_recorded_task_revision_still_applies_updates(self):
        """現行形式 (offered_tasks に revision) の記録は従来どおり CAS して適用する
        — 「不明」の扱いを分けたことで正常な再適用が壊れていないことの対。"""
        from saiverse import task_book
        entry = task_book.add_entry(
            self.manager, "tester", "挿絵を渡す", origin="user", counterpart="user",
        )
        execution_id, runnable, _status = self.ledger.claim_execution(
            "sluice.pan", "tester:m0", "tester",
        )
        self.assertTrue(runnable)
        self.assertTrue(self.ledger.try_mark_running(execution_id))
        self.ledger.mark_applied(execution_id, result={
            "response": _sluice_result(promises=[{
                "op": "update", "task_ref": entry["task_id"],
                "content": "挿絵を月曜までに渡す",
            }]),
            "span_start_id": "m0", "span_end_id": "m4",
            "seen_ids": [f"m{i}" for i in range(5)],
            "offered_activities": {}, "core_snapshot": {}, "prompt": "p",
            "offered_tasks": {entry["task_id"]: entry["revision"]},
        })

        summary, client, _persona = self._run(self._RESULT)
        self.assertEqual(client.calls, [])
        self.assertEqual(summary["promises_applied"], 1)
        self.assertEqual(summary["promises_failed"], 0)
        current = task_book.get_entry(self.manager, "tester", entry["task_id"])
        self.assertEqual(current["content"], "挿絵を月曜までに渡す")

    # -- 型不正は凍結の前に落とす (Codex 第八巡 修正 6) ----------------------

    def test_type_invalid_element_is_not_frozen_and_replay_survives(self):
        """型不正の要素は台帳へ凍結される前に落ちる — 壊れた記録が再利用されて
        同じ例外を繰り返す縁を断つ。棄却の事実は記録から復元される。"""
        result = {
            **_sluice_result(reflection="採取"),
            "ops": [{"op": "add", "content": 123}],
            "want_memos": [{"new_activity_name": "小説を書く", "text": "続き"}],
        }
        with patch(
            "sai_memory.memory.pocketbook.add_memo",
            side_effect=RuntimeError("disk error"),
        ):
            with self.assertRaises(RuntimeError):
                self._run(result)
        row = self._ledger_row()
        self.assertEqual(row["status"], "applied")
        # 凍結された応答から型不正の要素は消えている。
        self.assertEqual(row["result"]["response"]["ops"], [])
        self.assertTrue(
            any(r.get("field") == "ops" for r in row["result"]["rejections"]),
        )

        # 再適用は同じ例外を繰り返さず完走し、棄却は判断ターンに残る。
        summary, client, _persona = self._run(result)
        self.assertEqual(client.calls, [])
        self.assertEqual(summary["ops_applied"], 0)
        self.assertEqual(summary["ops_failed"], 1)
        self.assertEqual(len(self._memo_rows()), 1)
        self.assertEqual(self._ledger_row()["status"], "completed")
        record = _read_sluice_record(self.adapter)[0]
        self.assertIn("コア記憶の操作の1件目を棄却", record)

    def test_success_records_completed_and_result(self):
        summary, _client, _persona = self._run(self._RESULT)
        self.assertEqual(summary["memos_applied"], 1)
        row = self._ledger_row()
        self.assertEqual(row["status"], "completed")
        self.assertEqual(row["result"]["span_start_id"], "m0")
        self.assertEqual(row["result"]["span_end_id"], "m4")
        self.assertEqual(
            row["result"]["response"]["ops"][0]["content"], "まはーは海外赴任中",
        )


class MetabolismUnseenTailGuardTest(_AdapterTestBase):
    """記録済み結果の再適用時、退場は記録の span_end まで (Codex 第二巡 修正 1)。

    部分失敗 → 新着が積もる → 再試行、の並びで: 記録が適用され・マーカーが
    記録の span_end で止まり・スルースが見ていない新着は退場されず・次回の
    スルースが新着を担当してから退場が進むこと。
    """

    def setUp(self):
        super().setUp()
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from database.models import AI, Base
        from saiverse.execution_ledger import ExecutionLedger

        self._tb_tmp = tempfile.TemporaryDirectory()
        db_path = str(Path(self._tb_tmp.name) / "central.db")
        self.engine = create_engine(f"sqlite:///{db_path}")
        Base.metadata.create_all(self.engine)
        self.SessionLocal = sessionmaker(bind=self.engine)
        self.ledger = ExecutionLedger(self.SessionLocal)
        self.manager = SimpleNamespace(
            SessionLocal=self.SessionLocal, execution_ledger=self.ledger,
        )
        db = self.SessionLocal()
        try:
            db.add(AI(AIID="tester", HOME_CITYID=1, AINAME="tester"))
            db.commit()
        finally:
            db.close()
        self.addCleanup(self._cleanup_tb)

    def _cleanup_tb(self):
        self.engine.dispose()
        gc.collect()
        try:
            self._tb_tmp.cleanup()
        except (PermissionError, OSError):
            pass

    def _make_lifecycle(self, client, presented_ids="auto"):
        from sea.session_lifecycle import SessionLifecycle

        runtime = FakeRuntime(client, presented_ids=presented_ids)
        lifecycle = SessionLifecycle(runtime, self.manager)
        lifecycle.ensure_recall_embeddings = lambda p: None
        lifecycle._attach_chronicle_refs = _stub_chronicle_refs
        lifecycle.is_chronicle_enabled_for_persona = lambda p: False
        anchor_updates = []
        lifecycle.update_anchor_for_model = (
            lambda p, m, aid, ttl=None: anchor_updates.append((m, aid))
        )
        return lifecycle, anchor_updates

    def _persona(self, messages):
        history_manager = SimpleNamespace(
            get_history_from_anchor=(
                lambda anchor, required_line_roles=None, required_scopes=None,
                pulse_id=None: list(messages)
            )
        )
        return SimpleNamespace(
            persona_id="tester", persona_name="エア", model="claude-x",
            sai_memory=self.adapter, history_manager=history_manager,
        )

    def _run_metabolism(self, lifecycle, persona, messages):
        window = _metabolism_window(messages)
        with patch.dict(os.environ, {"SAIVERSE_CHRONICLE_BAND_BUDGET": "2500"}), \
                patch("saiverse.dynamic_state.DynamicStateManager.on_metabolism",
                      lambda *a, **k: None):
            return lifecycle.run_metabolism(
                persona, "b", window, _METABOLISM_WATERMARKS, None,
            )

    def test_reapply_does_not_evict_unseen_tail(self):
        result = {
            **_sluice_result(reflection="採取"),
            "want_memos": [{"new_activity_name": "小説を書く", "text": "続きを書きたい"}],
        }
        base = _metabolism_messages()  # m0..m4 (各 1,000 字)

        # 1 回目: メモ書き込み障害 → スルース失敗 → 退場停止。結果は台帳に凍結。
        client1 = FakeLLMClient(result)
        lifecycle1, anchors1 = self._make_lifecycle(
            client1, presented_ids=[m["id"] for m in base],
        )
        with patch(
            "sai_memory.memory.pocketbook.add_memo",
            side_effect=RuntimeError("disk error"),
        ):
            ret1 = self._run_metabolism(lifecycle1, self._persona(base), base)
        self.assertEqual(ret1, "failed")
        self.assertEqual(anchors1, [])
        self.assertEqual(
            self.ledger.find_execution("sluice.pan", "tester:m0")["status"],
            "applied",
        )

        # 失敗中に新着 m5..m14 が積もる (退場計画が未見の範囲に届く量)。
        grown = base + [
            {"id": f"m{i}", "content": "x" * 1_000, "created_at": 100 + i}
            for i in range(5, 15)
        ]

        # 2 回目: 記録を再適用 (LLM 再コールなし)。退場計画は m5 以降 (未見) に
        # 届くため退場は見送り、マーカーは記録の span_end (m4) で止まる。
        client2 = FakeLLMClient(RuntimeError("must not be called"))
        lifecycle2, anchors2 = self._make_lifecycle(client2)
        persona2 = self._persona(grown)
        ret2 = self._run_metabolism(lifecycle2, persona2, grown)
        self.assertEqual(ret2, "deferred")
        self.assertEqual(client2.calls, [])       # LLM は呼ばれない
        self.assertEqual(anchors2, [])            # 未見の新着は退場されない
        self.assertEqual(persona2._sluice_last_pan_id, "m4")
        memo_rows = self.adapter.conn.execute("SELECT text FROM memos").fetchall()
        self.assertEqual(len(memo_rows), 1)       # 記録の適用は確定している
        self.assertEqual(
            self.ledger.find_execution("sluice.pan", "tester:m0")["status"],
            "completed",
        )

        # 3 回目: 新しいスルースが新着 (m5〜) を担当し、退場が進む。
        client3 = FakeLLMClient(_sluice_result())
        lifecycle3, anchors3 = self._make_lifecycle(
            client3, presented_ids=[m["id"] for m in grown],
        )
        ret3 = self._run_metabolism(lifecycle3, self._persona(grown), grown)
        self.assertEqual(ret3, "ok")
        self.assertEqual(len(client3.calls), 1)   # 新しい LLM コール
        self.assertTrue(anchors3)                 # 退場 (anchor 前進) が進む
        self.assertEqual(
            self.ledger.find_execution("sluice.pan", "tester:m5")["status"],
            "completed",
        )
        # メモは増えていない (新着側のスルースは空応答)。
        memo_rows = self.adapter.conn.execute("SELECT text FROM memos").fetchall()
        self.assertEqual(len(memo_rows), 1)

    # -- コールド実行相当: _prepare_context の anchor 前進で入力がズレる並び ----

    def test_cold_run_anchor_advance_blocks_eviction(self):
        """コールド実行では _prepare_context 自身が anchor を前進させ、スルースの
        LLM 入力から退場対象が漏れうる (Codex 第三巡 修正 1)。実入力 ID 列
        (presented_message_ids) に退場計画の対象 ID 全件が含まれなければ退場は
        止まる (Codex 第四巡 修正 1: 見た集合は実入力由来)。"""
        base = _metabolism_messages()  # m0..m4 — 退場計画は m0..m2 を畳む
        client = FakeLLMClient(_sluice_result())
        # 実入力は anchor 前進後の m2..m4 だけ (m0, m1 が漏れる)。
        lifecycle, anchors = self._make_lifecycle(
            client, presented_ids=["m2", "m3", "m4"],
        )
        persona = self._persona(base)
        ret = self._run_metabolism(lifecycle, persona, base)
        self.assertEqual(ret, "deferred")
        self.assertEqual(anchors, [])           # 退場 (anchor 前進) は止まる
        self.assertEqual(len(client.calls), 1)  # スルース自体は走って成功している
        # 確定も保留される (Codex 第五巡 修正 2): マーカーがヘッドギャップ
        # (未提示の m0, m1) を跨いで m4 へ進むと、m0, m1 は次回の担当範囲からも
        # 漏れて永遠に採取されない。台帳は applied のまま次回の再適用に委ねる。
        self.assertIsNone(getattr(persona, "_sluice_last_pan_id", None))
        from sai_memory.memory.storage import get_embed_metadata
        with self.adapter._db_lock:
            self.assertIsNone(
                get_embed_metadata(self.adapter.conn, sluice._PAN_MARKER_KEY),
            )
        self.assertEqual(
            self.ledger.find_execution("sluice.pan", "tester:m0")["status"],
            "applied",
        )

    def test_cold_run_head_gap_recovers_on_next_metabolism(self):
        """コールド anchor ズレ → 次回回収の統合。anchor 前進が永続した次回の
        Metabolism では窓自体が m2 から始まり、新しい担当範囲 (key も新しい) の
        フレッシュなスルースが走って確定・退場まで進む。"""
        base = _metabolism_messages()  # m0..m4

        # 1 回目: 実入力 m2..m4 (ヘッドギャップ) → 確定保留・退場見送り。
        client1 = FakeLLMClient(_sluice_result())
        lifecycle1, anchors1 = self._make_lifecycle(
            client1, presented_ids=["m2", "m3", "m4"],
        )
        ret1 = self._run_metabolism(lifecycle1, self._persona(base), base)
        self.assertEqual(ret1, "deferred")
        self.assertEqual(anchors1, [])

        # 2 回目: anchor 前進が永続し、窓は m2 から始まる (会話も m5..m9 まで
        # 進んだ)。span 起点が m2 になるため台帳の key も新しく、フレッシュな
        # スルースが窓全体を見て走る。
        advanced = base[2:] + [
            {"id": f"m{i}", "content": "x" * 1_000, "created_at": 100 + i}
            for i in range(5, 10)
        ]
        client2 = FakeLLMClient(_sluice_result())
        lifecycle2, anchors2 = self._make_lifecycle(
            client2, presented_ids=[m["id"] for m in advanced],
        )
        persona2 = self._persona(advanced)
        ret2 = self._run_metabolism(lifecycle2, persona2, advanced)
        self.assertEqual(ret2, "ok")
        self.assertEqual(len(client2.calls), 1)  # 新しい LLM コール
        self.assertTrue(anchors2)                # 退場 (anchor 前進) が進む
        self.assertEqual(persona2._sluice_last_pan_id, "m9")  # 確定済み
        self.assertEqual(
            self.ledger.find_execution("sluice.pan", "tester:m2")["status"],
            "completed",
        )
        # 旧記録 (tester:m0) は applied の孤児として残る (再利用されない)。
        self.assertEqual(
            self.ledger.find_execution("sluice.pan", "tester:m0")["status"],
            "applied",
        )

    def test_cold_run_full_containment_allows_eviction(self):
        """退場対象の全件がスルース入力に含まれていれば退場は進む。"""
        base = _metabolism_messages()
        client = FakeLLMClient(_sluice_result())
        lifecycle, anchors = self._make_lifecycle(
            client, presented_ids=[m["id"] for m in base],
        )
        ret = self._run_metabolism(lifecycle, self._persona(base), base)
        self.assertEqual(ret, "ok")
        self.assertTrue(anchors)

    def test_history_build_failure_blocks_eviction(self):
        """履歴構築の失敗 (presented_message_ids キー不在) は fail-closed —
        LLM を呼ばず退場が止まる (Codex 第四巡 修正 1: フォールバック全廃)。"""
        base = _metabolism_messages()
        client = FakeLLMClient(_sluice_result())
        lifecycle, anchors = self._make_lifecycle(client, presented_ids=None)
        ret = self._run_metabolism(lifecycle, self._persona(base), base)
        self.assertEqual(ret, "failed")
        self.assertEqual(anchors, [])
        self.assertEqual(client.calls, [])  # LLM は呼ばれない


class DeferToHotTest(unittest.TestCase):
    """SessionLifecycle.maybe_run_metabolism の defer-to-hot をスタブ化して検証する。"""

    #: 1 通あたりの文字数 (水位は文字数基準 — chronicle_eviction.md §4)
    CHARS_PER_MESSAGE = 1_000

    def _make_lifecycle(self, *, hot, messages_count):
        from sea.eviction_plan import Watermarks
        from sea.session_lifecycle import SessionLifecycle

        manager = SimpleNamespace()
        runtime = SimpleNamespace()
        lifecycle = SessionLifecycle(runtime, manager)

        history_mgr = SimpleNamespace()
        messages = [
            {"id": f"m{i}", "content": "x" * self.CHARS_PER_MESSAGE}
            for i in range(messages_count)
        ]
        history_mgr.get_history_from_anchor = (
            lambda anchor, required_line_roles=None, required_scopes=None: messages
        )
        persona = SimpleNamespace(persona_id="tester", model="claude-x", history_manager=history_mgr)

        # 依存メソッドをスタブ (anchor 行 / 水位 / hot 判定 / 実行)。
        # 発火判定の anchor は session_anchor 行 (persona, model) から読まれる
        # (§6-5 で persona 属性は廃止)。
        lifecycle.load_anchor_entry = lambda pid, mk: {"anchor_id": "anchor"}
        lifecycle.get_metabolism_watermarks = lambda p, mk=None: Watermarks(
            low=10_000, target=20_000, high=20_000,
        )
        lifecycle._is_cache_hot = lambda p, mk=None: hot
        ran = []
        lifecycle.run_metabolism = (
            lambda p, b, win, wm, cb=None, model_key=None: ran.append((len(win.presented), wm))
        )
        return lifecycle, persona, ran

    def test_cold_defers_and_sets_pending(self):
        # cold + high(20,000字) < 25,000字 <= cap(20,000*1.5=30,000) → 繰り延べ
        lifecycle, persona, ran = self._make_lifecycle(hot=False, messages_count=25)
        lifecycle.maybe_run_metabolism(persona, "b", None)
        self.assertEqual(ran, [])  # metabolism は走らない
        self.assertTrue(getattr(persona, "_metabolism_pending", False))

    def test_pressure_valve_runs_cold(self):
        # cold だが 40,000字 > cap(30,000) → 圧力弁でコールド実行
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


class SluiceGateTest(_AdapterTestBase):
    """確実に通るゲート (§13.3): スルース失敗で退場が止まり、成功で進むこと。"""

    def test_llm_exception_propagates_out_of_run_sluice(self):
        # 隔離しない — 呼び出し側 (run_metabolism) が失敗を退場停止に写像する。
        client = FakeLLMClient(RuntimeError("boom"))
        lifecycle = SimpleNamespace(runtime=FakeRuntime(client))
        persona = SimpleNamespace(
            persona_id="tester", persona_name="エア", model="claude-x", sai_memory=self.adapter,
        )
        with self.assertRaises(RuntimeError):
            sluice.run_sluice(lifecycle, persona, "b", [], 0)

    def _make_metabolism_lifecycle(self, client, presented_ids="auto"):
        """run_metabolism 検証用の SessionLifecycle (重い経路をスタブ)。"""
        from sea.session_lifecycle import SessionLifecycle

        runtime = FakeRuntime(client, presented_ids=presented_ids)
        manager = SimpleNamespace()
        lifecycle = SessionLifecycle(runtime, manager)
        lifecycle.ensure_recall_embeddings = lambda p: None
        lifecycle._attach_chronicle_refs = _stub_chronicle_refs
        anchor_updates = []
        lifecycle.update_anchor_for_model = lambda p, m, aid, ttl=None: anchor_updates.append((m, aid))
        return lifecycle, anchor_updates

    def _make_metabolism_persona(self, messages=()):
        history_manager = SimpleNamespace(
            get_history_from_anchor=(
                lambda anchor, required_line_roles=None, required_scopes=None,
                pulse_id=None: list(messages)
            )
        )
        return SimpleNamespace(
            persona_id="tester", persona_name="エア", model="claude-x",
            sai_memory=self.adapter, history_manager=history_manager,
        )

    def test_run_metabolism_sluice_failure_holds_anchor_back(self):
        """スルースの LLM 例外は退場を止める (確実に通るゲート、v3 §13.3)。
        旧 gold_panning の「失敗しても anchor 更新は実行される」は廃止。
        Chronicle トグル OFF = status 'disabled' でも sluice の失敗が据え置きにする。"""
        client = FakeLLMClient(RuntimeError("boom"))
        lifecycle, anchor_updates = self._make_metabolism_lifecycle(client)
        lifecycle.is_chronicle_enabled_for_persona = lambda p: False
        messages = _metabolism_messages()
        persona = self._make_metabolism_persona(messages)

        window = _metabolism_window(messages)
        with patch.dict(os.environ, {"SAIVERSE_CHRONICLE_BAND_BUDGET": "2500"}), \
                patch("saiverse.dynamic_state.DynamicStateManager.on_metabolism", lambda *a, **k: None):
            result = lifecycle.run_metabolism(persona, "b", window, _METABOLISM_WATERMARKS, None)

        self.assertEqual(anchor_updates, [])
        self.assertEqual(result, "failed")

    def test_run_metabolism_sluice_success_advances_anchor(self):
        """スルースが通れば退場 (anchor 前進) が進む。"""
        client = FakeLLMClient(_sluice_result())
        messages = _metabolism_messages()
        lifecycle, anchor_updates = self._make_metabolism_lifecycle(
            client, presented_ids=[m["id"] for m in messages],
        )
        lifecycle.is_chronicle_enabled_for_persona = lambda p: False
        persona = self._make_metabolism_persona(messages)

        window = _metabolism_window(messages)
        with patch.dict(os.environ, {"SAIVERSE_CHRONICLE_BAND_BUDGET": "2500"}), \
                patch("saiverse.dynamic_state.DynamicStateManager.on_metabolism", lambda *a, **k: None):
            result = lifecycle.run_metabolism(persona, "b", window, _METABOLISM_WATERMARKS, None)

        self.assertEqual(anchor_updates, [("claude-x", "m3")])
        self.assertEqual(result, "ok")

    def test_run_metabolism_omitted_field_response_blocks_eviction(self):
        """採取欄を省略した応答 (旧形式) は fail-closed — 退場が止まる
        (Codex 第七巡 修正 1)。"""
        client = FakeLLMClient({"reflection": "x", "ops": []})  # 欄省略の応答
        messages = _metabolism_messages()
        lifecycle, anchor_updates = self._make_metabolism_lifecycle(
            client, presented_ids=[m["id"] for m in messages],
        )
        lifecycle.is_chronicle_enabled_for_persona = lambda p: False
        persona = self._make_metabolism_persona(messages)

        window = _metabolism_window(messages)
        with patch.dict(os.environ, {"SAIVERSE_CHRONICLE_BAND_BUDGET": "2500"}), \
                patch("saiverse.dynamic_state.DynamicStateManager.on_metabolism", lambda *a, **k: None):
            result = lifecycle.run_metabolism(persona, "b", window, _METABOLISM_WATERMARKS, None)

        self.assertEqual(anchor_updates, [])
        self.assertEqual(result, "failed")

    def test_run_metabolism_sluice_disabled_advances_anchor(self):
        """スルース無効 (トグル OFF) は「採取なしで退場する」設計 — ゲートは塞がない。"""
        client = FakeLLMClient(RuntimeError("must not be called"))
        lifecycle, anchor_updates = self._make_metabolism_lifecycle(client)
        lifecycle.is_chronicle_enabled_for_persona = lambda p: False
        messages = _metabolism_messages()
        persona = self._make_metabolism_persona(messages)

        window = _metabolism_window(messages)
        with patch.dict(os.environ, {
            "SAIVERSE_SLUICE_ENABLED": "0",
            "SAIVERSE_CHRONICLE_BAND_BUDGET": "2500",
        }), patch("saiverse.dynamic_state.DynamicStateManager.on_metabolism", lambda *a, **k: None):
            result = lifecycle.run_metabolism(persona, "b", window, _METABOLISM_WATERMARKS, None)

        self.assertEqual(anchor_updates, [("claude-x", "m3")])
        self.assertEqual(result, "ok")
        self.assertEqual(client.calls, [])

    def test_run_sluice_without_memory_raises(self):
        """no_memory は fail-closed (Codex 第三巡 修正 3) — 成功扱いのスキップは
        disabled だけで、ストレージ未準備は送出してゲート失敗に写像する。"""
        client = FakeLLMClient(_sluice_result())
        lifecycle = SimpleNamespace(runtime=FakeRuntime(client))
        persona = SimpleNamespace(
            persona_id="tester", persona_name="エア", model="claude-x",
            sai_memory=None,
        )
        with self.assertRaises(sluice.SluiceStorageUnavailableError):
            sluice.run_sluice(lifecycle, persona, "b", [], 0)
        self.assertEqual(client.calls, [])

    def test_run_sluice_without_presented_ids_raises(self):
        """presented_message_ids 不在 (履歴構築失敗の契約) は fail-closed —
        LLM を呼ばず送出する (Codex 第四巡 修正 1)。"""
        client = FakeLLMClient(_sluice_result())
        lifecycle = SimpleNamespace(
            runtime=FakeRuntime(client, presented_ids=None),
        )
        persona = SimpleNamespace(
            persona_id="tester", persona_name="エア", model="claude-x",
            sai_memory=self.adapter,
        )
        with self.assertRaises(sluice.SluiceContextUnavailableError):
            sluice.run_sluice(lifecycle, persona, "b", [], 0)
        self.assertEqual(client.calls, [])

    def test_run_metabolism_memory_unavailable_blocks_eviction(self):
        """adapter 未準備の Metabolism は退場 (anchor 前進) が止まる — 採取ゼロの
        まま忘れる事故を防ぐ。次回 (adapter 回復後) に自然再試行される。"""
        client = FakeLLMClient(_sluice_result())
        lifecycle, anchor_updates = self._make_metabolism_lifecycle(client)
        lifecycle.is_chronicle_enabled_for_persona = lambda p: False
        messages = _metabolism_messages()
        persona = self._make_metabolism_persona(messages)
        persona.sai_memory = SimpleNamespace(is_ready=lambda: False)

        window = _metabolism_window(messages)
        with patch.dict(os.environ, {"SAIVERSE_CHRONICLE_BAND_BUDGET": "2500"}), \
                patch("saiverse.dynamic_state.DynamicStateManager.on_metabolism", lambda *a, **k: None):
            result = lifecycle.run_metabolism(persona, "b", window, _METABOLISM_WATERMARKS, None)

        self.assertEqual(anchor_updates, [])
        self.assertEqual(result, "failed")
        self.assertEqual(client.calls, [])

    def test_run_metabolism_chronicle_failure_holds_anchor_and_skips_sluice(self):
        """Chronicle 編纂が失敗したら anchor は据え置き (SEA 監査 S2)。スルースは
        走らない (退場は既に止まっており、パンマーカーだけ前進させない)。"""
        client = FakeLLMClient(_sluice_result())
        lifecycle, anchor_updates = self._make_metabolism_lifecycle(client)
        lifecycle.is_chronicle_enabled_for_persona = lambda p: True
        lifecycle.generate_chronicle = lambda p, cb=None, **kw: (_ for _ in ()).throw(RuntimeError("llm down"))
        messages = _metabolism_messages()
        persona = self._make_metabolism_persona(messages)

        window = _metabolism_window(messages)
        dispatched = []
        with patch.dict(os.environ, {"ENABLE_MEMORY_WEAVE_CONTEXT": "true"}), \
                patch.dict(os.environ, {"SAIVERSE_CHRONICLE_BAND_BUDGET": "2500"}), \
                patch("saiverse.dynamic_state.DynamicStateManager.on_metabolism",
                      lambda *a, **k: dispatched.append(k)):
            lifecycle.run_metabolism(persona, "b", window, _METABOLISM_WATERMARKS, None)

        # anchor 据え置き + 可視化 (head 再 capture) も走らない + sluice も呼ばれない。
        self.assertEqual(anchor_updates, [])
        self.assertEqual(dispatched, [])
        self.assertEqual(client.calls, [])

    def test_run_metabolism_chronicle_ok_advances_specified_model_only(self):
        """編纂 'ok' なら渡された model の行だけが前進し、可視化もその model で dispatch。"""
        client = FakeLLMClient(_sluice_result())
        messages = _metabolism_messages()
        lifecycle, anchor_updates = self._make_metabolism_lifecycle(
            client, presented_ids=[m["id"] for m in messages],
        )
        lifecycle.is_chronicle_enabled_for_persona = lambda p: True
        lifecycle.generate_chronicle = lambda p, cb=None, **kw: "ok"
        persona = self._make_metabolism_persona(messages)

        window = _metabolism_window(messages)
        dispatched = []
        with patch.dict(os.environ, {"ENABLE_MEMORY_WEAVE_CONTEXT": "true"}), \
                patch.dict(os.environ, {"SAIVERSE_CHRONICLE_BAND_BUDGET": "2500"}), \
                patch("saiverse.dynamic_state.DynamicStateManager.on_metabolism",
                      lambda p, m, model_key=None: dispatched.append(model_key)):
            lifecycle.run_metabolism(
                persona, "b", window, _METABOLISM_WATERMARKS, None, model_key="light-model",
            )

        self.assertEqual(anchor_updates, [("light-model", "m3")])
        self.assertEqual(dispatched, ["light-model"])


class SessionCloseTest(_AdapterTestBase):
    """sluice.run_session_close (Phase 3) を実 SAIMemory (temp DB) で検証する。"""

    # -- fixtures -------------------------------------------------------

    def _make_lifecycle(self, client, *, hot, chronicle_enabled=True,
                        presented_ids="auto"):
        from unittest.mock import MagicMock

        lifecycle = SimpleNamespace(
            runtime=FakeRuntime(client, presented_ids=presented_ids),
        )
        lifecycle.is_chronicle_enabled_for_persona = lambda p: chronicle_enabled
        lifecycle.generate_chronicle = MagicMock(return_value="ok")
        lifecycle.ensure_recall_embeddings = MagicMock()
        lifecycle._is_cache_hot = lambda p: hot
        # 初回 (pan マーカー無し) の window 起点は session_anchor 行から読まれる
        # (§6-5 で persona 属性は廃止)。
        lifecycle.load_anchor_entry = lambda pid, mk: {"anchor_id": "cache_anchor"}
        return lifecycle

    def _make_persona(self, messages, *, last_pan_id=None, building_id="b"):
        history_mgr = SimpleNamespace()
        history_mgr.get_history_from_anchor = (
            lambda anchor, required_line_roles=None, required_scopes=None: messages
        )
        persona = SimpleNamespace(
            persona_id="tester", persona_name="エア", model="claude-x",
            sai_memory=self.adapter, history_manager=history_mgr,
            current_building_id=building_id,
        )
        if last_pan_id is not None:
            persona._sluice_last_pan_id = last_pan_id
        return persona

    @staticmethod
    def _msgs(n):
        return [{"id": f"m{i}", "role": "user", "content": f"msg {i}"} for i in range(n)]

    # -- case 1: marker guard skips pan but Chronicle runs --------------

    def test_marker_guard_skips_pan_without_chronicle(self):
        msgs = self._msgs(12)
        client = FakeLLMClient(_sluice_result())
        lifecycle = self._make_lifecycle(client, hot=True)
        # marker が最新 id と一致 → 新規 0 件 → 採取スキップ。
        # 編纂はどの分岐でも呼ばれない (§13: クローズは編纂しない)。
        persona = self._make_persona(msgs, last_pan_id="m11")
        with patch.dict(os.environ, {"ENABLE_MEMORY_WEAVE_CONTEXT": "true"}), \
                patch("sea.sluice.run_sluice") as pan:
            result = sluice.run_session_close(lifecycle, persona)
        pan.assert_not_called()
        lifecycle.generate_chronicle.assert_not_called()
        self.assertFalse(result["panned"])
        self.assertFalse(result["chronicle"])
        self.assertEqual(result["skipped_reason"], "below_min")

    # -- case 2: enough new + hot -> pan runs, marker updates ------------

    def test_new_messages_hot_pans_and_updates_marker(self):
        msgs = self._msgs(5)
        client = FakeLLMClient(_sluice_result(reflection="採取なし"))
        lifecycle = self._make_lifecycle(
            client, hot=True, presented_ids=[m["id"] for m in msgs],
        )
        persona = self._make_persona(msgs, last_pan_id=None)
        with patch.dict(os.environ, {"SAIVERSE_SLUICE_CLOSE_MIN_MESSAGES": "3"}):
            result = sluice.run_session_close(lifecycle, persona)
        self.assertTrue(result["panned"])
        # 実 run_sluice が LLM を 1 回呼んでいる。
        self.assertEqual(len(client.calls), 1)
        # マーカーが窓の末尾 id に更新される。
        self.assertEqual(persona._sluice_last_pan_id, "m4")

    # -- case: window 起点は pan マーカー・性質フィルタ無し (2026-07-08 sophie 修正) --

    def _persona_recording_history(self, messages, *, last_pan_id=None):
        calls = []

        def fake_get(anchor, required_line_roles=None, required_scopes=None):
            calls.append((anchor, required_line_roles, required_scopes))
            return messages

        history_mgr = SimpleNamespace()
        history_mgr.get_history_from_anchor = fake_get
        persona = SimpleNamespace(
            persona_id="tester", persona_name="エア", model="claude-x",
            sai_memory=self.adapter, history_manager=history_mgr, current_building_id="b",
        )
        if last_pan_id is not None:
            persona._sluice_last_pan_id = last_pan_id
        return persona, calls

    def test_window_starts_from_pan_marker_without_line_filter(self):
        """window 起点は pan マーカー (cache 都合で動く metabolism anchor でない)、かつ
        性質フィルタ (main_line/committed) を付けない。metabolism anchor + main_line 絞りで
        window が 4 件に縮んだ sophie 問題の修正。"""
        persona, calls = self._persona_recording_history(self._msgs(12), last_pan_id="prev_pan")
        lifecycle = self._make_lifecycle(FakeLLMClient(_sluice_result()), hot=True)
        with patch.dict(os.environ, {"SAIVERSE_SLUICE_CLOSE_MIN_MESSAGES": "3"}):
            sluice.run_session_close(lifecycle, persona)
        start, lr, sc = calls[0]
        self.assertEqual(start, "prev_pan")   # metabolism anchor "cache_anchor" ではない
        self.assertIsNone(lr)                 # 性質フィルタ無し
        self.assertIsNone(sc)

    def test_initial_window_uses_anchor_as_read_head(self):
        """初回 (pan マーカー無し) は metabolism anchor を『読んでいる範囲の先頭』として
        起点に使い、全生履歴 (12件) を window にして採取する。"""
        persona, calls = self._persona_recording_history(self._msgs(12), last_pan_id=None)
        lifecycle = self._make_lifecycle(FakeLLMClient(_sluice_result()), hot=True)
        with patch.dict(os.environ, {"SAIVERSE_SLUICE_CLOSE_MIN_MESSAGES": "3"}):
            result = sluice.run_session_close(lifecycle, persona)
        self.assertEqual(calls[0][0], "cache_anchor")  # 初回は anchor 起点
        self.assertTrue(result["panned"])              # 12件全部新規 → close_min 3 超え

    # -- case 3: cold -> pan skipped ------------------------------------

    def test_cold_skips_pan_and_never_generates_chronicle(self):
        """§13 (arasuji_levels.md): クローズは編纂しない (cold は採取も skip)。"""
        msgs = self._msgs(5)
        client = FakeLLMClient(_sluice_result())
        lifecycle = self._make_lifecycle(client, hot=False)
        persona = self._make_persona(msgs, last_pan_id=None)
        with patch.dict(os.environ, {
            "ENABLE_MEMORY_WEAVE_CONTEXT": "true",
            "SAIVERSE_SLUICE_CLOSE_MIN_MESSAGES": "3",
        }), patch("sea.sluice.run_sluice") as pan:
            result = sluice.run_session_close(lifecycle, persona)
        pan.assert_not_called()
        lifecycle.generate_chronicle.assert_not_called()
        self.assertFalse(result["panned"])
        self.assertFalse(result["chronicle"])
        self.assertEqual(result["skipped_reason"], "cold")

    # -- case 4: in-flight guard + flag reset ---------------------------

    def test_inflight_guard_and_flag_reset(self):
        msgs = self._msgs(5)
        client = FakeLLMClient(_sluice_result())

        # (a) flag が立っていると再入は skip され、何も走らない。
        busy = self._make_persona(msgs, last_pan_id=None)
        busy._sluice_close_inflight = True
        lifecycle_a = self._make_lifecycle(client, hot=True)
        with patch.dict(os.environ, {"ENABLE_MEMORY_WEAVE_CONTEXT": "true"}), \
                patch("sea.sluice.run_sluice") as pan:
            result = sluice.run_session_close(lifecycle_a, busy)
        self.assertEqual(result["skipped_reason"], "inflight")
        pan.assert_not_called()
        lifecycle_a.generate_chronicle.assert_not_called()

        # (b) 正常終了後は flag が False に戻る。
        fresh = self._make_persona(msgs, last_pan_id=None)
        lifecycle_b = self._make_lifecycle(client, hot=True)
        with patch.dict(os.environ, {"SAIVERSE_SLUICE_CLOSE_MIN_MESSAGES": "3"}), \
                patch("sea.sluice.run_sluice"):
            sluice.run_session_close(lifecycle_b, fresh)
        self.assertFalse(getattr(fresh, "_sluice_close_inflight", True))

    # -- case 5: chain termination (2nd call pans nothing) --------------

    def test_chain_terminates_on_second_call(self):
        msgs = self._msgs(5)
        client = FakeLLMClient(_sluice_result())
        lifecycle = self._make_lifecycle(
            client, hot=True, presented_ids=[m["id"] for m in msgs],
        )
        persona = self._make_persona(msgs, last_pan_id=None)
        with patch.dict(os.environ, {"SAIVERSE_SLUICE_CLOSE_MIN_MESSAGES": "3"}):
            first = sluice.run_session_close(lifecycle, persona)
            second = sluice.run_session_close(lifecycle, persona)
        self.assertTrue(first["panned"])
        self.assertFalse(second["panned"])
        self.assertEqual(second["skipped_reason"], "below_min")
        # 追加 LLM コールが起きない = 連鎖はちょうど 1 回で止まる。
        self.assertEqual(len(client.calls), 1)

    # -- case 6: クローズ経路のマーカー安全ゲート (Codex 第六巡 修正 1) ------

    def test_close_withholds_marker_when_window_has_unpresented_tail(self):
        """クローズ側の窓 (無フィルタ — sub-line/discardable 込み) の末尾が LLM の
        提示対象に無いとき、マーカーはその ID を跨いで進まない (確定保留)。"""
        # 窓は w0..w11 (無フィルタ)。提示対象は main_line/committed の w0..w8 だけ
        # — 末尾 w9..w11 (sub-line/discardable 相当) を LLM は見ていない。
        msgs = self._msgs(12)  # id: m0..m11
        presented = [f"m{i}" for i in range(9)]
        client = FakeLLMClient(_sluice_result())
        lifecycle = self._make_lifecycle(
            client, hot=True, presented_ids=presented,
        )
        persona = self._make_persona(msgs, last_pan_id=None)
        with patch.dict(os.environ, {"SAIVERSE_SLUICE_CLOSE_MIN_MESSAGES": "3"}):
            result = sluice.run_session_close(lifecycle, persona)
        # スルース (LLM) は走ったが、マーカー候補 m11 は未提示 → 確定保留。
        self.assertEqual(len(client.calls), 1)
        self.assertTrue(result["panned"])
        self.assertEqual(result["skipped_reason"], "finalize_withheld")
        self.assertIsNone(getattr(persona, "_sluice_last_pan_id", None))
        from sai_memory.memory.storage import get_embed_metadata
        with self.adapter._db_lock:
            self.assertIsNone(
                get_embed_metadata(self.adapter.conn, sluice._PAN_MARKER_KEY),
            )

    def test_close_finalizes_when_marker_is_in_seen_set(self):
        """窓の末尾が提示対象に含まれていれば従来どおり確定 (マーカー前進)。"""
        msgs = self._msgs(5)
        client = FakeLLMClient(_sluice_result())
        lifecycle = self._make_lifecycle(
            client, hot=True, presented_ids=[m["id"] for m in msgs],
        )
        persona = self._make_persona(msgs, last_pan_id=None)
        with patch.dict(os.environ, {"SAIVERSE_SLUICE_CLOSE_MIN_MESSAGES": "3"}):
            result = sluice.run_session_close(lifecycle, persona)
        self.assertTrue(result["panned"])
        self.assertIsNone(result["skipped_reason"])
        self.assertEqual(persona._sluice_last_pan_id, "m4")

    # -- case 6b: クローズ経路もマーカー読み取り失敗で止まる (第八巡 修正 5) ---

    def test_close_marker_read_failure_propagates(self):
        """クローズ採取は best-effort だが、マーカーの読み取り失敗は「マーカー
        無し」へ丸めず送出する (呼び出しスレッドがログして終わる)。丸めると窓
        全体が新規扱いになり、採取済みの範囲を採り直したうえでマーカーを
        後ろへ書き戻しうる。"""
        msgs = self._msgs(12)
        client = FakeLLMClient(_sluice_result())
        lifecycle = self._make_lifecycle(client, hot=True)
        persona = self._make_persona(msgs, last_pan_id=None)
        with patch(
            "sai_memory.memory.storage.get_embed_metadata",
            side_effect=RuntimeError("db read error"),
        ):
            with self.assertRaises(RuntimeError):
                sluice.run_session_close(lifecycle, persona)
        self.assertEqual(client.calls, [])
        # in-flight フラグは finally で戻る (次回のクローズを塞がない)。
        self.assertFalse(getattr(persona, "_sluice_close_inflight", True))

    # -- case 7: クローズは編纂しない (§13) — weave 有効・hot でも呼ばれない ---

    def test_session_close_never_generates_chronicle(self):
        """arasuji_levels.md §13: 編纂の自動発火は予算超過の一本。クローズは
        採取 (pan) だけを行い、weave 有効・キャッシュ hot でも編纂しない。"""
        msgs = self._msgs(5)
        client = FakeLLMClient(_sluice_result())
        lifecycle = self._make_lifecycle(client, hot=True)
        persona = self._make_persona(msgs, last_pan_id=None)
        with patch.dict(os.environ, {
            "ENABLE_MEMORY_WEAVE_CONTEXT": "true",
            "SAIVERSE_SLUICE_CLOSE_MIN_MESSAGES": "3",
        }), patch("sea.sluice.run_sluice") as pan:
            result = sluice.run_session_close(lifecycle, persona)
        pan.assert_called_once()
        lifecycle.generate_chronicle.assert_not_called()
        self.assertTrue(result["panned"])
        self.assertFalse(result["chronicle"])


class KeepaliveSessionCloseHookTest(unittest.TestCase):
    """run_cache_keepalive の自律 OFF 分岐がセッションクローズを spawn すること。"""

    def test_not_active_branch_spawns_session_close(self):
        from sea.runtime import SEARuntime

        persona = SimpleNamespace(autonomy_enabled=False)
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
        self.assertEqual(key, "ttl:air:gem")
        # anchor validity 1200 × (1 - 0.3) = 840s
        self.assertAlmostEqual((fire_at - before).total_seconds(), 840, delta=5)

    def test_non_explicit_skipped_when_sluice_disabled(self):
        scheduled = []
        lc = self._make_lifecycle(scheduled)
        persona = SimpleNamespace(persona_id="air", model="gem")
        with patch.dict(os.environ, {"SAIVERSE_SLUICE_ENABLED": "0"}):
            lc.schedule_cache_ttl_pulse(persona, "gem", "gemini_explicit")
        self.assertEqual(scheduled, [])

    def test_explicit_schedules_regardless_of_sluice_flag(self):
        """explicit (Anthropic) の keep-alive 予約は sluice フラグに影響されない。"""
        scheduled = []
        lc = self._make_lifecycle(scheduled, ttl=3600, threshold=0.3)
        persona = SimpleNamespace(persona_id="air", model="claude-x")
        with patch.dict(os.environ, {"SAIVERSE_SLUICE_ENABLED": "0"}):
            lc.schedule_cache_ttl_pulse(persona, "claude-x", "explicit")
        self.assertEqual(len(scheduled), 1)
        fire_at, _callback, key = scheduled[0]
        self.assertEqual(key, "ttl:air:claude-x")

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
        self.assertEqual(scheduled, [("cancel", "ttl:air:claude-x")])


class KeepaliveNonExplicitBranchTest(unittest.TestCase):
    """run_cache_keepalive: 自律 ON + 非 explicit は LLM を呼ばず見張りだけ再予約。"""

    def test_active_non_explicit_reschedules_without_llm(self):
        from sea.runtime import SEARuntime

        persona = SimpleNamespace(autonomy_enabled=True, model="gem")
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
        """自律 OFF 分岐は cache 型非依存でクローズ spawn (gemini でも同じ)。"""
        from sea.runtime import SEARuntime

        persona = SimpleNamespace(autonomy_enabled=False, model="gem")
        rt = SimpleNamespace(manager=SimpleNamespace(personas={"air": persona}))
        spawned = []
        rt._spawn_session_close = lambda pid: spawned.append(pid)
        rt.run_cache_keepalive = SEARuntime.run_cache_keepalive.__get__(rt)

        result = rt.run_cache_keepalive("air")

        self.assertFalse(result)
        self.assertEqual(spawned, ["air"])


class PanMarkerPersistenceTest(_AdapterTestBase):
    """pan マーカーが memory.db (embed_metadata KV) に永続化され、属性キャッシュを
    持たない新 persona オブジェクト (プロセス再起動相当) でもガードが効くこと。"""

    def _fresh_persona(self):
        """属性キャッシュ (_sluice_last_pan_id) を持たない persona。

        プロセス再起動で in-memory 状態が消えた状態を模す。同じ memory.db を指す
        adapter を共有するため、永続ストアからの read-through を検証できる。
        """
        return SimpleNamespace(
            persona_id="tester", persona_name="エア", model="claude-x",
            sai_memory=self.adapter,
        )

    def _run_pan(self, persona, current_messages, result=None):
        client = FakeLLMClient(result or _sluice_result(reflection="採取なし"))
        lifecycle = SimpleNamespace(runtime=FakeRuntime(client))
        return sluice.run_sluice(
            lifecycle, persona, "b", current_messages, 0, None,
        )

    def _read_store(self, key=None):
        from sai_memory.memory.storage import get_embed_metadata
        with self.adapter._db_lock:
            return get_embed_metadata(self.adapter.conn, key or sluice._PAN_MARKER_KEY)

    # -- case 1: 永続ストアから read-through で読める (再起動相当) --------

    def test_marker_survives_persona_object_replacement(self):
        msgs = [{"id": f"m{i}", "content": "x"} for i in range(4)]
        self._run_pan(self._fresh_persona(), msgs)

        # 直接ストアにも末尾 id が書かれている。
        self.assertEqual(self._read_store(), "m3")

        # 属性キャッシュを持たない新 persona でも read-through でロードできる。
        reader = self._fresh_persona()
        self.assertIsNone(getattr(reader, "_sluice_last_pan_id", None))
        self.assertEqual(sluice._load_pan_marker(reader), "m3")
        # ロード後は属性にキャッシュされる。
        self.assertEqual(reader._sluice_last_pan_id, "m3")

    # -- case 1b: 旧 gold_panning 世代のキーから一回きり移行される ---------

    def test_marker_migrates_from_legacy_key(self):
        from sai_memory.memory.storage import set_embed_metadata
        with self.adapter._db_lock:
            set_embed_metadata(
                self.adapter.conn, sluice._LEGACY_PAN_MARKER_KEY, "legacy-id",
            )
        reader = self._fresh_persona()
        self.assertEqual(sluice._load_pan_marker(reader), "legacy-id")
        # 新キーへ写されている。
        self.assertEqual(self._read_store(), "legacy-id")

    # -- case 2: 新 persona で run_session_close のガードが効く -----------

    def test_marker_guard_effective_after_restart(self):
        from unittest.mock import MagicMock

        msgs = [{"id": f"m{i}", "content": "x"} for i in range(12)]
        self._run_pan(self._fresh_persona(), msgs)  # marker → m11

        # 再起動相当の fresh persona (属性キャッシュ無し) で run_session_close。
        history_mgr = SimpleNamespace()
        history_mgr.get_history_from_anchor = (
            lambda anchor, required_line_roles=None, required_scopes=None: msgs
        )
        reader = SimpleNamespace(
            persona_id="tester", persona_name="エア", model="claude-x",
            sai_memory=self.adapter, history_manager=history_mgr,
            current_building_id="b",
        )
        self.assertIsNone(getattr(reader, "_sluice_last_pan_id", None))

        lifecycle = SimpleNamespace(
            runtime=FakeRuntime(FakeLLMClient(_sluice_result())),
        )
        lifecycle.is_chronicle_enabled_for_persona = lambda p: False
        lifecycle.generate_chronicle = MagicMock()
        lifecycle.ensure_recall_embeddings = MagicMock()
        lifecycle._is_cache_hot = lambda p: True

        # close_min デフォルト (10) に対し新規 0 件 → below_min で採取スキップ。
        with patch("sea.sluice.run_sluice") as pan:
            result = sluice.run_session_close(lifecycle, reader)
        pan.assert_not_called()
        self.assertFalse(result["panned"])
        self.assertEqual(result["skipped_reason"], "below_min")
        # read-through で永続ストアの marker が属性へ昇格している。
        self.assertEqual(reader._sluice_last_pan_id, "m11")

    # -- case 2b: 読み取り失敗は「マーカー無し」に化けない (第八巡 修正 5) ----

    def test_marker_read_failure_is_fail_closed(self):
        """ストア読み出しの例外を None (= 初回 pan) へ丸めない。丸めると担当範囲が
        窓全体に広がって処理済みの履歴を採り直し、確定時にマーカーを現在値より
        後ろへ書き戻す縁ができる。"""
        msgs = [{"id": f"m{i}", "content": "x"} for i in range(4)]
        persona = self._fresh_persona()
        client = FakeLLMClient(_sluice_result())
        lifecycle = SimpleNamespace(runtime=FakeRuntime(client))
        with patch(
            "sai_memory.memory.storage.get_embed_metadata",
            side_effect=RuntimeError("db read error"),
        ):
            with self.assertRaises(RuntimeError):
                sluice.run_sluice(lifecycle, persona, "b", msgs, 0, None)
        self.assertEqual(client.calls, [])  # LLM を呼ぶ前に止まる
        self.assertIsNone(getattr(persona, "_sluice_last_pan_id", None))
        self.assertIsNone(self._read_store())

    def test_missing_marker_is_still_treated_as_first_pan(self):
        """未存在 (キーが無い) は従来どおり「初回 pan」— 読み取り失敗と区別する。"""
        self.assertIsNone(self._read_store())
        self.assertIsNone(sluice._load_pan_marker(self._fresh_persona()))

    # -- case 3: ストア書き込み失敗は fail-closed (Codex 第六巡 修正 2) ----

    def test_store_write_failure_fails_finalize_and_recovers_on_retry(self):
        """マーカー永続化の失敗は握り潰さず送出し、属性も進めない (属性だけ進むと
        確定していないのに次回の span 起点が動き、記録に合流できなくなる)。
        適用 (コア記憶) は済んでおり、再試行は冪等に完走する。"""
        msgs = [{"id": f"m{i}", "content": "x"} for i in range(4)]
        persona = self._fresh_persona()
        result = {
            **_sluice_result(reflection="赴任を覚える"),
            "ops": [{"op": "add", "content": "テスト用のコア記憶"}],
        }
        with patch(
            "sai_memory.memory.storage.set_embed_metadata",
            side_effect=RuntimeError("disk full"),
        ):
            with self.assertRaises(RuntimeError):
                self._run_pan(persona, msgs, result=result)

        # マーカーは属性も永続も進んでいない。
        self.assertIsNone(getattr(persona, "_sluice_last_pan_id", None))
        self.assertIsNone(self._read_store())
        # 適用 (コア記憶) は済んでいる — finalize より前の段。
        from sai_memory.core_memory import list_core_memories
        with self.adapter._db_lock:
            cores = list_core_memories(self.adapter.conn)
        self.assertEqual(len(cores), 1)

        # 再試行 (ストア回復): 完走し、コア記憶は内容一致ガードで重複しない。
        retry = self._fresh_persona()
        summary = self._run_pan(retry, msgs, result=result)
        self.assertEqual(summary["ops_applied"], 1)  # 同一内容スキップも成功扱い
        self.assertEqual(retry._sluice_last_pan_id, "m3")
        self.assertEqual(self._read_store(), "m3")
        with self.adapter._db_lock:
            cores = list_core_memories(self.adapter.conn)
        self.assertEqual(len(cores), 1)


if __name__ == "__main__":
    unittest.main()
