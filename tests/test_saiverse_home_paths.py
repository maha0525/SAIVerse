"""Canonical persona/backup path derivation must honor SAIVERSE_HOME.

Companion to tests/test_attachment_paths.py: any runtime path that is derived
from ``Path.home()/.saiverse`` instead of ``SAIVERSE_HOME`` silently points at
the wrong (ephemeral) location inside containers.
"""
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class PersonaPathHelperTests(unittest.TestCase):
    def test_get_personas_dir_honors_saiverse_home(self):
        from saiverse.data_paths import get_personas_dir

        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"SAIVERSE_HOME": tmp}):
                self.assertEqual(get_personas_dir(), Path(tmp) / "personas")

    def test_get_persona_memory_db_honors_saiverse_home(self):
        from saiverse.data_paths import get_persona_memory_db

        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"SAIVERSE_HOME": tmp}):
                self.assertEqual(
                    get_persona_memory_db("akane_city_a"),
                    Path(tmp) / "personas" / "akane_city_a" / "memory.db",
                )


class BackupRootTests(unittest.TestCase):
    def test_backup_roots_honor_saiverse_home(self):
        from sai_memory.backup import get_backup_root, get_simple_backup_root

        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"SAIVERSE_HOME": tmp}):
                self.assertEqual(
                    get_backup_root(), Path(tmp) / "backups" / "saimemory_rdiff"
                )
                self.assertEqual(
                    get_simple_backup_root(), Path(tmp) / "backups" / "saimemory_simple"
                )

    def test_simple_backup_writes_under_saiverse_home(self):
        """run_backup_auto (as called on startup, without output_root) must
        place backups inside SAIVERSE_HOME, not Path.home()."""
        from sai_memory.backup import run_backup_auto

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            db_path = Path(tmp) / "memory.db"
            # sqlite3 の `with` はトランザクションを閉じるだけで接続は閉じない。
            # Windows では接続が開いたままだと TemporaryDirectory の後片付けが
            # memory.db を削除できず WinError 32 になるため、明示的にクローズする。
            conn = sqlite3.connect(db_path)
            try:
                conn.execute("CREATE TABLE t (x INTEGER)")
                conn.execute("INSERT INTO t VALUES (1)")
                conn.commit()
            finally:
                conn.close()

            with patch.dict(os.environ, {"SAIVERSE_HOME": str(home)}):
                result = run_backup_auto(
                    persona_id="p1",
                    db_path=db_path,
                    prefer_simple=True,
                )

            self.assertIsNotNone(result)
            self.assertTrue(
                str(result).startswith(str(home / "backups")),
                f"backup written outside SAIVERSE_HOME: {result}",
            )


if __name__ == "__main__":
    unittest.main()
