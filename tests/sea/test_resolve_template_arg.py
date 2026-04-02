"""Tests for _resolve_template_arg in sea/runtime_utils.py."""

import unittest
from sea.runtime_utils import _resolve_template_arg


class TestResolveTemplateArg(unittest.TestCase):
    """Verify that pure variable references preserve original types."""

    def test_dict_value_preserved(self):
        """A pure {key} reference to a dict should return the dict, not str(dict)."""
        metadata = {"media": [{"type": "image", "uri": "saiverse://image/test.jpg"}]}
        variables = {"metadata": metadata, "input": "hello"}
        result = _resolve_template_arg("{metadata}", variables)
        self.assertIs(result, metadata)
        self.assertIsInstance(result, dict)

    def test_list_value_preserved(self):
        """A pure {key} reference to a list should return the list."""
        items = [1, 2, 3]
        variables = {"items": items}
        result = _resolve_template_arg("{items}", variables)
        self.assertIs(result, items)

    def test_string_value_returned_as_is(self):
        """A pure {key} reference to a string returns the string."""
        variables = {"name": "hello"}
        result = _resolve_template_arg("{name}", variables)
        self.assertEqual(result, "hello")

    def test_none_value_preserved(self):
        """A pure {key} reference to None returns None."""
        variables = {"empty": None}
        result = _resolve_template_arg("{empty}", variables)
        self.assertIsNone(result)

    def test_mixed_template_uses_format(self):
        """Templates with surrounding text should stringify values normally."""
        metadata = {"key": "value"}
        variables = {"metadata": metadata, "name": "test"}
        result = _resolve_template_arg("prefix {name} suffix", variables)
        self.assertEqual(result, "prefix test suffix")
        self.assertIsInstance(result, str)

    def test_missing_key_returns_original_template(self):
        """A reference to a missing key should return the template unchanged."""
        variables = {"other": "value"}
        result = _resolve_template_arg("{missing}", variables)
        self.assertEqual(result, "{missing}")

    def test_empty_string_value(self):
        """A pure {key} reference to an empty string returns empty string."""
        variables = {"text": ""}
        result = _resolve_template_arg("{text}", variables)
        self.assertEqual(result, "")

    def test_int_value_preserved(self):
        """A pure {key} reference to an int returns the int."""
        variables = {"count": 42}
        result = _resolve_template_arg("{count}", variables)
        self.assertEqual(result, 42)
        self.assertIsInstance(result, int)

    def test_dot_notation_key(self):
        """Dot-notation keys like {result.summary} should also work."""
        variables = {"result.summary": "some summary"}
        result = _resolve_template_arg("{result.summary}", variables)
        self.assertEqual(result, "some summary")


if __name__ == "__main__":
    unittest.main()
