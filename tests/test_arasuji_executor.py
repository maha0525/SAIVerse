"""チャンク実行器 (sai_memory/arasuji/executor.py) の回帰テスト。

docs/intent/arasuji_levels.md — レベル0 の畳みの実行側を固定する。
旧仕様の恒等圧縮 (identity) / digest 転写 (episode) は廃止され、
content 生成は常に LLM (小さくても要約する)。

- チャンク 1 個 = 単一 tx。途中失敗しても確定済みチャンクは残り、再試行は
  重複再検査で冪等 (M2 の生成側)。
- 由来メタ (digest_origin="batch" / coverage_chars / episode_refs) が entry に
  刻まれる。
- batch_callback (Fragment 抽出) は全チャンクで呼ばれる。
"""

import sqlite3
import unittest

from llm_clients.exceptions import EmptyResponseError, LLMError, RateLimitError
from sai_memory.arasuji.alignment import (
    CHUNK_LLM_BATCH,
    AlignmentPlan,
    PlannedChunk,
)
from sai_memory.arasuji.executor import execute_plan
from sai_memory.arasuji.storage import get_entries_by_level, init_arasuji_tables
from sai_memory.memory.storage import Message


def _msg(mid, content, episode=None, created_at=None, tags=None):
    metadata = {}
    if episode:
        metadata["origin_episode"] = episode
    if tags:
        metadata["tags"] = list(tags)
    return Message(
        id=mid,
        thread_id="main",
        role="user",
        content=content,
        resource_id=None,
        created_at=created_at if created_at is not None else int(mid.replace("m", "")),
        metadata=metadata or None,
    )


def _chunk(messages, *, refs=()):
    """LLM バッチチャンク (現設計では全チャンクがこの形)。"""
    return PlannedChunk(
        kind=CHUNK_LLM_BATCH,
        messages=list(messages),
        episode_refs=list(refs),
        coverage_chars=sum(len(m.content or "") for m in messages),
    )


def _plan(*chunks):
    return AlignmentPlan(
        chunks=list(chunks),
        total_unprocessed=sum(len(c.messages) for c in chunks),
    )


class _CountingClient:
    """generate の呼び出しを数える mock LLM client。"""

    def __init__(self, response="生成されたあらすじ。", fail_on_call=None):
        self.calls = 0
        self.prompts = []
        self.response = response
        self.fail_on_call = fail_on_call

    def generate(self, messages, tools):
        self.calls += 1
        self.prompts.append(messages[0]["content"])
        if self.fail_on_call is not None and self.calls == self.fail_on_call:
            raise RuntimeError("llm down")
        return self.response


class _SequenceClient:
    """呼び出しごとに決まった応答 (文字列) か例外を順に返す mock LLM client。"""

    def __init__(self, responses):
        self.calls = 0
        self.responses = list(responses)

    def generate(self, messages, tools):
        self.calls += 1
        item = self.responses[self.calls - 1]
        if isinstance(item, BaseException):
            raise item
        return item

    def consume_usage(self):
        return None


