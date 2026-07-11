"""desire 正規化 (P3c-0) の main DB データ移行テスト (concept_consolidation.md P3c-0)。

database.migrate._backfill_desire_stage_normalization / その public エントリ
``backfill_desire_stage_normalization`` を検証する:

- (a) stage IS NULL の全行に derive_stage() 相当の CASE で刻印
- (b) parent_kind='note' の行を親なし (parent_kind/note_id を NULL) にする
- (a)→(b) の順序 (候補の導出根拠が消える前に刻印する)
- (c) note_type='desire' の Note 行と note_page/note_message/track_open_note の
  関連リンクが削除される
- 全ステップが冪等 (2 回目の実行で対象行が残らず、何も壊さない)
"""
from __future__ import annotations

import gc
import sys
import tempfile
import unittest
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from database.migrate import (
    _backfill_desire_stage_normalization,
    backfill_desire_stage_normalization,
)
from database.models import Base, Note, NoteMessage, NotePage, TrackOpenNote


class DesireStageMigrationTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self._tmpdir.name) / "test.db")
        self.engine = create_engine(f"sqlite:///{self.db_path}")
        Base.metadata.create_all(self.engine)
        self.SessionLocal = sessionmaker(bind=self.engine)

        db = self.SessionLocal()
        try:
            # 生存中の desire 候補 (parent_kind='note') — 移行後は
            # parent_kind/note_id が NULL になり、stage='candidate' が刻まれる。
            db.execute(text(
                "INSERT INTO persona_task "
                "(id, persona_id, short_id, parent_kind, note_id, track_id, "
                " title, goal, summary, status, priority, origin, desire_state, "
                " created_at, updated_at, version) "
                "VALUES "
                "('a', 'p1', 1, 'note', 'note-1', NULL, "
                " '古い欲求', '', '', 'pending', 'normal', 'auto', 'fresh', "
                " '2026-01-01 00:00:00', '2026-01-01 00:00:00', 0)"
            ))
            # 期限切れで cancelled になった desire (parent_kind='note' のまま
            # cancelled + desire_state='expired') — dormant に刻まれるはず。
            db.execute(text(
                "INSERT INTO persona_task "
                "(id, persona_id, short_id, parent_kind, note_id, track_id, "
                " title, goal, summary, status, priority, origin, desire_state, "
                " created_at, updated_at, version) "
                "VALUES "
                "('b', 'p1', 2, 'note', 'note-1', NULL, "
                " '消えた欲求', '', '', 'cancelled', 'normal', 'auto', 'expired', "
                " '2026-01-01 00:00:00', '2026-01-01 00:00:00', 0)"
            ))
            # 普通の未所属タスク (parent_kind=NULL) — adopted に刻まれるはず。
            db.execute(text(
                "INSERT INTO persona_task "
                "(id, persona_id, short_id, parent_kind, note_id, track_id, "
                " title, goal, summary, status, priority, origin, "
                " created_at, updated_at, version) "
                "VALUES "
                "('c', 'p1', 3, NULL, NULL, NULL, "
                " '普通のタスク', '', '', 'pending', 'normal', 'auto', "
                " '2026-01-02 00:00:00', '2026-01-02 00:00:00', 0)"
            ))
            # 既に stage が刻まれている行 (再実行で上書きされないことの確認用)。
            db.execute(text(
                "INSERT INTO persona_task "
                "(id, persona_id, short_id, parent_kind, note_id, track_id, "
                " title, goal, summary, status, priority, origin, stage, "
                " created_at, updated_at, version) "
                "VALUES "
                "('d', 'p1', 4, NULL, NULL, NULL, "
                " '既に刻印済み', '', '', 'pending', 'normal', 'auto', 'adopted', "
                " '2026-01-03 00:00:00', '2026-01-03 00:00:00', 0)"
            ))
            db.commit()

            # desire ノート (削除対象) + 関連リンク
            db.add(Note(
                note_id="note-1", persona_id="p1", title="やりたいこと",
                note_type="desire", description="候補プール", is_active=True,
            ))
            # 通常の note (削除対象外の対照)
            db.add(Note(
                note_id="note-2", persona_id="p1", title="友人",
                note_type="person", is_active=True,
            ))
            db.commit()
            db.add(NotePage(note_id="note-1", page_id="page-1"))
            db.add(NoteMessage(note_id="note-1", message_id="msg-1"))
            db.add(TrackOpenNote(track_id="track-1", note_id="note-1"))
            db.commit()
        finally:
            db.close()

    def tearDown(self):
        self.engine.dispose()
        gc.collect()
        try:
            self._tmpdir.cleanup()
        except PermissionError:
            pass

    def _row(self, table, id_col, id_value, cols):
        db = self.SessionLocal()
        try:
            result = db.execute(text(
                f"SELECT {', '.join(cols)} FROM {table} WHERE {id_col} = :id"
            ), {"id": id_value}).fetchone()
            return tuple(result) if result is not None else None
        finally:
            db.close()

    def test_stamps_stage_and_normalizes_parent(self):
        backfill_desire_stage_normalization(self.db_path)

        # (a) stage の物理刻印 (derive_stage 相当)
        self.assertEqual(
            self._row("persona_task", "id", "a", ["stage"]), ("candidate",),
        )
        self.assertEqual(
            self._row("persona_task", "id", "b", ["stage"]), ("dormant",),
        )
        self.assertEqual(
            self._row("persona_task", "id", "c", ["stage"]), ("adopted",),
        )
        # 既刻印済みの行は上書きされない (stage IS NULL の行だけが対象)。
        self.assertEqual(
            self._row("persona_task", "id", "d", ["stage"]), ("adopted",),
        )

        # (b) note 親バインドの撤去 — 生存中の候補も親なしに正規化される
        self.assertEqual(
            self._row("persona_task", "id", "a", ["parent_kind", "note_id"]),
            (None, None),
        )
        self.assertEqual(
            self._row("persona_task", "id", "b", ["parent_kind", "note_id"]),
            (None, None),
        )

    def test_deletes_desire_notes_and_links_but_not_other_notes(self):
        backfill_desire_stage_normalization(self.db_path)

        self.assertIsNone(self._row("note", "note_id", "note-1", ["note_id"]))
        self.assertIsNone(self._row("note_page", "note_id", "note-1", ["note_id"]))
        self.assertIsNone(self._row("note_message", "note_id", "note-1", ["note_id"]))
        self.assertIsNone(
            self._row("track_open_note", "note_id", "note-1", ["note_id"])
        )
        # 通常の note (person 等) は残る。
        self.assertIsNotNone(self._row("note", "note_id", "note-2", ["note_id"]))

    def test_idempotent_on_second_run(self):
        backfill_desire_stage_normalization(self.db_path)
        first = {
            row_id: self._row(
                "persona_task", "id", row_id, ["stage", "parent_kind", "note_id"],
            )
            for row_id in ("a", "b", "c", "d")
        }
        # 2 回目の実行 — 対象行が既に無いので何も変化しないはず。
        backfill_desire_stage_normalization(self.db_path)
        second = {
            row_id: self._row(
                "persona_task", "id", row_id, ["stage", "parent_kind", "note_id"],
            )
            for row_id in ("a", "b", "c", "d")
        }
        self.assertEqual(first, second)
        self.assertIsNone(self._row("note", "note_id", "note-1", ["note_id"]))

    def test_order_a_then_b_preserves_candidate_derivation(self):
        """(a)→(b) の順序が不変条件: 先に親を外すと candidate の導出根拠が消える。

        internal 関数を直接叩いて、想定どおりの最終状態 (candidate のまま) に
        なることを確認する — 実装の呼び出し順序が壊れたときに落ちる回帰テスト。
        """
        engine = create_engine(f"sqlite:///{self.db_path}")
        try:
            _backfill_desire_stage_normalization(engine)
            with engine.connect() as conn:
                row = conn.execute(text(
                    "SELECT stage, parent_kind FROM persona_task WHERE id = 'a'"
                )).fetchone()
            self.assertEqual(tuple(row), ("candidate", None))
        finally:
            engine.dispose()


if __name__ == "__main__":
    unittest.main()
