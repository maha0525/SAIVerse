"""Gemini latest-contract model definitions and GenerateContent contract tests."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from google.genai import types

from llm_clients.exceptions import InvalidRequestError
from llm_clients.gemini import GeminiClient
from saiverse import model_configs
from sea.runtime_llm import _resolve_tool_call_id


def _make_client(model: str, config: dict | None = None) -> GeminiClient:
    with patch(
        "llm_clients.gemini.build_gemini_clients",
        return_value=(MagicMock(), MagicMock(), MagicMock()),
    ):
        return GeminiClient(model, config=config)


class TestGeminiLatestModelDefinitions(unittest.TestCase):
    def test_new_models_have_free_and_paid_definitions(self):
        expected = {
            "gemini-3.7-flash": ("gemini-3.7-flash", False),
            "gemini-3.7-flash-paid": ("gemini-3.7-flash", True),
            "gemini-3.6-flash": ("gemini-3.6-flash", False),
            "gemini-3.6-flash-paid": ("gemini-3.6-flash", True),
            "gemini-3.5-flash-lite": ("gemini-3.5-flash-lite", False),
            "gemini-3.5-flash-lite-paid": ("gemini-3.5-flash-lite", True),
        }
        for config_key, (api_model, prefer_paid) in expected.items():
            with self.subTest(config_key=config_key):
                config = model_configs.get_model_config(config_key)
                self.assertEqual(config["model"], api_model)
                self.assertEqual(config["context_length"], 1_048_576)
                self.assertEqual(config["prefer_paid"], prefer_paid)
                self.assertFalse(config["supports_sampling_parameters"])
                self.assertFalse(config["supports_model_prefill"])
                self.assertNotIn("temperature", config["parameters"])
                self.assertNotIn("top_p", config["parameters"])
                self.assertNotIn("top_k", config["parameters"])

    def test_thinking_levels_match_each_model_contract(self):
        """3.7 Flash dropped ``minimal``; sending it returns an API error."""
        expected = {
            "gemini-3.7-flash": ["low", "medium", "high"],
            "gemini-3.7-flash-paid": ["low", "medium", "high"],
            "gemini-3.6-flash": ["minimal", "low", "medium", "high"],
            "gemini-3.6-flash-paid": ["minimal", "low", "medium", "high"],
        }
        for config_key, options in expected.items():
            with self.subTest(config_key=config_key):
                thinking_level = model_configs.get_model_config(config_key)["parameters"][
                    "thinking_level"
                ]
                self.assertEqual(thinking_level["options"], options)
                self.assertIn(thinking_level["default"], options)

    def test_paid_model_pricing_matches_standard_tier(self):
        self.assertAlmostEqual(
            model_configs.calculate_cost("gemini-3.7-flash-paid", 1_000_000, 1_000_000),
            9.0,
        )
        self.assertAlmostEqual(
            model_configs.calculate_cost("gemini-3.6-flash-paid", 1_000_000, 1_000_000),
            9.0,
        )
        self.assertAlmostEqual(
            model_configs.calculate_cost(
                "gemini-3.5-flash-lite-paid", 1_000_000, 1_000_000
            ),
            2.8,
        )


class TestGeminiLatestGenerateContentContract(unittest.TestCase):
    def test_runtime_preserves_provider_issued_tool_call_id(self):
        self.assertEqual(
            _resolve_tool_call_id({"tool_call_id": "model_call_456"}),
            "model_call_456",
        )
        self.assertTrue(_resolve_tool_call_id({}).startswith("tc_"))

    def test_sampling_parameters_are_dropped_but_other_parameters_remain(self):
        client = _make_client("gemini-3.6-flash")
        client.configure_parameters(
            {
                "temperature": 0.2,
                "top_p": 0.8,
                "top_k": 20,
                "max_output_tokens": 1234,
                "stop_sequences": ["STOP"],
            }
        )

        config_kwargs: dict = {}
        client._apply_generation_parameters(config_kwargs, temperature=0.7)

        self.assertNotIn("temperature", config_kwargs)
        self.assertNotIn("top_p", config_kwargs)
        self.assertNotIn("top_k", config_kwargs)
        self.assertEqual(config_kwargs["max_output_tokens"], 1234)
        self.assertEqual(config_kwargs["stop_sequences"], ["STOP"])

    def test_older_models_keep_sampling_parameter_support(self):
        client = _make_client("gemini-3.5-flash")
        client.configure_parameters({"top_p": 0.8, "top_k": 20})

        config_kwargs: dict = {}
        client._apply_generation_parameters(config_kwargs, temperature=0.7)

        self.assertEqual(config_kwargs["temperature"], 0.7)
        self.assertEqual(config_kwargs["top_p"], 0.8)
        self.assertEqual(config_kwargs["top_k"], 20)

    def test_non_empty_model_turn_at_end_is_rejected_locally(self):
        client = _make_client("gemini-3.5-flash-lite")
        _, contents = client._convert_messages(
            [
                {"role": "user", "content": "question"},
                {"role": "assistant", "content": "prefill"},
            ]
        )

        with self.assertRaises(InvalidRequestError):
            client._validate_contents(contents)

    def test_empty_model_turn_does_not_hide_preceding_user_turn(self):
        client = _make_client("gemini-3.6-flash")
        _, contents = client._convert_messages(
            [
                {"role": "user", "content": "question"},
                {"role": "assistant", "content": ""},
            ]
        )

        client._validate_contents(contents)

    def test_function_call_and_response_ids_survive_conversion(self):
        client = _make_client("gemini-3.6-flash")
        _, contents = client._convert_messages(
            [
                {"role": "user", "content": "look it up"},
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "tc_123",
                            "type": "function",
                            "function": {
                                "name": "lookup",
                                "arguments": '{"query": "value"}',
                            },
                        }
                    ],
                },
                {
                    "role": "tool",
                    "tool_call_id": "tc_123",
                    "name": "lookup",
                    "content": '{"result": "ok"}',
                },
            ]
        )

        function_call = contents[1].parts[0].function_call
        function_response = contents[2].parts[0].function_response
        self.assertEqual(function_call.id, "tc_123")
        self.assertEqual(function_response.id, "tc_123")
        self.assertEqual(function_call.name, function_response.name)

    def test_model_issued_function_call_id_is_returned_to_runtime(self):
        client = _make_client("gemini-3.6-flash")
        function_call = types.FunctionCall(
            id="model_call_456",
            name="lookup",
            args={"query": "value"},
        )
        candidate = MagicMock()
        candidate.finish_reason = None
        candidate.function_call = None
        candidate.content = types.Content(
            role="model",
            parts=[types.Part(function_call=function_call)],
        )
        response = MagicMock()
        response.prompt_feedback = None
        response.usage_metadata = None
        response.candidates = [candidate]
        client.client.models.generate_content.return_value = response
        tool = types.Tool(
            function_declarations=[
                types.FunctionDeclaration(
                    name="lookup",
                    description="Lookup a value",
                    parameters={"type": "object", "properties": {}},
                )
            ]
        )

        result = client.generate(
            [{"role": "user", "content": "look it up"}],
            tools=[tool],
        )

        self.assertEqual(result["type"], "tool_call")
        self.assertEqual(result["tool_call_id"], "model_call_456")


if __name__ == "__main__":
    unittest.main()
