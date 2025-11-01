"""Factory helpers for LLM clients."""
from __future__ import annotations

from typing import Dict

from model_configs import get_model_config

from .anthropic import AnthropicClient
from .gemini import GeminiClient
from .ollama import OllamaClient
from .openai import OpenAIClient
from .base import LLMClient


def _supports_images(provider: str, config: Dict | None) -> bool:
    if isinstance(config, dict) and "supports_images" in config:
        return bool(config["supports_images"])
    return provider == "gemini"


def get_llm_client(model: str, provider: str, context_length: int) -> LLMClient:
    """Factory function to get the appropriate LLM client."""
    config = get_model_config(model)
    supports_images = _supports_images(provider, config if isinstance(config, dict) else None)
    if provider == "openai":
        return OpenAIClient(model, supports_images=supports_images)
    if provider == "anthropic":
        return AnthropicClient(model, config=config, supports_images=supports_images)
    if provider == "gemini":
        return GeminiClient(model, config=config, supports_images=supports_images)
    return OllamaClient(model, context_length, supports_images=supports_images)


__all__ = ["get_llm_client"]
