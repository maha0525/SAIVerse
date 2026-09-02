"""Every SAIVerse ``generate_content`` call site disables the SDK's automatic function calling.

SAIVerse dispatches function calls itself. Since google-genai 2.18,
``Models.generate_content`` logs "Direct use of automatic function calling (AFC)
... is not recommended" once per process unless
``automatic_function_calling.disable=True`` is set explicitly -- even when no
tools are passed at all. ``llm_clients/gemini.py`` always disabled it; these tests
pin the SDK behaviour and the three side call sites that were brought in line
when google-genai moved 1.75 -> 2.21 (docs/intent/dependency_management.md §3-4).
"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch

from google import genai
from google.genai import models, types


def _afc_disabled(config: types.GenerateContentConfig) -> bool:
    afc = config.automatic_function_calling
    return afc is not None and afc.disable is True


def _fake_generate_content(self, *, model, contents, config=None):  # noqa: ARG001
    return types.GenerateContentResponse()


def test_sdk_logs_afc_warning_unless_disabled(monkeypatch, caplog):
    """The SDK contract the call-site tests rely on (no network: the HTTP layer is stubbed)."""
    monkeypatch.setattr(models.Models, "_generate_content", _fake_generate_content)
    client = genai.Client(api_key="dummy")

    monkeypatch.setattr(models.Models, "_logged_afc_warning", False)
    with caplog.at_level(logging.WARNING, logger="google_genai.models"):
        client.models.generate_content(
            model="gemini-test",
            contents="hi",
            config=types.GenerateContentConfig(
                automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
            ),
        )
    assert "automatic function calling" not in caplog.text.lower()

    caplog.clear()
    monkeypatch.setattr(models.Models, "_logged_afc_warning", False)
    with caplog.at_level(logging.WARNING, logger="google_genai.models"):
        client.models.generate_content(model="gemini-test", contents="hi")
    assert "Direct use of automatic function calling" in caplog.text


@patch("saiverse.llm_router.client")
def test_llm_router_disables_afc(mock_client):
    from saiverse.llm_router import route
    from tools import OPENAI_TOOLS_SPEC

    part = MagicMock()
    part.text = '{"call":"no","tool":"","args":{}}'
    cand = MagicMock()
    cand.text = None
    cand.content.parts = [part]
    resp = MagicMock()
    resp.candidates = [cand]
    mock_client.models.generate_content.return_value = resp

    route("hello", OPENAI_TOOLS_SPEC)

    config = mock_client.models.generate_content.call_args.kwargs["config"]
    assert _afc_disabled(config)


def test_emotion_module_disables_afc(tmp_path):
    from persona.emotion_module import EmotionControlModule

    prompt = tmp_path / "emotion_control.txt"
    prompt.write_text("{user_message}|{assistant_message}", encoding="utf-8")
    client = MagicMock()
    resp = MagicMock()
    resp.text = '{"stability": {"mean": 0.1}}'
    client.models.generate_content.return_value = resp

    with patch("persona.emotion_module.build_gemini_clients", return_value=(client, None, client)):
        module = EmotionControlModule(prompt_path=prompt, model="gemini-test")
    assert module.evaluate("u", "a") == {"stability": {"mean": 0.1}}

    config = client.models.generate_content.call_args.kwargs["config"]
    assert _afc_disabled(config)


def test_image_generator_disables_afc():
    from tool_loader import load_builtin_tool

    mod = load_builtin_tool("image_generator")
    client = MagicMock()
    part = MagicMock()
    part.inline_data.data = b"png-bytes"
    part.inline_data.mime_type = "image/png"
    cand = MagicMock()
    cand.content.parts = [part]
    resp = MagicMock()
    resp.candidates = [cand]
    resp.prompt_feedback = None
    client.models.generate_content.return_value = resp

    with patch("llm_clients.gemini_utils.build_gemini_clients", return_value=(None, client, client)):
        for generate in (mod._generate_with_nano_banana_2, mod._generate_with_nano_banana_pro):
            client.models.generate_content.reset_mock()
            assert generate("a cat") == (b"png-bytes", "image/png")
            config = client.models.generate_content.call_args.kwargs["config"]
            assert _afc_disabled(config), generate.__name__
