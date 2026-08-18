"""知覚バッファ ストレージ層の単体テスト (sai_memory/perception_buffer.py)。

adapter を介さず sqlite conn 直で push / list / reduce / format / delete を検証する。
"""
from __future__ import annotations

import sqlite3
import unittest

from sai_memory.perception_buffer import (
    create_consumption_batch,
    delete_perceptions,
    format_perception_message,
    init_perception_buffer_table,
    list_consumed_since,
    list_pending,
    list_unannexed_batches,
    mark_batches_annexed,
    push_perception,
    reduce_perceptions,
)


class PerceptionBufferTest(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        init_perception_buffer_table(self.conn)
        self.addCleanup(self.conn.close)

    def test_init_is_idempotent(self):
        # 二度呼んでも壊れない (冪等)。
        init_perception_buffer_table(self.conn)
        self.assertEqual(list_pending(self.conn), [])

    def test_push_and_list_order(self):
        push_perception(self.conn, "core_memory_correction", "A")
        push_perception(self.conn, "core_memory_correction", "B")
        items = list_pending(self.conn)
        self.assertEqual([it.content for it in items], ["A", "B"])
        self.assertEqual(items[0].kind, "core_memory_correction")
        self.assertEqual(items[0].salient, 0)
        self.assertIsNone(items[0].reduce_key)

    def test_reduce_keeps_latest_per_key(self):
        push_perception(self.conn, "core_memory_correction", "edit1", reduce_key="c:5")
        push_perception(self.conn, "core_memory_correction", "other", reduce_key="c:9")
        push_perception(self.conn, "core_memory_correction", "edit2", reduce_key="c:5")
        reduced = reduce_perceptions(list_pending(self.conn))
        contents = [it.content for it in reduced]
        # c:5 は最新 (edit2) だけ残る。c:9 はそのまま。順序は発生順を保つ。
        self.assertEqual(contents, ["other", "edit2"])

    def test_reduce_keeps_all_when_no_key(self):
        push_perception(self.conn, "k", "a")
        push_perception(self.conn, "k", "b")
        reduced = reduce_perceptions(list_pending(self.conn))
        self.assertEqual([it.content for it in reduced], ["a", "b"])

    def test_format_groups_by_kind_with_headers(self):
        push_perception(self.conn, "core_memory_correction", "訂正1")
        push_perception(self.conn, "unknown_kind", "なにか")
        text = format_perception_message(list_pending(self.conn))
        self.assertIn("[コア記憶の更新通知]", text)
        self.assertIn("訂正1", text)
        # 未知の型は汎用見出しにフォールバック。
        self.assertIn("[システム通知]", text)
        self.assertIn("なにか", text)

    def test_world_state_uses_system_header(self):
        push_perception(self.conn, "world_state", "アイフィが入室した")
        text = format_perception_message(list_pending(self.conn))
        self.assertIn("[システム通知]", text)
        self.assertIn("アイフィが入室した", text)

    def test_persona_recall_has_no_header(self):
        push_perception(self.conn, "persona_recall", "過去の会話: …")
        text = format_perception_message(list_pending(self.conn))
        # persona_recall は本文が自己完結なので見出しを付けない。
        self.assertNotIn("[システム通知]", text)
        self.assertNotIn("[コア記憶の更新通知]", text)
        self.assertEqual(text, "過去の会話: …")

    def test_multiple_kinds_in_one_message(self):
        # 同一 Pulse で消費される複数型の知覚は 1 メッセージにまとまる (C3)。
        push_perception(self.conn, "core_memory_correction", "訂正あり")
        push_perception(self.conn, "world_state", "誰かが退室した")
        push_perception(self.conn, "persona_recall", "そういえば前に…")
        text = format_perception_message(list_pending(self.conn))
        self.assertIn("[コア記憶の更新通知]", text)
        self.assertIn("訂正あり", text)
        self.assertIn("[システム通知]", text)
        self.assertIn("誰かが退室した", text)
        self.assertIn("そういえば前に…", text)  # persona_recall 本文

    def test_format_preserves_chronological_order(self):
        # 移動→surroundings→次の移動、の時系列が壊れないこと (kind グルーピング廃止)。
        push_perception(self.conn, "world_state", "現在地が A から B に変わりました")
        push_perception(self.conn, "surroundings", "「B」の様子…")
        push_perception(self.conn, "world_state", "現在地が B から A に変わりました")
        text = format_perception_message(list_pending(self.conn))
        # surroundings は 2 つの world_state の「間」に来る (末尾に固まらない)。
        pos_b_arrive = text.index("A から B")
        pos_surround = text.index("「B」の様子")
        pos_a_return = text.index("B から A")
        self.assertLess(pos_b_arrive, pos_surround)
        self.assertLess(pos_surround, pos_a_return)
        # world_state が連続してないので [システム通知] 見出しは 2 回出る。
        self.assertEqual(text.count("[システム通知]"), 2)

    def test_delete_removes_only_given_ids(self):
        i1 = push_perception(self.conn, "k", "a")
        push_perception(self.conn, "k", "b")
        delete_perceptions(self.conn, [i1])
        remaining = list_pending(self.conn)
        self.assertEqual([it.content for it in remaining], ["b"])

    def test_delete_empty_is_noop(self):
        push_perception(self.conn, "k", "a")
        delete_perceptions(self.conn, [])
        self.assertEqual(len(list_pending(self.conn)), 1)

    def test_media_roundtrip(self):
        media = [{"path": "/img/room.png", "mime_type": "image/png", "role": "image"}]
        push_perception(self.conn, "surroundings", "移動先の様子…", media=media)
        item = list_pending(self.conn)[0]
        self.assertEqual(item.media_list(), media)

    def test_media_list_empty_when_none(self):
        push_perception(self.conn, "world_state", "何か")
        item = list_pending(self.conn)[0]
        self.assertEqual(item.media_list(), [])

    def test_salient_and_metadata_roundtrip(self):
        push_perception(
            self.conn, "k", "x", salient=True, metadata='{"foo": 1}', reduce_key="rk",
        )
        item = list_pending(self.conn)[0]
        self.assertEqual(item.salient, 1)
        self.assertEqual(item.metadata, '{"foo": 1}')
        self.assertEqual(item.reduce_key, "rk")

    # --- 消費バッチ (W14 知覚レンダリング, perception_buffer.md §10.2) ---

    def test_new_columns_default_to_null(self):
        push_perception(self.conn, "k", "x")
        item = list_pending(self.conn)[0]
        self.assertIsNone(item.consumed_at)
        self.assertIsNone(item.consumed_batch_id)

    def test_migration_adds_columns_to_legacy_table(self):
        # media 列時代の DDL (消費記帳の列なし) からのマイグレーション。
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        conn.execute(
            "CREATE TABLE perception_buffer ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, "
            "content TEXT NOT NULL, reduce_key TEXT, "
            "salient INTEGER NOT NULL DEFAULT 0, media TEXT, metadata TEXT, "
            "created_at INTEGER NOT NULL)"
        )
        conn.execute(
            "INSERT INTO perception_buffer (kind, content, created_at) "
            "VALUES ('k', 'legacy', 1)"
        )
        init_perception_buffer_table(conn)
        items = list_pending(conn)
        self.assertEqual([it.content for it in items], ["legacy"])
        self.assertIsNone(items[0].consumed_at)
        self.assertEqual(list_unannexed_batches(conn), [])

    def test_create_batch_marks_items_and_keeps_rows(self):
        i1 = push_perception(self.conn, "k", "a")
        i2 = push_perception(self.conn, "k", "b")
        media = [{"path": "/img/a.png", "mime_type": "image/png", "role": "image"}]
        batch_id = create_consumption_batch(
            self.conn, [i1], consumed_at=1000, rendered_text="[通知]\na",
            pulse_id="p1", episode_id="episode:1", media=media,
        )
        # pending からは消えるが、行は台帳に残る (削除しない = 証跡)。
        self.assertEqual([it.id for it in list_pending(self.conn)], [i2])
        consumed = list_consumed_since(self.conn, 0)
        self.assertEqual([it.id for it in consumed], [i1])
        self.assertEqual(consumed[0].consumed_at, 1000)
        self.assertEqual(consumed[0].consumed_batch_id, batch_id)
        # バッチ行に確定文面・pulse/episode・media が永続化される。
        batches = list_unannexed_batches(self.conn)
        self.assertEqual([b.id for b in batches], [batch_id])
        self.assertEqual(batches[0].rendered_text, "[通知]\na")
        self.assertEqual(batches[0].pulse_id, "p1")
        self.assertEqual(batches[0].episode_id, "episode:1")
        self.assertEqual(batches[0].media_list(), media)
        self.assertIsNone(batches[0].annexed_entry_id)

    def test_create_batch_refuses_reconsumption(self):
        # 消費済み id を含む呼び出しは rollback + ValueError (C2: 再消費禁止)。
        i1 = push_perception(self.conn, "k", "a")
        i2 = push_perception(self.conn, "k", "b")
        create_consumption_batch(
            self.conn, [i1], consumed_at=1000, rendered_text="a",
        )
        with self.assertRaises(ValueError):
            create_consumption_batch(
                self.conn, [i1, i2], consumed_at=2000, rendered_text="x",
            )
        # rollback: i2 は未消費のまま、バッチは最初の 1 件だけ。
        self.assertEqual([it.id for it in list_pending(self.conn)], [i2])
        self.assertEqual(len(list_unannexed_batches(self.conn)), 1)
        self.assertEqual(list_consumed_since(self.conn, 0)[0].consumed_at, 1000)

    def test_create_batch_empty_raises(self):
        with self.assertRaises(ValueError):
            create_consumption_batch(
                self.conn, [], consumed_at=1000, rendered_text="x",
            )

    def test_count_pending_excludes_consumed(self):
        from sai_memory.perception_buffer import count_pending
        i1 = push_perception(self.conn, "feed", "a")
        push_perception(self.conn, "feed", "b")
        self.assertEqual(count_pending(self.conn, "feed"), 2)
        create_consumption_batch(
            self.conn, [i1], consumed_at=1000, rendered_text="a",
        )
        self.assertEqual(count_pending(self.conn, "feed"), 1)

    def test_unannexed_batches_order_and_bounds(self):
        i1 = push_perception(self.conn, "k", "a")
        i2 = push_perception(self.conn, "k", "b")
        i3 = push_perception(self.conn, "k", "c")
        b1 = create_consumption_batch(
            self.conn, [i1], consumed_at=100, rendered_text="a",
        )
        b2 = create_consumption_batch(
            self.conn, [i2], consumed_at=200, rendered_text="b",
        )
        b3 = create_consumption_batch(
            self.conn, [i3], consumed_at=300, rendered_text="c",
        )
        self.assertEqual(
            [b.id for b in list_unannexed_batches(self.conn)], [b1, b2, b3],
        )
        # since (以上) / before (未満) の絞り込み。
        self.assertEqual(
            [b.id for b in list_unannexed_batches(self.conn, since=200)],
            [b2, b3],
        )
        self.assertEqual(
            [b.id for b in list_unannexed_batches(self.conn, since=100, before=300)],
            [b1, b2],
        )

    def test_mark_batches_annexed_removes_from_unannexed(self):
        i1 = push_perception(self.conn, "k", "a")
        b1 = create_consumption_batch(
            self.conn, [i1], consumed_at=100, rendered_text="a",
        )
        touched = mark_batches_annexed(self.conn, [b1], "entry-1")
        self.assertEqual(touched, 1)
        self.conn.commit()
        self.assertEqual(list_unannexed_batches(self.conn), [])
        # 既付記への再印字は効かない (別エントリで上書きしない)。
        self.assertEqual(mark_batches_annexed(self.conn, [b1], "entry-2"), 0)

    def test_unmark_batches_annexed_returns_batches_to_presentation(self):
        # entry 削除経路の返却口: 印を戻すと未付記一覧 (= 提示対象) に再登場する。
        from sai_memory.perception_buffer import unmark_batches_annexed
        i1 = push_perception(self.conn, "k", "a")
        i2 = push_perception(self.conn, "k", "b")
        b1 = create_consumption_batch(
            self.conn, [i1], consumed_at=100, rendered_text="a",
        )
        b2 = create_consumption_batch(
            self.conn, [i2], consumed_at=200, rendered_text="b",
        )
        mark_batches_annexed(self.conn, [b1], "entry-1")
        mark_batches_annexed(self.conn, [b2], "entry-2")
        self.conn.commit()
        self.assertEqual(list_unannexed_batches(self.conn), [])
        # entry-1 だけ消えた → その印だけ戻る。
        self.assertEqual(unmark_batches_annexed(self.conn, ["entry-1"]), 1)
        self.conn.commit()
        self.assertEqual(
            [b.id for b in list_unannexed_batches(self.conn)], [b1],
        )
        # 空 / 存在しない entry は no-op。
        self.assertEqual(unmark_batches_annexed(self.conn, []), 0)
        self.assertEqual(unmark_batches_annexed(self.conn, ["ghost"]), 0)

    def _legacy_schema_conn(self):
        """旧世代 DDL (media 列まで・消費記帳なし) + messages の器を作る。"""
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        conn.execute(
            "CREATE TABLE perception_buffer ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, "
            "content TEXT NOT NULL, reduce_key TEXT, "
            "salient INTEGER NOT NULL DEFAULT 0, media TEXT, metadata TEXT, "
            "created_at INTEGER NOT NULL)"
        )
        conn.execute(
            "CREATE TABLE messages (id TEXT PRIMARY KEY, resource_id TEXT, "
            "created_at INTEGER, metadata TEXT)"
        )
        return conn

    def _insert_legacy_pending(self, conn, content, created_at=None):
        """旧 DDL の perception_buffer へ pending を直接積む (旧世代の行を模す)。

        新しい push_perception は ledger_outbox_id 列を含む INSERT を発行する
        ため、列の無い旧 DDL には使えない — 生 INSERT で当時の行を作る。
        """
        import time as _time
        cur = conn.execute(
            "INSERT INTO perception_buffer (kind, content, created_at) "
            "VALUES ('world_state', ?, ?)",
            (content, int(created_at or _time.time())),
        )
        conn.commit()
        return int(cur.lastrowid)

    def _insert_flush_marker(self, conn, mid, ids, created_at, resource_id="p1"):
        import json as _json
        conn.execute(
            "INSERT INTO messages VALUES (?, ?, ?, ?)",
            (mid, resource_id, created_at, _json.dumps({
                "tags": ["internal", "event_message", "perception"],
                "perception_ids": ids,
            })),
        )
        conn.commit()

    def test_upgrade_reconciles_interrupted_two_phase_flush(self):
        # 旧二段 flush (event_message 書き込み → 削除) の中断状態を持つ DB の
        # アップグレード: messages の perception_ids に現れる pending は一度きり
        # の移行で削除され (legacy 行が既に提示している = バッチ化すると二重
        # 提示)、印の無い pending は残る (Codex 第七巡 #4)。
        import json as _json
        import time as _time
        conn = self._legacy_schema_conn()
        now = int(_time.time())
        written_id = self._insert_legacy_pending(conn, "書き込み済みの中断分")
        kept_id = self._insert_legacy_pending(conn, "未書き込みの新着分")
        self._insert_flush_marker(conn, "m1", [written_id], now)

        init_perception_buffer_table(conn, resource_id="p1")
        remaining = list_pending(conn)
        self.assertEqual([it.id for it in remaining], [kept_id])

        # 一度きり: 列が既にある DB では走らない (perception_ids に載る id を
        # 新たに積んでも消されない)。
        conn.execute(
            "UPDATE messages SET metadata = ? WHERE id = 'm1'",
            (_json.dumps({"perception_ids": [kept_id]}),),
        )
        conn.commit()
        init_perception_buffer_table(conn, resource_id="p1")
        self.assertEqual([it.id for it in list_pending(conn)], [kept_id])

    def test_upgrade_reconcile_is_window_bounded(self):
        # 照合は旧 C6 と同じ絞り (Codex 第八巡 #4): 最古 pending − 3600 秒より
        # 古い行と、別 resource の行は読まない — 窓外のマーカーは pending を
        # 消さない。pending が空なら messages を一行も読まない。
        import time as _time
        conn = self._legacy_schema_conn()
        now = int(_time.time())
        pending_id = self._insert_legacy_pending(conn, "窓の中の pending")
        # 窓外 (2 時間前) のマーカーと、別 resource の窓内マーカー — どちらも
        # 照合に使われてはいけない。
        self._insert_flush_marker(conn, "m-old", [pending_id], now - 7200)
        self._insert_flush_marker(
            conn, "m-other", [pending_id], now, resource_id="other",
        )
        init_perception_buffer_table(conn, resource_id="p1")
        self.assertEqual([it.id for it in list_pending(conn)], [pending_id])

    def test_upgrade_reconcile_skips_messages_when_no_pending(self):
        # pending が空なら照合クエリ自体が走らない — messages を落として
        # (読めば OperationalError で内部握りになるが、読まない設計の検算として
        # 正常終了を確認する)。
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        conn.execute(
            "CREATE TABLE perception_buffer ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, "
            "content TEXT NOT NULL, reduce_key TEXT, "
            "salient INTEGER NOT NULL DEFAULT 0, media TEXT, metadata TEXT, "
            "created_at INTEGER NOT NULL)"
        )
        init_perception_buffer_table(conn, resource_id="p1")  # 例外なし
        self.assertEqual(list_pending(conn), [])

    def test_ledger_outbox_unique_is_atomic_across_connections(self):
        # 台帳配送の冪等は専用列 + UNIQUE で DB 側が強制する — 別 connection の
        # 同時配送でも 1 行になる (Codex 第八巡 #1。check-then-act の照合は
        # この並びに破れていた)。
        import os
        import tempfile
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        db_path = os.path.join(tmp.name, "perception.db")
        conn_a = sqlite3.connect(db_path)
        conn_b = sqlite3.connect(db_path)
        self.addCleanup(conn_a.close)
        self.addCleanup(conn_b.close)
        init_perception_buffer_table(conn_a)

        first = push_perception(
            conn_a, "world_state", "配送1", ledger_outbox_id="7",
        )
        self.assertIsNotNone(first)
        # 別 connection からの再配送は DB の UNIQUE が原子的に弾く。
        second = push_perception(
            conn_b, "world_state", "配送1 (再配送)", ledger_outbox_id="7",
        )
        self.assertIsNone(second)
        rows = conn_a.execute(
            "SELECT COUNT(*) FROM perception_buffer WHERE ledger_outbox_id = '7'"
        ).fetchone()
        self.assertEqual(rows[0], 1)
        # 通常 push (NULL) は UNIQUE の対象外 — 何件でも積める。
        self.assertIsNotNone(push_perception(conn_b, "world_state", "通常1"))
        self.assertIsNotNone(push_perception(conn_b, "world_state", "通常2"))

    def test_mark_batches_annexed_rolls_back_with_tx(self):
        # 付記印は digest 確定と同一 tx — rollback で印も戻る (§10.4)。
        i1 = push_perception(self.conn, "k", "a")
        b1 = create_consumption_batch(
            self.conn, [i1], consumed_at=100, rendered_text="a",
        )
        self.conn.execute("BEGIN")
        self.assertEqual(mark_batches_annexed(self.conn, [b1], "entry-1"), 1)
        self.conn.rollback()
        self.assertEqual(
            [b.id for b in list_unannexed_batches(self.conn)], [b1],
        )


if __name__ == "__main__":
    unittest.main()
