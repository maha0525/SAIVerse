"""自動想起 第0層 (sea/auto_recall.py) のユニットテスト。

記憶アーキv2 §4。unified_recall をモックして、しきい値選別・sticky 台帳・
コンテキスト内除外・文字数上限のコアロジックのみを検証する (埋め込み / DB は使わない)。
"""

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from sai_memory.unified_recall import RecallHit
from sea import auto_recall


def _hit(source_type, source_id, *, embed_score, title="タイトル", content="内容テキスト",
         chronicle_entry_id=None):
    return RecallHit(
        source_type=source_type,
        source_id=source_id,
        title=title,
        content=content,
        score=0.01,  # RRF スケール (最終スコア)。しきい値判定には使われないはず。
        uri=f"saiverse://self/{source_type}/{source_id}",
        embed_score=embed_score,
        chronicle_entry_id=chronicle_entry_id,
    )


def _msgs(*pairs):
    """(role, content, [id]) タプル列から message dict 列を作る。"""
    out = []
    for p in pairs:
        role, content = p[0], p[1]
        m = {"role": role, "content": content}
        if len(p) >= 3:
            m["id"] = p[2]
        out.append(m)
    return out


class AutoRecallBase(unittest.TestCase):
    PERSONA = "test_persona"
    THREAD = "test_persona:__persona__"

    def setUp(self):
        auto_recall.reset_ledger(self.PERSONA)
        # env をクリーンにして既定値でテストする
        self._env_keys = [
            "SAIVERSE_AUTO_RECALL_THRESHOLD",
            "SAIVERSE_AUTO_RECALL_STICKY_TURNS",
            "SAIVERSE_AUTO_RECALL_QUERY_MESSAGES",
            "SAIVERSE_AUTO_RECALL_TOPK",
            "SAIVERSE_AUTO_RECALL_MSG_THRESHOLD_OFFSET",
            "SAIVERSE_AUTO_RECALL_ENTITY_AMBIENT_COUNT",
        ]
        self._patcher = patch.dict("os.environ", {}, clear=False)
        self._patcher.start()
        import os
        for k in self._env_keys:
            os.environ.pop(k, None)

    def tearDown(self):
        self._patcher.stop()
        auto_recall.reset_ledger(self.PERSONA)

    def _run(self, hits, messages):
        # sea.auto_recall は unified_recall を lazy import するので原本を patch する
        with patch("sai_memory.unified_recall.unified_recall", return_value=hits):
            return auto_recall.run_auto_recall(
                conn=object(), embedder=object(), messages=messages,
                persona_id=self.PERSONA, thread_id=self.THREAD,
            )


class TestThresholdSelection(AutoRecallBase):

    def test_above_threshold_injected(self):
        hits = [_hit("chronicle", "c1", embed_score=0.88, title="Chronicle Lv1: 2025-06-07")]
        res = self._run(hits, _msgs(("user", "スタックチャンの話")))
        self.assertTrue(res.injected)
        self.assertIn("ふと浮かんだ記憶", res.block)
        self.assertIn("Chronicle", res.block)
        self.assertEqual(res.accepted_count, 1)

    def test_below_threshold_not_injected(self):
        hits = [_hit("memopedia", "p1", embed_score=0.5)]
        res = self._run(hits, _msgs(("user", "無関係な話")))
        self.assertFalse(res.injected)
        self.assertIsNone(res.block)
        self.assertEqual(res.accepted_count, 0)

    def test_keyword_only_hit_dropped(self):
        # embed_score=None (キーワードのみ) は採用しない
        hits = [_hit("fragment", "f1", embed_score=None)]
        res = self._run(hits, _msgs(("user", "何か")))
        self.assertFalse(res.injected)
        self.assertEqual(res.accepted_count, 0)

    def test_custom_threshold_env(self):
        import os
        os.environ["SAIVERSE_AUTO_RECALL_THRESHOLD"] = "0.9"
        hits = [_hit("chronicle", "c1", embed_score=0.85)]
        res = self._run(hits, _msgs(("user", "話題")))
        self.assertFalse(res.injected)  # 0.85 < 0.9

    def test_empty_query_skips(self):
        res = self._run([_hit("chronicle", "c1", embed_score=0.99)], _msgs(("user", "   ")))
        self.assertFalse(res.injected)
        self.assertEqual(res.query, "")


class TestContextExclusion(AutoRecallBase):

    def test_message_hit_already_in_context_excluded(self):
        # source_id が現在のコンテキスト窓のメッセージ id と一致する message ヒットは除外
        messages = _msgs(("user", "こんにちは", "m123"), ("assistant", "やあ", "m124"))
        hits = [_hit("message", "m123", embed_score=0.95, title="user @ 2025-06-07")]
        res = self._run(hits, messages)
        self.assertFalse(res.injected)

    def test_message_hit_outside_context_kept(self):
        messages = _msgs(("user", "こんにちは", "m123"))
        hits = [_hit("message", "m999", embed_score=0.95, title="user @ 2024-01-01")]
        res = self._run(hits, messages)
        self.assertTrue(res.injected)
        self.assertIn("過去の会話", res.block)

    def test_non_message_hit_not_excluded_by_id(self):
        # chronicle/memopedia は message id 除外の対象外
        messages = _msgs(("user", "hi", "m1"))
        hits = [_hit("chronicle", "m1", embed_score=0.9)]
        res = self._run(hits, messages)
        self.assertTrue(res.injected)


