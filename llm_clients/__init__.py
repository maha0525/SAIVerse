"""Public API for LLM clients."""
from __future__ import annotations

import requests  # re-exported for backward-compatible test patching

from dotenv import load_dotenv

load_dotenv()

from .anthropic import AnthropicClient
from .base import LLMClient, log_llm_request, log_llm_response, get_llm_logger
from .factory import get_llm_client
from .gemini import (
    GEMINI_SAFETY_CONFIG,
    GROUNDING_TOOL,
    GeminiClient,
    genai,
    merge_tools_for_gemini,
)
from .gemini_utils import build_gemini_clients
from .ollama import OllamaClient
from .openai import OpenAI, OpenAIClient
from .xai import XAIClient
from tools import OPENAI_TOOLS_SPEC

__all__ = [
    "AnthropicClient",
    "GEMINI_SAFETY_CONFIG",
    "GROUNDING_TOOL",
    "GeminiClient",
    "LLMClient",
    "OllamaClient",
    "OpenAIClient",
    "OpenAI",
    "XAIClient",
    "build_gemini_clients",
    "OPENAI_TOOLS_SPEC",
    "log_llm_request",
    "log_llm_response",
    "get_llm_logger",
    "genai",
    "get_llm_client",
    "merge_tools_for_gemini",
    "requests",
]

