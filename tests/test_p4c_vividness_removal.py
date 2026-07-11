"""P4-c vividness 除去 + vivid→机移行 の回帰テスト。

検証内容:
1. migrate_vivid_pages_to_desk — 冪等性、対象条件フィルタ
2. get_memory_weave_context — 以前 buried は skip されていたが、
   P4-c 以降は全ページを平等に描画するか
3. memopedia_manage スキーマに set_vividness が無いこと
"""
import sqlite3
import tempfile
import unittest
from pathlib import Path

from sai_memory.memopedia import Memopedia, init_memopedia_tables
from sai_memory.desk import init_desk_tables
from sai_memory.memopedia.vivid_to_desk_migration import migrate_vivid_pages_to_desk


def _make_db() -> tuple[sqlite3.Connection, Memopedia]:
    """テスト用インメモリ DB とMemopedia インスタンスを返す。"""
    conn = sqlite3.connect(":memory:")
    init_memopedia_tables(conn)
    init_desk_tables(conn)
    return conn, Memopedia(conn)


class VividToDeskMigrationTest(unittest.TestCase):
    """migrate_vivid_pages_to_desk の動作検証。"""

    def _get_desk_refs(self, conn: sqlite3.Connection) -> set[str]:
        rows = conn.execute("SELECT ref FROM desk_items").fetchall()
        return {r[0] for r in rows}

    def test_vivid_page_moved_to_desk(self):
        """vivid ページが desk_items に追加される。"""
        conn, mem = _make_db()
        page = mem.create_page(parent_id="root_terms", title="test", summary="s")
        # vividness を直接書き込む (廃止カラムだが migration テストのために操作)
        conn.execute(
            "UPDATE memopedia_pages SET vividness='vivid' WHERE id=?", (page.id,)
        )
        conn.commit()
        # short_id が付いているか確認
        row = conn.execute(
            "SELECT short_id FROM memopedia_pages WHERE id=?", (page.id,)
        ).fetchone()
        self.assertIsNotNone(row[0], "short_id が付いていないと ref が組めない")

        count = migrate_vivid_pages_to_desk(conn)
        self.assertEqual(count, 1)
        refs = self._get_desk_refs(conn)
        self.assertIn(f"m:{row[0]}", refs)

    def test_idempotent(self):
        """2 回実行しても 2 回目は 0 件追加 (冪等)。"""
        conn, mem = _make_db()
        page = mem.create_page(parent_id="root_terms", title="test", summary="s")
        conn.execute(
            "UPDATE memopedia_pages SET vividness='vivid' WHERE id=?", (page.id,)
        )
        conn.commit()

        first = migrate_vivid_pages_to_desk(conn)
        second = migrate_vivid_pages_to_desk(conn)
        self.assertEqual(first, 1)
        self.assertEqual(second, 0)

    def test_closed_page_not_reopened_by_migration(self):
        """移行後に本人が机から閉じたページを、再実行 (=次の起動) が開き直さない。

        検収で発見した行間バグの回帰テスト: 冪等チェックが「いま机にあるか」
        だけだと、閉じる (本人の行為) を毎起動の移行が上書きしてしまう。
        移行は処理済みページの vivid の印を落とすことで一回きり性を担保する。
        """
        from sai_memory import desk as desk_mod

        conn, mem = _make_db()
        page = mem.create_page(parent_id="root_terms", title="test", summary="s")
        conn.execute(
            "UPDATE memopedia_pages SET vividness='vivid' WHERE id=?", (page.id,)
        )
        conn.commit()
        row = conn.execute(
            "SELECT short_id FROM memopedia_pages WHERE id=?", (page.id,)
        ).fetchone()
        ref = f"m:{row[0]}"

        self.assertEqual(migrate_vivid_pages_to_desk(conn), 1)
        # 本人が机から閉じる
        desk_mod.close_item(conn, ref)
        self.assertNotIn(ref, self._get_desk_refs(conn))
        # 次の起動 (再実行) — 開き直されない
        self.assertEqual(migrate_vivid_pages_to_desk(conn), 0)
        self.assertNotIn(ref, self._get_desk_refs(conn))
        # vivid の印は落ちている (一回きり性の実体)
        v = conn.execute(
            "SELECT vividness FROM memopedia_pages WHERE id=?", (page.id,)
        ).fetchone()[0]
        self.assertEqual(v, "rough")

    def test_non_vivid_not_moved(self):
        """rough ページは対象外。"""
        conn, mem = _make_db()
        mem.create_page(parent_id="root_terms", title="rough page", summary="s")
        conn.commit()

        count = migrate_vivid_pages_to_desk(conn)
        self.assertEqual(count, 0)
        self.assertEqual(len(self._get_desk_refs(conn)), 0)

    def test_deleted_page_not_moved(self):
        """削除済みページは対象外。"""
        conn, mem = _make_db()
        page = mem.create_page(parent_id="root_terms", title="del", summary="s")
        conn.execute(
            "UPDATE memopedia_pages SET vividness='vivid', is_deleted=1 WHERE id=?",
            (page.id,),
        )
        conn.commit()

        count = migrate_vivid_pages_to_desk(conn)
        self.assertEqual(count, 0)

    def test_trunk_page_not_moved(self):
        """trunk ページは対象外。"""
        conn, mem = _make_db()
        page = mem.create_page(parent_id="root_terms", title="trunk", summary="s")
        conn.execute(
            "UPDATE memopedia_pages SET vividness='vivid', is_trunk=1 WHERE id=?",
            (page.id,),
        )
        conn.commit()

        count = migrate_vivid_pages_to_desk(conn)
        self.assertEqual(count, 0)

    def test_already_on_desk_not_double_inserted(self):
        """既に desk にある ref はスキップされる。"""
        conn, mem = _make_db()
        page = mem.create_page(parent_id="root_terms", title="already", summary="s")
        row = conn.execute(
            "SELECT short_id FROM memopedia_pages WHERE id=?", (page.id,)
        ).fetchone()
        ref = f"m:{row[0]}"

        # 先に desk に追加しておく
        now = 1000000
        conn.execute(
            "INSERT INTO desk_items (ref, opened_at, last_touched_at, purpose_ref) VALUES (?, ?, ?, NULL)",
            (ref, now, now),
        )
        conn.execute(
            "UPDATE memopedia_pages SET vividness='vivid' WHERE id=?", (page.id,)
        )
        conn.commit()

        count = migrate_vivid_pages_to_desk(conn)
        self.assertEqual(count, 0)

    def test_no_memopedia_table_returns_zero(self):
        """memopedia_pages テーブルが存在しない場合は 0 を返す。"""
        conn = sqlite3.connect(":memory:")
        init_desk_tables(conn)
        count = migrate_vivid_pages_to_desk(conn)
        self.assertEqual(count, 0)

    def test_no_desk_table_returns_zero(self):
        """desk_items テーブルが存在しない場合は 0 を返す。"""
        conn = sqlite3.connect(":memory:")
        init_memopedia_tables(conn)
        # desk_items は作らない
        count = migrate_vivid_pages_to_desk(conn)
        self.assertEqual(count, 0)


