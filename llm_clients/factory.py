"""Factory helpers for LLM clients."""
from __future__ import annotations

import logging
from typing import Dict

from model_configs import get_model_config, get_model_parameter_defaults

from .anthropic import AnthropicClient
from .gemini import GeminiClient
from .ollama import OllamaClient
from .openai import OpenAIClient
from .nvidia_nim import NvidiaNIMClient
from .base import LLMClient


def _supports_images(provider: str, config: Dict | None) -> bool:
    if isinstance(config, dict) and "supports_images" in config:
        return bool(config["supports_images"])
    return provider == "gemini"


def get_llm_client(model: str, provider: str, context_length: int) -> LLMClient:
    """Factory function to get the appropriate LLM client."""
    config = get_model_config(model)
    supports_images = _supports_images(provider, config if isinstance(config, dict) else None)
    client: LLMClient
    if provider == "openai":
        extra_kwargs: Dict[str, object] = {}
        if isinstance(config, dict):
            base_url = config.get("base_url")
            if isinstance(base_url, str) and base_url.strip():
                extra_kwargs["base_url"] = base_url.strip()

            api_key_env = config.get("api_key_env")
            if isinstance(api_key_env, str) and api_key_env.strip():
                extra_kwargs["api_key_env"] = api_key_env.strip()

            request_kwargs = config.get("request_kwargs")
            if isinstance(request_kwargs, dict):
                extra_kwargs["request_kwargs"] = request_kwargs

            # Convert system messages to user messages if needed for compatibility
            convert_system = config.get("convert_system_to_user")
            if isinstance(convert_system, bool):
                extra_kwargs["convert_system_to_user"] = convert_system

            # Structured output backend (for Nvidia NIM, etc.)
            structured_output_backend = config.get("structured_output_backend")
            if isinstance(structured_output_backend, str):
                extra_kwargs["structured_output_backend"] = structured_output_backend
                logging.info("Using structured_output_backend='%s' for model '%s'", structured_output_backend, model)

        logging.debug("Creating OpenAI client for model '%s' with kwargs: %s", model, extra_kwargs)
        client = OpenAIClient(model, supports_images=supports_images, **extra_kwargs)
    elif provider == "nvidia_nim":
        extra_kwargs: Dict[str, object] = {}
        if isinstance(config, dict):
            base_url = config.get("base_url")
            if isinstance(base_url, str) and base_url.strip():
                extra_kwargs["base_url"] = base_url.strip()

            api_key_env = config.get("api_key_env")
            if isinstance(api_key_env, str) and api_key_env.strip():
                extra_kwargs["api_key_env"] = api_key_env.strip()

            request_kwargs = config.get("request_kwargs")
            if isinstance(request_kwargs, dict):
                extra_kwargs["request_kwargs"] = request_kwargs

            convert_system = config.get("convert_system_to_user")
            if isinstance(convert_system, bool):
                extra_kwargs["convert_system_to_user"] = convert_system

        logging.debug("Creating Nvidia NIM client for model '%s' with kwargs: %s", model, extra_kwargs)
        client = NvidiaNIMClient(model, supports_images=supports_images, **extra_kwargs)
    elif provider == "anthropic":
        client = AnthropicClient(model, config=config, supports_images=supports_images)
    elif provider == "gemini":
        client = GeminiClient(model, config=config, supports_images=supports_images)
    else:
        client = OllamaClient(model, context_length, supports_images=supports_images)

    parameter_defaults = get_model_parameter_defaults(model)
    if parameter_defaults:
        try:
            client.configure_parameters(parameter_defaults)
        except Exception:
            logging.debug("Failed to apply parameter defaults for model %s", model, exc_info=True)
    return client


__all__ = ["get_llm_client"]
