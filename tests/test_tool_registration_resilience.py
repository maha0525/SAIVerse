"""Regression tests for MCP tool registration resilience.

Root cause this pins (the missing Stack-chan LED spells):
the raw ``move_head`` MCP tool's JSON schema uses ``oneOf`` for its ``speed``
parameter. ``tools.adapters.gemini.to_gemini`` fed that straight into
``google.genai.types.Schema``, which rejects ``oneOf`` as ``extra_forbidden``.
That exception aborted the per-tool registration loop, so every tool discovered
after ``move_head`` — including all four LED spells — was never registered.

Two layers of fix:
  * ``to_gemini`` remaps ``oneOf`` -> ``anyOf`` (genai supports ``anyOf``)
    and drops / rewrites other keywords ``types.Schema`` cannot hold.
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
        from tools.adapters.gemini import _sanitize_schema

        node = {"anyOf": [{"type": "string"}], "oneOf": [{"type": "integer"}]}
        dropped = []
        out = _sanitize_schema(node, dropped)
        # Pre-existing anyOf must survive; the colliding oneOf is dropped
        # (types.Schema has no oneOf field, keeping it would fail the tool).
        self.assertEqual(out["anyOf"], [{"type": "string"}])
        self.assertNotIn("oneOf", out)
        self.assertEqual(dropped, ["oneOf"])


class GeminiUnsupportedKeywordTest(unittest.TestCase):
    """Real shapes that used to make ``to_gemini`` raise, so the whole tool was
    missing for Gemini personas (startup WARNING "to_gemini failed ...")."""

    @staticmethod
    def _convert(params):
        from tools.adapters import gemini as gm

        schema = ToolSchema(
            name="t", description="d", parameters=params,
            result_type="string", spell=True,
        )
        return gm.to_gemini(schema).function_declarations[0].parameters

    def test_stackchan_follow_pose_stream_shape(self):
        # stackchan-mcp 0.18.0: integer enum + exclusiveMinimum.
        params = self._convert({
            "type": "object",
            "properties": {
                "flip_yaw": {"type": "integer", "enum": [-1, 1], "default": 1,
                             "description": "Yaw multiplier."},
                "downsample_hz": {"type": "number", "exclusiveMinimum": 0,
                                  "maximum": 20},
            },
        })
        flip = params.properties["flip_yaw"]
        self.assertIsNone(flip.enum)
        self.assertEqual(flip.description, "Yaw multiplier. Allowed values: -1, 1.")
        hz = params.properties["downsample_hz"]
        self.assertEqual(hz.maximum, 20)
        self.assertEqual(hz.description, "Must be greater than 0.")

    def test_string_const_becomes_single_enum(self):
        # elyth perform_field_action: anyOf variants discriminated by const.
        params = self._convert({
            "type": "object",
            "anyOf": [
                {"type": "object",
                 "properties": {"action": {"type": "string", "const": a}}}
                for a in ("move", "sit")
            ],
        })
        self.assertEqual(
            [v.properties["action"].enum for v in params.any_of],
            [["move"], ["sit"]],
        )

    def test_nullable_type_list(self):
        params = self._convert({
            "type": "object",
            "properties": {"n": {"type": ["integer", "null"]}},
        })
        self.assertTrue(params.properties["n"].nullable)

    def test_property_named_like_keyword_is_kept(self):
        # Keys inside "properties" are parameter names, not schema keywords.
        params = self._convert({
            "type": "object",
            "properties": {"const": {"type": "string"},
                           "oneOf": {"type": "string"}},
        })
        self.assertEqual(set(params.properties), {"const", "oneOf"})

    def test_type_list_does_not_clobber_union(self):
        # type list + oneOf on the same node: the explicit union must survive
        # regardless of key order.
        for order in ("type_first", "oneof_first"):
            alts = [{"type": "string"}, {"type": "integer"}]
            node = (
                {"type": ["string", "integer", "null"], "oneOf": alts}
                if order == "type_first"
                else {"oneOf": alts, "type": ["string", "integer", "null"]}
            )
            params = self._convert({"type": "object", "properties": {"v": node}})
            v = params.properties["v"]
            self.assertEqual(len(v.any_of), 2, order)
            self.assertTrue(v.nullable, order)

    def test_tuple_items_become_anyof(self):
        params = self._convert({
            "type": "object",
            "properties": {"pair": {"type": "array",
                                    "items": [{"type": "string"}, {"type": "integer"}]}},
        })
        self.assertEqual(len(params.properties["pair"].items.any_of), 2)

    def test_defs_ref_is_inlined(self):
        # pydantic-generated MCP schemas: nested model via $defs / $ref.
        params = self._convert({
            "type": "object",
            "$defs": {"Color": {"type": "object",
                                "properties": {"r": {"type": "integer"}}}},
            "properties": {"color": {"$ref": "#/$defs/Color",
                                     "description": "LED color"}},
        })
        color = params.properties["color"]
        self.assertEqual(color.description, "LED color")
        self.assertIn("r", color.properties)

    def test_self_referencing_ref_terminates(self):
        params = self._convert({
            "type": "object",
            "$defs": {"Node": {"type": "object",
                               "properties": {"child": {"$ref": "#/$defs/Node"}}}},
            "properties": {"root": {"$ref": "#/$defs/Node"}},
        })
        self.assertIn("child", params.properties["root"].properties)

    def test_non_string_enum_note_uses_json_spelling(self):
        params = self._convert({
            "type": "object",
            "properties": {"x": {"enum": ["a", None, True]}},
        })
        self.assertEqual(params.properties["x"].description,
                         'Allowed values: "a", null, true.')


class GeminiResponseSchemaEnumTest(unittest.TestCase):
    """Sibling path: structured-output response_schema. Non-string enum/const
    cannot go through types.Schema, so it must be sent as raw JSON Schema."""

    def test_non_string_enum_routes_to_json_schema(self):
        from llm_clients.gemini import GeminiClient

        self.assertTrue(GeminiClient._requires_json_schema({
            "type": "object",
            "properties": {"level": {"type": "integer", "enum": [1, 2, 3]}},
        }))
        self.assertTrue(GeminiClient._requires_json_schema({
            "type": "object",
            "properties": {"flag": {"const": True}},
        }))
        self.assertFalse(GeminiClient._requires_json_schema({
            "type": "object",
            "properties": {"kind": {"type": "string", "enum": ["a", "b"]}},
        }))


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

        # A known keyword with a value types.Schema cannot hold (minLength must
        # be an int) survives sanitizing and still fails, so it exercises the
        # defensive path in _add_registered_tool.
        bad_name = "test_resilience__bad_minlength"
        good_name = "test_resilience__good_after_bad"

        # Must not raise, and must return True (tool is callable).
        self.assertTrue(
            self._register(bad_name, {
                "type": "object",
                "properties": {"x": {"type": "string", "minLength": "many"}},
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
