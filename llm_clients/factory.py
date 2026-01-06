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
from .llama_cpp import LlamaCppClient
from .base import LLMClient


def _supports_images(provider: str, config: Dict | None) -> bool:
    if isinstance(config, dict) and "supports_images" in config:
        return bool(config["supports_images"])
    return provider == "gemini"


def get_llm_client(model: str, provider: str, context_length: int, config: Dict | None = None) -> LLMClient:
    """Factory function to get the appropriate LLM client.
    
    Args:
        model: Model ID (config key, typically filename) to use
        provider: Provider name (openai, anthropic, gemini, ollama, nvidia_nim)
        context_length: Context length for the model
        config: Optional model config dict. If not provided, will be looked up by model ID.
    """
    if config is None:
        config = get_model_config(model)
    
    # Use the actual API model name from config if available
    # This is important because 'model' might be the config key (filename)
    # while config["model"] is the actual API model name
    api_model = model
    if isinstance(config, dict) and "model" in config:
        api_model = config["model"]
        if api_model != model:
            logging.debug("Using API model name '%s' (config key: '%s')", api_model, model)
    
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
                logging.info("Using structured_output_backend='%s' for model '%s'", structured_output_backend, api_model)

        logging.debug("Creating OpenAI client for model '%s' with kwargs: %s", api_model, extra_kwargs)
        client = OpenAIClient(api_model, supports_images=supports_images, **extra_kwargs)
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

        logging.debug("Creating Nvidia NIM client for model '%s' with kwargs: %s", api_model, extra_kwargs)
        client = NvidiaNIMClient(api_model, supports_images=supports_images, **extra_kwargs)
    elif provider == "anthropic":
        client = AnthropicClient(api_model, config=config, supports_images=supports_images)
    elif provider == "gemini":
        client = GeminiClient(api_model, config=config, supports_images=supports_images)
    elif provider == "llama_cpp":
        extra_kwargs: Dict[str, object] = {}
        if isinstance(config, dict):
            # model_path is required for llama.cpp
            model_path = config.get("model_path") or config.get("model")
            if not model_path:
                raise ValueError("llama_cpp provider requires 'model_path' in config")

            # GPU layers (-1 = all, 0 = CPU only)
            n_gpu_layers = config.get("n_gpu_layers", -1)
            if isinstance(n_gpu_layers, int):
                extra_kwargs["n_gpu_layers"] = n_gpu_layers

            # Fallback to Gemini on error (default: True)
            fallback_on_error = config.get("fallback_on_error", True)
            if isinstance(fallback_on_error, bool):
                extra_kwargs["fallback_on_error"] = fallback_on_error
        else:
            # Fallback: use api_model as model_path
            model_path = api_model

        logging.debug("Creating llama.cpp client for model path '%s' with kwargs: %s", model_path, extra_kwargs)
        client = LlamaCppClient(model_path, context_length, supports_images=supports_images, **extra_kwargs)
    else:
        client = OllamaClient(api_model, context_length, supports_images=supports_images)

    parameter_defaults = get_model_parameter_defaults(model)
    if parameter_defaults:
        try:
            client.configure_parameters(parameter_defaults)
        except Exception:
            logging.debug("Failed to apply parameter defaults for model %s", model, exc_info=True)
    return client


__all__ = ["get_llm_client"]
