"""Tests for saiverse.addon_paths."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from saiverse.addon_paths import get_addon_storage_path


class GetAddonStoragePathTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._home = Path(self._tmp.name) / ".saiverse"
        os.environ["SAIVERSE_HOME"] = str(self._home)

    def tearDown(self):
        os.environ.pop("SAIVERSE_HOME", None)

    def test_returns_path_under_addons_dir(self):
        path = get_addon_storage_path("saiverse-x-addon")
        self.assertEqual(path, self._home / "addons" / "saiverse-x-addon")

    def test_creates_directory_if_missing(self):
        path = get_addon_storage_path("saiverse-test-addon")
        self.assertTrue(path.exists())
        self.assertTrue(path.is_dir())

    def test_idempotent(self):
        path1 = get_addon_storage_path("saiverse-x-addon")
        path2 = get_addon_storage_path("saiverse-x-addon")
        self.assertEqual(path1, path2)

    def test_rejects_path_traversal(self):
        with self.assertRaises(ValueError):
            get_addon_storage_path("../escape")
        with self.assertRaises(ValueError):
            get_addon_storage_path("foo/bar")
        with self.assertRaises(ValueError):
            get_addon_storage_path("foo\\bar")

    def test_rejects_empty_name(self):
        with self.assertRaises(ValueError):
            get_addon_storage_path("")


if __name__ == "__main__":
    unittest.main()
