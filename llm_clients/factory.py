"""Factory helpers for LLM clients."""
from __future__ import annotations

import logging
import os
from typing import Dict

from saiverse import model_configs as _model_configs
from saiverse.model_configs import get_model_config, get_model_parameter_defaults

from .anthropic import AnthropicClient
from .gemini import GeminiClient
from .ollama import OllamaClient
from .openai import OpenAIClient
from .openai_codex import OpenAICodexClient
from .nvidia_nim import NvidiaNIMClient
from .xai import XAIClient
from .base import LLMClient


# Map legacy 'provider' field values to the new 'protocol' field. Used when
# a model config predates the provider/protocol split (no explicit protocol).
# Sent as the API key to local servers that declare api_key_required: false.
# The value is never checked by those servers; the OpenAI SDK just requires
# the field to be non-empty.
_LOCAL_SERVER_PLACEHOLDER_KEY = "local-server-no-auth"


_LEGACY_PROVIDER_TO_PROTOCOL = {
    "openai": "openai_compat",
    "ollama": "ollama_compat",
    "anthropic": "anthropic_native",
    "gemini": "gemini_native",
    "xai": "xai_native",
    "nvidia_nim": "nvidia_nim",
    "openai_codex": "openai_codex",
}


def _resolve_protocol(provider: str, config: Dict | None) -> str:
    """Pick the protocol to dispatch on.

    Priority:
        1. config['protocol'] if explicitly set (new schema)
        2. Map legacy provider name to protocol
        3. Fall back to provider as-is (unknown providers raise downstream)
    """
    if isinstance(config, dict):
        explicit = config.get("protocol")
        if isinstance(explicit, str) and explicit:
            return explicit
    return _LEGACY_PROVIDER_TO_PROTOCOL.get(provider, provider)


def _supports_images(provider: str, config: Dict | None) -> bool:
    if isinstance(config, dict) and "supports_images" in config:
        return bool(config["supports_images"])
    return provider == "gemini"


def _read_default_headers(config: Dict | None) -> Dict | None:
    """Read ``default_headers`` off a resolved model config.

    Only the shape is checked here. Which entries are admissible is decided at
    the client boundary (``llm_clients/openai.py: _strip_reserved_headers``),
    because per-request ``extra_headers`` is the same door onto the credential
    and has to pass the same gate — a second copy of the rule here would drift
    away from that one.
    """
    if not isinstance(config, dict):
        return None
    raw = config.get("default_headers")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        logging.warning(
            "[factory] default_headers must be an object of string pairs, got %s; ignoring",
            type(raw).__name__,
        )
        return None
    return raw or None


def _loggable_kwargs(kwargs: Dict[str, object]) -> Dict[str, object]:
    """Copy of ``kwargs`` with header *values* replaced by their key names.

    This log line runs before the client boundary drops inadmissible entries,
    and the user runs at DEBUG — so a config that (wrongly) carries a token in
    ``Authorization`` would have it written to a persisted log even though the
    request never sends it. Key names are enough to debug a header problem.
    """
    safe = dict(kwargs)
    for field in ("default_headers", "request_kwargs"):
        value = safe.get(field)
        if not isinstance(value, dict):
            continue
        if field == "default_headers":
            safe[field] = f"<{len(value)} headers: {sorted(value)}>"
        elif "extra_headers" in value:
            # Mask whatever shape it has: a string here would print in full.
            headers = value["extra_headers"]
            inner = dict(value)
            inner["extra_headers"] = (
                f"<{len(headers)} headers: {sorted(headers)}>"
                if isinstance(headers, dict)
                else f"<{type(headers).__name__}, not an object>"
            )
            safe[field] = inner
    return safe