class TestStickyWindow(AutoRecallBase):

    def test_item_persists_after_dropping_below(self):
        import os
        os.environ["SAIVERSE_AUTO_RECALL_STICKY_TURNS"] = "3"
        good = [_hit("chronicle", "c1", embed_score=0.9, content="重要な記憶")]
        # ターン1: 採用
        r1 = self._run(good, _msgs(("user", "c1 の話")))
        self.assertTrue(r1.injected)
        # ターン2-4: しきい値を割る (別ヒット) が、c1 は sticky で残り続ける
        low = [_hit("memopedia", "p9", embed_score=0.1)]
        for turn in range(3):
            r = self._run(low, _msgs(("user", f"別の話 {turn}")))
            self.assertTrue(r.injected, f"turn {turn}: c1 should stick")
            self.assertIn("重要な記憶", r.block)
        # ターン5: sticky_turns=3 を超えたので c1 は台帳から除去され注入されなくなる
        r5 = self._run(low, _msgs(("user", "さらに別の話")))
        self.assertFalse(r5.injected)

    def test_reaccept_resets_stale_counter(self):
        import os
        os.environ["SAIVERSE_AUTO_RECALL_STICKY_TURNS"] = "2"
        good = [_hit("chronicle", "c1", embed_score=0.9)]
        low = [_hit("memopedia", "p9", embed_score=0.1)]
        self._run(good, _msgs(("user", "c1")))       # accept, stale=0
        self._run(low, _msgs(("user", "x")))          # stale=1
        self._run(good, _msgs(("user", "c1 again")))  # re-accept, stale=0
        # 再び低スコアが 2 ターン続いても、リセットされたので生き残る
        r = self._run(low, _msgs(("user", "y")))       # stale=1
        self.assertTrue(r.injected)


class TestStickyAlwaysShown(AutoRecallBase):
    """intent doc §4.3: 台帳に残っている (粘着中の) アイテムは、新規採用が何件浮かんで
    も解除されるまで必ず注入される。以前は char_budget で末尾の粘着分が切られ、台帳に
    は残るのに LLM/UI に出ない幽霊になっていた (sticky 仕様違反バグ)。"""

    def test_all_ledger_items_injected(self):
        # 台帳に載ったアイテムは (数の抑制は取り込み側の責務なので) 表示側で
        # 予算足切りされず全件出る。
        hits = [
            _hit("chronicle", f"c{i}", embed_score=0.9 - i * 0.01,
                 title=f"Chronicle Lv1: 2025-06-0{i}",
                 content="X" * 80)
            for i in range(5)
        ]
        res = self._run(hits, _msgs(("user", "話題")))
        self.assertTrue(res.injected)
        # 全 5 件が入る (chronicle は vivid サブ行を持たないので行数 == アイテム数)。
        self.assertEqual(res.block.count("\n- "), 5)
        self.assertIn("深掘り用スペル", res.block)
        self.assertIn("chronicle_read_detail", res.block)

    def test_sticky_item_survives_flood_of_fresh_accepts(self):
        # ターン1: 記憶 A が浮かぶ (stale=0)。
        a = [_hit("fragment", "A", embed_score=0.9, title="記憶A", content="Aの内容")]
        r1 = self._run(a, _msgs(("user", "Aの話")))
        self.assertIn("記憶A", r1.block)
        # ターン2: A は再ヒットしない (stale=1 で粘着継続) が、新規が大量に浮かぶ。
        flood = [
            _hit("fragment", f"N{i}", embed_score=0.9, title=f"新規{i}", content="X" * 80)
            for i in range(8)
        ]
        r2 = self._run(flood, _msgs(("user", "別の話")))
        # 新規8件すべてに加え、粘着中の A も残る (budget で押し出されない = 幽霊化しない)。
        self.assertIn("記憶A", r2.block)
        for i in range(8):
            self.assertIn(f"新規{i}", r2.block)


class TestFooterHandles(AutoRecallBase):
    """タスクB: 行末尾の個別ハンドル表記を廃止し、ブロック末尾の union 1行にまとめる。"""

    def test_no_per_line_handle_suffix(self):
        hits = [_hit("chronicle", "c1", embed_score=0.9, title="Chronicle Lv1: 2025-06-07")]
        res = self._run(hits, _msgs(("user", "話題")))
        self.assertTrue(res.injected)
        # 個別行に「詳細: ...」のような接尾辞が付かない。
        self.assertNotIn("（詳細:", res.block)

    def test_footer_lists_union_of_included_source_handles(self):
        hits = [
            _hit("chronicle", "c1", embed_score=0.9, title="Chronicle Lv1: 2025-06-07"),
            _hit("memopedia", "p1", embed_score=0.88, title="スタックチャン"),
        ]
        res = self._run(hits, _msgs(("user", "話題")))
        self.assertTrue(res.injected)
        # union なので重複せず1回ずつ、chronicle 用と memopedia 用の両方が出る。
        self.assertEqual(res.block.count("chronicle_read_detail"), 1)
        self.assertEqual(res.block.count("memory_read"), 1)
        # フッターはブロック末尾の1行のみ。
        self.assertEqual(res.block.count("深掘り用スペル"), 1)

    def test_orphan_fragment_adds_memory_recall_unified_to_footer(self):
        # 孤児 Fragment (親ページ削除済み = title 空) は memory_read で
        # 深掘りできないので、footer には memory_recall_unified を含める。
        hits = [_hit("fragment", "f1", embed_score=0.9, title="", content="断片の中身")]
        res = self._run(hits, _msgs(("user", "話題")))
        self.assertTrue(res.injected)
        self.assertIn("[記憶の断片]", res.block)
        self.assertIn("memory_recall_unified", res.block)
        # memory_read は (孤児なので) union に含まれない。
        self.assertNotIn("memory_read", res.block)

    def test_footer_omitted_source_types_not_listed(self):
        # 採用されたのが chronicle だけなら memopedia/message 用ハンドルは出ない。
        hits = [_hit("chronicle", "c1", embed_score=0.9)]
        res = self._run(hits, _msgs(("user", "話題")))
        self.assertTrue(res.injected)
        self.assertNotIn("memory_read", res.block)
        self.assertNotIn("memory_recall_unified", res.block)


