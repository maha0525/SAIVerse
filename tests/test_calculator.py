import unittest
from pathlib import Path
from tools.calculator import (
    calculate_expression,
    get_gemini_tool,
    get_openai_tool,
    logger,
    call_history,
)
from tools.tool_tracker import get_called_count

class TestCalculator(unittest.TestCase):
    def test_calculate_expression(self):
        self.assertEqual(calculate_expression("(3+2-9)*6/2"), -12.0)

    def test_factorial(self):
        self.assertEqual(calculate_expression("5^"), 120.0)

    def test_call_history_and_file(self):
        log_file = Path("saiverse_log.txt")
        # Logger initialization should create the file
        self.assertTrue(log_file.exists())
        with open(log_file) as f:
            init_content = f.read()
        self.assertIn("calculator logger initialized", init_content)

        size_before_len = len(call_history)
        file_size_before = log_file.stat().st_size
        calculate_expression("1+1")
        self.assertEqual(len(call_history), size_before_len + 1)
        self.assertEqual(call_history[-1], "1+1")
        for h in logger.handlers:
            h.flush()
        with open(log_file) as f:
            f.seek(file_size_before)
            content = f.read()
        self.assertIn("calculate_expression called with: 1+1", content)

    def test_called_count(self):
        prev = get_called_count("calculate_expression")
        calculate_expression("2+2")
        self.assertEqual(get_called_count("calculate_expression"), prev + 1)

    def test_tool_specs(self):
        gem_tool = get_gemini_tool()
        self.assertEqual(gem_tool.function_declarations[0].name, "calculate_expression")
        oa_tool = get_openai_tool()
        self.assertEqual(oa_tool["function"]["name"], "calculate_expression")

if __name__ == '__main__':
    unittest.main()
