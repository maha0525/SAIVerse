"""Tests for Memopedia category registry (CATEGORY_DEFS).

Verifies:
- Registry completeness and correctness
- category_keys() filtering per role
- category_label() lookup
- Drift fixes: terms in metabolizable, events in writable/extractable
"""

import unittest


class TestCategoryRegistry(unittest.TestCase):
    """Tests for the CATEGORY_DEFS registry and helper functions."""

    def setUp(self):
        from sai_memory.memopedia.storage import CATEGORY_DEFS, category_keys, category_label
        self.CATEGORY_DEFS = CATEGORY_DEFS
        self.category_keys = category_keys
        self.category_label = category_label

    def test_registry_has_all_seven_categories(self):
        expected = {"people", "terms", "plans", "events", "theme", "core", "chronicle"}
        self.assertEqual(set(self.CATEGORY_DEFS.keys()), expected)

    def test_in_tree_returns_five_categories_in_order(self):
        keys = self.category_keys("in_tree")
        self.assertEqual(keys, ["people", "terms", "plans", "events", "theme"])

    def test_extractable_returns_four_categories(self):
        keys = self.category_keys("extractable")
        self.assertEqual(keys, ["people", "terms", "plans", "events"])

    def test_writable_returns_four_categories(self):
        keys = self.category_keys("writable")
        self.assertEqual(keys, ["people", "terms", "plans", "events"])

    def test_metabolizable_returns_four_categories(self):
        keys = self.category_keys("metabolizable")
        self.assertEqual(keys, ["people", "terms", "plans", "events"])

    def test_order_is_consistent_across_roles(self):
        """All role-filtered lists should preserve the global order."""
        extractable = self.category_keys("extractable")
        metabolizable = self.category_keys("metabolizable")
        writable = self.category_keys("writable")
        # All four should share the same order
        self.assertEqual(extractable, metabolizable)
        self.assertEqual(extractable, writable)

    def test_theme_is_in_tree_but_not_extractable(self):
        in_tree = self.category_keys("in_tree")
        extractable = self.category_keys("extractable")
        self.assertIn("theme", in_tree)
        self.assertNotIn("theme", extractable)

    def test_core_and_chronicle_not_in_tree(self):
        in_tree = self.category_keys("in_tree")
        self.assertNotIn("core", in_tree)
        self.assertNotIn("chronicle", in_tree)

    def test_theme_hide_when_empty(self):
        self.assertTrue(self.CATEGORY_DEFS["theme"].hide_when_empty)
        for key in ["people", "terms", "plans", "events", "core", "chronicle"]:
            self.assertFalse(self.CATEGORY_DEFS[key].hide_when_empty)

    def test_category_label_returns_japanese(self):
        self.assertEqual(self.category_label("people"), "人物")
        self.assertEqual(self.category_label("terms"), "用語")
        self.assertEqual(self.category_label("plans"), "計画")
        self.assertEqual(self.category_label("events"), "出来事")
        self.assertEqual(self.category_label("theme"), "テーマ")

    def test_category_label_unknown_key_returns_key(self):
        self.assertEqual(self.category_label("unknown_key"), "unknown_key")

    def test_plans_label_is_keikaku_not_yotei(self):
        """Regression: 'plans' label was changed from '予定' to '計画' in P4-0."""
        self.assertEqual(self.category_label("plans"), "計画")
        self.assertNotEqual(self.category_label("plans"), "予定")

    def test_category_defs_are_frozen(self):
        """CategoryDef is a frozen dataclass — mutation must raise."""
        import dataclasses
        with self.assertRaises((AttributeError, dataclasses.FrozenInstanceError)):
            self.CATEGORY_DEFS["people"].key = "mutated"  # type: ignore[misc]

    def test_unknown_role_returns_empty(self):
        """category_keys with a non-existent role returns empty list."""
        keys = self.category_keys("nonexistent_role")
        self.assertEqual(keys, [])


class TestDriftFixes(unittest.TestCase):
    """Regression tests for the drift bugs fixed in P4-0."""

    def setUp(self):
        from sai_memory.memopedia.storage import category_keys
        self.category_keys = category_keys

    def test_terms_in_metabolizable(self):
        """Bug fix: maintain_memopedia.py was using ['people', 'events', 'plans'] — missing 'terms'."""
        self.assertIn("terms", self.category_keys("metabolizable"))

    def test_events_in_writable(self):
        """Bug fix: memopedia_save_page.py and memopedia_note.py were missing 'events'."""
        self.assertIn("events", self.category_keys("writable"))

    def test_events_in_extractable(self):
        """Bug fix: build_memopedia_core.py first schema had ['people', 'events', 'plans'] — missing 'terms'."""
        self.assertIn("events", self.category_keys("extractable"))

    def test_terms_in_extractable(self):
        """Bug fix: build_memopedia_core.py first schema was missing 'terms'."""
        self.assertIn("terms", self.category_keys("extractable"))

    def test_category_root_ids_include_events(self):
        """Bug fix: entity_extractor and tool files lacked 'events' in root IDs."""
        from sai_memory.memory.entity_extractor import CATEGORY_ROOT_IDS
        self.assertIn("events", CATEGORY_ROOT_IDS)
        self.assertEqual(CATEGORY_ROOT_IDS["events"], "root_events")

    def test_valid_categories_includes_all_four(self):
        """Regression: VALID_CATEGORIES should contain all extractable categories."""
        from sai_memory.memory.entity_extractor import VALID_CATEGORIES
        self.assertEqual(VALID_CATEGORIES, {"people", "terms", "plans", "events"})

    def test_initial_roots_plans_title_is_keikaku(self):
        """Bug fix: INITIAL_ROOTS root_plans title was '予定', now '計画'."""
        from sai_memory.memopedia.storage import INITIAL_ROOTS
        root_plans = next((r for r in INITIAL_ROOTS if r["id"] == "root_plans"), None)
        self.assertIsNotNone(root_plans)
        self.assertEqual(root_plans["title"], "計画")


if __name__ == "__main__":
    unittest.main()