class TestScope(AutoRecallBase):
    """CONVERSATION スコープ判定は prepare_context 側だが、aspect 導出を確認しておく。"""

    def test_aspect_derivation(self):
        from sea.pulse_context import Aspect, aspect_from_pulse_type
        self.assertIs(aspect_from_pulse_type("user"), Aspect.CONVERSATION)
        self.assertIs(aspect_from_pulse_type("schedule"), Aspect.CONVERSATION)
        self.assertIs(aspect_from_pulse_type(None), Aspect.CONVERSATION)
        self.assertIs(aspect_from_pulse_type("auto"), Aspect.AUTONOMOUS)
        self.assertIs(aspect_from_pulse_type("meta_judgment"), Aspect.META)


class TestNoOp(AutoRecallBase):

    def test_none_conn_embedder(self):
        res = auto_recall.run_auto_recall(
            conn=None, embedder=None, messages=_msgs(("user", "hi")),
            persona_id=self.PERSONA, thread_id=self.THREAD,
        )
        self.assertFalse(res.injected)


class TestEntityTrigger(AutoRecallBase):
    """rev2: タイトル部分一致による決定論エンティティトリガー。

    unified_recall は空ヒットにモックし、埋め込み経路の混入を排除して
    エンティティトリガー単独の挙動を検証する。
    """

    def _titles(self, rows):
        # rows: [(page_id, short_id, title, summary), ...]
        return patch("sea.auto_recall._fetch_memopedia_titles", return_value=rows)

    def test_title_match_triggers_entity(self):
        rows = [("page_aifi", 12, "アイフィ", "アイフィの要約テキスト")]
        messages = _msgs(("user", "例えばアイフィについて。連想が起きれば成功。"))
        with self._titles(rows), patch("sai_memory.unified_recall.unified_recall", return_value=[]):
            res = auto_recall.run_auto_recall(
                conn=object(), embedder=object(), messages=messages,
                persona_id=self.PERSONA, thread_id=self.THREAD,
            )
        self.assertTrue(res.injected)
        self.assertIn("アイフィ", res.block)
        self.assertIn("memopedia:12", res.block)
        self.assertEqual(res.accepted_count, 1)

    def test_ambient_entity_skipped(self):
        # 「エア」が直近3回以上ウィンドウに出現していれば、最新発話に含まれても
        # トリガーされない (常在エンティティの ambient 抑制)。
        rows = [("page_air", 1, "エア", "エアの要約")]
        messages = _msgs(
            ("user", "エア、今日もよろしくね"),
            ("assistant", "エアです、了解"),
            ("user", "エアはどう思う？"),
            ("assistant", "エアとして答えるね"),
            ("user", "エア、最後にこれ聞きたい"),
        )
        with self._titles(rows), patch("sai_memory.unified_recall.unified_recall", return_value=[]):
            res = auto_recall.run_auto_recall(
                conn=object(), embedder=object(), messages=messages,
                persona_id=self.PERSONA, thread_id=self.THREAD,
            )
        self.assertFalse(res.injected)

    def test_short_title_not_matched(self):
        # タイトル長 2 文字未満は照合対象外
        rows = [("page_x", 5, "A", "何かの要約")]
        messages = _msgs(("user", "A という文字を含む発話です"))
        with self._titles(rows), patch("sai_memory.unified_recall.unified_recall", return_value=[]):
            res = auto_recall.run_auto_recall(
                conn=object(), embedder=object(), messages=messages,
                persona_id=self.PERSONA, thread_id=self.THREAD,
            )
        self.assertFalse(res.injected)

    def test_no_title_match_no_trigger(self):
        rows = [("page_aifi", 12, "アイフィ", "アイフィの要約")]
        messages = _msgs(("user", "今日の天気いいね、散歩でも行こうかな"))
        with self._titles(rows), patch("sai_memory.unified_recall.unified_recall", return_value=[]):
            res = auto_recall.run_auto_recall(
                conn=object(), embedder=object(), messages=messages,
                persona_id=self.PERSONA, thread_id=self.THREAD,
            )
        self.assertFalse(res.injected)


