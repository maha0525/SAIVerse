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
    _parse_spell_lines,
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


class TestMultilineArgsRescue(unittest.TestCase):
    """args JSON の文字列値に生改行が入ったケースの救済と、救えないケースの
    malformed 報告 (2026-07-05 実 LLM シム 異常 #2 の回帰防止)。"""

    def test_multiline_json_string_rescued(self):
        # LLM が content 内に生の改行を書いた実例形。行単位の canonical パターン
        # では先頭行で千切れるが、brace 追跡 + 改行の \n 正規化で救済される。
        text = (
            "作業に取りかかる。\n"
            '/spell name=\'document_create\' args={"title": "メモ", '
            '"content": "1行目\n2行目\n\n3行目"}\n'
            "これでよし。"
        )
        malformed = []
        parsed = _parse_spell_lines(text, malformed_out=malformed)
        self.assertEqual(malformed, [])
        self.assertEqual(len(parsed), 1)
        name, args, span, norm = parsed[0]
        self.assertEqual(name, "document_create")
        self.assertEqual(args["title"], "メモ")
        self.assertEqual(args["content"], "1行目\n2行目\n\n3行目")
        # span は閉じ } まで。後続の平文は失われない
        self.assertEqual(text[span.end():].strip(), "これでよし。")
        # 正規化行は 1 行の canonical 形 (SAIMemory に正しい書式が残る)
        self.assertNotIn("\n", norm)
        self.assertTrue(norm.startswith("/spell name='document_create' args={"))

    def test_unclosed_brace_reported_as_malformed(self):
        # brace が閉じない = 救済不能。黙って捨てず malformed_out に報告される
        text = '/spell name=\'document_create\' args={"content": "途中で切れた'
        malformed = []
        parsed = _parse_spell_lines(text, malformed_out=malformed)
        self.assertEqual(parsed, [])
        self.assertEqual(len(malformed), 1)
        name, args_raw, _m = malformed[0]
        self.assertEqual(name, "document_create")
        self.assertIn("途中で切れた", args_raw)

    def test_without_malformed_out_unparseable_is_skipped(self):
        # malformed_out を渡さない既存呼び出し (表示系等) は従来どおり skip
        self.assertEqual(
            _parse_spell_lines("/spell name='x' args={broken", quiet=True), []
        )

    def test_trailing_prose_after_json_on_same_line(self):
        # } の後に同一行で文が続くケース: brace 対応で JSON 部分だけを読む
        text = '/spell name=\'make_doc\' args={"a": 1} と唱えた'
        parsed = _parse_spell_lines(text)
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0][1], {"a": 1})
        self.assertEqual(text[parsed[0][2].end():].strip(), "と唱えた")

    def test_spell_like_line_inside_rescued_args_not_double_parsed(self):
        # 救済した args 本文の中に /spell 行そっくりの文字列があっても、
        # 飲み込んだ範囲内の再マッチは無視される
        text = (
            '/spell name=\'document_create\' args={"content": "スペルの書式:\n'
            "/spell name='calculator' args={\\\"expression\\\": \\\"1+1\\\"}\n"
            'という形で書く"}'
        )
        malformed = []
        parsed = _parse_spell_lines(text, malformed_out=malformed)
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0][0], "document_create")
        self.assertEqual(malformed, [])


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
    def test_task_update_step_quoted_int(self):
        # LLM が step_position を "2" とクオートして渡すと str<int で TypeError になる
        # 既知バグの回帰防止。整数パラメータは coerce で int 化される。
        schema = ToolSchema(
            name="task_update_step",
            description="",
            parameters={
                "type": "object",
                "properties": {
                    "task_ref": {"type": "string"},
                    "step_position": {"type": "integer"},
                    "status": {"type": "string"},
                },
            },
            result_type="string",
        )
        with patch.dict(
            "sea.runtime_llm.SPELL_TOOL_SCHEMAS",
            {"task_update_step": schema},
            clear=False,
        ):
            result = _coerce_spell_args(
                "task_update_step",
                {"task_ref": "task:5", "step_position": "2", "status": "completed"},
            )
        self.assertEqual(
            result, {"task_ref": "task:5", "step_position": 2, "status": "completed"}
        )

    def test_unknown_tool_passthrough(self):
        args = {"x": "2"}
        self.assertEqual(_coerce_spell_args("no_such_tool", args), args)


if __name__ == "__main__":
    unittest.main()