class ExecutorTestBase(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        init_arasuji_tables(self.conn)
        self.addCleanup(self.conn.close)

    def _lv1_entries(self):
        return get_entries_by_level(self.conn, 1, order_by_time=True)

    def _entry_meta(self, entry_id):
        import json
        row = self.conn.execute(
            "SELECT metadata FROM memopedia_pages WHERE id = ?", (entry_id,)
        ).fetchone()
        return json.loads(row[0])


class TestChunkExecution(ExecutorTestBase):
    def test_chunk_content_is_llm_generated(self):
        """content は LLM 生成で、由来メタが entry に刻まれる。"""
        client = _CountingClient(response="要約されたあらすじ。")
        chunk = _chunk(
            [_msg("m1", "a" * 50, episode="episode:1"), _msg("m2", "b" * 50)],
            refs=["episode:1"],
        )
        result = execute_plan(_plan(chunk), client, self.conn)
        self.assertEqual(client.calls, 1)
        self.assertEqual(result.created_count, 1)
        entry = self._lv1_entries()[0]
        self.assertEqual(entry.content, "要約されたあらすじ。")
        self.assertEqual(entry.source_ids, ["m1", "m2"])
        meta = self._entry_meta(entry.id)
        self.assertEqual(meta["digest_origin"], "batch")
        self.assertEqual(meta["coverage_chars"], 100)
        self.assertEqual(meta["episode_refs"], ["episode:1"])

    def test_tiny_chunk_still_uses_llm(self):
        """小さいチャンクも LLM 圧縮する (恒等圧縮の廃止 — 小さくても要約する)。"""
        client = _CountingClient()
        result = execute_plan(_plan(_chunk([_msg("m1", "こんにちは")])), client, self.conn)
        self.assertEqual(client.calls, 1)
        self.assertEqual(result.created_count, 1)
        self.assertEqual(self._lv1_entries()[0].content, "生成されたあらすじ。")

    def test_llm_empty_response_retries_then_commits(self):
        """空応答 (推論モデルが reasoning_content だけで閉じる) は既定 3 回まで
        試し直す — 2 回空のあと本文が返ればチャンクは確定する (2026-09-03)。"""
        client = _SequenceClient(["", "\n", "三度目のあらすじ。"])
        with self.assertLogs("sai_memory.arasuji.generator", level="WARNING") as logs:
            result = execute_plan(
                _plan(_chunk([_msg("m1", "a" * 50)])), client, self.conn,
            )
        self.assertEqual(client.calls, 3)
        self.assertEqual(result.created_count, 1)
        self.assertEqual(self._lv1_entries()[0].content, "三度目のあらすじ。")
        retry_lines = [m for m in logs.output if "empty LLM response" in m]
        self.assertEqual(len(retry_lines), 2)
        self.assertIn("attempt 1/3", retry_lines[0])
        self.assertIn("attempt 2/3", retry_lines[1])

    def test_llm_empty_response_exhausted_raises_empty_response_error(self):
        """3 回とも空なら EmptyResponseError (error_code=empty_response) が
        batch_meta 付きで propagate し、走行はそのチャンクで止まる (確定なし)。"""
        client = _SequenceClient(["", "   ", ""])
        with self.assertRaises(EmptyResponseError) as ctx:
            execute_plan(_plan(_chunk([_msg("m1", "a" * 50)])), client, self.conn)
        exc = ctx.exception
        self.assertEqual(client.calls, 3)
        self.assertEqual(exc.error_code, "empty_response")
        self.assertIn("チャンク処理中", exc.user_message)
        self.assertEqual(exc.batch_meta["message_ids"], ["m1"])
        self.assertEqual(self._lv1_entries(), [])

    def test_empty_response_error_from_client_is_retried_too(self):
        """クライアントが EmptyResponseError を投げる形 (openai.py の実装) も
        空文字と同じく再試行の対象。"""
        client = _SequenceClient([EmptyResponseError("empty"), "二度目で出た。"])
        result = execute_plan(_plan(_chunk([_msg("m1", "a" * 50)])), client, self.conn)
        self.assertEqual(client.calls, 2)
        self.assertEqual(result.created_count, 1)

    def test_other_llm_errors_are_not_retried(self):
        """空応答以外の LLMError (rate limit 等) は 1 回目でそのまま上がる。"""
        client = _SequenceClient([RateLimitError("429"), "来ないはず"])
        with self.assertRaises(RateLimitError):
            execute_plan(_plan(_chunk([_msg("m1", "a" * 50)])), client, self.conn)
        self.assertEqual(client.calls, 1)
        self.assertEqual(self._lv1_entries(), [])

    def test_llm_error_propagates_with_batch_meta(self):
        """LLMError は文脈 (user_message / batch_meta) を付けて propagate する
        (frontend がバッチナビゲーションに使う契約)。"""

        class _FailingClient:
            def generate(self, messages, tools):
                raise LLMError("boom", user_message="上流のエラー")

        with self.assertRaises(LLMError) as ctx:
            execute_plan(
                _plan(_chunk([_msg("m1", "a" * 50)])), _FailingClient(), self.conn,
            )
        exc = ctx.exception
        self.assertIn("チャンク処理中", exc.user_message)
        self.assertIn("上流のエラー", exc.user_message)
        self.assertEqual(exc.batch_meta["message_ids"], ["m1"])
        self.assertEqual(self._lv1_entries(), [])


class TestSessionDigestLabel(ExecutorTestBase):
    def test_session_digest_message_is_labeled_in_prompt(self):
        """tags に 'session_digest' を含む行はプロンプトで [作業のまとめ] と
        印が付き、扱い方の指示行も入る (arasuji_levels.md §3-4 種別明示)。"""
        client = _CountingClient()
        chunk = _chunk([
            _msg("m1", "普通の会話"),
            _msg("m2", "作業セッションのダイジェスト本文", tags=["session_digest"]),
        ])
        execute_plan(_plan(chunk), client, self.conn)
        self.assertEqual(len(client.prompts), 1)
        prompt = client.prompts[0]
        self.assertIn("[作業のまとめ]", prompt)
        self.assertIn("[作業のまとめ] と印の付いた項目は", prompt)

    def test_plain_message_is_not_labeled(self):
        client = _CountingClient()
        execute_plan(_plan(_chunk([_msg("m1", "普通の会話")])), client, self.conn)
        # 指示行の分は常に入るが、材料行への印は付かない
        material_lines = [
            line for line in client.prompts[0].splitlines()
            if "普通の会話" in line
        ]
        self.assertTrue(material_lines)
        self.assertFalse(any("[作業のまとめ]" in line for line in material_lines))


class TestPartialFailureIdempotency(ExecutorTestBase):
    """チャンク途中失敗 → 確定済みは残る → 再試行で二重生成しない (M2)。"""

    def _two_chunk_plan(self):
        return _plan(
            _chunk([_msg("m1", "a" * 50)]),
            _chunk([_msg("m2", "b" * 50)]),
        )

    def test_mid_failure_keeps_committed_and_retry_skips(self):
        # 2 チャンク目の LLM で失敗
        client = _CountingClient(fail_on_call=2)
        with self.assertRaises(RuntimeError):
            execute_plan(self._two_chunk_plan(), client, self.conn)
        self.assertEqual(len(self._lv1_entries()), 1)  # 1 個目は確定済み

        # 同一 plan の再実行: 1 個目は重複再検査で skip、2 個目だけ生成
        client2 = _CountingClient()
        result = execute_plan(self._two_chunk_plan(), client2, self.conn)
        self.assertEqual(client2.calls, 1)
        self.assertEqual(result.created_count, 1)
        self.assertEqual(result.skipped_duplicates, 1)
        self.assertEqual(len(self._lv1_entries()), 2)  # 二重生成なし

    def test_cancel_stops_remaining_chunks(self):
        cancels = iter([False, True])
        client = _CountingClient()
        result = execute_plan(
            self._two_chunk_plan(), client, self.conn,
            cancel_check=lambda: next(cancels),
        )
        self.assertTrue(result.cancelled)
        self.assertEqual(result.created_count, 1)

    def test_in_tx_recheck_skips_concurrent_compilation(self):
        """事前検査から LLM の間に別コネクションが同じ先頭 source を確定した
        場合、tx 内再検査が INSERT を止める (Codex W4 #1)。"""
        outer_conn = self.conn

        class _RacingClient:
            """LLM 呼び出し中に並走ジョブの確定を模擬する client。"""

            def generate(self, messages, tools):
                from sai_memory.arasuji.storage import create_entry
                create_entry(
                    outer_conn, level=1, content="並走ジョブの先着分",
                    source_ids=["m1"], start_time=1, end_time=1,
                    source_count=1, message_count=1,
                )
                return "後着の生成結果"

            def consume_usage(self):
                return None

        chunk = _chunk([_msg("m1", "a" * 50)])
        result = execute_plan(_plan(chunk), _RacingClient(), self.conn)
        self.assertEqual(result.created_count, 0)
        self.assertEqual(result.skipped_duplicates, 1)
        # 先着分 1 件だけが残る (二重 INSERT なし)
        entries = self._lv1_entries()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].content, "並走ジョブの先着分")


