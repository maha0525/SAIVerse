"""Regression tests for MCP tool registration resilience.

Root cause this pins (the missing Stack-chan LED spells):
the raw ``move_head`` MCP tool's JSON schema uses ``oneOf`` for its ``speed``
parameter. ``tools.adapters.gemini.to_gemini`` fed that straight into
``google.genai.types.Schema``, which rejects ``oneOf`` as ``extra_forbidden``.
That exception aborted the per-tool registration loop, so every tool discovered
after ``move_head`` — including all four LED spells — was never registered.

Two layers of fix:
  * ``to_gemini`` remaps ``oneOf`` -> ``anyOf`` (genai supports ``anyOf``).
  * ``_add_registered_tool`` builds provider specs defensively, so a single
    tool the SDK still cannot represent is skipped instead of aborting the
    whole server's registration.
"""

import unittest

from tools.core import ToolSchema


class GeminiOneOfRemapTest(unittest.TestCase):
    def test_oneof_schema_no_longer_crashes(self):
        from tools.adapters import gemini as gm

        params = {
            "type": "object",
            "properties": {
                "speed": {
                    "oneOf": [
                        {"enum": ["low", "mid", "high"]},
                        {"type": "integer", "minimum": 1, "maximum": 10000},
                    ]
                }
            },
            "required": [],
        }
        schema = ToolSchema(
            name="stackchan__move_head", description="d", parameters=params,
            result_type="string", spell=True,
        )
        tool = gm.to_gemini(schema)
        speed = tool.function_declarations[0].parameters.properties["speed"]
        # oneOf was renamed to anyOf and preserved (both alternatives kept).
        self.assertIsNotNone(speed.any_of)
        self.assertEqual(len(speed.any_of), 2)

    def test_existing_anyof_not_clobbered(self):
        from tools.adapters.gemini import _remap_oneof_to_anyof

        node = {"anyOf": [{"type": "string"}], "oneOf": [{"type": "integer"}]}
        out = _remap_oneof_to_anyof(node)
        # Pre-existing anyOf must survive; oneOf is left as-is to avoid collision.
        self.assertEqual(out["anyOf"], [{"type": "string"}])
        self.assertIn("oneOf", out)


class RegistrationResilienceTest(unittest.TestCase):
    """A tool whose schema the Gemini SDK still cannot represent must not abort
    registration of the next tool."""

    def setUp(self):
        self._added = []

    def tearDown(self):
        from tools import unregister_external_tool

        for name in self._added:
            unregister_external_tool(name)

    def _register(self, name, params):
        from tools import register_external_tool

        schema = ToolSchema(
            name=name, description="d", parameters=params,
            result_type="string", spell=True,
        )
        ok = register_external_tool(name, schema, lambda **kw: "ok")
        if ok:
            self._added.append(name)
        return ok

    def test_bad_schema_tool_still_registers_and_does_not_abort(self):
        import tools as tools_pkg

        # ``allOf`` is rejected by google-genai and is NOT remapped, so it
        # exercises the defensive path in _add_registered_tool.
        bad_name = "test_resilience__bad_alloff"
        good_name = "test_resilience__good_after_bad"

        # Must not raise, and must return True (tool is callable).
        self.assertTrue(
            self._register(bad_name, {
                "type": "object",
                "properties": {"x": {"allOf": [{"type": "string"}]}},
                "required": [],
            })
        )
        # The bad tool is callable + a spell, but has NO Gemini spec.
        self.assertIn(bad_name, tools_pkg.TOOL_REGISTRY)
        self.assertIn(bad_name, tools_pkg.SPELL_TOOL_NAMES)
        self.assertFalse(self._gemini_spec_has(bad_name))

        # The next tool registered after the bad one is unaffected (no abort)
        # and DOES get a Gemini spec.
        self.assertTrue(
            self._register(good_name, {
                "type": "object",
                "properties": {"y": {"type": "string"}},
                "required": [],
            })
        )
        self.assertIn(good_name, tools_pkg.TOOL_REGISTRY)
        self.assertTrue(self._gemini_spec_has(good_name))

    @staticmethod
    def _gemini_spec_has(name):
        import tools as tools_pkg

        for spec in tools_pkg.GEMINI_TOOLS_SPEC:
            for decl in getattr(spec, "function_declarations", []) or []:
                if getattr(decl, "name", None) == name:
                    return True
        return False


if __name__ == "__main__":
    unittest.main()
