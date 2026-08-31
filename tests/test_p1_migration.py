"""P1 (DB 基盤) の additive migration テスト (life_concept_map.md §14)。

検証項目:
- episodes テーブル (新規) と persona_task の stage / nature / promoted_from
  (新規列) が try_additive_migration の追加系パスで適用されること
  (全書換パスに落ちないこと — Windows の WinError 32 回避の前提)
- 旧スキーマの既存行 (desire / task) がデータを失わないこと。stage は
  P3c-0 (desire 正規化) 以降、読み出し時導出でなく
  ``database.migrate.backfill_desire_stage_normalization`` の一回きりデータ
  移行が derive_stage() 相当の規則で物理刻印する (旧行の parent_kind='note'
  も併せて親なしへ正規化される)
"""
from __future__ import annotations

import gc
import sys
import tempfile
import unittest
from pathlib import Path

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from database.migrate import needs_migration, try_additive_migration
from database.models import Base
from saiverse.persona_task_manager import PersonaTaskManager


NEW_TASK_COLUMNS = ("stage", "nature", "promoted_from")


class P1AdditiveMigrationTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self._tmpdir.name) / "test.db")
        # 現行スキーマで作ってから「P1 以前」の状態に戻す (episodes を落とし、
        # persona_task の新列を落とす)。SQLite 3.35+ の DROP COLUMN を使う。
        engine = create_engine(f"sqlite:///{self.db_path}")
        Base.metadata.create_all(engine)
        with engine.begin() as conn:
            conn.execute(text("DROP TABLE episodes"))
            for col in NEW_TASK_COLUMNS:
                conn.execute(text(f'ALTER TABLE persona_task DROP COLUMN "{col}"'))
            # 旧スキーマ時代の既存行: desire (parent_kind='note') と 未所属 task
            conn.execute(text(
                "INSERT INTO persona_task "
                "(id, persona_id, short_id, parent_kind, note_id, track_id, "
                " title, goal, summary, status, priority, origin, "
                " created_at, updated_at, version) "
                "VALUES "
                "('a', 'p1', 1, 'note', 'note-1', NULL, "
                " '古い欲求', '', '', 'pending', 'normal', 'auto', "
                " '2026-01-01 00:00:00', '2026-01-01 00:00:00', 0)"
            ))
            conn.execute(text(
                "INSERT INTO persona_task "
                "(id, persona_id, short_id, parent_kind, note_id, track_id, "
                " title, goal, summary, status, priority, origin, "
                " created_at, updated_at, version) "
                "VALUES "
                "('b', 'p1', 2, NULL, NULL, NULL, "
                " '古いタスク', '', '', 'pending', 'normal', 'auto', "
                " '2026-01-02 00:00:00', '2026-01-02 00:00:00', 0)"
            ))
        engine.dispose()

    def tearDown(self):
        gc.collect()
        try:
            self._tmpdir.cleanup()
        except PermissionError:
            pass

    def test_needs_migration_detects_p1_diff(self):
        self.assertTrue(needs_migration(self.db_path))

    def test_additive_migration_applies_without_rewrite(self):
        # 追加系パスだけで差分が解消される (= 全書換に落ちない) こと
        self.assertTrue(try_additive_migration(self.db_path))
        self.assertFalse(needs_migration(self.db_path))

        engine = create_engine(f"sqlite:///{self.db_path}")
        try:
            insp = inspect(engine)
            self.assertTrue(insp.has_table("episodes"))
            episode_cols = {c["name"] for c in insp.get_columns("episodes")}
            self.assertTrue({
                "EPISODE_ID", "SHORT_ID", "PERSONA_ID", "KIND", "OCCURRENCE_ID",
                "STARTED_AT", "ENDED_AT", "BUILDING_ID", "PARTICIPANTS_JSON",
                "ORIGIN_REF", "STATUS", "DIGEST_REF", "META_JSON",
            } <= episode_cols)
            task_cols = {c["name"] for c in insp.get_columns("persona_task")}
            self.assertTrue(set(NEW_TASK_COLUMNS) <= task_cols)
        finally:
            engine.dispose()

    def test_legacy_rows_survive_and_stage_gets_stamped(self):
        """スキーマの追加系移行 (列追加) だけでは stage は NULL のまま。

        P3c-0 以降、物理刻印は別ステップ (backfill_desire_stage_normalization、
        main.py で schema 移行の直後に無条件で呼ばれる) が担う — 両方を通した
        後の最終状態を検証する。
        """
        self.assertTrue(try_additive_migration(self.db_path))

        # 列追加だけでは NULL のまま (derive_stage 相当の刻印はまだ走っていない)。
        check_engine = create_engine(f"sqlite:///{self.db_path}")
        try:
            with check_engine.connect() as conn:
                raw = conn.execute(text(
                    "SELECT stage FROM persona_task WHERE id='a'"
                )).fetchone()
            self.assertEqual(tuple(raw), (None,))
        finally:
            check_engine.dispose()

        from database.migrate import backfill_desire_stage_normalization
        backfill_desire_stage_normalization(self.db_path)

        engine = create_engine(f"sqlite:///{self.db_path}")
        try:
            SessionLocal = sessionmaker(bind=engine)
            ptm = PersonaTaskManager(SessionLocal)
            desire = ptm.get_task("a", persona_id="p1")
            task = ptm.get_task("b", persona_id="p1")
            # データ保全
            self.assertEqual(desire["title"], "古い欲求")
            self.assertEqual(task["title"], "古いタスク")
            # derive_stage() 相当の規則で物理刻印される
            self.assertEqual(desire["stage"], "candidate")
            self.assertEqual(task["stage"], "adopted")
            # 候補は親なしへ正規化される (P3c-0)
            self.assertIsNone(desire["parent_kind"])
            self.assertIsNone(desire["note_id"])
            with engine.connect() as conn:
                raw = conn.execute(text(
                    "SELECT stage, nature, promoted_from FROM persona_task WHERE id='a'"
                )).fetchone()
            self.assertEqual(tuple(raw), ("candidate", None, None))
        finally:
            engine.dispose()


if __name__ == "__main__":
    unittest.main()
