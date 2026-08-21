"""継承エッジ (継承 DAG) の器と記帳のテスト (experience_structure.md §3.3 / W13)。

⚠ かつては「範囲が開いた瞬間に機械的に記帳する」入口 = ``open_episode`` の
``predecessors=`` 引数を通してエッジを刻んでいた。その入口は束 6c
(2026-08-22、autonomous_behavior_v3.md §7) で ``open_episode`` ごと退役した —
「エピソードという専用の記録行は持たない」の裁定で書き込み API が消えたため。
``open_episode(predecessors=...)`` の配管そのものを見ていたテスト
(予約 tx 内でエッジ記帳が失敗したら episode ごと巻き戻る、選択なしの open は
エッジ 0 本) は、検証対象の引数ごと消えたので削除した。

:mod:`saiverse.experience_inheritance` 自体は生きている (``episode_inheritance``
テーブルと ``record_edges`` / ``get_parents`` / ``get_children`` /
``get_ancestors``)。範囲ノードは ``episodes`` の残置行を指すので、fixture は
ORM で Episode 行を直接挿入し、エッジは :func:`record_edges` を明示的に呼んで
刻む。

検証項目:
- 事実層 / 咀嚼層エッジの記帳と層による絞り込み
- 既存データ (エッジなし) が探索クエリで無害 (親 0・子 0・祖先 0)
- 冪等性 (同一 (子,親,層) の再記帳は行を増やさない)・自己ループ禁止・層検証
- 並列統合 (ε が両親を digest 層で持つ) と分岐 (anchor_ref 付き fact 層)
- 継承祖先の幅優先探索 (咀嚼を継承チェーンに閉じる生成規律の原始関数)
- 呼び出し元 tx (session=) に相乗りしたエッジ記帳の原子性
"""
from __future__ import annotations

