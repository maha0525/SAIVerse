"""Episode 整列器 (sai_memory/arasuji/alignment.py) の回帰テスト。

体験の構造 (docs/intent/experience_structure.md) §4 圧縮七原則のうち、
整列計画で固定できる §4-2 / §4-3 / §4-4 / §4-5 を原則番号つきで固定する。
(§4-1 退場時圧縮は生成経路側 test_metabolism_two_layer、§4-6/§4-7 は
帯あふれ側のテストが持つ。)

設計正典: docs/handoff/2026-07-21_w4_metabolism_ledger_handoff.md D3。
"""

import unittest

from sai_memory.arasuji.alignment import (
    CHUNK_EPISODE_DIGEST,
    CHUNK_IDENTITY,
    CHUNK_LLM_BATCH,
    AlignmentPlan,
    message_episode_ref,
    plan_alignment,
)
from sai_memory.memory.storage import Message

# テスト用の小さいパラメータ (既定 10000/1000 だと本文が長くなりすぎる)
TARGET = 100
MIN_LLM = 20


def _msg(mid, content, episode=None, created_at=None):
    """テストメッセージ。episode は metadata 側 (実運用の主経路) に置く。"""
    return Message(
        id=mid,
        thread_id="main",
        role="user",
        content=content,
        resource_id=None,
        created_at=created_at if created_at is not None else int(mid.replace("m", "")),
        metadata={"origin_episode": episode} if episode else None,
    )


def _plan(messages, processed=(), digests=None):
    return plan_alignment(
        messages,
        set(processed),
        digests or {},
        target_chars=TARGET,
        min_llm_chars=MIN_LLM,
    )


class TestMessageEpisodeRef(unittest.TestCase):
    def test_reads_dedicated_column_first(self):
        m = _msg("m1", "x")
        m.origin_episode = "episode:9"
        m.metadata = {"origin_episode": "episode:1"}
        self.assertEqual(message_episode_ref(m), "episode:9")

    def test_falls_back_to_metadata(self):
        m = _msg("m1", "x", episode="episode:1")
        self.assertEqual(message_episode_ref(m), "episode:1")

    def test_none_when_absent(self):
        self.assertIsNone(message_episode_ref(_msg("m1", "x")))


class TestRunSplitting(unittest.TestCase):
    """§4-5 連続束ねのみ — 編纂済みを跨いだ束ねをしない。"""

    def test_processed_message_splits_runs(self):
        msgs = [
            _msg("m1", "a" * 30),
            _msg("m2", "b" * 30),  # processed → run 境界
            _msg("m3", "c" * 30),
        ]
        plan = _plan(msgs, processed=["m2"])
        # m1 と m3 は別チャンク (跨いで束ねない)
        self.assertEqual(len(plan.chunks), 2)
        self.assertEqual(plan.chunks[0].message_ids, ["m1"])
        self.assertEqual(plan.chunks[1].message_ids, ["m3"])
        self.assertEqual(plan.total_unprocessed, 2)

    def test_all_processed_yields_empty_plan(self):
        msgs = [_msg("m1", "a"), _msg("m2", "b")]
        plan = _plan(msgs, processed=["m1", "m2"])
        self.assertEqual(plan.chunks, [])
        self.assertEqual(plan.total_unprocessed, 0)


