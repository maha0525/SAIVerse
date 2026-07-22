"""継承エッジ (継承 DAG) の器と機械的記帳のテスト (experience_structure.md §3.3 / W13)。

検証項目:
- 事実層 / 咀嚼層エッジが範囲オープン時 (open_episode の predecessors) に機械的に刻まれる
- 選択なし (predecessors 無し) の open はエッジ 0 本 = 直列の縮退 (既存挙動は無変化)
- 既存データ (エッジなし) が探索クエリで無害 (親 0・子 0・祖先 0)
- 冪等性 (同一 (子,親,層) の再記帳は行を増やさない)・自己ループ禁止・層検証
- 並列統合 (ε が両親を digest 層で持つ) と分岐 (anchor_ref 付き fact 層)
- 継承祖先の幅優先探索 (咀嚼を継承チェーンに閉じる生成規律の原始関数)
- open_episode の予約 tx (session=) に相乗りしたエッジ記帳の原子性
"""
from __future__ import annotations

import gc
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from database.models import Base, EpisodeInheritance
from saiverse import clock
from saiverse import episodes as E
from saiverse import experience_inheritance as EI


class ExperienceInheritanceTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self._tmpdir.name) / "test.db")
        self.engine = create_engine(f"sqlite:///{self.db_path}")
        Base.metadata.create_all(self.engine)
        self.SessionLocal = sessionmaker(bind=self.engine)
        self.manager = SimpleNamespace(SessionLocal=self.SessionLocal)

    def tearDown(self):
        clock.disable_virtual()
        self.engine.dispose()
        gc.collect()
        try:
            self._tmpdir.cleanup()
        except PermissionError:
            pass

    def _count_edges(self) -> int:
        db = self.SessionLocal()
        try:
            return db.query(EpisodeInheritance).count()
        finally:
            db.close()

    # ---- 既存データはエッジなしで無害 ----

    def test_open_without_predecessors_records_no_edges(self):
        """選択なしの open はエッジ 0 本 (直列の縮退・既存挙動は無変化)。"""
        E.open_episode(self.manager, "p1", E.KIND_CONVERSATION)
        E.open_episode(self.manager, "p1", E.KIND_WORK_SESSION)
        self.assertEqual(self._count_edges(), 0)

    def test_queries_on_edgeless_episode_are_harmless(self):
        """エッジのない範囲ノードは親 0・子 0・祖先 0 を返す (既存データ無害)。"""
        ep = E.open_episode(self.manager, "p1", E.KIND_CONVERSATION)
        ref = ep["episode_ref"]
        self.assertEqual(EI.get_parents(self.manager, "p1", ref), [])
        self.assertEqual(EI.get_children(self.manager, "p1", ref), [])
        self.assertEqual(EI.get_ancestors(self.manager, "p1", ref), [])

    def test_empty_predecessors_list_is_noop(self):
        E.open_episode(self.manager, "p1", E.KIND_CONVERSATION, predecessors=[])
        self.assertEqual(self._count_edges(), 0)

    # ---- 事実層 / 咀嚼層エッジの機械的記帳 ----

    def test_open_records_fact_layer_edge(self):
        parent = E.open_episode(self.manager, "p1", E.KIND_CONVERSATION)
        child = E.open_episode(
            self.manager, "p1", E.KIND_CONVERSATION,
            predecessors=[{"parent_ref": parent["episode_ref"], "layer": EI.LAYER_FACT}],
        )
        parents = EI.get_parents(self.manager, "p1", child["episode_ref"])
        self.assertEqual(len(parents), 1)
        self.assertEqual(parents[0]["layer"], EI.LAYER_FACT)
        self.assertEqual(parents[0]["parent_episode_id"], parent["episode_id"])
        self.assertEqual(parents[0]["child_episode_id"], child["episode_id"])
        self.assertIsNone(parents[0]["anchor_ref"])

    def test_open_records_digest_layer_edge(self):
        parent = E.open_episode(self.manager, "p1", E.KIND_WORK_SESSION)
        child = E.open_episode(
            self.manager, "p1", E.KIND_CONVERSATION,
            predecessors=[{
                "parent_ref": parent["episode_ref"],
                "layer": EI.LAYER_DIGEST,
                "origin": "import",
            }],
        )
        parents = EI.get_parents(self.manager, "p1", child["episode_ref"], layer=EI.LAYER_DIGEST)
        self.assertEqual(len(parents), 1)
        self.assertEqual(parents[0]["layer"], EI.LAYER_DIGEST)
        self.assertEqual(parents[0]["origin"], "import")

    def test_branch_edge_carries_anchor_ref(self):
        """分岐・再生成: fact 層 + 分岐点 (親内の特定メッセージ = pulse 関節)。"""
        parent = E.open_episode(self.manager, "p1", E.KIND_CONVERSATION)
        child = E.open_episode(
            self.manager, "p1", E.KIND_CONVERSATION,
            predecessors=[{
                "parent_ref": parent["episode_ref"],
                "layer": EI.LAYER_FACT,
                "anchor_ref": "message:abc123",
                "origin": "branch",
                "meta": {"note": "regen from turn 4"},
            }],
        )
        parents = EI.get_parents(self.manager, "p1", child["episode_ref"])
        self.assertEqual(parents[0]["anchor_ref"], "message:abc123")
        self.assertEqual(parents[0]["origin"], "branch")
        self.assertEqual(parents[0]["meta"], {"note": "regen from turn 4"})

    def test_parallel_merge_two_digest_parents(self):
        """並列統合 (γδ→ε): ε は両親を咀嚼層エッジで持つ。"""
        gamma = E.open_episode(self.manager, "p1", E.KIND_CONVERSATION)
        delta = E.open_episode(self.manager, "p1", E.KIND_CONVERSATION)
        epsilon = E.open_episode(
            self.manager, "p1", E.KIND_CONVERSATION,
            predecessors=[
                {"parent_ref": gamma["episode_ref"], "layer": EI.LAYER_DIGEST, "origin": "merge"},
                {"parent_ref": delta["episode_ref"], "layer": EI.LAYER_DIGEST, "origin": "merge"},
            ],
        )
        parents = EI.get_parents(self.manager, "p1", epsilon["episode_ref"])
        self.assertEqual(len(parents), 2)
        parent_ids = {p["parent_episode_id"] for p in parents}
        self.assertEqual(parent_ids, {gamma["episode_id"], delta["episode_id"]})

    def test_mixed_layer_parents_on_same_open(self):
        """直接継続 = 事実層、他の親 = 咀嚼層 (§11-4) を 1 回の open で両方刻む。"""
        direct = E.open_episode(self.manager, "p1", E.KIND_CONVERSATION)
        known = E.open_episode(self.manager, "p1", E.KIND_WORK_SESSION)
        child = E.open_episode(
            self.manager, "p1", E.KIND_CONVERSATION,
            predecessors=[
                {"parent_ref": direct["episode_ref"], "layer": EI.LAYER_FACT},
                {"parent_ref": known["episode_ref"], "layer": EI.LAYER_DIGEST},
            ],
        )
        fact = EI.get_parents(self.manager, "p1", child["episode_ref"], layer=EI.LAYER_FACT)
        digest = EI.get_parents(self.manager, "p1", child["episode_ref"], layer=EI.LAYER_DIGEST)
        self.assertEqual([p["parent_episode_id"] for p in fact], [direct["episode_id"]])
        self.assertEqual([p["parent_episode_id"] for p in digest], [known["episode_id"]])

    # ---- get_children (親 → 子方向) ----

    def test_get_children_lists_continuations(self):
        parent = E.open_episode(self.manager, "p1", E.KIND_CONVERSATION)
        c1 = E.open_episode(
            self.manager, "p1", E.KIND_CONVERSATION,
            predecessors=[{"parent_ref": parent["episode_ref"], "layer": EI.LAYER_FACT}],
        )
        c2 = E.open_episode(
            self.manager, "p1", E.KIND_CONVERSATION,
            predecessors=[{"parent_ref": parent["episode_ref"], "layer": EI.LAYER_DIGEST}],
        )
        children = EI.get_children(self.manager, "p1", parent["episode_ref"])
        child_ids = {c["child_episode_id"] for c in children}
        self.assertEqual(child_ids, {c1["episode_id"], c2["episode_id"]})

    # ---- 継承祖先の探索 (咀嚼を継承チェーンに閉じる原始関数) ----

    def test_get_ancestors_walks_chain(self):
        a = E.open_episode(self.manager, "p1", E.KIND_CONVERSATION)
        b = E.open_episode(
            self.manager, "p1", E.KIND_CONVERSATION,
            predecessors=[{"parent_ref": a["episode_ref"], "layer": EI.LAYER_FACT}],
        )
        c = E.open_episode(
            self.manager, "p1", E.KIND_CONVERSATION,
            predecessors=[{"parent_ref": b["episode_ref"], "layer": EI.LAYER_FACT}],
        )
        ancestors = EI.get_ancestors(self.manager, "p1", c["episode_ref"])
        self.assertEqual(set(ancestors), {a["episode_id"], b["episode_id"]})

    def test_get_ancestors_layer_filter(self):
        a = E.open_episode(self.manager, "p1", E.KIND_CONVERSATION)
        b = E.open_episode(
            self.manager, "p1", E.KIND_CONVERSATION,
            predecessors=[{"parent_ref": a["episode_ref"], "layer": EI.LAYER_DIGEST}],
        )
        c = E.open_episode(
            self.manager, "p1", E.KIND_CONVERSATION,
            predecessors=[{"parent_ref": b["episode_ref"], "layer": EI.LAYER_FACT}],
        )
        # 事実層だけ辿ると b で止まる (b→a は digest 層)。
        fact_only = EI.get_ancestors(self.manager, "p1", c["episode_ref"], layer=EI.LAYER_FACT)
        self.assertEqual(set(fact_only), {b["episode_id"]})

    def _raw_edge(self, persona_id: str, child_id: str, parent_id: str, layer: str) -> None:
        """公開 API を迂回してエッジ行を直接挿入する (壊れたデータの模擬)。"""
        import uuid
        db = self.SessionLocal()
        try:
            db.add(EpisodeInheritance(
                EDGE_ID=str(uuid.uuid4()), PERSONA_ID=persona_id,
                CHILD_EPISODE_ID=child_id, PARENT_EPISODE_ID=parent_id,
                LAYER=layer, CREATED_AT=1,
            ))
            db.commit()
        finally:
            db.close()

    def test_get_ancestors_tolerates_cycle(self):
        """外部由来の壊れた循環データでも visited で停止する (暴走しない)。"""
        a = E.open_episode(self.manager, "p1", E.KIND_CONVERSATION)
        b = E.open_episode(
            self.manager, "p1", E.KIND_CONVERSATION,
            predecessors=[{"parent_ref": a["episode_ref"], "layer": EI.LAYER_FACT}],
        )
        # 逆向きエッジを直接挿入して循環を作る (a の親 = b)。公開 API は今や
        # これを拒否するので、防御ガードの検証には ORM 直挿しで壊れた状態を作る。
        self._raw_edge("p1", a["episode_id"], b["episode_id"], EI.LAYER_FACT)
        # 循環でも暴走せず有限で停止する。start (=b) は「自分を含まない」設計で
        # 除外されるので祖先は a のみ。
        ancestors = EI.get_ancestors(self.manager, "p1", b["episode_ref"])
        self.assertEqual(set(ancestors), {a["episode_id"]})

    def test_multi_hop_cycle_rejected_by_public_api(self):
        """公開 API だけで多段の循環は作れない (DAG 不変条件を書き込み時に強制)。"""
        a = E.open_episode(self.manager, "p1", E.KIND_CONVERSATION)
        b = E.open_episode(
            self.manager, "p1", E.KIND_CONVERSATION,
            predecessors=[{"parent_ref": a["episode_ref"], "layer": EI.LAYER_FACT}],
        )
        # b→a が既にある。a→b を足すと a→b→a で循環 → 拒否。
        with self.assertRaises(EI.InheritanceError):
            EI.record_edges(
                self.manager, "p1", a["episode_ref"],
                [{"parent_ref": b["episode_ref"], "layer": EI.LAYER_FACT}],
            )
        self.assertEqual(self._count_edges(), 1)

    def test_deep_cycle_rejected(self):
        """3 段の連鎖でも循環は弾く (a→b→c、c→a を拒否)。層違いでも巡回は巡回。"""
        a = E.open_episode(self.manager, "p1", E.KIND_CONVERSATION)
        b = E.open_episode(
            self.manager, "p1", E.KIND_CONVERSATION,
            predecessors=[{"parent_ref": a["episode_ref"], "layer": EI.LAYER_FACT}],
        )
        c = E.open_episode(
            self.manager, "p1", E.KIND_CONVERSATION,
            predecessors=[{"parent_ref": b["episode_ref"], "layer": EI.LAYER_FACT}],
        )
        with self.assertRaises(EI.InheritanceError):
            EI.record_edges(
                self.manager, "p1", a["episode_ref"],
                [{"parent_ref": c["episode_ref"], "layer": EI.LAYER_DIGEST}],
            )

    def test_long_chain_cycle_rejected(self):
        """129 段超の直列鎖でも先頭→末尾の逆向きを弾く (深さ上限で見逃さない)。"""
        refs = [E.open_episode(self.manager, "p1", E.KIND_CONVERSATION)["episode_ref"]]
        for _ in range(140):
            child = E.open_episode(
                self.manager, "p1", E.KIND_CONVERSATION,
                predecessors=[{"parent_ref": refs[-1], "layer": EI.LAYER_FACT}],
            )
            refs.append(child["episode_ref"])
        head, tail = refs[0], refs[-1]
        # head→…→tail の鎖がある。head の親に tail を足すと巨大な循環 → 拒否。
        with self.assertRaises(EI.InheritanceError):
            EI.record_edges(
                self.manager, "p1", head,
                [{"parent_ref": tail, "layer": EI.LAYER_FACT}],
            )

    def test_get_ancestors_returns_full_deep_chain(self):
        """深い鎖でも全祖先を返す (深さ上限で古い側を落とさない)。"""
        refs = [E.open_episode(self.manager, "p1", E.KIND_CONVERSATION)]
        for _ in range(140):
            refs.append(E.open_episode(
                self.manager, "p1", E.KIND_CONVERSATION,
                predecessors=[{"parent_ref": refs[-1]["episode_ref"], "layer": EI.LAYER_FACT}],
            ))
        ancestors = EI.get_ancestors(self.manager, "p1", refs[-1]["episode_ref"])
        # 末尾の祖先 = 自分以外の全ノード (140 個)。
        self.assertEqual(len(ancestors), len(refs) - 1)
        self.assertIn(refs[0]["episode_id"], ancestors)

    # ---- 冪等・検証・エラー ----

    def test_idempotent_edge_recording(self):
        parent = E.open_episode(self.manager, "p1", E.KIND_CONVERSATION)
        child = E.open_episode(
            self.manager, "p1", E.KIND_CONVERSATION,
            predecessors=[{"parent_ref": parent["episode_ref"], "layer": EI.LAYER_FACT}],
        )
        # 同一 (子, 親, 層) を再記帳しても増えない。
        EI.record_edges(
            self.manager, "p1", child["episode_ref"],
            [{"parent_ref": parent["episode_ref"], "layer": EI.LAYER_FACT}],
        )
        self.assertEqual(self._count_edges(), 1)

    def test_same_parent_different_layers_coexist(self):
        parent = E.open_episode(self.manager, "p1", E.KIND_CONVERSATION)
        child = E.open_episode(
            self.manager, "p1", E.KIND_CONVERSATION,
            predecessors=[
                {"parent_ref": parent["episode_ref"], "layer": EI.LAYER_FACT},
                {"parent_ref": parent["episode_ref"], "layer": EI.LAYER_DIGEST},
            ],
        )
        parents = EI.get_parents(self.manager, "p1", child["episode_ref"])
        self.assertEqual(len(parents), 2)

    def test_duplicate_in_same_batch_collapses(self):
        parent = E.open_episode(self.manager, "p1", E.KIND_CONVERSATION)
        child = E.open_episode(
            self.manager, "p1", E.KIND_CONVERSATION,
            predecessors=[
                {"parent_ref": parent["episode_ref"], "layer": EI.LAYER_FACT},
                {"parent_ref": parent["episode_ref"], "layer": EI.LAYER_FACT},
            ],
        )
        self.assertEqual(
            len(EI.get_parents(self.manager, "p1", child["episode_ref"])), 1,
        )

    def test_self_loop_rejected(self):
        ep = E.open_episode(self.manager, "p1", E.KIND_CONVERSATION)
        with self.assertRaises(EI.InheritanceError):
            EI.record_edges(
                self.manager, "p1", ep["episode_ref"],
                [{"parent_ref": ep["episode_ref"], "layer": EI.LAYER_FACT}],
            )

    def test_invalid_layer_rejected(self):
        parent = E.open_episode(self.manager, "p1", E.KIND_CONVERSATION)
        with self.assertRaises(EI.InheritanceError):
            E.open_episode(
                self.manager, "p1", E.KIND_CONVERSATION,
                predecessors=[{"parent_ref": parent["episode_ref"], "layer": "bogus"}],
            )

    def test_missing_parent_ref_rejected(self):
        with self.assertRaises(EI.InheritanceError):
            EI.record_edges(
                self.manager, "p1", "episode:1",
                [{"layer": EI.LAYER_FACT}],
            )

    def test_nonexistent_uuid_parent_rejected(self):
        """UUID の親参照でも存在しなければ弾く (形式検査だけで通さない)。"""
        child = E.open_episode(self.manager, "p1", E.KIND_CONVERSATION)
        with self.assertRaises(EI.InheritanceError):
            EI.record_edges(
                self.manager, "p1", child["episode_ref"],
                [{"parent_ref": "00000000-0000-0000-0000-000000000000",
                  "layer": EI.LAYER_FACT}],
            )
        self.assertEqual(self._count_edges(), 0)

    def test_cross_persona_uuid_parent_rejected(self):
        """他ペルソナの Episode UUID を自分の継承エッジ親にできない (所属照合)。"""
        p2_ep = E.open_episode(self.manager, "p2", E.KIND_CONVERSATION)
        p1_child = E.open_episode(self.manager, "p1", E.KIND_CONVERSATION)
        with self.assertRaises(EI.InheritanceError):
            EI.record_edges(
                self.manager, "p1", p1_child["episode_ref"],
                [{"parent_ref": p2_ep["episode_id"], "layer": EI.LAYER_FACT}],
            )
        self.assertEqual(self._count_edges(), 0)

    def test_cross_persona_uuid_child_rejected(self):
        """他ペルソナの Episode UUID を自分の子として記帳できない。"""
        p2_ep = E.open_episode(self.manager, "p2", E.KIND_CONVERSATION)
        p1_parent = E.open_episode(self.manager, "p1", E.KIND_CONVERSATION)
        with self.assertRaises(EI.InheritanceError):
            EI.record_edges(
                self.manager, "p1", p2_ep["episode_id"],
                [{"parent_ref": p1_parent["episode_ref"], "layer": EI.LAYER_FACT}],
            )
        self.assertEqual(self._count_edges(), 0)

    def test_self_loop_open_is_atomic_rollback(self):
        """open の予約 tx 内でエッジ記帳が失敗したら episode ごとロールバックされる。"""
        parent = E.open_episode(self.manager, "p1", E.KIND_CONVERSATION)
        before = self._episode_count("p1")
        # 存在しない親参照でエラー → episode INSERT ごと巻き戻るべき (同一 tx)。
        with self.assertRaises(Exception):
            E.open_episode(
                self.manager, "p1", E.KIND_CONVERSATION,
                predecessors=[{"parent_ref": "episode:999", "layer": EI.LAYER_FACT}],
            )
        self.assertEqual(self._episode_count("p1"), before)
        # 元の親は無傷。
        self.assertIsNotNone(E.get_by_ref(self.manager, "p1", parent["episode_ref"]))
        self.assertEqual(self._count_edges(), 0)

    def _episode_count(self, persona_id: str) -> int:
        from database.models import Episode
        db = self.SessionLocal()
        try:
            return db.query(Episode).filter(Episode.PERSONA_ID == persona_id).count()
        finally:
            db.close()

    # ---- 予約 tx 相乗り (session=) ----

    def test_edge_recorded_in_reservation_session(self):
        """open_episode(session=) の 1 commit に episode + エッジが同梱される。"""
        parent = E.open_episode(self.manager, "p1", E.KIND_CONVERSATION)
        db = self.SessionLocal()
        try:
            child = E.open_episode(
                self.manager, "p1", E.KIND_SLOT,
                predecessors=[{"parent_ref": parent["episode_ref"], "layer": EI.LAYER_FACT}],
                session=db,
            )
            # commit 前は別コネクションから見えない。
            self.assertEqual(self._count_edges(), 0)
            db.commit()
        finally:
            db.close()
        self.assertEqual(self._count_edges(), 1)
        self.assertEqual(
            len(EI.get_parents(self.manager, "p1", child["episode_ref"])), 1,
        )

    # ---- ペルソナ分離 ----

    def test_edges_are_per_persona(self):
        p1_parent = E.open_episode(self.manager, "p1", E.KIND_CONVERSATION)
        E.open_episode(
            self.manager, "p1", E.KIND_CONVERSATION,
            predecessors=[{"parent_ref": p1_parent["episode_ref"], "layer": EI.LAYER_FACT}],
        )
        # p2 の episode:1 は別物 — p2 側の探索は p1 のエッジを拾わない。
        E.open_episode(self.manager, "p2", E.KIND_CONVERSATION)
        self.assertEqual(EI.get_children(self.manager, "p2", "episode:1"), [])


if __name__ == "__main__":
    unittest.main()
