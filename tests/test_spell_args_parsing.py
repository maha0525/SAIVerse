"""Spell args パーサのテスト (sea/runtime_llm.py)。

特に JSON 脳の小文字 true/false/null と Python dict (シングルクォート) の混在を
救う _normalize_json_literals の回帰防止。
"""
import unittest
from unittest.mock import patch

from sea.runtime_llm import (
    _coerce_arg_to_type,
    _coerce_spell_args,
    _normalize_json_literals,
    _parse_spell_args,
)
from tools.core import ToolSchema


class TestSpellArgsParsing(unittest.TestCase):
    def test_python_dict(self):
        self.assertEqual(_parse_spell_args("{'a': 1, 'b': 'x'}"), {"a": 1, "b": "x"})

    def test_pure_json(self):
        self.assertEqual(_parse_spell_args('{"activate": true}'), {"activate": True})

    def test_single_quote_dict_with_lowercase_bool(self):
        # まはーの頻出ケース: シングルクォート dict + 小文字 true/false/null
        result = _parse_spell_args(
            "{'activate': true, 'x': false, 'y': null}"
        )
        self.assertEqual(result, {"activate": True, "x": False, "y": None})

    def test_string_value_true_is_preserved(self):
        # 文字列値内の "true" は変換されない (tokenize の STRING トークン保護)
        result = _parse_spell_args("{'title': 'this is true', 'flag': true}")
        self.assertEqual(result, {"title": "this is true", "flag": True})

    def test_track_create_realistic(self):
        result = _parse_spell_args(
            "{'track_type': 'autonomous', 'title': 'Webリサーチ', "
            "'activate': true, 'entry_line_role': 'sub_line'}"
        )
        self.assertEqual(
            result,
            {
                "track_type": "autonomous",
                "title": "Webリサーチ",
                "activate": True,
                "entry_line_role": "sub_line",
            },
        )

    def test_invalid_returns_none(self):
        self.assertIsNone(_parse_spell_args("not a dict at all"))

    def test_non_dict_returns_none(self):
        self.assertIsNone(_parse_spell_args("[1, 2, 3]"))

    def test_normalize_noop_when_no_literals(self):
        src = "{'a': 1}"
        self.assertEqual(_normalize_json_literals(src), src)


class TestCoerceArgToType(unittest.TestCase):
    def test_integer_from_string(self):
        self.assertEqual(_coerce_arg_to_type("2", "integer"), 2)

    def test_number_from_string(self):
        self.assertEqual(_coerce_arg_to_type("0.5", "number"), 0.5)

    def test_boolean_from_string(self):
        self.assertIs(_coerce_arg_to_type("true", "boolean"), True)
        self.assertIs(_coerce_arg_to_type("false", "boolean"), False)

    def test_already_correct_type_untouched(self):
        self.assertEqual(_coerce_arg_to_type(2, "integer"), 2)
        self.assertIs(_coerce_arg_to_type(True, "boolean"), True)

    def test_unconvertible_string_preserved(self):
        # 数値化できない文字列はそのまま (tool 側バリデーションに委ねる)
        self.assertEqual(_coerce_arg_to_type("abc", "integer"), "abc")

    def test_string_type_untouched(self):
        self.assertEqual(_coerce_arg_to_type("t:13", "string"), "t:13")


class TestCoerceSpellArgs(unittest.TestCase):
    def test_track_task_done_quoted_index(self):
        # air_city_a の実バグ: LLM が index を "2" とクオートして TypeError
        schema = ToolSchema(
            name="track_task_done",
            description="",
            parameters={
                "type": "object",
                "properties": {
                    "track_id": {"type": "string"},
                    "index": {"type": "integer"},
                },
            },
            result_type="string",
        )
        with patch.dict(
            "sea.runtime_llm.SPELL_TOOL_SCHEMAS",
            {"track_task_done": schema},
            clear=False,
        ):
            result = _coerce_spell_args(
                "track_task_done", {"track_id": "t:13", "index": "2"}
            )
        self.assertEqual(result, {"track_id": "t:13", "index": 2})

    def test_unknown_tool_passthrough(self):
        args = {"x": "2"}
        self.assertEqual(_coerce_spell_args("no_such_tool", args), args)


if __name__ == "__main__":
    unittest.main()
