"""Regression tests for native addon tool namespacing.

Native tools loaded from ``expansion_data/<addon>/tools/`` are registered under
a namespaced key ``<addon>__<tool>`` so that two addons shipping a native tool
with the same bare name (e.g. ``see``) no longer silently overwrite each other
(last loader wins). Bare names persisted before namespacing still resolve via
``canonicalize_spell_name`` when unambiguous.

See docs/issues/archive/native_tool_addon_prefix_missing.md.
"""

import unittest

from tools import (
    _registry_key,
    canonicalize_spell_name,
    canonicalize_tool_name,
    register_external_tool,
    unregister_external_tool,
)
from tools.core import ToolSchema


def _schema(name, addon_name=None, spell=True):
    schema = ToolSchema(
        name=name, description="d", parameters={"type": "object", "properties": {}, "required": []},
        result_type="string", spell=spell,
    )
    if addon_name is not None:
        schema.addon_name = addon_name
    return schema


class RegistryKeyTest(unittest.TestCase):
    def test_addon_tool_is_namespaced(self):
        self.assertEqual(_registry_key(_schema("see", addon_name="stackchan")), "stackchan__see")

    def test_builtin_tool_keeps_bare_name(self):
        self.assertEqual(_registry_key(_schema("calculator")), "calculator")

    def test_two_addons_same_name_get_distinct_keys(self):
        a = _registry_key(_schema("see", addon_name="stackchan"))
        b = _registry_key(_schema("see", addon_name="othercam"))
        self.assertEqual(a, "stackchan__see")
        self.assertEqual(b, "othercam__see")
        self.assertNotEqual(a, b)


class CanonicalizeSpellNameTest(unittest.TestCase):
    def setUp(self):
        self._added = []

    def tearDown(self):
        for name in self._added:
            unregister_external_tool(name)

    def _register(self, name):
        if register_external_tool(name, _schema(name), lambda **kw: "ok"):
            self._added.append(name)

    def test_exact_match_returned_as_is(self):
        self._register("nt_prefix__alpha")
        self.assertEqual(canonicalize_spell_name("nt_prefix__alpha"), "nt_prefix__alpha")

    def test_unique_bare_name_resolves_to_prefixed(self):
        self._register("nt_prefix__beta")
        self.assertEqual(canonicalize_spell_name("beta"), "nt_prefix__beta")

    def test_ambiguous_bare_name_left_unchanged(self):
        self._register("nt_one__gamma")
        self._register("nt_two__gamma")
        # Two addons expose ``gamma`` — refuse to guess, so the caller reports
        # it as unknown rather than firing the wrong one.
        self.assertEqual(canonicalize_spell_name("gamma"), "gamma")

    def test_unknown_name_left_unchanged(self):
        self.assertEqual(canonicalize_spell_name("nt_does_not_exist_xyz"), "nt_does_not_exist_xyz")


class CanonicalizeToolNameTest(unittest.TestCase):
    """Playbook TOOL / tool_call ノードの素名参照が名前空間キーへ解決されること。

    canonicalize_spell_name と違い SPELL_TOOL_NAMES でなく TOOL_REGISTRY 全体を
    見るため、spell=False のツール (generate_image_local 等の Playbook 専用
    ツール) も解決できる必要がある。
    """

    def setUp(self):
        self._added = []

    def tearDown(self):
        for name in self._added:
            unregister_external_tool(name)

    def _register(self, name, spell=False):
        if register_external_tool(name, _schema(name, spell=spell), lambda **kw: "ok"):
            self._added.append(name)

    def test_exact_match_returned_as_is(self):
        self._register("ct_prefix__alpha")
        self.assertEqual(canonicalize_tool_name("ct_prefix__alpha"), "ct_prefix__alpha")

    def test_unique_bare_name_resolves_to_prefixed(self):
        self._register("ct_prefix__beta")
        self.assertEqual(canonicalize_tool_name("beta"), "ct_prefix__beta")

    def test_non_spell_tool_resolves(self):
        # spell=False でも解決できること (canonicalize_spell_name との差分の核)
        self._register("ct_addon__imagegen", spell=False)
        self.assertEqual(canonicalize_tool_name("imagegen"), "ct_addon__imagegen")

    def test_ambiguous_bare_name_left_unchanged(self):
        self._register("ct_one__gamma")
        self._register("ct_two__gamma")
        self.assertEqual(canonicalize_tool_name("gamma"), "gamma")

    def test_unknown_name_left_unchanged(self):
        self.assertEqual(canonicalize_tool_name("ct_does_not_exist_xyz"), "ct_does_not_exist_xyz")


if __name__ == "__main__":
    unittest.main()