class WeaveContextBuriedRenderTest(unittest.TestCase):
    """P4-c: buried ページが weave context に表示されるか検証。

    以前は buried は skip されていたが、P4-c 以降は全ページを平等に描画する。
    """

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.persona_dir = self._tmpdir.name
        self.db_path = Path(self.persona_dir) / "memory.db"

        conn = sqlite3.connect(str(self.db_path))
        init_memopedia_tables(conn)
        mem = Memopedia(conn)
        # buried ページを作成
        mem.create_page(
            parent_id="root_terms",
            title="BuriedPage",
            summary="buried の概要",
            content="buried 本文",
        )
        # vividness を buried に直接書く (廃止カラムのテスト用操作)
        conn.execute(
            "UPDATE memopedia_pages SET vividness='buried' WHERE title='BuriedPage'"
        )
        # rough ページも作成
        mem.create_page(
            parent_id="root_terms",
            title="RoughPage",
            summary="rough の概要",
            content="rough 本文",
        )
        conn.commit()
        conn.close()

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_buried_page_is_rendered_in_weave(self):
        """P4-c: buried ページが weave に表示されること（以前は skip されていた）。"""
        from builtin_data.tools.get_memory_weave_context import get_memory_weave_context

        messages = get_memory_weave_context(
            persona_id="test_persona",
            persona_dir=self.persona_dir,
            include_memopedia=True,
        )
        memopedia_messages = [
            m for m in messages
            if m["metadata"]["__memory_weave_type__"] == "memopedia"
        ]
        self.assertEqual(len(memopedia_messages), 1)
        content = memopedia_messages[0]["content"]
        # buried ページのタイトルと概要が表示されていること
        self.assertIn("BuriedPage", content)
        self.assertIn("buried の概要", content)
        # rough ページも表示されていること
        self.assertIn("RoughPage", content)
        self.assertIn("rough の概要", content)


class MemopediaManageSchemaTest(unittest.TestCase):
    """memopedia_manage スキーマから set_vividness が除去されたことを確認。"""

    def test_set_vividness_not_in_schema(self):
        from builtin_data.tools import memopedia_manage
        schema = memopedia_manage.schema()
        actions_enum = schema.parameters["properties"]["action"]["enum"]
        self.assertNotIn("set_vividness", actions_enum)

    def test_vividness_param_not_in_schema(self):
        from builtin_data.tools import memopedia_manage
        schema = memopedia_manage.schema()
        self.assertNotIn("vividness", schema.parameters.get("properties", {}))

    def test_valid_actions_still_present(self):
        from builtin_data.tools import memopedia_manage
        schema = memopedia_manage.schema()
        actions_enum = schema.parameters["properties"]["action"]["enum"]
        for action in ("delete", "move", "set_important"):
            self.assertIn(action, actions_enum)


if __name__ == "__main__":
    unittest.main()
