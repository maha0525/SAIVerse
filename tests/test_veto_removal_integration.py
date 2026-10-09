"""束ねの拒否権撤去の一気通貫テスト — 実物の束ね → 実物の帯組み立て。

docs/intent/chronicle_consolidation_veto_removal.md の検証の旅「発端の形の再現」
を隔離 DB で組む。単体テスト (tests/test_arasuji_bands.py /
tests/test_episode_context.py) は束ねと帯を別々に固定しているが、ここでは
両者を**差し替えなしで直列に**通し、接続の食い違い (束ねが作った親を帯の
降下規則が拾えない等) を検出する。差し替えるのは LLM クライアントだけ
(決定論の fake)。

使う実物:

- ``sai_memory.arasuji.bands.run_band_overflow`` (束ね・連鎖)
- ``sai_memory.arasuji.context.get_episode_context`` (帯の組み立て・降下規則・
  予算ガード)
- ``SessionLifecycle`` の窓の記録 API (session_anchor 行の upsert・畳みの保存と
  読み出し)、畳みへの一次あらすじ id の引き当て (``_attach_chronicle_refs``)、
  窓の digest 解決 (``apply_window_folds``)
- ``collect_folded_chronicle_entry_ids`` (全モデル集約 — 吸収の供給口)

形 (発端 = なかみつさんの報告の形): 未統合の一次あらすじ 58 件、使われなく
なった古いモデルの窓の記録 3 本 (どれも畳みを持つ)、現役 2 モデル (A は古い
区間を digest 提示中、B は畳みなし)。旧仕様ではどれか一つの窓の畳みが束ねを
止めていた。

除外名簿の取り方は memory_weave section (sea/head_pipeline/sections/
memory_weave.py) と同じく、**いま組んでいるモデルの行だけ**を読む
(``load_folded_ranges(persona, model)`` = ``load_anchor_entry`` +
``deserialize_folds``)。
"""

from __future__ import annotations

import json
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Dict, List, Set

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from database.models import Base
from sai_memory.arasuji.bands import BAND_CHAR_LIMIT, run_band_overflow
from sai_memory.arasuji.context import get_episode_context
from sai_memory.arasuji.storage import create_entry, get_entry, init_arasuji_tables
from sai_memory.memory.storage import add_message, init_db
from sea.session_lifecycle import (
    SessionLifecycle,
    collect_folded_chronicle_entry_ids,
)
from sea.session_window import FoldedRange

PERSONA_ID = "veto_itest"

#: 発端の報告の未統合 Lv1 の件数。
N_LV1 = 58
#: 一次あらすじ 1 本の字数 (58 × 600 = 34,800 字 ≫ 発火上限 5,000)。
LV1_CHARS = 600
#: fake LLM が返す親あらすじの字数。見込み (500) より長くして、レベル 2 の
#: 並びも溢れさせ、レベル 3 までの連鎖を実際に起こす。
PARENT_CHARS = 1_200
#: 帯の予算 (既定値と同じ 2 万字を明示 — env に依存させない)。
BAND_BUDGET = 20_000

T0 = 1_760_000_000  # 2025-10 頃の epoch 秒 (値に意味は無い)


class _FakeClient:
    """決定論の fake LLM — 呼び出しごとに区別できる本文を返す。"""

    def __init__(self):
        self.calls = 0

    def generate(self, messages, tools=None, **kwargs):
        self.calls += 1
        head = f"統合あらすじ{self.calls:02d}。"
        return head + "ま" * (PARENT_CHARS - len(head))

    def consume_usage(self):
        return None


def _session_factory():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine), engine


