"""Gemini latest-contract model definitions and GenerateContent contract tests."""

from __future__ import annotations

from datetime import datetime, timezone
import unittest
from unittest.mock import MagicMock, patch

from google.genai import types

from llm_clients.exceptions import InvalidRequestError
from llm_clients.gemini import GeminiClient, _gemini_version, _is_gemini_3_or_later
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

    def test_argon_definition_matches_the_official_contract(self):
        """Gemini 4 Argon: thinking_level は low/medium/high (既定 medium)、
        sampling は無視される、明示キャッシュ非対応、長考のためタイムアウト 15 分。"""
        config = model_configs.get_model_config("gemini-4-argon")
        self.assertEqual(config["model"], "gemini-4-argon")
        self.assertTrue(config["prefer_paid"])
        self.assertFalse(config["supports_sampling_parameters"])
        self.assertFalse(config["supports_model_prefill"])
        for key in ("temperature", "top_p", "top_k"):
            self.assertNotIn(key, config["parameters"])
        thinking_level = config["parameters"]["thinking_level"]
        self.assertEqual(thinking_level["options"], ["low", "medium", "high"])
        self.assertEqual(thinking_level["default"], "medium")
        self.assertIs(config["cache"]["explicit_supported"], False)
        self.assertEqual(config["timeout"], 900)

    def test_paid_model_pricing_matches_standard_tier(self):
        self.assertAlmostEqual(
            model_configs.calculate_cost("gemini-3.7-flash-paid", 1_000_000, 1_000_000),
            9.0,
        )
        self.assertAlmostEqual(
            model_configs.calculate_cost(
                "gemini-3.6-flash-paid", 1_000_000, 1_000_000,
                at=datetime(2027, 1, 1, tzinfo=timezone.utc),
            ),
            9.0,
        )
        self.assertAlmostEqual(
            model_configs.calculate_cost(
                "gemini-3.5-flash-lite-paid", 1_000_000, 1_000_000
            ),
            2.8,
        )


class TestGeminiGenerationDetection(unittest.TestCase):
    """Gemini 3 以降の判定は版番号で行い、Gemini 4 以降も拾う。"""

    def test_version_is_parsed_from_the_model_name(self):
        self.assertEqual(_gemini_version("gemini-4-argon"), (4, 0))
        self.assertEqual(_gemini_version("gemini-3.8-flash"), (3, 8))
        self.assertEqual(_gemini_version("gemini-3-flash-preview"), (3, 0))
        self.assertEqual(_gemini_version("gemini-2.5-flash"), (2, 5))
        self.assertIsNone(_gemini_version("learnlm-2.0-flash"))
        self.assertIsNone(_gemini_version(""))

    def test_generation_flags_follow_the_version(self):
        expected = {
            "gemini-4-argon": True,
            "gemini-3-flash-preview": True,
            "gemini-3.8-flash": True,
            "gemini-3.1-pro-preview": True,
            "gemini-2.5-flash": False,
            "gemini-2.0-flash": False,
        }
        for model, later in expected.items():
            with self.subTest(model=model):
                client = _make_client(model)
                self.assertIs(client._is_gemini_3_or_later, later)
                self.assertIs(_is_gemini_3_or_later(model), later)

    def test_include_thoughts_is_auto_enabled_for_2_5_and_3_or_later(self):
        expected = {
            "gemini-4-argon": True,
            "gemini-3-flash-preview": True,
            "gemini-3.8-flash": True,
            "gemini-2.5-flash": True,
            "gemini-2.0-flash": False,
        }
        for model, include in expected.items():
            with self.subTest(model=model):
                self.assertIs(_make_client(model)._include_thoughts, include)

    def test_argon_minimal_reasoning_uses_thinking_level(self):
        client = _make_client("gemini-4-argon")
        client.prefer_minimal_reasoning()
        self.assertEqual(client._thinking_level, "low")
        self.assertIsNone(client._thinking_budget)

    def test_argon_keeps_function_call_signature_without_multi_turn_thinking(self):
        """multi_turn_thinking が off でも、3 以降は関数呼び出しの署名を載せ直す。"""
        client = _make_client("gemini-4-argon")
        client.configure_parameters({"multi_turn_thinking": "off"})
        self.assertFalse(client._multi_turn_thinking)
        _, contents = client._convert_messages(
            [
                {"role": "user", "content": "look it up"},
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "tc_1",
                            "type": "function",
                            "function": {"name": "lookup", "arguments": "{}"},
                            "thought_signature": b"sig",
                        }
                    ],
                },
                {
                    "role": "tool",
                    "tool_call_id": "tc_1",
                    "name": "lookup",
                    "content": '{"result": "ok"}',
                },
            ]
        )
        self.assertEqual(contents[1].parts[0].thought_signature, b"sig")


