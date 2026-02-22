import json
import logging
import os
from pathlib import Path
from typing import Any, Dict

LOGGER = logging.getLogger(__name__)

# Legacy directory-based configs
LEGACY_MODELS_DIR = Path("models")


def load_configs() -> Dict[str, Dict]:
    """Load model configurations from user_data and builtin_data directories.
    
    Priority order:
    1. user_data/models/ (highest priority)
    2. builtin_data/models/
    3. models/ (legacy, for backwards compatibility)
    """
    from .data_paths import iter_files, MODELS_DIR
    
    configs: Dict[str, Dict] = {}
    seen_keys: set[str] = set()
    
    # Load from user_data and builtin_data (iter_files handles priority)
    for config_file in iter_files(MODELS_DIR, "*.json"):
        try:
            config_data = json.loads(config_file.read_text(encoding="utf-8"))
            
            # Extract model ID from config (required field for API calls)
            model_id = config_data.get("model")
            if not model_id:
                LOGGER.warning("Model config %s missing 'model' field, skipping", config_file.name)
                continue
            
            # Use filename (without extension) as config key
            config_key = config_file.stem
            if config_key not in seen_keys:
                configs[config_key] = config_data
                seen_keys.add(config_key)
                LOGGER.debug("Loaded model config: %s (model=%s) from %s", config_key, model_id, config_file)
        except Exception as exc:
            LOGGER.warning("Failed to load model config from %s: %s", config_file.name, exc)
    
    # Fallback to legacy models/ directory if no configs loaded yet
    if not configs and LEGACY_MODELS_DIR.exists() and LEGACY_MODELS_DIR.is_dir():
        for config_file in sorted(LEGACY_MODELS_DIR.glob("*.json")):
            try:
                config_data = json.loads(config_file.read_text(encoding="utf-8"))
                model_id = config_data.get("model")
                if not model_id:
                    continue
                config_key = config_file.stem
                if config_key not in seen_keys:
                    configs[config_key] = config_data
                    seen_keys.add(config_key)
            except Exception as exc:
                LOGGER.warning("Failed to load model config from %s: %s", config_file.name, exc)

    LOGGER.info("Loaded %d model configurations", len(configs))
    return configs


MODEL_CONFIGS = load_configs()


def reload_configs() -> Dict[str, Dict]:
    """Reload model configurations from disk and update the global cache.

    Call this after adding, editing, or removing model JSON files
    to pick up changes without restarting the server.
    """
    global MODEL_CONFIGS
    MODEL_CONFIGS = load_configs()
    LOGGER.info("Model configurations reloaded: %d models", len(MODEL_CONFIGS))
    return MODEL_CONFIGS


def get_model_provider(model: str) -> str:
    config = MODEL_CONFIGS.get(model)
    if config is None:
        raise ValueError(
            f"Model config not found: '{model}'. "
            f"Check that a matching JSON file exists in builtin_data/models/ or user_data/models/."
        )
    return config.get("provider", "ollama")


def get_context_length(model: str) -> int:
    config = MODEL_CONFIGS.get(model)
    if config is None:
        raise ValueError(
            f"Model config not found: '{model}'. "
            f"Check that a matching JSON file exists in builtin_data/models/ or user_data/models/."
        )
    return int(config.get("context_length", 120000))


def get_default_max_history_messages(model: str) -> int | None:
    """Get the default maximum number of history messages for a model.

    Returns None if not configured (falls back to character-based limit).
    """
    config = MODEL_CONFIGS.get(model, {})
    val = config.get("default_max_history_messages")
    if val is not None:
        return int(val)
    return None


def get_metabolism_keep_messages(model: str) -> int | None:
    """Get the number of messages to keep after metabolism (low watermark).

    Returns None if not configured (metabolism disabled for this model).
    """
    config = MODEL_CONFIGS.get(model, {})
    val = config.get("metabolism_keep_messages")
    if val is not None:
        return int(val)
    return None


def get_model_display_name(model: str) -> str:
    """Get display name for a model, falling back to model ID if not set."""
    config = MODEL_CONFIGS.get(model, {})
    return config.get("display_name", model)


def get_model_choices() -> list[str]:
    """Get list of available model IDs."""
    return list(MODEL_CONFIGS.keys())


def get_model_choices_with_display_names() -> list[tuple[str, str]]:
    """Get list of (model_id, display_name) tuples for UI dropdowns."""
    return [(model_id, get_model_display_name(model_id)) for model_id in get_model_choices()]


def get_model_config(model: str) -> Dict:
    return MODEL_CONFIGS.get(model, {})


def model_supports_images(model: str) -> bool:
    config = get_model_config(model)
    return bool(config.get("supports_images"))


def get_model_parameters(model: str) -> Dict[str, Dict[str, Any]]:
    config = get_model_config(model)
    params = config.get("parameters")
    if isinstance(params, dict):
        return params
    return {}


def get_model_parameter_defaults(model: str) -> Dict[str, Any]:
    defaults: Dict[str, Any] = {}
    for name, spec in get_model_parameters(model).items():
        if isinstance(spec, dict) and "default" in spec:
            defaults[name] = spec.get("default")
    return defaults


