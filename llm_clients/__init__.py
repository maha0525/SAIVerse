"""Public API for LLM clients."""
from __future__ import annotations

import requests  # re-exported for backward-compatible test patching

from dotenv import load_dotenv

load_dotenv()

from .anthropic import AnthropicClient
from .base import LLMClient, RAW_LOG_FILE, raw_logger
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
    "build_gemini_clients",
    "OPENAI_TOOLS_SPEC",
    "RAW_LOG_FILE",
    "genai",
    "get_llm_client",
    "merge_tools_for_gemini",
    "raw_logger",
    "requests",
]