class TestEpisodeDigestChunks(unittest.TestCase):
    """§4-4 同一レベルの再圧縮禁止 — digest 済み episode は恒等転写で単独。"""

    def test_digested_episode_is_standalone_chunk(self):
        msgs = [
            _msg("m1", "a" * 30),
            _msg("m2", "b" * 30, episode="episode:1"),
            _msg("m3", "c" * 30, episode="episode:1"),
            _msg("m4", "d" * 30),
        ]
        digests = {"episode:1": ("digest-mid", "セッションの要約")}
        plan = _plan(msgs, digests=digests)
        kinds = [c.kind for c in plan.chunks]
        self.assertIn(CHUNK_EPISODE_DIGEST, kinds)
        ep = next(c for c in plan.chunks if c.kind == CHUNK_EPISODE_DIGEST)
        # digest 済み範囲は前後の材料と混ざらない
        self.assertEqual(ep.message_ids, ["m2", "m3"])
        self.assertEqual(ep.digest_message_id, "digest-mid")
        self.assertEqual(ep.digest_text, "セッションの要約")
        self.assertEqual(ep.episode_refs, ["episode:1"])
        # 前後は digest チャンクに巻き込まれない
        for c in plan.chunks:
            if c.kind != CHUNK_EPISODE_DIGEST:
                self.assertNotIn("m2", c.message_ids)
                self.assertNotIn("m3", c.message_ids)

    def test_undigested_episode_is_batch_material(self):
        """digest の無い episode は通常材料 (§4-4 の対象外)。"""
        msgs = [
            _msg("m1", "a" * 30, episode="episode:1"),
            _msg("m2", "b" * 30, episode="episode:1"),
        ]
        plan = _plan(msgs)
        self.assertEqual(len(plan.chunks), 1)
        self.assertEqual(plan.chunks[0].kind, CHUNK_LLM_BATCH)

    def test_split_episode_second_segment_demoted(self):
        """分裂した digest 済み episode の 2 個目以降は batch 材料に降格
        (同じ digest の二重転写防止)。"""
        msgs = [
            _msg("m1", "a" * 30, episode="episode:1"),
            _msg("m2", "x" * 30),  # 帰属切れ (タグ付与漏れ等)
            _msg("m3", "b" * 30, episode="episode:1"),
        ]
        digests = {"episode:1": ("digest-mid", "要約")}
        plan = _plan(msgs, digests=digests)
        ep_chunks = [c for c in plan.chunks if c.kind == CHUNK_EPISODE_DIGEST]
        self.assertEqual(len(ep_chunks), 1)
        self.assertEqual(ep_chunks[0].message_ids, ["m1"])
        # m3 は batch/identity 側に落ちる
        others = [m for c in plan.chunks if c.kind != CHUNK_EPISODE_DIGEST for m in c.message_ids]
        self.assertIn("m3", others)

    def test_split_episode_across_runs_transcribed_once(self):
        """processed 挟みで同じ episode が別 run に分裂しても転写は計画全体で
        1 回だけ (Codex W4 #6 — run 毎リセットの穴)。"""
        msgs = [
            _msg("m1", "a" * 30, episode="episode:1"),
            _msg("m2", "x" * 30),  # processed → run 境界
            _msg("m3", "b" * 30, episode="episode:1"),
        ]
        digests = {"episode:1": ("digest-mid", "要約")}
        plan = _plan(msgs, processed=["m2"], digests=digests)
        ep_chunks = [c for c in plan.chunks if c.kind == CHUNK_EPISODE_DIGEST]
        self.assertEqual(len(ep_chunks), 1)
        self.assertEqual(ep_chunks[0].message_ids, ["m1"])
        # m3 は別 run で batch/identity 材料に落ちる (二重転写しない)
        others = [m for c in plan.chunks if c.kind != CHUNK_EPISODE_DIGEST for m in c.message_ids]
        self.assertIn("m3", others)


class TestBundling(unittest.TestCase):
    """§4-2 原子であって量子ではない — episode は分割禁止・結合可。"""

    def test_small_episodes_bundle_until_target(self):
        # 各 40 字の episode 3 個 → 合計 120 ≥ TARGET(100) で 1 チャンク目が
        # 閉じ、残りが端数チャンクになる
        msgs = [
            _msg("m1", "a" * 40, episode="episode:1"),
            _msg("m2", "b" * 40, episode="episode:2"),
            _msg("m3", "c" * 40, episode="episode:3"),
            _msg("m4", "d" * 40, episode="episode:4"),
        ]
        plan = _plan(msgs)
        self.assertEqual(plan.chunks[0].kind, CHUNK_LLM_BATCH)
        # 1 チャンク目は 3 episode (40×3=120 で target 達成)
        self.assertEqual(plan.chunks[0].message_ids, ["m1", "m2", "m3"])
        self.assertEqual(
            plan.chunks[0].episode_refs, ["episode:1", "episode:2", "episode:3"]
        )
        # 端数 m4 (40 ≥ MIN_LLM) は独立の llm_batch
        self.assertEqual(plan.chunks[1].kind, CHUNK_LLM_BATCH)
        self.assertEqual(plan.chunks[1].message_ids, ["m4"])

    def test_huge_episode_is_not_split(self):
        """target 超過の巨大 episode も分割しない (単独チャンク)。"""
        msgs = [
            _msg("m1", "a" * 80, episode="episode:1"),
            _msg("m2", "b" * 80, episode="episode:1"),
            _msg("m3", "c" * 80, episode="episode:1"),
            _msg("m4", "d" * 30),
        ]
        plan = _plan(msgs)
        self.assertEqual(plan.chunks[0].kind, CHUNK_LLM_BATCH)
        # episode:1 の 3 件 (240 字 > target 100) が丸ごと 1 チャンク
        self.assertEqual(plan.chunks[0].message_ids, ["m1", "m2", "m3"])

    def test_chunk_never_closes_mid_episode(self):
        """束ねの途中で target に達しても episode の途中では閉じない。"""
        msgs = [
            _msg("m1", "a" * 60),                       # 無帰属 60
            _msg("m2", "b" * 30, episode="episode:1"),  # ep1 開始 (90)
            _msg("m3", "c" * 30, episode="episode:1"),  # ep1 継続 (120 ≥ 100)
            _msg("m4", "d" * 30, episode="episode:1"),  # ep1 継続
        ]
        plan = _plan(msgs)
        # ep1 の途中 (m3) では閉じず、原子 (ep1 全体) を足してから閉じる
        self.assertEqual(plan.chunks[0].message_ids, ["m1", "m2", "m3", "m4"])


