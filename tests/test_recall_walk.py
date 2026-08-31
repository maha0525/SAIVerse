"""決定論的な連想歩行 saiverse/recall_walk.py のテスト (life_concept_map.md §9.3 / P4)。

一時 DB 2 つ (world 側 saiverse.db 相当 + ペルソナ memory.db) に合成データ
(目的ノード・purpose_tags・mark・メッセージ・episode・Memopedia ページ) を組み、
LLM コールなしで辺が繋がることを検証する。

- (a) タグ直撃が最優先で出る
- (b) 2 ホップ共起で隣の目的の記憶が拾える
- (c) budget 打ち切りで truncated=True
- (d) 空シード / 何も繋がらないシードで空結果 (§9.2 失敗の正直さ)
- (e) 意味的近傍はローカル埋め込みのみ (Embedder を patch — DummyEmbedder 流儀は
  tests/test_marker_store_memory.py に倣う)
- 木構造 / mark / episode / Memopedia の各辺

実 DB を使い Embedder だけ patch する (tests/test_core_memory_scene.py の流儀)。
"""
from __future__ import annotations

import gc
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from database.models import Base, Episode
from sai_memory import clips as clips_store
from sai_memory import purpose_tags as tags_store
from sai_memory.memopedia import storage as memopedia_storage
from saiverse import clock
from saiverse import recall_walk as RW
from saiverse.persona_task_manager import PersonaTaskManager


class KeywordEmbedder:
    """決定論のローカル埋め込み代役: 'poem' を含むテキストだけ別方向のベクトル。"""

    def __init__(self, model=None, **kwargs):
        self.model_name = model

    def embed(self, texts, **kwargs):
        return [
            [1.0, 0.0, 0.0] if "poem" in t else [0.0, 1.0, 0.0]
            for t in texts
        ]