def _config_key_looks_wrong(model: str, config: Dict | None) -> str | None:
    """``model`` を設定キーとして受け取ってよいか検分し、疑わしければ理由を返す。

    第一引数はそのまま ``client.config_key`` になり、価格引き当ての正典として使われる
    (docs/intent/model_provider_management.md「使用量の帰属」)。設定キーのつもりで
    API モデル名を渡すと、その名前を持つ別設定の単価が使用量に付く。

    見るのは二つ。

    1. **設定キーとして未知で、その名前を API 名に持つ設定がある** — 取り違えの疑い。
    2. **設定キーとしては実在するが、渡された config がその設定と食い違う** —
       呼び出し側は同じ API 名を持つ別の設定を意図している。2026-08-01 に実害が出た
       ``scripts/`` の呼び出しがこの形だった (``gpt-5.6-terra`` は従量課金版のキーとして
       実在するので 1 では捕まらないが、渡していた config は ``provider_ref`` が
       ``openai_codex`` で、キーに登録された設定の ``openai`` と食い違う)。

    ``MODEL_CONFIGS`` はモジュール属性として都度読む。``reload_configs()`` が新しい
    辞書へ再束縛するため、import 時の辞書を掴むと再読込後に古い一覧で判定してしまう。

    残る死角は「model 文字列だけが渡され、config からも意図が読めない」場合。そこは
    呼び出し側と設定の保存境界で防ぐしかない
    (docs/issues/usage_pricing_lookup_falls_back_to_api_name.md)。
    """
    if not model:
        return None
    configs = _model_configs.MODEL_CONFIGS
    known = configs.get(model)

    if known is None:
        shares_api_name = any(
            isinstance(cfg, dict) and cfg.get("model") == model
            for cfg in configs.values()
        )
        if shares_api_name:
            return "it is an API model name shared by another model config"
        return None

    if isinstance(config, dict) and isinstance(known, dict) and config is not known:
        for field in ("provider_ref", "provider"):
            passed, registered = config.get(field), known.get(field)
            if passed and registered and passed != registered:
                return (
                    f"the passed config has {field}={passed!r} but the config registered "
                    f"under this key has {field}={registered!r}"
                )
    return None


