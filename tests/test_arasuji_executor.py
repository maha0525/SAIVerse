"""チャンク実行器 (sai_memory/arasuji/executor.py) の回帰テスト (W4 D4)。

- チャンク 1 個 = 単一 tx。途中失敗しても確定済みチャンクは残り、再試行は
  重複再検査で冪等 (M2 の生成側)。
- 恒等転写 (episode) / 恒等圧縮 (identity) は LLM を呼ばない。
- 由来メタ (digest_origin / coverage_chars / episode_refs) が entry に刻まれる。
"""

import sqlite3
import unittest
from types import SimpleNamespace

from sai_memory.arasuji.alignment import (
    CHUNK_EPISODE_DIGEST,
    CHUNK_IDENTITY,
    CHUNK_LLM_BATCH,
    AlignmentPlan,
    PlannedChunk,
)
from sai_memory.arasuji.executor import (
    ChunkExecutionError,
    execute_plan,
)
from sai_memory.arasuji.storage import get_entries_by_level, init_arasuji_tables
from sai_memory.memory.storage import Message


def _msg(mid, content, episode=None, created_at=None):
    return Message(
        id=mid,
        thread_id="main",
        role="user",
        content=content,
        resource_id=None,
        created_at=created_at if created_at is not None else int(mid.replace("m", "")),
        metadata={"origin_episode": episode} if episode else None,
    )


def _chunk(kind, messages, *, refs=(), digest_mid=None, digest_text=None):
    return PlannedChunk(
        kind=kind,
        messages=list(messages),
        episode_refs=list(refs),
        coverage_chars=sum(len(m.content or "") for m in messages),
        digest_message_id=digest_mid,
        digest_text=digest_text,
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
        self.response = response
        self.fail_on_call = fail_on_call

    def generate(self, messages, tools):
        self.calls += 1
        if self.fail_on_call is not None and self.calls == self.fail_on_call:
            raise RuntimeError("llm down")
        return self.response


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


class TestChunkKinds(ExecutorTestBase):
    def test_identity_chunk_no_llm(self):
        """恒等圧縮 (§4-3): LLM を呼ばず生ログ整形をそのまま置く。"""
        client = _CountingClient()
        chunk = _chunk(CHUNK_IDENTITY, [_msg("m1", "こんにちは")])
        result = execute_plan(_plan(chunk), client, self.conn)
        self.assertEqual(client.calls, 0)
        self.assertEqual(result.created_count, 1)
        entry = self._lv1_entries()[0]
        self.assertIn("こんにちは", entry.content)
        meta = self._entry_meta(entry.id)
        self.assertEqual(meta["digest_origin"], "identity")
        self.assertEqual(meta["coverage_chars"], 5)

    def test_episode_digest_chunk_transcribes(self):
        """恒等転写 (§4-4): digest 本文をそのまま級 1 ノードに置く (LLM なし)。"""
        client = _CountingClient()
        chunk = _chunk(
            CHUNK_EPISODE_DIGEST,
            [_msg("m1", "a" * 30, episode="episode:1"),
             _msg("m2", "b" * 30, episode="episode:1")],
            refs=["episode:1"],
            digest_mid="digest-mid",
            digest_text="セッションの要約テキスト",
        )
        result = execute_plan(_plan(chunk), client, self.conn)
        self.assertEqual(client.calls, 0)
        self.assertEqual(result.created_count, 1)
        entry = self._lv1_entries()[0]
        self.assertEqual(entry.content, "セッションの要約テキスト")
        self.assertEqual(entry.source_ids, ["m1", "m2"])
        meta = self._entry_meta(entry.id)
        self.assertEqual(meta["digest_origin"], "episode")
        self.assertEqual(meta["episode_refs"], ["episode:1"])
        self.assertEqual(meta["coverage_chars"], 60)

    def test_episode_digest_empty_text_fails(self):
        chunk = _chunk(
            CHUNK_EPISODE_DIGEST, [_msg("m1", "x", episode="episode:1")],
            refs=["episode:1"], digest_mid="d", digest_text="  ",
        )
        with self.assertRaises(ChunkExecutionError):
            execute_plan(_plan(chunk), _CountingClient(), self.conn)

    def test_llm_batch_chunk_calls_llm(self):
        client = _CountingClient(response="要約されたあらすじ。")
        chunk = _chunk(CHUNK_LLM_BATCH, [_msg("m1", "a" * 50), _msg("m2", "b" * 50)])
        result = execute_plan(_plan(chunk), client, self.conn)
        self.assertEqual(client.calls, 1)
        self.assertEqual(result.created_count, 1)
        entry = self._lv1_entries()[0]
        self.assertEqual(entry.content, "要約されたあらすじ。")
        self.assertEqual(self._entry_meta(entry.id)["digest_origin"], "batch")

    def test_llm_empty_response_raises(self):
        client = _CountingClient(response="   ")
        chunk = _chunk(CHUNK_LLM_BATCH, [_msg("m1", "a" * 50)])
        with self.assertRaises(ChunkExecutionError):
            execute_plan(_plan(chunk), client, self.conn)
        self.assertEqual(self._lv1_entries(), [])


class TestPartialFailureIdempotency(ExecutorTestBase):
    """チャンク途中失敗 → 確定済みは残る → 再試行で二重生成しない (M2)。"""

    def _two_chunk_plan(self):
        return _plan(
            _chunk(CHUNK_LLM_BATCH, [_msg("m1", "a" * 50)]),
            _chunk(CHUNK_LLM_BATCH, [_msg("m2", "b" * 50)]),
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

        chunk = _chunk(CHUNK_LLM_BATCH, [_msg("m1", "a" * 50)])
        result = execute_plan(_plan(chunk), _RacingClient(), self.conn)
        self.assertEqual(result.created_count, 0)
        self.assertEqual(result.skipped_duplicates, 1)
        # 先着分 1 件だけが残る (二重 INSERT なし)
        entries = self._lv1_entries()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].content, "並走ジョブの先着分")


class TestBatchCallback(ExecutorTestBase):
    def test_callback_for_llm_and_episode_not_identity(self):
        """Fragment 抽出は llm_batch / episode_digest のみ (identity はスキップ)。"""
        seen = []

        def callback(messages, entry_id):
            seen.append((len(messages), entry_id is not None))

        plan = _plan(
            _chunk(CHUNK_IDENTITY, [_msg("m1", "hi")]),
            _chunk(
                CHUNK_EPISODE_DIGEST, [_msg("m2", "a" * 30, episode="episode:1")],
                refs=["episode:1"], digest_mid="d", digest_text="要約",
            ),
            _chunk(CHUNK_LLM_BATCH, [_msg("m3", "b" * 50)]),
        )
        execute_plan(_plan(*plan.chunks), _CountingClient(), self.conn,
                     batch_callback=callback)
        self.assertEqual(len(seen), 2)  # identity は呼ばれない

    def test_callback_failure_does_not_stop_execution(self):
        def bad_callback(messages, entry_id):
            raise RuntimeError("extractor down")

        plan = _plan(
            _chunk(CHUNK_LLM_BATCH, [_msg("m1", "a" * 50)]),
            _chunk(CHUNK_LLM_BATCH, [_msg("m2", "b" * 50)]),
        )
        result = execute_plan(plan, _CountingClient(), self.conn,
                              batch_callback=bad_callback)
        self.assertEqual(result.created_count, 2)


if __name__ == "__main__":
    unittest.main()
