"""Note → テーマノードページ移行 (P3c①) のテスト。

concept_consolidation.md「P3c①② 設計 v0.1」①:
``saiverse/note_theme_migration.py`` (main DB 側オーケストレーション) +
``sai_memory/theme_pages.py`` (per-persona memory.db 側の物理格納) を検証する。

検証項目:
- 移行: main DB に note 行を仕込み →
  ``migrate_persona_notes_to_theme_pages`` 相当の呼び出しで memory.db に
  ページが立つ (trunk root_theme / id=旧 note_id / metadata / m:N 採番) →
  main DB 行 (note + note_page/note_message/track_open_note の関連) が消える
- 再実行で冪等 (二重ページ化しない、エラーにならない)
- 机へは自動で開かない (移行はページ化のみ)
- database.migrate._drop_empty_legacy_note_tables の空 DROP 冪等性
"""
from __future__ import annotations

import gc
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker


class DummyEmbedder:
    def __init__(self, model=None, **kwargs):
        self.model_name = model

    def embed(self, texts, **kwargs):
        return [[0.0] * 3 for _ in texts]


class NoteThemeMigrationTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.persona_dir = Path(self._tmp.name) / "personas" / "air"
        self.persona_dir.mkdir(parents=True, exist_ok=True)
        os.environ["SAIMEMORY_MEMORY"] = "1"
        self.addCleanup(self._cleanup_temp)
        self.addCleanup(os.environ.pop, "SAIMEMORY_MEMORY", None)

        patcher = patch("saiverse_memory.adapter.Embedder", DummyEmbedder)
        self.addCleanup(patcher.stop)
        patcher.start()

        from saiverse_memory import SAIMemoryAdapter

        self.adapter = SAIMemoryAdapter(
            "air", persona_dir=self.persona_dir, resource_id="air"
        )
        self.addCleanup(self.adapter.close)

        # main DB 側: note/note_page/note_message/track_open_note は P3c① で
        # database.models から ORM クラスが削除された (Note はテーマノード
        # ページへ物理統合済み) ため、raw SQL で「まだ移行を経ていない旧DB」を
        # 模す (tests/test_desire_stage_migration.py と同じ流儀)。
        self.db_path = str(Path(self._tmp.name) / "main.db")
        self.engine = create_engine(f"sqlite:///{self.db_path}")
        self.addCleanup(self.engine.dispose)
        self.SessionLocal = sessionmaker(bind=self.engine)
        db = self.SessionLocal()
        try:
            db.execute(text(
                "CREATE TABLE note ("
                " note_id VARCHAR(36) PRIMARY KEY, persona_id VARCHAR(255) NOT NULL,"
                " title VARCHAR(255) NOT NULL, note_type VARCHAR(32) NOT NULL,"
                " description TEXT, note_metadata TEXT,"
                " is_active BOOLEAN NOT NULL DEFAULT 1,"
                " created_at DATETIME, last_opened_at DATETIME, closed_at DATETIME)"
            ))
            db.execute(text(
                "CREATE TABLE note_page ("
                " note_id VARCHAR(36), page_id VARCHAR(255),"
                " PRIMARY KEY (note_id, page_id))"
            ))
            db.execute(text(
                "CREATE TABLE note_message ("
                " note_id VARCHAR(36), message_id VARCHAR(255),"
                " added_at DATETIME, auto_added BOOLEAN DEFAULT 0,"
                " PRIMARY KEY (note_id, message_id))"
            ))
            db.execute(text(
                "CREATE TABLE track_open_note ("
                " track_id VARCHAR(36), note_id VARCHAR(36), opened_at DATETIME,"
                " PRIMARY KEY (track_id, note_id))"
            ))
            db.commit()
        finally:
            db.close()

        self.manager = SimpleNamespace(
            SessionLocal=self.SessionLocal,
            personas={"air": SimpleNamespace(sai_memory=self.adapter)},
        )

    def _cleanup_temp(self):
        gc.collect()
        try:
            self._tmp.cleanup()
        except OSError:
            pass

    def _insert_note(self, note_id, title, note_type="vocation", description="", **extra):
        db = self.SessionLocal()
        try:
            cols = ["note_id", "persona_id", "title", "note_type", "description", "is_active"]
            params = {
                "note_id": note_id, "persona_id": "air", "title": title,
                "note_type": note_type, "description": description, "is_active": 1,
            }
            for k, v in extra.items():
                cols.append(k)
                params[k] = v
            placeholders = ", ".join(f":{c}" for c in cols)
            db.execute(text(
                f"INSERT INTO note ({', '.join(cols)}) VALUES ({placeholders})"
            ), params)
            db.commit()
        finally:
            db.close()

    def _note_row_exists(self, note_id):
        db = self.SessionLocal()
        try:
            row = db.execute(
                text("SELECT 1 FROM note WHERE note_id = :id"), {"id": note_id},
            ).fetchone()
            return row is not None
        finally:
            db.close()

    def test_migrates_note_to_theme_page(self):
        from saiverse.note_theme_migration import migrate_persona_notes_to_theme_pages

        self._insert_note(
            "note-1", "エアの存在哲学", note_type="vocation",
            description="AIとパートナーシップの記録",
        )

        migrated = migrate_persona_notes_to_theme_pages(self.manager, "air")
        self.assertEqual(migrated, 1)

        from sai_memory.memopedia.storage import get_page
        from sai_memory.theme_pages import CATEGORY_THEME, ROOT_THEME_ID

        page = get_page(self.adapter.conn, "note-1")
        self.assertIsNotNone(page)
        # id = 旧 note_id (UUID 継承)
        self.assertEqual(page.id, "note-1")
        self.assertEqual(page.parent_id, ROOT_THEME_ID)
        self.assertEqual(page.category, CATEGORY_THEME)
        self.assertEqual(page.title, "エアの存在哲学")
        self.assertEqual(page.content, "AIとパートナーシップの記録")
        # m:N 採番 (通常の Memopedia ページと同じ短縮参照)
        self.assertIsNotNone(page.short_id)
        self.assertEqual(page.metadata.get("note_type"), "vocation")
        self.assertEqual(page.metadata.get("legacy_note_id"), "note-1")
        self.assertTrue(page.metadata.get("legacy_is_active"))

        # main DB 側の行は消える
        self.assertFalse(self._note_row_exists("note-1"))

    def test_migration_does_not_open_page_on_desk(self):
        from saiverse.note_theme_migration import migrate_persona_notes_to_theme_pages
        from sai_memory.desk import list_open

        self._insert_note("note-1", "Project N")
        migrate_persona_notes_to_theme_pages(self.manager, "air")

        self.assertEqual(list_open(self.adapter.conn), [])

    def test_migration_deletes_related_links(self):
        from saiverse.note_theme_migration import migrate_persona_notes_to_theme_pages

        self._insert_note("note-1", "Project N")
        db = self.SessionLocal()
        try:
            db.execute(text(
                "INSERT INTO note_page (note_id, page_id) VALUES ('note-1', 'page-x')"
            ))
            db.execute(text(
                "INSERT INTO note_message (note_id, message_id) VALUES ('note-1', 'msg-x')"
            ))
            db.execute(text(
                "INSERT INTO track_open_note (track_id, note_id) VALUES ('track-x', 'note-1')"
            ))
            db.commit()
        finally:
            db.close()

        migrate_persona_notes_to_theme_pages(self.manager, "air")

        db = self.SessionLocal()
        try:
            for table in ("note_page", "note_message", "track_open_note"):
                row = db.execute(
                    text(f"SELECT 1 FROM {table} WHERE note_id = 'note-1'")
                ).fetchone()
                self.assertIsNone(row, f"{table} should have no rows left for note-1")
        finally:
            db.close()

    def test_migration_is_idempotent(self):
        from saiverse.note_theme_migration import migrate_persona_notes_to_theme_pages

        self._insert_note("note-1", "Project N")
        first = migrate_persona_notes_to_theme_pages(self.manager, "air")
        self.assertEqual(first, 1)

        # 2 回目: main DB 行は既に無いので no-op (0 件)
        second = migrate_persona_notes_to_theme_pages(self.manager, "air")
        self.assertEqual(second, 0)

        from sai_memory.memopedia.storage import get_children
        from sai_memory.theme_pages import ROOT_THEME_ID

        children = get_children(self.adapter.conn, ROOT_THEME_ID)
        self.assertEqual(len(children), 1)  # 重複ページ化されていない

    def test_no_notes_is_noop(self):
        from saiverse.note_theme_migration import migrate_persona_notes_to_theme_pages

        migrated = migrate_persona_notes_to_theme_pages(self.manager, "air")
        self.assertEqual(migrated, 0)

    def test_desire_notes_are_excluded(self):
        """desire ノートはテーマページ化しない (P3c-0 の削除対象を先取りしない防御)。

        本番の起動順序では backfill_desire_stage_normalization (P3c-0) が先に
        desire ノートを削除するが、本モジュール単体でも desire を移行しない
        こと (起動順序への暗黙依存の排除) を確認する。
        """
        from saiverse.note_theme_migration import migrate_persona_notes_to_theme_pages

        self._insert_note("note-d", "やりたいこと", note_type="desire",
                          description="候補プール")
        self._insert_note("note-1", "Project N", note_type="project")

        migrated = migrate_persona_notes_to_theme_pages(self.manager, "air")
        self.assertEqual(migrated, 1)  # project だけ

        from sai_memory.memopedia.storage import get_page

        self.assertIsNone(get_page(self.adapter.conn, "note-d"))
        # desire 行は移行では消さない (P3c-0 側の責務のまま残す)
        self.assertTrue(self._note_row_exists("note-d"))

    def test_created_at_string_is_stamped_as_epoch(self):
        """raw SQL 経由の DATETIME 文字列でも created_at がページに刻み直される。

        sqlite3 の DATETIME は '2026-04-30 20:09:38.498838' の str で届くため、
        _epoch が ISO パースできないと刻み直しが黙ってスキップされる (検収で
        発見した実バグの回帰テスト)。
        """
        from datetime import datetime

        from saiverse.note_theme_migration import migrate_persona_notes_to_theme_pages

        self._insert_note(
            "note-1", "Project N", note_type="project",
            created_at="2026-04-30 20:09:38.498838",
        )
        migrate_persona_notes_to_theme_pages(self.manager, "air")

        from sai_memory.memopedia.storage import get_page

        page = get_page(self.adapter.conn, "note-1")
        expected = int(datetime.fromisoformat("2026-04-30 20:09:38.498838").timestamp())
        self.assertEqual(page.created_at, expected)

    def test_missing_adapter_skips_gracefully(self):
        from saiverse.note_theme_migration import migrate_persona_notes_to_theme_pages

        self._insert_note("note-1", "Project N")
        manager = SimpleNamespace(SessionLocal=self.SessionLocal, personas={})
        migrated = migrate_persona_notes_to_theme_pages(manager, "air")
        self.assertEqual(migrated, 0)
        # 移行できていないので main DB の行は残る
        self.assertTrue(self._note_row_exists("note-1"))

    def test_missing_note_table_is_noop(self):
        from saiverse.note_theme_migration import migrate_persona_notes_to_theme_pages

        db = self.SessionLocal()
        try:
            for table in ("track_open_note", "note_message", "note_page", "note"):
                db.execute(text(f"DROP TABLE {table}"))
            db.commit()
        finally:
            db.close()

        migrated = migrate_persona_notes_to_theme_pages(self.manager, "air")
        self.assertEqual(migrated, 0)


class DropEmptyLegacyNoteTablesTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self._tmp.name) / "main.db")
        self.engine = create_engine(f"sqlite:///{self.db_path}")
        self.addCleanup(self.engine.dispose)
        self.addCleanup(self._cleanup_temp)

    def _cleanup_temp(self):
        gc.collect()
        try:
            self._tmp.cleanup()
        except OSError:
            pass

    def _create_legacy_tables(self):
        with self.engine.begin() as conn:
            conn.execute(text(
                "CREATE TABLE note (note_id VARCHAR(36) PRIMARY KEY, "
                "persona_id VARCHAR(255) NOT NULL, title VARCHAR(255) NOT NULL, "
                "note_type VARCHAR(32) NOT NULL, is_active BOOLEAN NOT NULL DEFAULT 1)"
            ))
            conn.execute(text(
                "CREATE TABLE note_page (note_id VARCHAR(36), page_id VARCHAR(255), "
                "PRIMARY KEY (note_id, page_id))"
            ))
            conn.execute(text(
                "CREATE TABLE note_message (note_id VARCHAR(36), message_id VARCHAR(255), "
                "PRIMARY KEY (note_id, message_id))"
            ))
            conn.execute(text(
                "CREATE TABLE track_open_note (track_id VARCHAR(36), note_id VARCHAR(36), "
                "PRIMARY KEY (track_id, note_id))"
            ))

    def _table_exists(self, name):
        with self.engine.connect() as conn:
            row = conn.execute(text(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=:n"
            ), {"n": name}).fetchone()
            return row is not None

    def test_drops_when_empty(self):
        from database.migrate import _drop_empty_legacy_note_tables

        self._create_legacy_tables()
        _drop_empty_legacy_note_tables(self.engine)

        for table in ("note", "note_page", "note_message", "track_open_note"):
            self.assertFalse(self._table_exists(table))

    def test_keeps_when_not_empty(self):
        from database.migrate import _drop_empty_legacy_note_tables

        self._create_legacy_tables()
        with self.engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO note (note_id, persona_id, title, note_type) "
                "VALUES ('note-1', 'air', 'T', 'vocation')"
            ))
        _drop_empty_legacy_note_tables(self.engine)

        self.assertTrue(self._table_exists("note"))

    def test_noop_when_table_absent(self):
        from database.migrate import _drop_empty_legacy_note_tables

        # note テーブル自体が存在しない (新規 DB 相当) — 例外なく完了する
        _drop_empty_legacy_note_tables(self.engine)
        self.assertFalse(self._table_exists("note"))

    def test_idempotent_second_call_is_noop(self):
        from database.migrate import _drop_empty_legacy_note_tables

        self._create_legacy_tables()
        _drop_empty_legacy_note_tables(self.engine)
        _drop_empty_legacy_note_tables(self.engine)  # 2 回目も例外なし
        self.assertFalse(self._table_exists("note"))


if __name__ == "__main__":
    unittest.main()
