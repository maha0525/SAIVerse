import unittest
from pathlib import Path
from tools.defs.calculator import calculate_expression, logger
from tools import OPENAI_TOOLS_SPEC, GEMINI_TOOLS_SPEC, TOOL_REGISTRY

class TestCalculator(unittest.TestCase):
    def test_calculate_expression(self):
        self.assertEqual(calculate_expression("(3+2-9)*6/2"), -12.0)

    def test_factorial(self):
        self.assertEqual(calculate_expression("5!"), 120.0)

    def test_power(self):
        self.assertEqual(calculate_expression("2^3"), 8.0)

    def test_logging_file(self):
        log_file = Path("saiverse_log.txt")
        # Logger initialization should create the file
        self.assertTrue(log_file.exists())
        with open(log_file) as f:
            init_content = f.read()
        self.assertIn("calculator logger initialized", init_content)

        file_size_before = log_file.stat().st_size
        calculate_expression("1+1")
        for h in logger.handlers:
            h.flush()
        with open(log_file) as f:
            f.seek(file_size_before)
            content = f.read()
        self.assertIn("calculate_expression called with: 1+1", content)

    def test_tool_specs(self):
        self.assertIn("calculate_expression", TOOL_REGISTRY)
        self.assertEqual(
            OPENAI_TOOLS_SPEC[0]["function"]["name"], "calculate_expression"
        )
        self.assertEqual(
            GEMINI_TOOLS_SPEC[0].function_declarations[0].name,
            "calculate_expression",
        )

if __name__ == '__main__':
    unittest.main()
