"""Tests for saiverse.addon_metadata (set / get / delete)."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from database.models import Base
from saiverse import addon_metadata


class AddonMetadataDeleteTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="saiverse_addon_meta_")
        self.addCleanup(self._tmp.cleanup)
        db_path = Path(self._tmp.name) / "saiverse.db"
        self._engine = create_engine(f"sqlite:///{db_path}")
        self.addCleanup(self._engine.dispose)
        Base.metadata.create_all(self._engine)
        TestSession = sessionmaker(bind=self._engine, autocommit=False, autoflush=False)

        # addon_metadata._get_session は呼び出し時に database.session.SessionLocal を
        # import するので、モジュール属性を差し替えれば一時 DB に向く。
        from database import session as session_module
        session_patch = patch.object(session_module, "SessionLocal", TestSession)
        session_patch.start()
        self.addCleanup(session_patch.stop)

    def test_delete_removes_existing_key(self):
        addon_metadata.set_metadata("msg-1", "rating_addon", "rating", "good")
        self.assertEqual(
            addon_metadata.get_metadata_value("msg-1", "rating_addon", "rating"),
            "good",
        )

        self.assertTrue(addon_metadata.delete_metadata("msg-1", "rating_addon", "rating"))
        self.assertIsNone(
            addon_metadata.get_metadata_value("msg-1", "rating_addon", "rating"),
        )

    def test_delete_only_touches_the_named_key(self):
        addon_metadata.set_metadata("msg-1", "rating_addon", "rating", "bad")
        addon_metadata.set_metadata("msg-1", "rating_addon", "note", "keep")
        addon_metadata.set_metadata("msg-2", "rating_addon", "rating", "good")
        addon_metadata.set_metadata("msg-1", "other_addon", "rating", "good")

        self.assertTrue(addon_metadata.delete_metadata("msg-1", "rating_addon", "rating"))

        self.assertEqual(addon_metadata.get_metadata("msg-1", "rating_addon"), {"note": "keep"})
        self.assertEqual(
            addon_metadata.get_metadata_value("msg-2", "rating_addon", "rating"), "good",
        )
        self.assertEqual(
            addon_metadata.get_metadata_value("msg-1", "other_addon", "rating"), "good",
        )

    def test_delete_missing_key_returns_false(self):
        self.assertFalse(addon_metadata.delete_metadata("msg-x", "rating_addon", "rating"))

        addon_metadata.set_metadata("msg-1", "rating_addon", "rating", "good")
        self.assertFalse(addon_metadata.delete_metadata("msg-1", "rating_addon", "missing"))
        self.assertEqual(
            addon_metadata.get_metadata_value("msg-1", "rating_addon", "rating"), "good",
        )


if __name__ == "__main__":
    unittest.main()
