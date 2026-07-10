"""コア記憶ストレージ (sai_memory/core_memory.py) の CRUD テスト。"""
import json
import sqlite3
import unittest

from sai_memory.core_memory import (
    add_core_memory,
    confirm_core_memory,
    count_unconfirmed_core_memories,
    get_core_memory,
    init_core_memory_table,
    list_core_memories,
    list_deleted_core_memories,
    remove_core_memory,
    restore_core_memory,
    total_core_memory_chars,
    update_core_memory,
)


class CoreMemoryStorageTest(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        init_core_memory_table(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_init_creates_root_core_trunk_page(self):
        # P3a (2026-07-11): 物理格納が memopedia_pages に統合された。旧
        # core_memories テーブルはもう作られず、代わりに trunk root_core
        # ページが冪等に用意される (書き換え理由: このテストは旧テーブルの
        # 列を直接 PRAGMA で検査しており、物理格納に依存していたため)。
        row = self.conn.execute(
            "SELECT category, is_trunk, title FROM memopedia_pages WHERE id = 'root_core'"
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], "core")
        self.assertEqual(row[1], 1)
        self.assertEqual(row[2], "コア記憶")
        # 新規 DB では旧テーブルは作られない。
        legacy = self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='core_memories'"
        ).fetchone()
        self.assertIsNone(legacy)

    def test_init_idempotent(self):
        init_core_memory_table(self.conn)  # 2 回目でも壊れない
        self.assertEqual(list_core_memories(self.conn), [])

    def test_add_returns_id_and_lists_ordered(self):
        id1 = add_core_memory(self.conn, "まはーの誕生日は1月14日")
        id2 = add_core_memory(self.conn, "私は「エア」という名前")
        self.assertEqual(id1, 1)
        self.assertEqual(id2, 2)
        items = list_core_memories(self.conn)
        self.assertEqual([i.id for i in items], [1, 2])
        self.assertEqual(items[0].content, "まはーの誕生日は1月14日")
        # 既定 kind は 'note'、metadata は None。
        self.assertEqual(items[0].kind, "note")
        self.assertIsNone(items[0].metadata)
        self.assertEqual(items[0].ref, "c:1")

    def test_update_existing(self):
        mid = add_core_memory(self.conn, "旧い内容")
        ok = update_core_memory(self.conn, mid, "新しい内容")
        self.assertTrue(ok)
        items = list_core_memories(self.conn)
        self.assertEqual(items[0].content, "新しい内容")

    def test_update_missing_returns_false(self):
        self.assertFalse(update_core_memory(self.conn, 999, "x"))

    def test_remove_existing(self):
        mid = add_core_memory(self.conn, "消す対象")
        self.assertTrue(remove_core_memory(self.conn, mid))
        self.assertEqual(list_core_memories(self.conn), [])

    def test_remove_missing_returns_false(self):
        self.assertFalse(remove_core_memory(self.conn, 999))

    def test_total_chars(self):
        self.assertEqual(total_core_memory_chars(self.conn), 0)
        add_core_memory(self.conn, "12345")   # 5 字
        add_core_memory(self.conn, "あいう")  # 3 字
        self.assertEqual(total_core_memory_chars(self.conn), 8)

    # -- confirmed (未確認フラグ) --------------------------------------

    def test_add_defaults_to_confirmed(self):
        add_core_memory(self.conn, "手動追加は確認済み")
        self.assertEqual(count_unconfirmed_core_memories(self.conn), 0)
        self.assertEqual(list_core_memories(self.conn)[0].confirmed, 1)

    def test_add_unconfirmed_counts_and_confirm_clears(self):
        mid = add_core_memory(self.conn, "自動採取", confirmed=0)
        self.assertEqual(count_unconfirmed_core_memories(self.conn), 1)
        self.assertEqual(list_core_memories(self.conn)[0].confirmed, 0)
        self.assertTrue(confirm_core_memory(self.conn, mid))
        self.assertEqual(count_unconfirmed_core_memories(self.conn), 0)

    def test_update_can_reset_confirmed(self):
        mid = add_core_memory(self.conn, "確認済み")  # confirmed=1
        update_core_memory(self.conn, mid, "書き換え", confirmed=0)
        self.assertEqual(list_core_memories(self.conn)[0].confirmed, 0)

    # -- soft-delete (ごみ箱) + 復元 -----------------------------------

    def test_remove_is_soft_delete_and_listed_in_trash(self):
        mid = add_core_memory(self.conn, "削除対象")
        self.assertTrue(remove_core_memory(self.conn, mid))
        # 生存一覧からは消えるが、ごみ箱に残り物理削除されていない。
        self.assertEqual(list_core_memories(self.conn), [])
        trash = list_deleted_core_memories(self.conn)
        self.assertEqual([i.id for i in trash], [mid])
        self.assertIsNotNone(trash[0].deleted_at)

    def test_restore_from_trash(self):
        mid = add_core_memory(self.conn, "復元対象")
        remove_core_memory(self.conn, mid)
        self.assertTrue(restore_core_memory(self.conn, mid))
        self.assertEqual([i.id for i in list_core_memories(self.conn)], [mid])
        self.assertEqual(list_deleted_core_memories(self.conn), [])

    def test_double_remove_returns_false(self):
        mid = add_core_memory(self.conn, "x")
        self.assertTrue(remove_core_memory(self.conn, mid))
        self.assertFalse(remove_core_memory(self.conn, mid))  # 二重削除しない

    def test_total_chars_excludes_deleted(self):
        add_core_memory(self.conn, "12345")   # 5 字
        mid = add_core_memory(self.conn, "あいう")  # 3 字
        remove_core_memory(self.conn, mid)
        self.assertEqual(total_core_memory_chars(self.conn), 5)