class TestBatchCallback(ExecutorTestBase):
    def test_callback_called_for_every_chunk(self):
        """Fragment 抽出の発火点は全チャンク (恒等圧縮の廃止で一本化)。"""
        seen = []

        def callback(messages, entry_id):
            seen.append((len(messages), entry_id is not None))

        plan = _plan(
            _chunk([_msg("m1", "hi")]),  # 小チャンクでも呼ばれる
            _chunk([_msg("m2", "a" * 50), _msg("m3", "b" * 50)]),
        )
        execute_plan(plan, _CountingClient(), self.conn, batch_callback=callback)
        self.assertEqual(seen, [(1, True), (2, True)])

    def test_callback_failure_does_not_stop_execution(self):
        def bad_callback(messages, entry_id):
            raise RuntimeError("extractor down")

        plan = _plan(
            _chunk([_msg("m1", "a" * 50)]),
            _chunk([_msg("m2", "b" * 50)]),
        )
        result = execute_plan(plan, _CountingClient(), self.conn,
                              batch_callback=bad_callback)
        self.assertEqual(result.created_count, 2)

    def test_callback_failure_is_recorded_not_swallowed(self):
        """⭐ 抽出の失敗は握り潰さず ExecutionResult に entry id で残す。

        確定済みチャンクは再実行で冪等スキップされ batch_callback が再発火しない
        ため、記録しなければ記憶の抽出が黙って落ちる
        (docs/issues/memopedia_writers_bypass_adapter_lock.md)。
        """
        calls = []

        def flaky_callback(messages, entry_id):
            calls.append(entry_id)
            if len(calls) == 1:
                raise RuntimeError("extractor down")

        plan = _plan(
            _chunk([_msg("m1", "a" * 50)]),
            _chunk([_msg("m2", "b" * 50)]),
        )
        result = execute_plan(plan, _CountingClient(), self.conn,
                              batch_callback=flaky_callback)
        self.assertEqual(result.created_count, 2)
        # 1 チャンク目だけ失敗 → その entry id が記録され、2 チャンク目は載らない
        self.assertEqual(result.extraction_failures, [calls[0]])
        # 付箋 (backlog) にも貼られている — 次の Metabolism が拾い直す
        backlog = self.conn.execute(
            "SELECT entry_id, attempts FROM entity_extraction_backlog"
        ).fetchall()
        self.assertEqual(backlog, [(calls[0], 1)])
        # 付箋に残せた分は「やり直せない側」に入らない
        self.assertEqual(result.extraction_failures_unrecorded, [])

    def test_a_failure_that_cannot_be_noted_is_reported_separately(self):
        """⭐ 付箋にも残せなかった失敗は、拾い直せる失敗と分けて返す。

        分けないと「次回の記憶の整理でやり直します」という画面の報告が、
        やり直しようのない相手にも出てしまう (嘘の約束になる)。
        """
        from unittest.mock import patch

        def bad_callback(messages, entry_id):
            raise RuntimeError("extractor down")

        plan = _plan(_chunk([_msg("m1", "a" * 50)]))
        with patch(
            "sai_memory.memory.entity_extractor.record_extraction_failure",
            side_effect=RuntimeError("backlog table is gone"),
        ):
            result = execute_plan(plan, _CountingClient(), self.conn,
                                  batch_callback=bad_callback)

        self.assertEqual(result.created_count, 1)
        self.assertEqual(len(result.extraction_failures), 1)
        self.assertEqual(
            result.extraction_failures_unrecorded, result.extraction_failures,
        )