import gc
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from database.models import Base, Episode, EpisodeInheritance
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
        self._short_ids: Dict[str, int] = {}

    def tearDown(self):
        self.engine.dispose()
        gc.collect()
        try:
            self._tmpdir.cleanup()
        except PermissionError:
            pass

    # ---- 範囲ノード (episodes 残置行) の用意 ----

    def _episode(self, persona_id: str = "p1",
                 kind: str = E.KIND_CONVERSATION) -> Dict[str, Any]:
        """継承エッジの端点になる episodes 行を 1 件作る。

        SHORT_ID はペルソナ内連番 — ``episode:N`` 参照子の解決がこれを引く。
        """
        short_id = self._short_ids.get(persona_id, 0) + 1
        self._short_ids[persona_id] = short_id
        episode_id = str(uuid.uuid4())
        db = self.SessionLocal()
        try:
            db.add(Episode(
                EPISODE_ID=episode_id, PERSONA_ID=persona_id, SHORT_ID=short_id,
                KIND=kind, STARTED_AT=1_000, STATUS=E.STATUS_OPEN,
            ))
            db.commit()
        finally:
            db.close()
        return {
            "episode_id": episode_id,
            "short_id": short_id,
            "episode_ref": f"episode:{short_id}",
        }

    def _link(self, child, *predecessors, persona_id: str = "p1"):
        """``child`` に前駆を刻む (旧 open_episode(predecessors=) の置き換え)。"""
        return EI.record_edges(
            self.manager, persona_id, child["episode_ref"], list(predecessors),
        )

    def _count_edges(self) -> int:
        db = self.SessionLocal()
        try:
            return db.query(EpisodeInheritance).count()
        finally:
            db.close()

    def _episode_count(self, persona_id: str) -> int:
        db = self.SessionLocal()
        try:
            return db.query(Episode).filter(Episode.PERSONA_ID == persona_id).count()
        finally:
            db.close()

    # ---- 既存データはエッジなしで無害 ----

    def test_queries_on_edgeless_episode_are_harmless(self):
        """エッジのない範囲ノードは親 0・子 0・祖先 0 を返す (既存データ無害)。"""
        ref = self._episode()["episode_ref"]
        self.assertEqual(EI.get_parents(self.manager, "p1", ref), [])
        self.assertEqual(EI.get_children(self.manager, "p1", ref), [])
        self.assertEqual(EI.get_ancestors(self.manager, "p1", ref), [])

    def test_empty_predecessors_list_is_noop(self):
        """選択なし = 直列の縮退。行も増えず、参照解決すら走らない。"""
        child = self._episode()
        self.assertEqual(self._link(child), [])
        self.assertEqual(self._count_edges(), 0)

    # ---- 事実層 / 咀嚼層エッジの記帳 ----

    def test_records_fact_layer_edge(self):
        parent = self._episode()
        child = self._episode()
        self._link(child, {"parent_ref": parent["episode_ref"], "layer": EI.LAYER_FACT})
        parents = EI.get_parents(self.manager, "p1", child["episode_ref"])
        self.assertEqual(len(parents), 1)
        self.assertEqual(parents[0]["layer"], EI.LAYER_FACT)
        self.assertEqual(parents[0]["parent_episode_id"], parent["episode_id"])
        self.assertEqual(parents[0]["child_episode_id"], child["episode_id"])
        self.assertIsNone(parents[0]["anchor_ref"])

    def test_records_digest_layer_edge(self):
        parent = self._episode(kind=E.KIND_WORK_SESSION)
        child = self._episode()
        self._link(child, {
            "parent_ref": parent["episode_ref"],
            "layer": EI.LAYER_DIGEST,
            "origin": "import",
        })
        parents = EI.get_parents(
            self.manager, "p1", child["episode_ref"], layer=EI.LAYER_DIGEST,
        )
        self.assertEqual(len(parents), 1)
        self.assertEqual(parents[0]["layer"], EI.LAYER_DIGEST)
        self.assertEqual(parents[0]["origin"], "import")

    def test_branch_edge_carries_anchor_ref(self):
        """分岐・再生成: fact 層 + 分岐点 (親内の特定メッセージ = pulse 関節)。"""
        parent = self._episode()
        child = self._episode()
        self._link(child, {
            "parent_ref": parent["episode_ref"],
            "layer": EI.LAYER_FACT,
            "anchor_ref": "message:abc123",
            "origin": "branch",
            "meta": {"note": "regen from turn 4"},
        })
        parents = EI.get_parents(self.manager, "p1", child["episode_ref"])
        self.assertEqual(parents[0]["anchor_ref"], "message:abc123")
        self.assertEqual(parents[0]["origin"], "branch")
        self.assertEqual(parents[0]["meta"], {"note": "regen from turn 4"})

    def test_parallel_merge_two_digest_parents(self):
        """並列統合 (γδ→ε): ε は両親を咀嚼層エッジで持つ。"""
        gamma = self._episode()
        delta = self._episode()
        epsilon = self._episode()
        self._link(
            epsilon,
            {"parent_ref": gamma["episode_ref"], "layer": EI.LAYER_DIGEST,
             "origin": "merge"},
            {"parent_ref": delta["episode_ref"], "layer": EI.LAYER_DIGEST,
             "origin": "merge"},
        )
        parents = EI.get_parents(self.manager, "p1", epsilon["episode_ref"])
        self.assertEqual(len(parents), 2)
        parent_ids = {p["parent_episode_id"] for p in parents}
        self.assertEqual(parent_ids, {gamma["episode_id"], delta["episode_id"]})

    def test_mixed_layer_parents_in_one_call(self):
        """直接継続 = 事実層、他の親 = 咀嚼層 (§11-4) を 1 回の記帳で両方刻む。"""
        direct = self._episode()
        known = self._episode(kind=E.KIND_WORK_SESSION)
        child = self._episode()
        self._link(
            child,
            {"parent_ref": direct["episode_ref"], "layer": EI.LAYER_FACT},
            {"parent_ref": known["episode_ref"], "layer": EI.LAYER_DIGEST},
        )
        fact = EI.get_parents(
            self.manager, "p1", child["episode_ref"], layer=EI.LAYER_FACT,
        )
        digest = EI.get_parents(
            self.manager, "p1", child["episode_ref"], layer=EI.LAYER_DIGEST,
        )
        self.assertEqual([p["parent_episode_id"] for p in fact], [direct["episode_id"]])
        self.assertEqual([p["parent_episode_id"] for p in digest], [known["episode_id"]])

    # ---- get_children (親 → 子方向) ----

    def test_get_children_lists_continuations(self):
        parent = self._episode()
        c1 = self._episode()
        self._link(c1, {"parent_ref": parent["episode_ref"], "layer": EI.LAYER_FACT})
        c2 = self._episode()
        self._link(c2, {"parent_ref": parent["episode_ref"], "layer": EI.LAYER_DIGEST})
        children = EI.get_children(self.manager, "p1", parent["episode_ref"])
        child_ids = {c["child_episode_id"] for c in children}
        self.assertEqual(child_ids, {c1["episode_id"], c2["episode_id"]})

    # ---- 継承祖先の探索 (咀嚼を継承チェーンに閉じる原始関数) ----

    def test_get_ancestors_walks_chain(self):
        a = self._episode()
        b = self._episode()
        self._link(b, {"parent_ref": a["episode_ref"], "layer": EI.LAYER_FACT})
        c = self._episode()
        self._link(c, {"parent_ref": b["episode_ref"], "layer": EI.LAYER_FACT})
        ancestors = EI.get_ancestors(self.manager, "p1", c["episode_ref"])
        self.assertEqual(set(ancestors), {a["episode_id"], b["episode_id"]})

    def test_get_ancestors_layer_filter(self):
        a = self._episode()
        b = self._episode()
        self._link(b, {"parent_ref": a["episode_ref"], "layer": EI.LAYER_DIGEST})
        c = self._episode()
        self._link(c, {"parent_ref": b["episode_ref"], "layer": EI.LAYER_FACT})
        # 事実層だけ辿ると b で止まる (b→a は digest 層)。
        fact_only = EI.get_ancestors(
            self.manager, "p1", c["episode_ref"], layer=EI.LAYER_FACT,
        )
        self.assertEqual(set(fact_only), {b["episode_id"]})

    def _raw_edge(self, persona_id: str, child_id: str, parent_id: str, layer: str) -> None:
        """公開 API を迂回してエッジ行を直接挿入する (壊れたデータの模擬)。"""
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
        a = self._episode()
        b = self._episode()
        self._link(b, {"parent_ref": a["episode_ref"], "layer": EI.LAYER_FACT})
        # 逆向きエッジを直接挿入して循環を作る (a の親 = b)。公開 API は今や
        # これを拒否するので、防御ガードの検証には ORM 直挿しで壊れた状態を作る。
        self._raw_edge("p1", a["episode_id"], b["episode_id"], EI.LAYER_FACT)
        # 循環でも暴走せず有限で停止する。start (=b) は「自分を含まない」設計で
        # 除外されるので祖先は a のみ。
        ancestors = EI.get_ancestors(self.manager, "p1", b["episode_ref"])
        self.assertEqual(set(ancestors), {a["episode_id"]})

    def test_multi_hop_cycle_rejected_by_public_api(self):
        """公開 API だけで多段の循環は作れない (DAG 不変条件を書き込み時に強制)。"""
        a = self._episode()
        b = self._episode()
        self._link(b, {"parent_ref": a["episode_ref"], "layer": EI.LAYER_FACT})
        # b→a が既にある。a→b を足すと a→b→a で循環 → 拒否。
        with self.assertRaises(EI.InheritanceError):
            self._link(a, {"parent_ref": b["episode_ref"], "layer": EI.LAYER_FACT})
        self.assertEqual(self._count_edges(), 1)

    def test_deep_cycle_rejected(self):
        """3 段の連鎖でも循環は弾く (a→b→c、c→a を拒否)。層違いでも巡回は巡回。"""
        a = self._episode()
        b = self._episode()
        self._link(b, {"parent_ref": a["episode_ref"], "layer": EI.LAYER_FACT})
        c = self._episode()
        self._link(c, {"parent_ref": b["episode_ref"], "layer": EI.LAYER_FACT})
        with self.assertRaises(EI.InheritanceError):
            self._link(a, {"parent_ref": c["episode_ref"], "layer": EI.LAYER_DIGEST})

    def test_long_chain_cycle_rejected(self):
        """129 段超の直列鎖でも先頭→末尾の逆向きを弾く (深さ上限で見逃さない)。"""
        nodes = [self._episode()]
        for _ in range(140):
            child = self._episode()
            self._link(
                child, {"parent_ref": nodes[-1]["episode_ref"], "layer": EI.LAYER_FACT},
            )
            nodes.append(child)
        head, tail = nodes[0], nodes[-1]
        # head→…→tail の鎖がある。head の親に tail を足すと巨大な循環 → 拒否。
        with self.assertRaises(EI.InheritanceError):
            self._link(head, {"parent_ref": tail["episode_ref"], "layer": EI.LAYER_FACT})

    def test_get_ancestors_returns_full_deep_chain(self):
        """深い鎖でも全祖先を返す (深さ上限で古い側を落とさない)。"""
        nodes = [self._episode()]
        for _ in range(140):
            child = self._episode()
            self._link(
                child, {"parent_ref": nodes[-1]["episode_ref"], "layer": EI.LAYER_FACT},
            )
            nodes.append(child)
        ancestors = EI.get_ancestors(self.manager, "p1", nodes[-1]["episode_ref"])
        # 末尾の祖先 = 自分以外の全ノード (140 個)。
        self.assertEqual(len(ancestors), len(nodes) - 1)
        self.assertIn(nodes[0]["episode_id"], ancestors)

    # ---- 冪等・検証・エラー ----

    def test_idempotent_edge_recording(self):
        parent = self._episode()
        child = self._episode()
        spec = {"parent_ref": parent["episode_ref"], "layer": EI.LAYER_FACT}
        self._link(child, spec)
        # 同一 (子, 親, 層) を再記帳しても増えない。
        self._link(child, spec)
        self.assertEqual(self._count_edges(), 1)

    def test_same_parent_different_layers_coexist(self):
        parent = self._episode()
        child = self._episode()
        self._link(
            child,
            {"parent_ref": parent["episode_ref"], "layer": EI.LAYER_FACT},
            {"parent_ref": parent["episode_ref"], "layer": EI.LAYER_DIGEST},
        )
        parents = EI.get_parents(self.manager, "p1", child["episode_ref"])
        self.assertEqual(len(parents), 2)

    def test_duplicate_in_same_batch_collapses(self):
        parent = self._episode()
        child = self._episode()
        spec = {"parent_ref": parent["episode_ref"], "layer": EI.LAYER_FACT}
        self._link(child, spec, dict(spec))
        self.assertEqual(
            len(EI.get_parents(self.manager, "p1", child["episode_ref"])), 1,
        )

    def test_self_loop_rejected(self):
        ep = self._episode()
        with self.assertRaises(EI.InheritanceError):
            self._link(ep, {"parent_ref": ep["episode_ref"], "layer": EI.LAYER_FACT})

    def test_invalid_layer_rejected(self):
        parent = self._episode()
        child = self._episode()
        with self.assertRaises(EI.InheritanceError):
            self._link(child, {"parent_ref": parent["episode_ref"], "layer": "bogus"})
        self.assertEqual(self._count_edges(), 0)

    def test_missing_parent_ref_rejected(self):
        child = self._episode()
        with self.assertRaises(EI.InheritanceError):
            self._link(child, {"layer": EI.LAYER_FACT})

    def test_unknown_parent_ref_rejected(self):
        """存在しない ``episode:N`` の親は弾く (エッジは 1 本も残らない)。

        ``episode:N`` の解決失敗は参照解決層の
        :class:`saiverse.episodes.EpisodeNotFoundError` で出る (所属照合まで
        辿り着かないので InheritanceError にはならない)。
        """
        child = self._episode()
        with self.assertRaises(E.EpisodeNotFoundError):
            self._link(child, {"parent_ref": "episode:999", "layer": EI.LAYER_FACT})
        self.assertEqual(self._count_edges(), 0)
        self.assertEqual(self._episode_count("p1"), 1)

    def test_nonexistent_uuid_parent_rejected(self):
        """UUID の親参照でも存在しなければ弾く (形式検査だけで通さない)。"""
        child = self._episode()
        with self.assertRaises(EI.InheritanceError):
            self._link(child, {
                "parent_ref": "00000000-0000-0000-0000-000000000000",
                "layer": EI.LAYER_FACT,
            })
        self.assertEqual(self._count_edges(), 0)

    def test_cross_persona_uuid_parent_rejected(self):
        """他ペルソナの Episode UUID を自分の継承エッジ親にできない (所属照合)。"""
        p2_ep = self._episode(persona_id="p2")
        p1_child = self._episode()
        with self.assertRaises(EI.InheritanceError):
            self._link(p1_child, {
                "parent_ref": p2_ep["episode_id"], "layer": EI.LAYER_FACT,
            })
        self.assertEqual(self._count_edges(), 0)

    def test_cross_persona_uuid_child_rejected(self):
        """他ペルソナの Episode UUID を自分の子として記帳できない。"""
        p2_ep = self._episode(persona_id="p2")
        p1_parent = self._episode()
        with self.assertRaises(EI.InheritanceError):
            EI.record_edges(
                self.manager, "p1", p2_ep["episode_id"],
                [{"parent_ref": p1_parent["episode_ref"], "layer": EI.LAYER_FACT}],
            )
        self.assertEqual(self._count_edges(), 0)

    # ---- 呼び出し元 tx への相乗り (session=) ----

    def test_edge_recorded_in_caller_session(self):
        """``record_edges(session=)`` は commit しない — 確定は呼び出し元の 1 commit。"""
        parent = self._episode()
        child = self._episode()
        db = self.SessionLocal()
        try:
            EI.record_edges(
                self.manager, "p1", child["episode_ref"],
                [{"parent_ref": parent["episode_ref"], "layer": EI.LAYER_FACT}],
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

    def test_caller_session_rollback_leaves_no_edge(self):
        """呼び出し元が rollback したらエッジも残らない (原子性)。"""
        parent = self._episode()
        child = self._episode()
        db = self.SessionLocal()
        try:
            EI.record_edges(
                self.manager, "p1", child["episode_ref"],
                [{"parent_ref": parent["episode_ref"], "layer": EI.LAYER_FACT}],
                session=db,
            )
            db.rollback()
        finally:
            db.close()
        self.assertEqual(self._count_edges(), 0)

    # ---- ペルソナ分離 ----

    def test_edges_are_per_persona(self):
        p1_parent = self._episode()
        p1_child = self._episode()
        self._link(p1_child, {
            "parent_ref": p1_parent["episode_ref"], "layer": EI.LAYER_FACT,
        })
        # p2 の episode:1 は別物 — p2 側の探索は p1 のエッジを拾わない。
        self._episode(persona_id="p2")
        self.assertEqual(EI.get_children(self.manager, "p2", "episode:1"), [])


if __name__ == "__main__":
    unittest.main()