class TestMessageThresholdOffset(AutoRecallBase):

    def test_message_hit_between_threshold_and_offset_dropped(self):
        # threshold=0.86 (既定), offset=0.02 → message の実効しきい値は 0.88。
        # embed_score=0.87 は threshold は超えるが実効しきい値未満なので落ちる。
        messages = _msgs(("user", "こんにちは", "m1"))
        hits = [_hit("message", "m999", embed_score=0.87, title="user @ 2024-01-01")]
        with patch("sea.auto_recall._fetch_memopedia_titles", return_value=[]):
            res = self._run(hits, messages)
        self.assertFalse(res.injected)

    def test_message_hit_above_offset_kept(self):
        messages = _msgs(("user", "こんにちは", "m1"))
        hits = [_hit("message", "m999", embed_score=0.89, title="user @ 2024-01-01")]
        with patch("sea.auto_recall._fetch_memopedia_titles", return_value=[]):
            res = self._run(hits, messages)
        self.assertTrue(res.injected)

    def test_non_message_hit_uses_plain_threshold(self):
        # chronicle/memopedia/fragment はオフセットなしの素の threshold で判定される。
        messages = _msgs(("user", "こんにちは"))
        hits = [_hit("chronicle", "c1", embed_score=0.87)]
        with patch("sea.auto_recall._fetch_memopedia_titles", return_value=[]):
            res = self._run(hits, messages)
        self.assertTrue(res.injected)


class TestVividRecall(AutoRecallBase):
    """鮮明想起 — fragment + chronicle_entry_id を持つアイテムに

    「そのときの会話」生ログサブ行を添える。DB は実体 (sqlite3 in-memory) を使い、
    unified_recall だけモックしてコアロジック (選定・スキップ条件) を検証する。
    rev4: 全 fragment 項目が対象 (先頭は最大2行、他は1行)。TestVividRecallMultiItem
    にその配分・タイトル窓・フォールバックのテストをまとめる。
    """

    def _make_conn(self):
        import sqlite3
        conn = sqlite3.connect(":memory:")
        conn.execute(
            "CREATE TABLE arasuji_entries (id TEXT PRIMARY KEY, source_ids_json TEXT)"
        )
        conn.execute(
            "CREATE TABLE memopedia_fragment_embeddings (fragment_id TEXT PRIMARY KEY, vector TEXT)"
        )
        conn.execute(
            "CREATE TABLE message_embeddings (message_id TEXT, chunk_index INTEGER, vector TEXT)"
        )
        conn.execute(
            "CREATE TABLE messages (id TEXT PRIMARY KEY, role TEXT, content TEXT, created_at INTEGER)"
        )
        conn.commit()
        return conn

    def _seed_chronicle_and_messages(self, conn, *, entry_id, message_ids_and_vectors, frag_id, frag_vector):
        import json
        conn.execute(
            "INSERT INTO arasuji_entries (id, source_ids_json) VALUES (?, ?)",
            (entry_id, json.dumps([mid for mid, _v, _c, _r, _t in message_ids_and_vectors])),
        )
        conn.execute(
            "INSERT INTO memopedia_fragment_embeddings (fragment_id, vector) VALUES (?, ?)",
            (frag_id, json.dumps(frag_vector)),
        )
        for mid, vec, content, role, created_at in message_ids_and_vectors:
            conn.execute(
                "INSERT INTO message_embeddings (message_id, chunk_index, vector) VALUES (?, 0, ?)",
                (mid, json.dumps(vec)),
            )
            conn.execute(
                "INSERT INTO messages (id, role, content, created_at) VALUES (?, ?, ?, ?)",
                (mid, role, content, created_at),
            )
        conn.commit()

    def test_vivid_line_attached_to_top_fragment_with_chronicle_link(self):
        conn = self._make_conn()
        # フラグメント埋め込みに近い方 (m_close) と遠い方 (m_far) を用意する。
        self._seed_chronicle_and_messages(
            conn,
            entry_id="chron1",
            frag_id="f1",
            frag_vector=[1.0, 0.0, 0.0],
            message_ids_and_vectors=[
                ("m_close", [1.0, 0.0, 0.0], "感情パラメータについて相談したい", "user", 1735092057),
                ("m_far", [0.0, 1.0, 0.0], "全然関係ない話題", "assistant", 1735092080),
            ],
        )
        hits = [_hit("fragment", "f1", embed_score=0.9, title="感情パラメータ",
                     content="7項目で構成されている", chronicle_entry_id="chron1")]
        with patch("sea.auto_recall._fetch_memopedia_titles", return_value=[]), \
             patch("sai_memory.unified_recall.unified_recall", return_value=hits):
            res = auto_recall.run_auto_recall(
                conn=conn, embedder=object(), messages=_msgs(("user", "感情パラメータの話")),
                persona_id=self.PERSONA, thread_id=self.THREAD,
            )
        self.assertTrue(res.injected)
        self.assertIn("そのときの会話", res.block)
        self.assertIn("感情パラメータについて相談したい", res.block)
        # 遠い方のメッセージは (2件まで許容だが) 近い方が先に出る。
        idx_close = res.block.index("感情パラメータについて相談したい")
        self.assertNotEqual(idx_close, -1)
        conn.close()

    def test_vivid_skipped_when_no_message_embeddings(self):
        conn = self._make_conn()
        import json
        conn.execute(
            "INSERT INTO arasuji_entries (id, source_ids_json) VALUES (?, ?)",
            ("chron1", json.dumps(["m1"])),
        )
        conn.execute(
            "INSERT INTO memopedia_fragment_embeddings (fragment_id, vector) VALUES (?, ?)",
            ("f1", json.dumps([1.0, 0.0, 0.0])),
        )
        # message_embeddings は空のまま (埋め込み未生成)。
        conn.commit()
        hits = [_hit("fragment", "f1", embed_score=0.9, title="タイトル", chronicle_entry_id="chron1")]
        with patch("sea.auto_recall._fetch_memopedia_titles", return_value=[]), \
             patch("sai_memory.unified_recall.unified_recall", return_value=hits):
            res = auto_recall.run_auto_recall(
                conn=conn, embedder=object(), messages=_msgs(("user", "話題")),
                persona_id=self.PERSONA, thread_id=self.THREAD,
            )
        self.assertTrue(res.injected)  # フラグメント自体は注入される
        self.assertNotIn("そのときの会話", res.block)  # vivid だけスキップ
        conn.close()

    def test_vivid_not_attempted_for_non_fragment_top_item(self):
        conn = self._make_conn()
        hits = [_hit("chronicle", "c1", embed_score=0.9)]
        with patch("sea.auto_recall._fetch_memopedia_titles", return_value=[]), \
             patch("sai_memory.unified_recall.unified_recall", return_value=hits):
            res = auto_recall.run_auto_recall(
                conn=conn, embedder=object(), messages=_msgs(("user", "話題")),
                persona_id=self.PERSONA, thread_id=self.THREAD,
            )
        self.assertTrue(res.injected)
        self.assertNotIn("そのときの会話", res.block)
        conn.close()

    def test_vivid_not_attempted_when_fragment_has_no_chronicle_link(self):
        conn = self._make_conn()
        hits = [_hit("fragment", "f1", embed_score=0.9, title="タイトル", chronicle_entry_id=None)]
        with patch("sea.auto_recall._fetch_memopedia_titles", return_value=[]), \
             patch("sai_memory.unified_recall.unified_recall", return_value=hits):
            res = auto_recall.run_auto_recall(
                conn=conn, embedder=object(), messages=_msgs(("user", "話題")),
                persona_id=self.PERSONA, thread_id=self.THREAD,
            )
        self.assertTrue(res.injected)
        self.assertNotIn("そのときの会話", res.block)
        conn.close()

    def test_vivid_char_budget_env_limits_sub_lines(self):
        conn = self._make_conn()
        self._seed_chronicle_and_messages(
            conn,
            entry_id="chron1",
            frag_id="f1",
            frag_vector=[1.0, 0.0, 0.0],
            message_ids_and_vectors=[
                ("m1", [1.0, 0.0, 0.0], "1件目のかなり長めの本文" * 5, "user", 1735092057),
                ("m2", [0.99, 0.01, 0.0], "2件目のかなり長めの本文" * 5, "assistant", 1735092080),
            ],
        )
        import os
        os.environ["SAIVERSE_AUTO_RECALL_VIVID_CHARS"] = "40"
        hits = [_hit("fragment", "f1", embed_score=0.9, title="タイトル", chronicle_entry_id="chron1")]
        with patch("sea.auto_recall._fetch_memopedia_titles", return_value=[]), \
             patch("sai_memory.unified_recall.unified_recall", return_value=hits):
            res = auto_recall.run_auto_recall(
                conn=conn, embedder=object(), messages=_msgs(("user", "話題")),
                persona_id=self.PERSONA, thread_id=self.THREAD,
            )
        self.assertTrue(res.injected)
        # 予算 40 字では1行しか入らない (先頭候補は必ず1行入るガードがあるため)。
        self.assertEqual(res.block.count("そのときの会話"), 1)
        conn.close()