def get_model_system_prompt(model: str) -> str:
    """Get the additional system prompt defined in model config.

    Returns an empty string if not configured.
    """
    config = get_model_config(model)
    return config.get("system_prompt", "") or ""


def get_structured_output_backend(model: str) -> str | None:
    """Get structured output backend for a model (e.g., 'xgrammar', 'outlines')."""
    config = get_model_config(model)
    return config.get("structured_output_backend")


def supports_structured_output(model: str) -> bool:
    """Check if a model supports structured output.

    Returns True by default unless explicitly set to False in model config.
    """
    config = get_model_config(model)
    # Default to True unless explicitly set to False
    return config.get("supports_structured_output", True)


def find_model_config(query: str) -> tuple[str, Dict]:
    """Find model config by model ID or filename.

    Searches in order:
    1. Exact match on config key (filename without .json)
    2. Exact match on config["model"] value (API model name)
    3. Exact filename match from file system
    4. Partial match on model ID suffix (e.g., "qwen3-coder" matches "qwen/qwen3-coder-480b...")

    Args:
        query: Model ID, filename, or partial match

    Returns:
        Tuple of (config_key, config) or ("", {}) if not found
    """
    # 1. Exact match on config key (filename)
    if query in MODEL_CONFIGS:
        return query, MODEL_CONFIGS[query]

    # 2. Exact match on config["model"] value (API model name)
    for config_key, config in MODEL_CONFIGS.items():
        if config.get("model") == query:
            return config_key, config

    # 3. Check exact filename match - load config directly from file
    from .data_paths import get_data_paths, MODELS_DIR

    for models_path in get_data_paths(MODELS_DIR):
        config_file = models_path / f"{query}.json"
        if config_file.exists():
            try:
                config_data = json.loads(config_file.read_text(encoding="utf-8"))
                model_id = config_data.get("model", query)
                # Return the query (filename) as the resolved ID so caller knows which file was used
                # But include the actual model ID in the config for API calls
                return query, config_data
            except Exception:
                LOGGER.warning("Failed to load model config from %s", config_file, exc_info=True)

    # 4. Partial match on model ID (query is suffix or contains)
    for model_id, config in MODEL_CONFIGS.items():
        # Check if query matches the part after "/" (e.g., "qwen3-coder-480b" matches "qwen/qwen3-coder-480b")
        if "/" in model_id:
            suffix = model_id.split("/", 1)[1]
            if query == suffix or suffix.startswith(query):
                return model_id, config

    return "", {}


def get_agentic_model() -> str:
    """Get the default model for agentic tasks requiring structured output.

    Priority:
    1. SAIVERSE_AGENTIC_MODEL environment variable
    2. Built-in default: gemini-2.5-flash-lite-preview-09-2025
    """
    import os
    return os.environ.get("SAIVERSE_AGENTIC_MODEL", "gemini-2.5-flash-lite-preview-09-2025")


def get_model_pricing(model: str) -> Dict[str, Any] | None:
    """Get pricing information for a model.

    Uses find_model_config to search by both config key and model ID.

    Returns:
        Dict with keys:
            - input_per_1m_tokens: float (USD per 1M input tokens)
            - output_per_1m_tokens: float (USD per 1M output tokens)
            - currency: str (e.g., "USD")
        Or None if pricing not configured.
    """
    # First try direct lookup
    config = get_model_config(model)
    pricing = config.get("pricing")
    if isinstance(pricing, dict):
        return pricing

    # Fall back to find_model_config which searches by model ID too
    _, config = find_model_config(model)
    if config:
        pricing = config.get("pricing")
        if isinstance(pricing, dict):
            return pricing

    return None


