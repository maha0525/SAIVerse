import unittest
from tools.calculator import calculate_expression, get_gemini_tool, get_openai_tool

class TestCalculator(unittest.TestCase):
    def test_calculate_expression(self):
        self.assertEqual(calculate_expression("(3+2-9)*6/2"), -12.0)

    def test_tool_specs(self):
        gem_tool = get_gemini_tool()
        self.assertEqual(gem_tool.function_declarations[0].name, "calculate_expression")
        oa_tool = get_openai_tool()
        self.assertEqual(oa_tool["function"]["name"], "calculate_expression")

if __name__ == '__main__':
    unittest.main()