def get_llm_client(model: str, provider: str, context_length: int, config: Dict | None = None) -> LLMClient:
    """Factory function to get the appropriate LLM client.
    
    Args:
        model: Model ID (config key, typically filename) to use
        provider: Provider name (openai, anthropic, gemini, ollama, nvidia_nim)
        context_length: Context length for the model
        config: Optional model config dict. If not provided, will be looked up by model ID.
    """
    logging.info("[factory] get_llm_client called: model=%s, provider=%s", model, provider)
    
    if config is None:
        config = get_model_config(model)
        logging.info("[factory] Loaded config for model '%s': %s", model, "found" if config else "NOT FOUND")

    if isinstance(config, dict):
        from saiverse.provider_security import validate_model_config_connection

        validate_model_config_connection(model, config)
    
    # Use the actual API model name from config if available
    # This is important because 'model' might be the config key (filename)
    # while config["model"] is the actual API model name
    api_model = model
    if isinstance(config, dict) and "model" in config:
        api_model = config["model"]
        logging.info("[factory] Extracted api_model from config: '%s' (original model param: '%s')", api_model, model)
        if api_model != model:
            logging.debug("Using API model name '%s' (config key: '%s')", api_model, model)
    
    supports_images = _supports_images(provider, config if isinstance(config, dict) else None)
    protocol = _resolve_protocol(provider, config if isinstance(config, dict) else None)
    logging.debug("[factory] resolved protocol=%s for model=%s (provider=%s)", protocol, model, provider)
    client: LLMClient
    if protocol == "openai_compat":
        extra_kwargs: Dict[str, object] = {}
        if isinstance(config, dict):
            base_url = config.get("base_url")
            if isinstance(base_url, str) and base_url.strip():
                extra_kwargs["base_url"] = base_url.strip()

            api_key_env = config.get("api_key_env")
            if isinstance(api_key_env, str) and api_key_env.strip():
                extra_kwargs["api_key_env"] = api_key_env.strip()

            # Local OpenAI-compatible servers (LM Studio, llama.cpp server)
            # accept any key. Without this the client would demand a real
            # OPENAI_API_KEY and refuse to start, so supply a placeholder —
            # but only when the user has not set a key of their own, since
            # these servers can be put behind real auth.
            if config.get("api_key_required") is False:
                configured_key = os.getenv(api_key_env) if api_key_env else None
                if not configured_key:
                    extra_kwargs["api_key"] = _LOCAL_SERVER_PLACEHOLDER_KEY

            request_kwargs = config.get("request_kwargs")
            if isinstance(request_kwargs, dict):
                extra_kwargs["request_kwargs"] = request_kwargs

            max_image_bytes = config.get("max_image_bytes")
            if isinstance(max_image_bytes, int) and max_image_bytes > 0:
                extra_kwargs["max_image_bytes"] = max_image_bytes

            max_image_embeds = config.get("max_image_embeds")
            if isinstance(max_image_embeds, int) and max_image_embeds > 0:
                extra_kwargs["max_image_embeds"] = max_image_embeds

            # Convert system messages to user messages if needed for compatibility
            convert_system = config.get("convert_system_to_user")
            if isinstance(convert_system, bool):
                extra_kwargs["convert_system_to_user"] = convert_system

            # Structured output backend (for Nvidia NIM, etc.)
            structured_output_backend = config.get("structured_output_backend")
            if isinstance(structured_output_backend, str):
                extra_kwargs["structured_output_backend"] = structured_output_backend
                logging.info("Using structured_output_backend='%s' for model '%s'", structured_output_backend, api_model)

            # Structured output mode: "native" (default) or "json_object" (prompt-based schema)
            structured_output_mode = config.get("structured_output_mode")
            if isinstance(structured_output_mode, str) and structured_output_mode.strip():
                extra_kwargs["structured_output_mode"] = structured_output_mode.strip()
                logging.info("Using structured_output_mode='%s' for model '%s'", structured_output_mode, api_model)

            # Multi-turn reasoning pass-back field (e.g., "reasoning_details" for OpenRouter)
            reasoning_passback = config.get("reasoning_passback_field")
            if isinstance(reasoning_passback, str) and reasoning_passback.strip():
                extra_kwargs["reasoning_passback_field"] = reasoning_passback.strip()

            # Custom timeout (seconds) for slow local models
            timeout = config.get("timeout")
            if isinstance(timeout, (int, float)) and timeout > 0:
                extra_kwargs["timeout"] = float(timeout)
                logging.info("Using custom timeout=%.0fs for model '%s'", float(timeout), api_model)

            default_headers = _read_default_headers(config)
            if default_headers:
                extra_kwargs["default_headers"] = default_headers

        # Default max_image_bytes for OpenAI provider: 5MB.  OpenAI APIs
        # accept up to 20MB but very large images (e.g. 18MB) cause vision
        # recognition failures despite 200 OK responses.  Resizing client-
        # side is cheap and keeps base64 payloads reasonable.
        if supports_images and "max_image_bytes" not in extra_kwargs:
            extra_kwargs["max_image_bytes"] = 5 * 1024 * 1024

        if isinstance(config, dict) and config.get("llama_server"):
            from .llama_server import get_server_manager
            server_base = config.get("base_url", "http://127.0.0.1:8080/v1")
            get_server_manager().ensure_running(server_base, config)

        logging.debug(
            "Creating OpenAI client for model '%s' with kwargs: %s",
            api_model, _loggable_kwargs(extra_kwargs),
        )
        client = OpenAIClient(api_model, supports_images=supports_images, **extra_kwargs)

        # Wrap with llama.cpp slot cache if configured
        if isinstance(config, dict) and config.get("llama_slot_save_path"):
            from .llama_cache import LlamaCacheManager, LlamaCachedClient
            slot_save_path = str(config["llama_slot_save_path"])
            parallel = int(config.get("llama_parallel", 1))
            base_url = str(config.get("base_url", "http://127.0.0.1:8080"))
            cache_mgr = LlamaCacheManager(base_url, slot_save_path, parallel)
            client = LlamaCachedClient(client, cache_mgr)
            logging.info(
                "[factory] LlamaCachedClient enabled: slot_save_path=%s, parallel=%d",
                slot_save_path, parallel,
            )
    elif protocol == "nvidia_nim":
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

            max_image_bytes = config.get("max_image_bytes")
            if isinstance(max_image_bytes, int) and max_image_bytes > 0:
                extra_kwargs["max_image_bytes"] = max_image_bytes

            max_image_embeds = config.get("max_image_embeds")
            if isinstance(max_image_embeds, int) and max_image_embeds > 0:
                extra_kwargs["max_image_embeds"] = max_image_embeds

            convert_system = config.get("convert_system_to_user")
            if isinstance(convert_system, bool):
                extra_kwargs["convert_system_to_user"] = convert_system

            reasoning_passback = config.get("reasoning_passback_field")
            if isinstance(reasoning_passback, str) and reasoning_passback.strip():
                extra_kwargs["reasoning_passback_field"] = reasoning_passback.strip()

            default_headers = _read_default_headers(config)
            if default_headers:
                extra_kwargs["default_headers"] = default_headers

        logging.debug(
            "Creating Nvidia NIM client for model '%s' with kwargs: %s",
            api_model, _loggable_kwargs(extra_kwargs),
        )
        client = NvidiaNIMClient(api_model, supports_images=supports_images, **extra_kwargs)
    elif protocol == "anthropic_native":
        client = AnthropicClient(api_model, config=config, supports_images=supports_images)
    elif protocol == "gemini_native":
        logging.info("[factory] Creating GeminiClient with api_model='%s'", api_model)
        client = GeminiClient(api_model, config=config, supports_images=supports_images)
    elif protocol == "xai_native":
        extra_kwargs: Dict[str, object] = {}
        if isinstance(config, dict):
            api_key_env = config.get("api_key_env")
            if isinstance(api_key_env, str) and api_key_env.strip():
                extra_kwargs["api_key_env"] = api_key_env.strip()

            reasoning_effort = config.get("reasoning_effort")
            if isinstance(reasoning_effort, str) and reasoning_effort.strip():
                extra_kwargs["reasoning_effort"] = reasoning_effort.strip()

        if supports_images and "max_image_bytes" not in extra_kwargs:
            extra_kwargs["max_image_bytes"] = 5 * 1024 * 1024

        logging.debug("Creating xAI client for model '%s' with kwargs: %s", api_model, extra_kwargs)
        client = XAIClient(api_model, supports_images=supports_images, **extra_kwargs)
    elif protocol == "ollama_compat":
        extra_kwargs: Dict[str, object] = {}
        if isinstance(config, dict):
            base_url = config.get("base_url")
            if isinstance(base_url, str) and base_url.strip():
                extra_kwargs["base_url"] = base_url.strip()

            request_kwargs = config.get("request_kwargs")
            if isinstance(request_kwargs, dict):
                extra_kwargs["request_kwargs"] = request_kwargs

        logging.debug("Creating Ollama client for model '%s' with kwargs: %s", api_model, extra_kwargs)
        client = OllamaClient(api_model, context_length, supports_images=supports_images, **extra_kwargs)
    elif protocol == "openai_codex":
        extra_kwargs: Dict[str, object] = {}
        if isinstance(config, dict):
            timeout = config.get("timeout")
            if isinstance(timeout, (int, float)) and timeout > 0:
                extra_kwargs["timeout"] = float(timeout)

        logging.debug("Creating OpenAI Codex client for model '%s' with kwargs: %s", api_model, extra_kwargs)
        client = OpenAICodexClient(api_model, supports_images=supports_images, **extra_kwargs)
    else:
        raise ValueError(
            f"Unknown protocol '{protocol}' (resolved from provider='{provider}') for model '{model}'. "
            f"Valid protocols: openai_compat, openai_codex, nvidia_nim, anthropic_native, "
            f"gemini_native, xai_native, ollama_compat"
        )

    # default_headers is inherited by any model regardless of protocol, but only
    # the two protocols above send it. Silence here would let a hand-written
    # config look like it took effect when nothing carries it.
    if (
        isinstance(config, dict)
        and config.get("default_headers")
        and protocol not in ("openai_compat", "nvidia_nim")
    ):
        logging.warning(
            "[factory] model '%s' sets default_headers but protocol '%s' does not "
            "send them; the entries are ignored.", model, protocol,
        )

    # Set config_key for pricing lookup (model param is the config key/filename)
    #
    # 呼び出し側が設定キーでなく API モデル名を渡すと、その名前が config_key になり、
    # 同じ API 名を持つ別設定 (Codex 版と従量課金版など) の単価で使用量が記録される
    # (docs/intent/model_provider_management.md「使用量の帰属」)。2026-08-01 に
    # scripts/ の 6 箇所が実際にこの形だった。呼び出し側の変数名やディレクトリに
    # 依存せずに検出できるのはこの境界だけなので、ここで警告する。
    suspect_reason = _config_key_looks_wrong(model, config)
    if suspect_reason:
        logging.warning(
            "[factory] model='%s' does not look like the right config key: %s. "
            "Usage will be attributed to that name and priced accordingly. "
            "Pass the config key (the model JSON filename) of the config you mean.",
            model, suspect_reason,
        )
    client.config_key = model
    logging.debug("[factory] Set client.config_key='%s' for pricing lookup", model)

    parameter_defaults = get_model_parameter_defaults(model)
    if parameter_defaults:
        try:
            client.configure_parameters(parameter_defaults)
        except Exception:
            logging.debug("Failed to apply parameter defaults for model %s", model, exc_info=True)
    return client


__all__ = ["get_llm_client"]