class TestGeminiModelTimeout(unittest.TestCase):
    """モデル設定の timeout (秒) は SDK クライアントの構築へ渡る。"""

    def _built_kwargs(self, config: dict | None) -> dict:
        with patch(
            "llm_clients.gemini.build_gemini_clients",
            return_value=(MagicMock(), MagicMock(), MagicMock()),
        ) as build:
            GeminiClient("gemini-4-argon", config=config)
        return build.call_args.kwargs

    def test_model_timeout_is_passed_to_the_client_builder(self):
        kwargs = self._built_kwargs({"timeout": 900, "prefer_paid": True})
        self.assertEqual(kwargs, {"prefer_paid": True, "timeout_seconds": 900.0})

    def test_missing_or_invalid_timeout_leaves_the_env_default(self):
        for config in ({}, {"timeout": 0}, {"timeout": -5}, {"timeout": "900"}, {"timeout": True}):
            with self.subTest(config=config):
                self.assertNotIn("timeout_seconds", self._built_kwargs(config))


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

    def test_per_call_output_cap_overrides_the_model_config(self):
        """Per-call max_output_tokens wins over the model config, and leaves
        calls that do not pass it untouched.

        The sluice needs to cap exactly one call so a runaway structured output
        cannot burn tens of thousands of tokens
        (docs/issues/sluice_structured_output_digit_loop.md).
        """
        client = _make_client("gemini-3.6-flash")
        client.configure_parameters({"max_output_tokens": 65536})

        capped: dict = {}
        client._apply_generation_parameters(
            capped, temperature=None, max_output_tokens=4096,
        )
        self.assertEqual(capped["max_output_tokens"], 4096)

        uncapped: dict = {}
        client._apply_generation_parameters(uncapped, temperature=None)
        self.assertEqual(uncapped["max_output_tokens"], 65536)

    def test_per_call_output_cap_applies_without_a_model_config(self):
        client = _make_client("gemini-3.6-flash")

        config_kwargs: dict = {}
        client._apply_generation_parameters(
            config_kwargs, temperature=None, max_output_tokens=4096,
        )
        self.assertEqual(config_kwargs["max_output_tokens"], 4096)

    def test_structured_output_parse_failure_keeps_the_raw_body(self):
        """A parse failure inside the SDK must not swallow the response body.

        google-genai parses structured output in
        ``GenerateContentResponse._from_response`` and only catches
        ``JSONDecodeError``; a body that is valid JSON but blows CPython's
        integer-string limit escapes as a bare ValueError, taking the text with
        it. The client lifts the SDK frame's ``result_text`` so the body reaches
        llm_io.log instead of being lost.
        """
        from google.genai import types

        from llm_clients.gemini import _sdk_structured_parse_failure

        # Reproduce the production failure through the real SDK: a 200 response
        # whose text is a digit loop, parsed with a dict response_schema.
        body = '{"reflection": "x", "memory_id": ' + "2" * 65000 + "}"
        response = {
            "candidates": [{
                "content": {"parts": [{"text": body}], "role": "model"},
                "finishReason": "STOP",
            }],
        }
        kwargs = {"config": {"response_schema": {"type": "OBJECT"}}}

        try:
            types.GenerateContentResponse._from_response(
                response=response, kwargs=kwargs,
            )
        except ValueError as exc:
            message = str(exc)
            matched, raw = _sdk_structured_parse_failure(exc)
        else:  # pragma: no cover - the SDK must still fail here
            self.fail("the SDK no longer raises on an oversized integer literal")

        self.assertIn("4300 digits", message)
        self.assertTrue(matched)
        self.assertEqual(raw, body)

        # An unrelated failure is left alone (no frame match, nothing recovered).
        try:
            raise ValueError("unrelated")
        except ValueError as exc:
            self.assertEqual(_sdk_structured_parse_failure(exc), (False, None))

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