class TestVividRecallMultiItem(AutoRecallBase):
    """rev4: 注入ブロック内の全 fragment 項目への vivid 展開。

    先頭項目は最大2行、他は1行。文字数予算 (SAIVERSE_AUTO_RECALL_VIVID_CHARS) は
    全項目の vivid 合計として共有し、鮮度→スコア順に消費、尽きたら残りはスキップする。
    タイトル出現位置での ±60字トリムと、埋め込み不在時のフォールバック検索も検証する。
    """

    def _make_conn(self):
        import sqlite3
        conn = sqlite3.connect(":memory:")
        conn.execute(
            "CREATE TABLE arasuji_entries (id TEXT PRIMARY KEY, source_ids_json TEXT)"
        )
        conn.execute(
            "CREATE TABLE memopedia_fragment_embeddings (fragment_id TEXT PRIMARY KEY, vector TEXT)"
        )
        conn.execute(
            "CREATE TABLE message_embeddings (message_id TEXT, chunk_index INTEGER, vector TEXT)"
        )
        conn.execute(
            "CREATE TABLE messages (id TEXT PRIMARY KEY, role TEXT, content TEXT, created_at INTEGER)"
        )
        conn.commit()
        return conn

    def _seed(self, conn, *, entry_id, frag_id, frag_vector, message_rows, with_embeddings=True):
        """message_rows: [(message_id, vector_or_None, content, role, created_at), ...]"""
        import json
        conn.execute(
            "INSERT INTO arasuji_entries (id, source_ids_json) VALUES (?, ?)",
            (entry_id, json.dumps([m[0] for m in message_rows])),
        )
        if with_embeddings:
            conn.execute(
                "INSERT INTO memopedia_fragment_embeddings (fragment_id, vector) VALUES (?, ?)",
                (frag_id, json.dumps(frag_vector)),
            )
        for mid, vec, content, role, created_at in message_rows:
            if with_embeddings and vec is not None:
                conn.execute(
                    "INSERT INTO message_embeddings (message_id, chunk_index, vector) VALUES (?, 0, ?)",
                    (mid, json.dumps(vec)),
                )
            conn.execute(
                "INSERT INTO messages (id, role, content, created_at) VALUES (?, ?, ?, ?)",
                (mid, role, content, created_at),
            )
        conn.commit()

    def test_multiple_fragments_all_get_vivid(self):
        # 2つの fragment がともに chronicle_entry_id を持つとき、両方に
        # 「そのときの会話」が付く (先頭は最大2行だが、この設定では各1件のみ用意)。
        conn = self._make_conn()
        self._seed(
            conn, entry_id="chron1", frag_id="f1", frag_vector=[1.0, 0.0],
            message_rows=[("m1", [1.0, 0.0], "アイフィについての第一の会話", "user", 1000)],
        )
        self._seed(
            conn, entry_id="chron2", frag_id="f2", frag_vector=[0.0, 1.0],
            message_rows=[("m2", [0.0, 1.0], "べつの話題についての第二の会話", "user", 2000)],
        )
        hits = [
            _hit("fragment", "f1", embed_score=0.9, title="アイフィ",
                 content="アイフィに関する断片1", chronicle_entry_id="chron1"),
            _hit("fragment", "f2", embed_score=0.88, title="べつの話題",
                 content="べつの話題に関する断片2", chronicle_entry_id="chron2"),
        ]
        with patch("sea.auto_recall._fetch_memopedia_titles", return_value=[]), \
             patch("sai_memory.unified_recall.unified_recall", return_value=hits):
            res = auto_recall.run_auto_recall(
                conn=conn, embedder=object(), messages=_msgs(("user", "アイフィの話")),
                persona_id=self.PERSONA, thread_id=self.THREAD,
            )
        self.assertTrue(res.injected)
        self.assertEqual(res.block.count("そのときの会話"), 2)
        self.assertIn("第一の会話", res.block)
        self.assertIn("第二の会話", res.block)
        conn.close()

    def test_non_top_item_limited_to_one_line(self):
        # 先頭以外の項目は候補が2件あっても1行しか出さない (鮮明度の勾配)。
        conn = self._make_conn()
        self._seed(
            conn, entry_id="chron1", frag_id="f1", frag_vector=[1.0, 0.0],
            message_rows=[("m1", [1.0, 0.0], "先頭項目の会話A", "user", 1000)],
        )
        self._seed(
            conn, entry_id="chron2", frag_id="f2", frag_vector=[1.0, 0.0],
            message_rows=[
                ("m2a", [1.0, 0.0], "非先頭項目の会話X", "user", 2000),
                ("m2b", [0.9, 0.1], "非先頭項目の会話Y", "assistant", 2100),
            ],
        )
        hits = [
            _hit("fragment", "f1", embed_score=0.95, title="先頭項目",
                 content="先頭の断片", chronicle_entry_id="chron1"),
            _hit("fragment", "f2", embed_score=0.9, title="非先頭項目",
                 content="非先頭の断片", chronicle_entry_id="chron2"),
        ]
        with patch("sea.auto_recall._fetch_memopedia_titles", return_value=[]), \
             patch("sai_memory.unified_recall.unified_recall", return_value=hits):
            res = auto_recall.run_auto_recall(
                conn=conn, embedder=object(), messages=_msgs(("user", "話題")),
                persona_id=self.PERSONA, thread_id=self.THREAD,
            )
        self.assertTrue(res.injected)
        # f2 (非先頭) は候補2件あっても1行のみ採用される。
        self.assertEqual(res.block.count("非先頭項目の会話"), 1)
        conn.close()

    def test_vivid_budget_shared_across_items_skips_when_exhausted(self):
        # VIVID_CHARS を全項目合計の予算にする。小さい予算では先頭項目で使い切り、
        # 後続項目の vivid はスキップされる (フッター等の注入自体は継続)。
        import os
        os.environ["SAIVERSE_AUTO_RECALL_VIVID_CHARS"] = "30"
        conn = self._make_conn()
        self._seed(
            conn, entry_id="chron1", frag_id="f1", frag_vector=[1.0, 0.0],
            message_rows=[("m1", [1.0, 0.0], "先頭項目のかなり長い本文" * 5, "user", 1000)],
        )
        self._seed(
            conn, entry_id="chron2", frag_id="f2", frag_vector=[1.0, 0.0],
            message_rows=[("m2", [1.0, 0.0], "後続項目の本文", "user", 2000)],
        )
        hits = [
            _hit("fragment", "f1", embed_score=0.95, title="先頭項目", chronicle_entry_id="chron1"),
            _hit("fragment", "f2", embed_score=0.9, title="後続項目", chronicle_entry_id="chron2"),
        ]
        with patch("sea.auto_recall._fetch_memopedia_titles", return_value=[]), \
             patch("sai_memory.unified_recall.unified_recall", return_value=hits):
            res = auto_recall.run_auto_recall(
                conn=conn, embedder=object(), messages=_msgs(("user", "話題")),
                persona_id=self.PERSONA, thread_id=self.THREAD,
            )
        self.assertTrue(res.injected)
        # 予算を先頭項目でほぼ使い切るため、後続項目の vivid サブ行は付かない。
        self.assertNotIn("後続項目の本文", res.block)
        conn.close()

    def test_title_window_trims_around_occurrence(self):
        # チャンク内にページタイトルが含まれる場合、その周辺 ±60字に詰められる
        # (まはーの案: 「アイフィ」の名前が出てる周辺だけ取る)。
        conn = self._make_conn()
        far_prefix = "無関係な前置きの文章です。" * 20
        far_suffix = "無関係な後書きの文章です。" * 20
        msg_content = far_prefix + "アイフィについての大事な一言。" + far_suffix
        self._seed(
            conn, entry_id="chron1", frag_id="f1", frag_vector=[1.0, 0.0],
            message_rows=[("m1", [1.0, 0.0], msg_content, "user", 1000)],
        )
        hits = [_hit("fragment", "f1", embed_score=0.9, title="アイフィ",
                     content="アイフィの断片", chronicle_entry_id="chron1")]
        with patch("sea.auto_recall._fetch_memopedia_titles", return_value=[]), \
             patch("sai_memory.unified_recall.unified_recall", return_value=hits):
            res = auto_recall.run_auto_recall(
                conn=conn, embedder=object(), messages=_msgs(("user", "アイフィ")),
                persona_id=self.PERSONA, thread_id=self.THREAD,
            )
        self.assertTrue(res.injected)
        self.assertIn("アイフィについての大事な一言", res.block)
        # 前置き・後書きの全文は含まれない (窓で切られている)。
        self.assertNotIn(far_prefix, res.block)
        self.assertNotIn(far_suffix, res.block)
        conn.close()

    def test_fallback_plain_title_search_when_no_embeddings(self):
        # Fragment 埋め込みも message 埋め込みも無い場合、埋め込み経路はスキップされ、
        # 元メッセージ群からページタイトルを素朴に文字列検索するフォールバックが働く。
        conn = self._make_conn()
        self._seed(
            conn, entry_id="chron1", frag_id="f1", frag_vector=[1.0, 0.0],
            message_rows=[("m1", None, "アイフィという名前が含まれる本文", "user", 1000)],
            with_embeddings=False,
        )
        hits = [_hit("fragment", "f1", embed_score=0.9, title="アイフィ",
                     content="アイフィの断片", chronicle_entry_id="chron1")]
        with patch("sea.auto_recall._fetch_memopedia_titles", return_value=[]), \
             patch("sai_memory.unified_recall.unified_recall", return_value=hits):
            res = auto_recall.run_auto_recall(
                conn=conn, embedder=object(), messages=_msgs(("user", "アイフィ")),
                persona_id=self.PERSONA, thread_id=self.THREAD,
            )
        self.assertTrue(res.injected)
        self.assertIn("そのときの会話", res.block)
        self.assertIn("アイフィという名前が含まれる本文", res.block)
        conn.close()

    def test_fallback_skipped_when_title_not_found_anywhere(self):
        # 埋め込みも無く、元メッセージ群にもタイトルが1つも含まれない場合は
        # その項目の vivid をスキップする (LLM は呼ばない)。
        conn = self._make_conn()
        self._seed(
            conn, entry_id="chron1", frag_id="f1", frag_vector=[1.0, 0.0],
            message_rows=[("m1", None, "全く関係のない内容の本文です", "user", 1000)],
            with_embeddings=False,
        )
        hits = [_hit("fragment", "f1", embed_score=0.9, title="アイフィ",
                     content="アイフィの断片", chronicle_entry_id="chron1")]
        with patch("sea.auto_recall._fetch_memopedia_titles", return_value=[]), \
             patch("sai_memory.unified_recall.unified_recall", return_value=hits):
            res = auto_recall.run_auto_recall(
                conn=conn, embedder=object(), messages=_msgs(("user", "アイフィ")),
                persona_id=self.PERSONA, thread_id=self.THREAD,
            )
        self.assertTrue(res.injected)  # fragment 自体は注入される
        self.assertNotIn("そのときの会話", res.block)  # vivid だけスキップ
        conn.close()