class TestIdentityCompression(unittest.TestCase):
    """§4-3 恒等圧縮 — 豆粒は生のまま、ただし束ねる相手がいれば束ねる。"""

    def test_tiny_between_digests_is_identity(self):
        msgs = [
            _msg("m1", "a" * 30, episode="episode:1"),
            _msg("m2", "tiny"),  # 4 字 < MIN_LLM、両側 digest
            _msg("m3", "b" * 30, episode="episode:2"),
        ]
        digests = {
            "episode:1": ("d1", "要約1"),
            "episode:2": ("d2", "要約2"),
        }
        plan = _plan(msgs, digests=digests)
        kinds = [c.kind for c in plan.chunks]
        self.assertEqual(
            kinds, [CHUNK_EPISODE_DIGEST, CHUNK_IDENTITY, CHUNK_EPISODE_DIGEST]
        )
        self.assertEqual(plan.chunks[1].message_ids, ["m2"])

    def test_leading_tiny_is_identity(self):
        """run 先頭の豆粒 (吸収先なし) は identity。"""
        msgs = [
            _msg("m1", "tiny"),
            _msg("m2", "a" * 30, episode="episode:1"),
        ]
        digests = {"episode:1": ("d1", "要約1")}
        plan = _plan(msgs, digests=digests)
        self.assertEqual(plan.chunks[0].kind, CHUNK_IDENTITY)
        self.assertEqual(plan.chunks[0].message_ids, ["m1"])

    def test_trailing_tiny_absorbed_into_previous_batch(self):
        """run 終端の豆粒は直前の束ねチャンクに吸収 (§4-2 結合可)。"""
        msgs = [
            _msg("m1", "a" * 50),
            _msg("m2", "tiny"),
        ]
        plan = _plan(msgs)
        self.assertEqual(len(plan.chunks), 1)
        self.assertEqual(plan.chunks[0].kind, CHUNK_LLM_BATCH)
        self.assertEqual(plan.chunks[0].message_ids, ["m1", "m2"])

    def test_small_remainder_above_min_is_llm_batch(self):
        """端数でも min_llm_chars 以上なら LLM 圧縮する
        (現行の「20 件未満スキップ」の廃止 — 地図の無い土地を残さない)。"""
        msgs = [_msg("m1", "a" * 25)]  # 25 ≥ MIN_LLM(20)、target 未達
        plan = _plan(msgs)
        self.assertEqual(len(plan.chunks), 1)
        self.assertEqual(plan.chunks[0].kind, CHUNK_LLM_BATCH)

    def test_whole_run_below_min_is_identity(self):
        msgs = [_msg("m1", "ab"), _msg("m2", "cd")]  # 合計 4 < MIN_LLM
        plan = _plan(msgs)
        self.assertEqual(len(plan.chunks), 1)
        self.assertEqual(plan.chunks[0].kind, CHUNK_IDENTITY)


class TestPlanSummary(unittest.TestCase):
    def test_llm_calls_and_summary(self):
        msgs = [
            _msg("m1", "a" * 120),                      # batch 1 (target 達成)
            _msg("m2", "b" * 30, episode="episode:1"),  # digest 転写
            _msg("m3", "xy"),                           # 豆粒 → identity
            _msg("m4", "c" * 30, episode="episode:2"),  # digest 転写
        ]
        digests = {
            "episode:1": ("d1", "要約1"),
            "episode:2": ("d2", "要約2"),
        }
        plan = _plan(msgs, digests=digests)
        self.assertEqual(plan.llm_calls, 1)
        summary = plan.summary
        self.assertEqual(summary["chunks_llm"], 1)
        self.assertEqual(summary["chunks_episode"], 2)
        self.assertEqual(summary["chunks_identity"], 1)
        self.assertEqual(summary["chunks_total"], 4)
        self.assertEqual(summary["total_unprocessed"], 4)

    def test_coverage_chars_counts_source_not_digest(self):
        """サイズは被覆生ログ字数 (digest 自身の長さではない) — D6 の決定論。"""
        msgs = [
            _msg("m1", "a" * 30, episode="episode:1"),
            _msg("m2", "b" * 30, episode="episode:1"),
        ]
        digests = {"episode:1": ("d1", "短い")}
        plan = _plan(msgs, digests=digests)
        self.assertEqual(plan.chunks[0].coverage_chars, 60)


class TestUnattributedMessages(unittest.TestCase):
    """無帰属 (origin_episode なし) は episode 制約なしで自由に切れる。"""

    def test_unattributed_run_splits_at_target(self):
        msgs = [_msg(f"m{i}", "a" * 40) for i in range(1, 6)]  # 40×5=200
        plan = _plan(msgs)
        # 40×3=120 ≥ 100 で 1 個目、残り 40×2=80 ≥ MIN で 2 個目
        self.assertEqual(len(plan.chunks), 2)
        self.assertEqual(plan.chunks[0].message_ids, ["m1", "m2", "m3"])
        self.assertEqual(plan.chunks[1].message_ids, ["m4", "m5"])
        self.assertTrue(all(c.kind == CHUNK_LLM_BATCH for c in plan.chunks))


if __name__ == "__main__":
    unittest.main()
