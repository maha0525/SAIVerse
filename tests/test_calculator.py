import unittest

from conftest import load_builtin_tool

calculator = load_builtin_tool("calculator")
calculate_expression = calculator.calculate_expression


class TestCalculator(unittest.TestCase):
    def test_calculate_expression(self):
        self.assertEqual(calculate_expression("(3+2-9)*6/2"), -12.0)

    def test_factorial(self):
        self.assertEqual(calculate_expression("5!"), 120.0)

    def test_power(self):
        self.assertEqual(calculate_expression("2^3"), 8.0)

    def test_tool_registry(self):
        from tools import TOOL_REGISTRY
        self.assertIn("calculate_expression", TOOL_REGISTRY)


if __name__ == '__main__':
    unittest.main()