class VetoRemovalIntegrationTest(unittest.TestCase):
    """発端の形で、実物の束ね → 実物の帯組み立てを直列に通す。"""

    def setUp(self):
        # --- 隔離 SAIMemory (Chronicle + 生ログ) ---
        self.conn = init_db(":memory:")
        init_arasuji_tables(self.conn)
        self.addCleanup(self.conn.close)

        # 一次あらすじ i は生ログ 2 件を覆う (source_ids = 実メッセージ id)。
        self.lv1: List = []
        self.msgs_of: Dict[str, List[Dict]] = {}
        self.all_messages: List[Dict] = []
        for i in range(N_LV1):
            t = T0 + i * 3_600
            pair = []
            for k, role in enumerate(("user", "assistant")):
                content = f"会話{i:02d}-{k}"
                mid = add_message(
                    self.conn, "main", role, content, created_at=t + k * 10,
                )
                msg = {"id": mid, "role": role, "content": content,
                       "created_at": t + k * 10}
                pair.append(msg)
                self.all_messages.append(msg)
            prefix = f"[一次{i:02d}]"
            entry = create_entry(
                self.conn, level=1,
                content=prefix + "あ" * (LV1_CHARS - len(prefix)),
                source_ids=[m["id"] for m in pair],
                start_time=pair[0]["created_at"], end_time=pair[1]["created_at"],
                source_count=2, message_count=2,
                extra_metadata={"digest_origin": "batch", "coverage_chars": 10_000},
            )
            self.lv1.append(entry)
            self.msgs_of[entry.id] = pair
        # 最後の一次あらすじより新しい、まだ編纂されていない生ログ (窓の末尾)。
        for k in range(3):
            t = T0 + N_LV1 * 3_600 + k * 60
            content = f"未編纂の末尾{k}"
            mid = add_message(self.conn, "main", "user", content, created_at=t)
            self.all_messages.append(
                {"id": mid, "role": "user", "content": content, "created_at": t},
            )

        # --- 隔離 world DB の窓の記録 (session_anchor 行) ---
        self.session_factory, engine = _session_factory()
        self.addCleanup(engine.dispose)
        self.manager = SimpleNamespace(SessionLocal=self.session_factory)
        self.lifecycle = SessionLifecycle(SimpleNamespace(), self.manager)
        adapter = SimpleNamespace(is_ready=lambda: True, conn=self.conn)
        self.persona = SimpleNamespace(
            persona_id=PERSONA_ID, model="model-a", sai_memory=adapter,
        )

        now = datetime.now().replace(microsecond=0)
        # 使われなくなった古いモデル 3 本 — 30 日前の記録、どれも古い区間を
        # digest 提示中 (旧仕様ではこれだけで束ねが恒久停止した)。
        self.stale_models = ["old-model-1", "old-model-2", "old-model-3"]
        for n, model in enumerate(self.stale_models):
            self._write_row(model, anchor_idx=2 * n, updated_at=now - timedelta(days=30))
            self._save_folds(model, [[2 * n, 2 * n + 1]])
        # 現役 A: 一次あらすじ 10〜13 の区間を digest 提示中 (2 つの畳み)。
        # 窓は 10 の最初のメッセージから今まで。
        self.a_window_start = 10
        self._write_row("model-a", anchor_idx=self.a_window_start, updated_at=now,
                        ttl=3_600)
        self._save_folds("model-a", [[10, 11], [12, 13]])
        # 現役 B: 畳みなし。
        self._write_row("model-b", anchor_idx=50, updated_at=now, ttl=300)

        # 除外名簿 (memory_weave と同じ読み方: いま組んでいるモデルの行だけ)。
        self.excl_a = self._exclusion_for("model-a")
        self.excl_b = self._exclusion_for("model-b")
        self.presented_ids = {self.lv1[i].id for i in (10, 11, 12, 13)}

        # --- 束ねの前の観測 ---
        self.a_window_messages = [
            m for m in self.all_messages
            if m["created_at"] >= self.lv1[self.a_window_start].start_time
        ]
        self.a_presented_before = self.lifecycle.apply_window_folds(
            self.persona, "model-a", self.a_window_messages,
        )
        self.all_folded_ids = collect_folded_chronicle_entry_ids(
            self.manager, PERSONA_ID,
        )
        self.content_before = self._contents(self.all_folded_ids)
        self.b_band_chars_before = self._band_chars(self.excl_b)

        # --- 実物の束ねを収束まで回す (承認ゲートなし = 1 呼び出し 3 回の
        # 安全弁ごとに呼び直す。generate_chronicle の呼び直しと同じ形) ---
        self.client = _FakeClient()
        self.created = 0
        for _ in range(50):
            n = run_band_overflow(self.conn, self.client)
            if n == 0:
                break
            self.created += n

    # ------------------------------------------------------------------
    # 組み立ての道具
    # ------------------------------------------------------------------

    def _write_row(self, model, *, anchor_idx, updated_at, ttl=None):
        entry = {
            "anchor_id": self.msgs_of[self.lv1[anchor_idx].id][0]["id"],
            "updated_at": updated_at.isoformat(),
        }
        if ttl is not None:
            entry["ttl_seconds"] = ttl
        self.assertTrue(
            self.lifecycle.upsert_anchor_entry(PERSONA_ID, model, entry),
        )

    def _save_folds(self, model, groups):
        """一次あらすじの番号の組ごとに 1 つの畳みを作り、実物の引き当て
        (``_attach_chronicle_refs`` — 退場時と同じ経路) で id を刻んで保存する。"""
        folds = []
        for group in groups:
            msgs = [m for i in group for m in self.msgs_of[self.lv1[i].id]]
            folds.append(FoldedRange(
                message_ids=[m["id"] for m in msgs],
                start_at=msgs[0]["created_at"], end_at=msgs[-1]["created_at"],
            ))
        self.lifecycle._attach_chronicle_refs(self.persona, folds)
        for group, fold in zip(groups, folds):
            # ハーネスの前提の検算: 引き当てが意図した一次あらすじを指している。
            self.assertEqual(
                fold.chronicle_entry_ids, [self.lv1[i].id for i in group],
            )
            self.assertEqual(len(fold.chronicle_short_ids), len(group))
        self.lifecycle.save_folded_ranges(PERSONA_ID, model, folds)

    def _exclusion_for(self, model) -> Set[str]:
        ids: Set[str] = set()
        for fold in self.lifecycle.load_folded_ranges(PERSONA_ID, model):
            ids.update(fold.chronicle_entry_ids)
        return ids

    def _contents(self, ids) -> Dict[str, bytes]:
        out = {}
        for eid in ids:
            row = self.conn.execute(
                "SELECT content FROM memopedia_pages WHERE id = ?", (eid,),
            ).fetchone()
            out[eid] = (row[0] or "").encode("utf-8")
        return out

    def _band(self, exclude, budget=BAND_BUDGET):
        return get_episode_context(
            self.conn, char_budget=budget, exclude_entry_ids=exclude or None,
        )

    def _band_chars(self, exclude, budget=BAND_BUDGET) -> int:
        return sum(len(e.content) for e in self._band(exclude, budget))

    def _leaves(self, entry_id) -> List[str]:
        """あるエントリが覆う一次あらすじの id (source_ids を下まで辿る)。"""
        entry = get_entry(self.conn, entry_id)
        if entry.level <= 1:
            return [entry.id]
        out: List[str] = []
        for cid in entry.source_ids:
            out.extend(self._leaves(cid))
        return out

    def _ancestors(self, entry_id) -> List[str]:
        out = []
        entry = get_entry(self.conn, entry_id)
        while entry.parent_id:
            out.append(entry.parent_id)
            entry = get_entry(self.conn, entry.parent_id)
        return out

    def _band_parents(self):
        rows = self.conn.execute(
            "SELECT id FROM memopedia_pages WHERE category = 'chronicle' "
            "AND json_extract(metadata, '$.digest_origin') = 'band'"
        ).fetchall()
        return [get_entry(self.conn, r[0]) for r in rows]

    def _assert_band_partitions(self, band, expected_leaves: Set[str]):
        """帯の各項目が覆う一次あらすじが、重ならず (二重提示なし)、
        期待集合をちょうど覆う (穴なし)。"""
        seen: List[str] = []
        for item in band:
            seen.extend(self._leaves(item.source_id))
        self.assertEqual(len(seen), len(set(seen)), "帯の中で同じ期間が二重に出ている")
        self.assertEqual(set(seen), expected_leaves)

    # ------------------------------------------------------------------
    # 前提 (ハーネスが発端の形になっていること)
    # ------------------------------------------------------------------

    def test_precondition_origin_shape(self):
        """束ねの前は帯が予算を超えていた (未統合 Lv1 が並ぶだけで畳める親が
        無い) こと、全モデル集約の集合が古い窓 3 本 + A の畳みを含むこと。"""
        self.assertGreater(self.b_band_chars_before, BAND_BUDGET)
        stale_ids = {self.lv1[i].id for i in range(6)}
        self.assertEqual(self.all_folded_ids, stale_ids | self.presented_ids)
        self.assertEqual(self.excl_a, self.presented_ids)
        self.assertEqual(self.excl_b, set())

    # ------------------------------------------------------------------
    # (a) 提示中の Lv1 も束ねられて親ができる
    # ------------------------------------------------------------------

    def test_a_presented_lv1_are_consolidated_into_parents(self):
        self.assertGreater(self.created, 0)
        self.assertEqual(self.client.calls, self.created)
        # どの窓 (古いモデルを含む) が提示中の一次あらすじも、束ねを止めない。
        for eid in self.all_folded_ids:
            entry = get_entry(self.conn, eid)
            self.assertTrue(entry.is_consolidated, f"{eid[:8]} was not consolidated")
            self.assertIsNotNone(entry.parent_id)
        # A の提示中 (10〜13) は同じ親 P の子で、P はさらに上位 G の子
        # (連鎖がレベル 3 まで届いている — 降下は 2 段になる)。
        parents = {get_entry(self.conn, eid).parent_id for eid in self.presented_ids}
        self.assertEqual(len(parents), 1)
        p = get_entry(self.conn, parents.pop())
        self.assertEqual(p.level, 2)
        self.assertEqual(p.source_ids, [self.lv1[i].id for i in range(8, 16)])
        self.assertIsNotNone(p.parent_id)
        self.assertEqual(get_entry(self.conn, p.parent_id).level, 3)
        # 収束: 残った未統合の並びはどのレベルも発火上限以下。
        rows = self.conn.execute(
            "SELECT level, SUM(length(content)) FROM arasuji_entries "
            "WHERE is_consolidated = 0 GROUP BY level"
        ).fetchall()
        for level, chars in rows:
            self.assertLessEqual(chars, BAND_CHAR_LIMIT, f"level {level} still overflows")

    # ------------------------------------------------------------------
    # (b) 束ねの前後で提示中 Lv1 の本文はバイト一致 = A の窓の digest 解決が不変
    # ------------------------------------------------------------------

    def test_b_presented_digest_bytes_unchanged(self):
        self.assertEqual(self._contents(self.all_folded_ids), self.content_before)
        # A の窓を実物の digest 解決で組み直しても、提示のバイト列が同じ。
        after = self.lifecycle.apply_window_folds(
            self.persona, "model-a", self.a_window_messages,
        )
        dump = lambda msgs: json.dumps(msgs, ensure_ascii=False, sort_keys=True)  # noqa: E731
        self.assertEqual(dump(after), dump(self.a_presented_before))
        # 検算: 置き換えが実際に立っている (digest が引けずに生ログへ倒れた
        # fail-open の「一致」ではない)。
        placeholders = [m for m in after if str(m["id"]).startswith("folded:")]
        self.assertEqual(len(placeholders), 2)
        self.assertIn(get_entry(self.conn, self.lv1[10].id).content,
                      placeholders[0]["content"])
        self.assertIn(get_entry(self.conn, self.lv1[13].id).content,
                      placeholders[1]["content"])

    # ------------------------------------------------------------------
    # (c)(d) A の節目の帯: 除外した Lv1 も、それを子孫に持つ親も出ない
    # ------------------------------------------------------------------

    def test_c_d_model_a_band_has_no_double_presentation(self):
        band = self._band(self.excl_a)
        shown = {e.source_id for e in band}
        # (c) 除外した一次あらすじは帯に出ない。
        self.assertFalse(shown & self.presented_ids)
        # (d) それを子孫に持つ新しい親 (P) と、さらにその親 (G) も出ない。
        ancestors = {a for eid in self.presented_ids for a in self._ancestors(eid)}
        self.assertEqual(len(ancestors), 2)
        self.assertFalse(shown & ancestors)
        # 一般形: どの項目の子孫にも除外が居ない、かつ帯は除外以外の全期間を
        # 重ならずに覆う (降りた先の兄弟 8, 9, 14, 15 が穴にならない)。
        all_lv1 = {e.id for e in self.lv1}
        self._assert_band_partitions(band, all_lv1 - self.presented_ids)
        for i in (8, 9, 14, 15):
            self.assertIn(self.lv1[i].id, shown)
        # 帯の予算内 (降下で開いた分を含めても収まる規模)。
        self.assertLessEqual(sum(len(e.content) for e in band), BAND_BUDGET)

    def test_d_budget_guard_does_not_reintroduce_descended_parents(self):
        """予算を締めて予算ガードが親を引きたがる状況でも、降下で開いた祖先
        (P, G) は戻らない。収まりきらない超過は WARNING で観測される
        (intent「帰結」— fold が窓に居る間、帯は予算を超えうる)。"""
        ancestors = {a for eid in self.presented_ids for a in self._ancestors(eid)}
        with self.assertLogs("sai_memory.arasuji.context", level="WARNING") as logs:
            band = self._band(self.excl_a, budget=5_000)
        self.assertTrue(any("budget exceeded" in line for line in logs.output))
        shown = {e.source_id for e in band}
        self.assertFalse(shown & (ancestors | self.presented_ids))
        all_lv1 = {e.id for e in self.lv1}
        self._assert_band_partitions(band, all_lv1 - self.presented_ids)

    # ------------------------------------------------------------------
    # (e) 除外の子孫も出ない
    # ------------------------------------------------------------------

    def test_e_descendants_of_an_excluded_parent_are_dropped(self):
        """除外名簿のエントリ自身の子孫も帯に出ない。

        実経路の窓の畳みは一次あらすじしか指さない (``_attach_chronicle_refs``
        は level=1 だけを引き当てる) ので、除外名簿に Chronicle の子孫を持つ
        id が載るのは壊れた・旧形式の記録のときだけ。その防御の枝を、実物の
        束ねが作った木の上で確かめる: 名簿に中間の親 P (Lv2) を載せると、
        P の祖先 G は降り、P 自身と P の子 (一次あらすじ 8〜15) は落ち、
        それ以外は重ならずに覆われる。"""
        p_id = get_entry(self.conn, self.lv1[10].id).parent_id
        g_id = get_entry(self.conn, p_id).parent_id
        p_leaves = set(self._leaves(p_id))
        self.assertEqual(p_leaves, {self.lv1[i].id for i in range(8, 16)})
        band = self._band({p_id})
        shown = {e.source_id for e in band}
        self.assertNotIn(p_id, shown)
        self.assertNotIn(g_id, shown)
        self.assertFalse(shown & p_leaves)
        all_lv1 = {e.id for e in self.lv1}
        self._assert_band_partitions(band, all_lv1 - p_leaves)

    # ------------------------------------------------------------------
    # 対照: B (畳みなし) の帯には親が普通に出る
    # ------------------------------------------------------------------

    def test_model_b_band_shows_the_new_parents(self):
        band = self._band(self.excl_b)
        shown = {e.source_id for e in band}
        # A が降りて避けた祖先 (P か G) が、B の帯には粗い粒度で出る。
        ancestors = {a for eid in self.presented_ids for a in self._ancestors(eid)}
        self.assertTrue(shown & ancestors, "B の帯に新しい親が出ていない")
        band_parent_ids = {p.id for p in self._band_parents()}
        self.assertTrue(shown & band_parent_ids)
        # 全期間を重ならずに覆い、予算内に収束している (束ね前は超過していた)。
        self._assert_band_partitions(band, {e.id for e in self.lv1})
        chars = sum(len(e.content) for e in band)
        self.assertLessEqual(chars, BAND_BUDGET)
        self.assertLess(chars, self.b_band_chars_before)


if __name__ == "__main__":
    unittest.main()
