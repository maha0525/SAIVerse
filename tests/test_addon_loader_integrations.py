"""Tests for saiverse.addon_loader integrations auto-discovery."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from database.models import AddonConfig, Base


class AddonIntegrationsLoaderTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

        self._expansion = Path(self._tmp.name) / "expansion_data"
        self._expansion.mkdir()

        # Patch EXPANSION_DATA_DIR
        from saiverse import data_paths
        self._patches = [
            patch.object(data_paths, "EXPANSION_DATA_DIR", self._expansion),
        ]
        for p in self._patches:
            p.start()

        # Patch SessionLocal with in-memory DB
        self._engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self._engine)
        TestSession = sessionmaker(bind=self._engine)

        from database import session as session_module
        self._session_patch = patch.object(
            session_module, "SessionLocal", TestSession,
        )
        self._session_patch.start()
        self._test_session = TestSession

        # Reset addon integration registry
        from saiverse import addon_loader
        addon_loader._addon_integration_registry.clear()

    def tearDown(self):
        for p in reversed(self._patches):
            p.stop()
        self._session_patch.stop()
        self._engine.dispose()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _make_addon(
        self,
        name: str,
        integration_class_name: str,
        integration_name: str,
        enabled: bool = True,
    ) -> Path:
        """Create a test addon with one integration class.

        Each addon's integration uses a unique class & instance ``.name``
        attribute so the IntegrationManager can distinguish them.
        """
        addon_dir = self._expansion / name
        (addon_dir / "integrations").mkdir(parents=True, exist_ok=True)
        (addon_dir / "addon.json").write_text(json.dumps({
            "name": name,
            "version": "0.0.1",
        }), encoding="utf-8")
        (addon_dir / "integrations" / "main.py").write_text(f"""
from saiverse.integrations.base import BaseIntegration


class {integration_class_name}(BaseIntegration):
    name = "{integration_name}"
    poll_interval_seconds = 60

    def poll(self, manager):
        return []
""", encoding="utf-8")

        # Register addon as enabled/disabled in the test DB
        db = self._test_session()
        try:
            db.add(AddonConfig(addon_name=name, is_enabled=enabled))
            db.commit()
        finally:
            db.close()

        return addon_dir

    def _make_integration_manager(self):
        """Create a real IntegrationManager (without starting its thread)."""
        from saiverse.integration_manager import IntegrationManager
        # Pass a dummy manager (poll() in our test integrations doesn't use it)
        return IntegrationManager(saiverse_manager=None, tick_interval=1)

    # ------------------------------------------------------------------
    # load_addon_integrations
    # ------------------------------------------------------------------

    def test_loads_integrations_from_enabled_addon(self):
        from saiverse.addon_loader import load_addon_integrations

        self._make_addon("test-addon-1", "AlphaIntegration", "alpha")
        im = self._make_integration_manager()

        load_addon_integrations(im)

        self.assertEqual(len(im._integrations), 1)
        self.assertEqual(im._integrations[0].name, "alpha")

    def test_skips_disabled_addons(self):
        from saiverse.addon_loader import load_addon_integrations

        self._make_addon("disabled-addon", "BetaIntegration", "beta", enabled=False)
        im = self._make_integration_manager()

        load_addon_integrations(im)

        self.assertEqual(len(im._integrations), 0)

    def test_loads_multiple_addons(self):
        from saiverse.addon_loader import load_addon_integrations

        self._make_addon("addon-a", "FooIntegration", "foo")
        self._make_addon("addon-b", "BarIntegration", "bar")
        im = self._make_integration_manager()

        load_addon_integrations(im)

        names = sorted(i.name for i in im._integrations)
        self.assertEqual(names, ["bar", "foo"])

    def test_skips_addon_without_integrations_dir(self):
        from saiverse.addon_loader import load_addon_integrations

        addon_dir = self._expansion / "no-integrations-addon"
        addon_dir.mkdir()
        (addon_dir / "addon.json").write_text(
            json.dumps({"name": "no-integrations-addon", "version": "0.0.1"}),
            encoding="utf-8",
        )
        db = self._test_session()
        try:
            db.add(AddonConfig(addon_name="no-integrations-addon", is_enabled=True))
            db.commit()
        finally:
            db.close()

        im = self._make_integration_manager()
        load_addon_integrations(im)
        self.assertEqual(len(im._integrations), 0)

    # ------------------------------------------------------------------
    # register/unregister_addon_integrations (runtime)
    # ------------------------------------------------------------------

    def test_register_addon_integrations_runtime(self):
        from saiverse.addon_loader import register_addon_integrations

        self._make_addon("rt-addon", "GammaIntegration", "gamma")
        im = self._make_integration_manager()

        count = register_addon_integrations(im, "rt-addon")
        self.assertEqual(count, 1)
        self.assertEqual(im._integrations[0].name, "gamma")

    def test_unregister_addon_integrations_removes_them(self):
        from saiverse.addon_loader import (
            register_addon_integrations,
            unregister_addon_integrations,
        )

        self._make_addon("toggle-addon", "DeltaIntegration", "delta")
        im = self._make_integration_manager()

        register_addon_integrations(im, "toggle-addon")
        self.assertEqual(len(im._integrations), 1)

        removed = unregister_addon_integrations(im, "toggle-addon")
        self.assertEqual(removed, 1)
        self.assertEqual(len(im._integrations), 0)

    def test_unregister_unknown_addon_is_safe(self):
        from saiverse.addon_loader import unregister_addon_integrations

        im = self._make_integration_manager()
        # Should not raise even when addon was never registered
        removed = unregister_addon_integrations(im, "never-seen-addon")
        self.assertEqual(removed, 0)


class IntegrationManagerUnregisterTests(unittest.TestCase):
    """Direct tests for IntegrationManager.unregister."""

    def test_unregister_removes_by_name(self):
        from saiverse.integration_manager import IntegrationManager
        from saiverse.integrations.base import BaseIntegration

        class A(BaseIntegration):
            name = "a"
            def poll(self, manager): return []

        class B(BaseIntegration):
            name = "b"
            def poll(self, manager): return []

        im = IntegrationManager(saiverse_manager=None)
        im.register(A())
        im.register(B())
        self.assertEqual(len(im._integrations), 2)

        self.assertTrue(im.unregister("a"))
        self.assertEqual(len(im._integrations), 1)
        self.assertEqual(im._integrations[0].name, "b")

    def test_unregister_returns_false_for_unknown(self):
        from saiverse.integration_manager import IntegrationManager
        im = IntegrationManager(saiverse_manager=None)
        self.assertFalse(im.unregister("missing"))


if __name__ == "__main__":
    unittest.main()