def calculate_cost(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cached_tokens: int = 0,
    cache_write_tokens: int = 0,
    cache_ttl: str = "",
) -> float:
    """Calculate cost in USD for a given token usage.

    Args:
        model: Model ID (config key)
        input_tokens: Number of input tokens (total including cached and cache_write)
        output_tokens: Number of output tokens
        cached_tokens: Number of tokens served FROM cache (cache read, discounted rate)
        cache_write_tokens: Number of tokens written TO cache
        cache_ttl: Cache TTL used ("5m" or "1h"). Affects write cost for Anthropic.

    Returns:
        Cost in USD. Returns 0.0 if pricing not configured (e.g., local models).

    Note:
        Token breakdown for Anthropic:
        - cached_tokens: Read from cache (0.1x rate)
        - cache_write_tokens: Written to cache (1.25x rate for 5m, 2x rate for 1h)
        - remaining: Regular input tokens (1x rate)

        For Gemini/OpenAI (implicit caching):
        - cached_tokens: Read from cache (discounted rate)
        - cache_write_tokens: 0 (no explicit write cost)
    """
    pricing = get_model_pricing(model)
    LOGGER.debug("[DEBUG] calculate_cost: model=%s, pricing=%s", model, pricing)
    if not pricing:
        LOGGER.debug("[DEBUG] No pricing found for model: %s", model)
        return 0.0

    input_rate = pricing.get("input_per_1m_tokens", 0.0)
    output_rate = pricing.get("output_per_1m_tokens", 0.0)
    # Cached tokens (read): use explicit cached rate if configured, otherwise same as input rate
    cached_rate = pricing.get("cached_input_per_1m_tokens", input_rate)
    # Cache write tokens: use TTL-specific rate if available
    if cache_ttl == "1h" and "cache_write_1h_per_1m_tokens" in pricing:
        cache_write_rate = pricing["cache_write_1h_per_1m_tokens"]
    else:
        cache_write_rate = pricing.get("cache_write_per_1m_tokens", input_rate)

    # Non-cached input tokens (input_tokens includes cached + cache_write, so subtract both)
    non_cached_input = max(0, input_tokens - cached_tokens - cache_write_tokens)

    non_cached_cost = (non_cached_input / 1_000_000) * input_rate
    cached_cost = (cached_tokens / 1_000_000) * cached_rate
    cache_write_cost = (cache_write_tokens / 1_000_000) * cache_write_rate
    output_cost = (output_tokens / 1_000_000) * output_rate

    total = non_cached_cost + cached_cost + cache_write_cost + output_cost
    LOGGER.debug(
        "[DEBUG] Cost calculated: $%.6f (non_cached_in=%d @ $%.4f, cached=%d @ $%.4f, cache_write=%d @ $%.4f, out=%d @ $%.4f)",
        total, non_cached_input, input_rate, cached_tokens, cached_rate, cache_write_tokens, cache_write_rate, output_tokens, output_rate
    )
    return total


def _get_required_env_vars(model: str) -> list[str]:
    """Return the environment variable names required for a model's API key.

    Returns an empty list for local models (ollama, llama_cpp) that need no key.
    For models with multiple possible keys (e.g. Gemini), returns all alternatives
    — the model is available if ANY of them is set.
    """
    config = MODEL_CONFIGS.get(model, {})
    provider = config.get("provider", "")

    # Local models need no API key
    if provider in ("ollama", "llama_cpp"):
        return []

    # Explicit api_key_env in config takes priority
    api_key_env = config.get("api_key_env")
    if api_key_env:
        return [api_key_env]

    # Provider defaults
    if provider == "anthropic":
        return ["CLAUDE_API_KEY"]
    if provider == "gemini":
        return ["GEMINI_API_KEY", "GEMINI_FREE_API_KEY"]
    if provider in ("openai",):
        return ["OPENAI_API_KEY"]
    if provider == "xai":
        return ["XAI_API_KEY"]

    # Unknown provider — assume available (don't hide by mistake)
    return []


def is_model_available(model: str) -> bool:
    """Check if a model's required API key is configured.

    Returns True if:
    - The model needs no API key (local models), or
    - At least one of the required env vars is set, or
    - The provider is unknown (don't hide by mistake).
    """
    env_vars = _get_required_env_vars(model)
    if not env_vars:
        return True
    return any(os.environ.get(var) for var in env_vars)


def is_local_model(model: str) -> bool:
    """Check if a model is a local model (Ollama or llama.cpp).

    Local models have zero API cost.
    Returns False if the model config is not found.
    """
    config = MODEL_CONFIGS.get(model)
    if config is None:
        return False
    return config.get("provider") in ("ollama", "llama_cpp")


def get_cache_config(model: str) -> Dict[str, Any]:
    """Get cache configuration for a model.

    Returns:
        Dict with keys:
            - supported: bool (whether model supports caching)
            - default_enabled: bool (default cache state)
            - default_ttl: str (e.g., "5m", "1h")
            - ttl_options: list[str] (available TTL options)
            - type: str ("explicit" or "implicit")
            - min_tokens: int (minimum tokens for caching)
    """
    config = get_model_config(model)
    cache = config.get("cache", {})

    # Determine if caching is supported based on provider
    # Use direct config lookup to avoid ValueError when model config is missing
    provider = config.get("provider")
    default_supported = provider in ("anthropic", "gemini", "openai")

    return {
        "supported": cache.get("supported", default_supported),
        "default_enabled": cache.get("default_enabled", True),
        "default_ttl": cache.get("default_ttl", "5m"),
        "ttl_options": cache.get("ttl_options", ["5m"]),
        "type": cache.get("type", "explicit" if provider == "anthropic" else "implicit"),
        "min_tokens": cache.get("min_tokens", 1024),
    }


def supports_cache(model: str) -> bool:
    """Check if a model supports prompt caching."""
    return get_cache_config(model).get("supported", False)


def get_cache_ttl_options(model: str) -> list:
    """Get available cache TTL options for a model."""
    return get_cache_config(model).get("ttl_options", ["5m"])


def get_cache_write_rate(model: str) -> float:
    """Get cache write cost rate per 1M tokens.

    For Anthropic:
        - 5m TTL: 1.25x input rate
        - 1h TTL: 2x input rate (not yet supported)

    Returns 0.0 if not configured (implicit caching has no write cost).
    """
    pricing = get_model_pricing(model)
    if not pricing:
        return 0.0
    return pricing.get("cache_write_per_1m_tokens", 0.0)
