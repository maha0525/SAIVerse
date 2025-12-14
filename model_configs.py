import json
import logging
from pathlib import Path
from typing import Any, Dict

LOGGER = logging.getLogger(__name__)

# Legacy single-file config
LEGACY_CONFIG_PATH = Path("models.json")
# New directory-based configs
MODELS_DIR = Path("models")


def load_configs() -> Dict[str, Dict]:
    """Load model configurations from models/ directory or legacy models.json."""
    configs: Dict[str, Dict] = {}

    # Try loading from models/ directory first (new structure)
    if MODELS_DIR.exists() and MODELS_DIR.is_dir():
        for config_file in sorted(MODELS_DIR.glob("*.json")):
            try:
                config_data = json.loads(config_file.read_text(encoding="utf-8"))

                # Extract model ID from config (required field)
                model_id = config_data.get("model")
                if not model_id:
                    LOGGER.warning("Model config %s missing 'model' field, skipping", config_file.name)
                    continue

                # Store config with model ID as key
                configs[model_id] = config_data
                LOGGER.debug("Loaded model config: %s from %s", model_id, config_file.name)
            except Exception as exc:
                LOGGER.warning("Failed to load model config from %s: %s", config_file.name, exc)

    # Fallback to legacy models.json if models/ dir is empty or doesn't exist
    if not configs and LEGACY_CONFIG_PATH.exists():
        try:
            legacy_configs = json.loads(LEGACY_CONFIG_PATH.read_text(encoding="utf-8"))
            # In legacy format, keys are model IDs and values don't have "model" field
            # Add "model" field to each config for consistency
            for model_id, config_data in legacy_configs.items():
                if "model" not in config_data:
                    config_data["model"] = model_id
                configs[model_id] = config_data
            LOGGER.info("Loaded %d models from legacy models.json", len(configs))
        except Exception as exc:
            LOGGER.warning("Failed to load legacy models.json: %s", exc)

    return configs

MODEL_CONFIGS = load_configs()


def get_model_provider(model: str) -> str:
    return MODEL_CONFIGS.get(model, {}).get("provider", "ollama")


def get_context_length(model: str) -> int:
    return int(MODEL_CONFIGS.get(model, {}).get("context_length", 120000))


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
    1. Exact match on model ID
    2. Exact match on filename (without .json)
    3. Partial match on model ID (e.g., "qwen3-coder" matches "qwen/qwen3-coder-480b...")

    Args:
        query: Model ID, filename, or partial match

    Returns:
        Tuple of (model_id, config) or ("", {}) if not found
    """
    # 1. Exact match on model ID
    if query in MODEL_CONFIGS:
        return query, MODEL_CONFIGS[query]

    # 2. Build filename -> model_id mapping
    filename_to_model: Dict[str, str] = {}
    if MODELS_DIR.exists() and MODELS_DIR.is_dir():
        for config_file in MODELS_DIR.glob("*.json"):
            filename = config_file.stem  # filename without .json
            try:
                config_data = json.loads(config_file.read_text(encoding="utf-8"))
                model_id = config_data.get("model", "")
                if model_id:
                    filename_to_model[filename] = model_id
            except Exception:
                pass

    # Check exact filename match
    if query in filename_to_model:
        model_id = filename_to_model[query]
        return model_id, MODEL_CONFIGS.get(model_id, {})

    # 3. Partial match on model ID (query is suffix or contains)
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
    2. Built-in default: gemini-2.0-flash
    """
    import os
    return os.environ.get("SAIVERSE_AGENTIC_MODEL", "gemini-2.0-flash")