class TestAfterChunkHook(ExecutorTestBase):
    """after_chunk (2026-09-03): チャンク確定ごとに (done, total) で呼ぶ。
    呼び出し元はここで上位あらすじの束ねを挟む (後続チャンクの「これまでの
    流れ」が階層を見られるように)。"""

    def test_called_once_per_committed_chunk(self):
        seen = []
        plan = _plan(
            _chunk([_msg("m1", "a" * 50)]),
            _chunk([_msg("m2", "b" * 50)]),
            _chunk([_msg("m3", "c" * 50)]),
        )
        result = execute_plan(
            plan, _CountingClient(), self.conn,
            after_chunk=lambda done, total: seen.append((done, total)),
        )
        self.assertEqual(result.created_count, 3)
        self.assertEqual(seen, [(1, 3), (2, 3), (3, 3)])

    def test_not_called_for_skipped_duplicates(self):
        seen = []
        plan = _plan(
            _chunk([_msg("m1", "a" * 50)]),
            _chunk([_msg("m2", "b" * 50)]),
        )
        execute_plan(plan, _CountingClient(), self.conn)
        # 同じ計画をもう一度 — 全チャンクが重複スキップされ、hook は鳴らない。
        result = execute_plan(
            plan, _CountingClient(), self.conn,
            after_chunk=lambda done, total: seen.append((done, total)),
        )
        self.assertEqual(result.skipped_duplicates, 2)
        self.assertEqual(seen, [])

    def test_raising_hook_does_not_stop_the_compile(self):
        calls = []

        def bad_hook(done, total):
            calls.append(done)
            raise RuntimeError("bands down")

        plan = _plan(
            _chunk([_msg("m1", "a" * 50)]),
            _chunk([_msg("m2", "b" * 50)]),
        )
        result = execute_plan(
            plan, _CountingClient(), self.conn, after_chunk=bad_hook,
        )
        self.assertEqual(result.created_count, 2)
        self.assertEqual(calls, [1, 2])
        self.assertEqual(len(self._lv1_entries()), 2)


if __name__ == "__main__":
    unittest.main()
