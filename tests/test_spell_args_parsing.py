"""Spell args パーサのテスト (sea/runtime_llm.py)。

特に JSON 脳の小文字 true/false/null と Python dict (シングルクォート) の混在を
救う _normalize_json_literals の回帰防止。
"""
import unittest

from sea.runtime_llm import _normalize_json_literals, _parse_spell_args


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


if __name__ == "__main__":
    unittest.main()