class RecallWalkTests(unittest.TestCase):
    PERSONA = "p1"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._cleanup_temp)
        os.environ["SAIMEMORY_MEMORY"] = "1"
        self.addCleanup(os.environ.pop, "SAIMEMORY_MEMORY", None)
        self.addCleanup(clock.disable_virtual)

        patcher = patch("saiverse_memory.adapter.Embedder", KeywordEmbedder)
        self.addCleanup(patcher.stop)
        patcher.start()

        # world 側 (目的の木・episodes)
        db_path = Path(self._tmp.name) / "saiverse.db"
        self.engine = create_engine(f"sqlite:///{db_path}")
        Base.metadata.create_all(self.engine)
        self.addCleanup(self.engine.dispose)
        self.SessionLocal = sessionmaker(bind=self.engine)
        self.manager = SimpleNamespace(SessionLocal=self.SessionLocal)
        self.ptm = PersonaTaskManager(self.SessionLocal)

        # ペルソナ側 (memory.db: メッセージ・タグ・mark・Memopedia)
        persona_dir = Path(self._tmp.name) / "personas" / self.PERSONA
        persona_dir.mkdir(parents=True, exist_ok=True)
        from saiverse_memory import SAIMemoryAdapter

        self.adapter = SAIMemoryAdapter(
            self.PERSONA, persona_dir=persona_dir, resource_id=self.PERSONA
        )
        self.addCleanup(self.adapter.close)
        with self.adapter._db_lock:
            tags_store.init_purpose_tags_tables(self.adapter.conn)

    def _cleanup_temp(self):
        gc.collect()
        try:
            self._tmp.cleanup()
        except PermissionError:
            pass

    # ---- helpers ----

    def _msg(self, content: str) -> str:
        mid = self.adapter.append_persona_message(
            {"role": "assistant", "content": content}
        )
        self.assertTrue(mid)
        return mid

    def _tag(self, target_ref: str, purpose_ref: str, layer: int = 2):
        with self.adapter._db_lock:
            return tags_store.add_tag(
                self.adapter.conn, target_ref=target_ref,
                purpose_ref=purpose_ref, layer=layer,
            )

    def _walk(self, seeds, budget=None) -> RW.WalkResult:
        return RW.walk(
            self.PERSONA, seeds, budget,
            manager=self.manager, adapter=self.adapter,
        )

    def _purpose(self, title: str, persona_id: str | None = None, **kw) -> str:
        """種にする目的ノードを 1 本作って ``task:N`` を返す。

        2026-08-22 (束 6c) の Track 退役まで、種は自律 Track (``track:1``) だった。
        ``recall_walk._resolve_purpose_node`` が ``track:N`` を解決しなくなった
        (旧行は読み取り専用の残置扱い) ので、目的ノードはタスクで立てる。
        """
        task = self.ptm.create_task(
            persona_id=persona_id or self.PERSONA, title=title,
            auto_activate=False, **kw,
        )
        return task["task_ref"]

    def _episode(self, persona_id: str, short_id: int, origin_ref: str) -> str:
        """出来事の行を ORM で直に置いて ``episode:N`` を返す。

        ``saiverse.episodes`` の書き込み API は束 6c で全廃され、``episodes``
        テーブルは旧データの読み取り専用の残置になった。出来事所属の辺は
        いまもその残置を読むので、検証用の行はこちらで直接用意する。
        """
        db = self.SessionLocal()
        try:
            db.add(Episode(
                EPISODE_ID=f"ep-{persona_id}-{short_id}", PERSONA_ID=persona_id,
                SHORT_ID=short_id, KIND="slot", STARTED_AT=1_700_000_000,
                ORIGIN_REF=origin_ref, STATUS="open",
            ))
            db.commit()
        finally:
            db.close()
        return f"episode:{short_id}"

    # ---- (d) 失敗の正直さ ----

    def test_empty_seeds_return_empty_result(self):
        result = self._walk([])
        self.assertEqual(result.items, [])
        self.assertFalse(result.truncated)
        # None / 空白だけの種も同様
        result = self._walk([None, "  "])
        self.assertEqual(result.items, [])

    def test_seed_with_no_edges_returns_empty(self):
        # 実在するが何も繋がっていない目的ノード → 空 (無理に埋めない §9.2)
        seed = self._purpose("untagged venture")
        result = self._walk([seed])
        self.assertEqual(result.items, [])
        self.assertFalse(result.truncated)

    # ---- (a) タグ直撃が最優先 ----

    def test_tag_direct_ranks_first(self):
        # tree 辺の材料: 種の子 (タスクのステップ)
        seed = self._purpose("poem writing", steps=[{"title": "draft anthology"}])
        # clip 辺の材料: purpose_ref 一致・未貼り付け
        with self.adapter._db_lock:
            clips_store.add_clip(
                self.adapter.conn, message_id="m-mark-1",
                quote="the sound of rain", purpose_ref=seed,
            )
        # タグ直撃の材料
        m1 = self._msg("poem session notes: wrote three haiku")
        self._tag(f"message:{m1}", seed)

        result = self._walk([seed])
        self.assertTrue(result.items)
        top = result.items[0]
        self.assertEqual(top.ref, f"message:{m1}")
        self.assertEqual(top.kind, "tag_direct")
        # snippet は保存本文の先頭からの決定論切り出し
        self.assertTrue(top.snippet.startswith("poem session notes"))
        self.assertEqual(top.path, (seed, f"message:{m1}"))
        # 他の辺も網に入っている
        kinds = {item.kind for item in result.items}
        self.assertIn("tree", kinds)
        self.assertIn("clip", kinds)
        # 種そのものは items に載らない
        self.assertNotIn(seed, [item.ref for item in result.items])

    # ---- (b) 2 ホップ共起 ----

    def test_two_hop_cooccurrence_reaches_neighbor_purpose_memory(self):
        seed = self._purpose("poem writing")
        neighbor_ref = self._purpose("rain diary")

        shared = self._msg("we talked about rain imagery")
        other = self._msg("diary entry: watched the storm")
        # seed → shared ← neighbor → other という共起網
        self._tag(f"message:{shared}", seed)
        self._tag(f"message:{shared}", neighbor_ref)
        self._tag(f"message:{other}", neighbor_ref)

        result = self._walk([seed])
        by_ref = {item.ref: item for item in result.items}
        # 隣の目的そのもの
        self.assertIn(neighbor_ref, by_ref)
        self.assertEqual(by_ref[neighbor_ref].kind, "tag_cooccur")
        # 隣の目的の別の記憶 (2 ホップ先)
        self.assertIn(f"message:{other}", by_ref)
        self.assertEqual(by_ref[f"message:{other}"].kind, "tag_cooccur")
        self.assertEqual(
            by_ref[f"message:{other}"].path,
            (seed, f"message:{shared}", neighbor_ref, f"message:{other}"),
        )
        # 直撃は共起より上位
        self.assertEqual(result.items[0].ref, f"message:{shared}")
        self.assertEqual(result.items[0].kind, "tag_direct")

    # ---- (c) budget 打ち切り ----

    def test_budget_truncates_and_flags(self):
        seed = self._purpose("poem writing")
        for i in range(3):
            mid = self._msg(f"note {i}")
            self._tag(f"message:{mid}", seed)

        result = self._walk([seed], budget=RW.WalkBudget(max_nodes=1))
        self.assertEqual(len(result.items), 1)
        self.assertTrue(result.truncated)

        # 文字予算でも打ち切られる
        result = self._walk([seed], budget=RW.WalkBudget(max_nodes=10, max_chars=8))
        self.assertLess(len(result.items), 3)
        self.assertTrue(result.truncated)

        # 予算内に収まれば truncated=False
        result = self._walk([seed], budget=RW.WalkBudget(max_nodes=50))
        self.assertFalse(result.truncated)

    # ---- (e) 意味的近傍 (ローカル埋め込み・DummyEmbedder patch) ----

    def test_semantic_edge_uses_local_embeddings(self):
        seed = self._purpose("poem writing")
        poem_mid = self._msg("a poem about the winter sea")
        for i in range(RW.SEMANTIC_TOPK + 2):
            self._msg(f"grocery list number {i}")

        result = self._walk([seed])
        semantic = [item for item in result.items if item.kind == "semantic"]
        # topk で絞られ、クエリ (種のタイトル 'poem writing') に似た方向の
        # ベクトルを持つメッセージが必ず入る
        self.assertEqual(len(semantic), RW.SEMANTIC_TOPK)
        self.assertIn(f"message:{poem_mid}", [item.ref for item in semantic])

    # ---- 木の構造 (子) ----

    def test_tree_edge_walks_children_from_task_seed(self):
        # 種の子 = そのタスクのステップ。
        # 旧テストは「Track を親に持つタスクから親と兄弟へ渡る」ことを見ていたが、
        # 2026-08-22 (束 6c) で ``_resolve_purpose_node`` が Track を解決しなく
        # なり、親も兄弟も辿れなくなったので子の辺だけを残した。
        seed = self._purpose(
            "poem writing",
            steps=[{"title": "draft anthology"}, {"title": "collect metaphors"}],
        )
        result = self._walk([seed])
        titles = {item.snippet: item.kind for item in result.items}
        self.assertEqual(titles.get("draft anthology"), "tree")
        self.assertEqual(titles.get("collect metaphors"), "tree")
        self.assertNotIn(seed, {item.ref for item in result.items})  # 自分は載らない

    # ---- 出来事所属 ----

    def test_episode_edge_finds_episodes_by_origin_ref(self):
        seed = self._purpose("poem writing")
        mine = self._episode(self.PERSONA, 1, origin_ref=seed)
        self._episode("p2", 1, origin_ref=seed)
        result = self._walk([seed])
        by_ref = {item.ref: item for item in result.items}
        self.assertIn(mine, by_ref)
        self.assertEqual(by_ref[mine].kind, "episode")
        # 他ペルソナの episode は複数主観 (§8.1) — 自分の歩行には出ない
        self.assertEqual(
            sum(1 for item in result.items if item.kind == "episode"), 1
        )

    # ---- Memopedia 実体言及 ----

    def test_memopedia_edge_title_match_and_message_link(self):
        seed = self._purpose("poem writing")
        with self.adapter._db_lock:
            # (a) seed タイトル完全一致のページ
            title_page = memopedia_storage.create_page(
                self.adapter.conn, parent_id="root_terms",
                title="poem writing", summary="the craft of verse",
                category="terms",
            )
        # (b) タグ直撃で見つかるメッセージを編集来歴に持つページ
        m1 = self._msg("poem session notes")
        self._tag(f"message:{m1}", seed)
        with self.adapter._db_lock:
            linked_page = memopedia_storage.create_page(
                self.adapter.conn, parent_id="root_people",
                title="Mahaa", summary="partner", category="people",
            )
            memopedia_storage.record_page_edit(
                self.adapter.conn, page_id=linked_page.id,
                diff_text="+x", edit_type="update",
                ref_start_message_id=m1,
            )

        result = self._walk([seed])
        by_ref = {item.ref: item for item in result.items}
        title_ref = f"memopedia:{title_page.short_id}"
        linked_ref = f"memopedia:{linked_page.short_id}"
        self.assertIn(title_ref, by_ref)
        self.assertEqual(by_ref[title_ref].kind, "memopedia")
        self.assertIn(linked_ref, by_ref)
        self.assertEqual(by_ref[linked_ref].kind, "memopedia")
        # message 経由の path は「どの記憶からそのページに届いたか」を残す
        self.assertEqual(by_ref[linked_ref].path[-2], f"message:{m1}")


if __name__ == "__main__":
    unittest.main()