class TestQueryDefaults(AutoRecallBase):

    def test_default_query_message_count_is_one(self):
        self.assertEqual(auto_recall.get_query_message_count(), 1)

    def test_build_query_uses_last_message_only(self):
        messages = _msgs(("user", "1つ目"), ("assistant", "2つ目"), ("user", "3つ目"))
        self.assertEqual(auto_recall.build_query(messages), "3つ目")


class TestThreadLedgerIsolation(AutoRecallBase):
    """sticky 台帳の thread 分離 (2026-07-12 監査 P1)。

    固定する不変条件:
    - thread A で採用された粘着記憶は、thread B へ切り替えた後の (検索 hit ゼロの)
      ターンに注入されない (thread 跨ぎの持ち込み禁止)。
    - B 滞在中に A の台帳は老化せず、A へ戻れば残存分が自然に復元される
      (キー分離による継続 — 2026-07-12 裁定で仕様として固定)。
    - reset_ledger(persona_id) は全 thread 分を破棄する。
    """

    THREAD_A = "test_persona:building_a"
    THREAD_B = "test_persona:building_b"

    def _run_in(self, thread_id, hits, messages):
        with patch("sai_memory.unified_recall.unified_recall", return_value=hits), \
             patch("sea.auto_recall._fetch_memopedia_titles", return_value=[]):
            return auto_recall.run_auto_recall(
                conn=object(), embedder=object(), messages=messages,
                persona_id=self.PERSONA, thread_id=thread_id,
            )

    def test_ledger_does_not_leak_into_other_thread(self):
        good = [_hit("fragment", "f1", embed_score=0.9, title="記憶A", content="Aで浮かんだ内容")]
        r_a = self._run_in(self.THREAD_A, good, _msgs(("user", "Aの話")))
        self.assertTrue(r_a.injected)
        # thread B: 検索 hit ゼロ → A の粘着分が持ち込まれず、注入なし。
        r_b = self._run_in(self.THREAD_B, [], _msgs(("user", "Bでの新しい会話")))
        self.assertFalse(r_b.injected)
        self.assertIsNone(r_b.block)

    def test_returning_to_thread_restores_its_ledger(self):
        import os
        os.environ["SAIVERSE_AUTO_RECALL_STICKY_TURNS"] = "2"
        good = [_hit("fragment", "f1", embed_score=0.9, title="記憶A", content="Aで浮かんだ内容")]
        self._run_in(self.THREAD_A, good, _msgs(("user", "Aの話")))
        # B で sticky_turns を超える回数のターンを回しても、A の台帳は老化しない。
        for i in range(5):
            self._run_in(self.THREAD_B, [], _msgs(("user", f"Bの話 {i}")))
        # A へ戻ると台帳が生きている (stale_turns=1 で粘着継続)。
        r_back = self._run_in(self.THREAD_A, [], _msgs(("user", "Aに戻ってきた")))
        self.assertTrue(r_back.injected)
        self.assertIn("Aで浮かんだ内容", r_back.block)

    def test_reset_ledger_discards_all_threads(self):
        good_a = [_hit("fragment", "f1", embed_score=0.9, title="記憶A")]
        good_b = [_hit("fragment", "f2", embed_score=0.9, title="記憶B")]
        self._run_in(self.THREAD_A, good_a, _msgs(("user", "A")))
        self._run_in(self.THREAD_B, good_b, _msgs(("user", "B")))
        self.assertTrue(any(k[0] == self.PERSONA for k in auto_recall._LEDGERS))
        auto_recall.reset_ledger(self.PERSONA)
        self.assertFalse(any(k[0] == self.PERSONA for k in auto_recall._LEDGERS))

    def test_reset_ledger_leaves_other_personas_untouched(self):
        other_key = ("other_persona", "other_persona:__persona__")
        auto_recall._LEDGERS[other_key] = auto_recall._LineLedger()
        try:
            good = [_hit("fragment", "f1", embed_score=0.9, title="記憶A")]
            self._run_in(self.THREAD_A, good, _msgs(("user", "A")))
            auto_recall.reset_ledger(self.PERSONA)
            self.assertIn(other_key, auto_recall._LEDGERS)
        finally:
            auto_recall._LEDGERS.pop(other_key, None)