class CoreMemoryLegacyMigrationTest(unittest.TestCase):
    """旧 core_memories テーブル → memopedia_pages への一回きり移行 (P3a)。"""

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        # init_core_memory_table を呼ぶ前に、旧スキーマのテーブルを直接作って
        # データを仕込む (移行前の実 DB を模す)。
        self.conn.execute(
            """
            CREATE TABLE core_memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                content TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                kind TEXT NOT NULL DEFAULT 'note',
                metadata TEXT,
                confirmed INTEGER NOT NULL DEFAULT 1,
                deleted_at INTEGER
            )
            """
        )
        scene_meta = json.dumps(
            {"anchor_id": "m1", "message_ids": ["m1", "m2"], "date_range": ["2025-01-01", "2025-01-01"]}
        )
        self.conn.executemany(
            "INSERT INTO core_memories "
            "(id, content, created_at, updated_at, kind, metadata, confirmed, deleted_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (1, "普通のメモ", 1000, 1000, "note", None, 1, None),
                (2, "user「A」\nエア「B」", 2000, 2000, "scene", scene_meta, 1, None),
                (3, "自動採取メモ", 3000, 3000, "note", json.dumps({"source": "gold_panning"}), 0, None),
                (4, "削除済みメモ", 4000, 4500, "note", None, 1, 4500),
            ],
        )
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def test_migration_creates_pages_and_drops_legacy_table(self):
        init_core_memory_table(self.conn)

        # 旧テーブルは移行後 DROP される (旧 path を残さない)。
        legacy = self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='core_memories'"
        ).fetchone()
        self.assertIsNone(legacy)

        # c:N 解決・件数・created_at/updated_at が忠実に写っている。
        items = list_core_memories(self.conn)
        self.assertEqual([i.id for i in items], [1, 2, 3])  # 4 は削除済みなので生存一覧に出ない
        self.assertEqual(items[0].content, "普通のメモ")
        self.assertEqual(items[0].created_at, 1000)
        self.assertEqual(items[0].updated_at, 1000)

        # scene の由来参照 (anchor_id/message_ids/date_range) が保全されている。
        scene_item = next(i for i in items if i.id == 2)
        self.assertEqual(scene_item.kind, "scene")
        meta = json.loads(scene_item.metadata)
        self.assertEqual(meta["anchor_id"], "m1")
        self.assertEqual(meta["date_range"], ["2025-01-01", "2025-01-01"])

        # 未確認フラグ (gold_panning 由来) が保全されている。
        auto_item = next(i for i in items if i.id == 3)
        self.assertEqual(auto_item.confirmed, 0)
        self.assertEqual(json.loads(auto_item.metadata)["source"], "gold_panning")

        # ごみ箱状態が保全されている (deleted_at・生存一覧から除外)。
        trash = list_deleted_core_memories(self.conn)
        self.assertEqual([i.id for i in trash], [4])
        self.assertEqual(trash[0].deleted_at, 4500)
        self.assertEqual(trash[0].updated_at, 4500)

        # get_core_memory (c:N) も移行後のページを正しく引ける。
        self.assertEqual(get_core_memory(self.conn, 1).content, "普通のメモ")
        self.assertIsNone(get_core_memory(self.conn, 4))  # 削除済みは既定で見えない
        self.assertIsNotNone(get_core_memory(self.conn, 4, include_deleted=True))

        # 新規追加は移行済みの最大 core_id (4) の続き (5) から採番される。
        new_id = add_core_memory(self.conn, "移行後の新規")
        self.assertEqual(new_id, 5)

    def test_migration_is_idempotent(self):
        init_core_memory_table(self.conn)
        first = {i.id: i.content for i in list_core_memories(self.conn)}
        init_core_memory_table(self.conn)  # 2回目は旧テーブルが無いので no-op
        second = {i.id: i.content for i in list_core_memories(self.conn)}
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