class TestPersonaToggleGate(unittest.TestCase):
    """AUTO_RECALL_ENABLED per-persona トグルによるゲート (sea/runtime_context.py)。

    _maybe_inject_auto_recall 冒頭のゲートのみを検証する。run_auto_recall 本体
    (しきい値選別・sticky 台帳等) は TestThresholdSelection 等で別途カバー済み。
    """

    PERSONA = "gate_test_persona"

    def setUp(self):
        auto_recall.reset_ledger(self.PERSONA)

    def tearDown(self):
        auto_recall.reset_ledger(self.PERSONA)

    def _persona(self):
        return SimpleNamespace(
            persona_id=self.PERSONA,
            persona_name="ゲートテスト",
            sai_memory=SimpleNamespace(
                conn=object(), embedder=object(),
                # canonical thread_id の解決 (SAIMemoryAdapter._thread_id(None) 相当)
                _thread_id=lambda building_id=None: f"{self.PERSONA}:__persona__",
            ),
        )

    def test_disabled_skips_injection_without_calling_run_auto_recall(self):
        from sea.runtime_context import _maybe_inject_auto_recall

        runtime = SimpleNamespace(
            manager=None,
            _is_auto_recall_enabled_for_persona=lambda persona: False,
        )
        messages = _msgs(("user", "こんにちは"))
        with patch("sea.auto_recall.run_auto_recall") as mock_run:
            _maybe_inject_auto_recall(
                runtime, self._persona(), messages,
                pulse_type="user",
            )
        mock_run.assert_not_called()
        # 注入されていないので user メッセージのみのまま
        self.assertEqual(len(messages), 1)

    def test_disabled_resets_sticky_ledger(self):
        from sea.runtime_context import _maybe_inject_auto_recall
        from sea.auto_recall import _LineLedger, _LEDGERS

        # 事前に複数 thread の台帳を積んでおく (中身の型は問わないので空 _LineLedger
        # で足りる)。明示リセットは当該 persona の全 thread 分を破棄する仕様。
        _LEDGERS[(self.PERSONA, f"{self.PERSONA}:__persona__")] = _LineLedger()
        _LEDGERS[(self.PERSONA, f"{self.PERSONA}:building_x")] = _LineLedger()

        runtime = SimpleNamespace(
            manager=None,
            _is_auto_recall_enabled_for_persona=lambda persona: False,
        )
        _maybe_inject_auto_recall(
            runtime, self._persona(), _msgs(("user", "こんにちは")),
            pulse_type="user",
        )
        self.assertFalse(any(k[0] == self.PERSONA for k in _LEDGERS))

    def test_enabled_proceeds_to_run_auto_recall(self):
        from sea.runtime_context import _maybe_inject_auto_recall
        from sea.auto_recall import AutoRecallResult

        runtime = SimpleNamespace(
            manager=None,
            _is_auto_recall_enabled_for_persona=lambda persona: True,
        )
        messages = _msgs(("user", "こんにちは"))
        empty_result = AutoRecallResult(
            injected=False, block=None, query="", hit_count=0,
            accepted_count=0, ledger_size=0, char_count=0, plain_text=None,
        )
        with patch(
            "sea.auto_recall.run_auto_recall",
            return_value=empty_result,
        ) as mock_run:
            _maybe_inject_auto_recall(
                runtime, self._persona(), messages,
                pulse_type="user",
            )
        mock_run.assert_called_once()


if __name__ == "__main__":
    unittest.main()
