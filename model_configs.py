import json
from pathlib import Path
from typing import Any, Dict

CONFIG_PATH = Path("models.json")


def load_configs(path: Path = CONFIG_PATH) -> Dict[str, Dict]:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}

MODEL_CONFIGS = load_configs()


def get_model_provider(model: str) -> str:
    return MODEL_CONFIGS.get(model, {}).get("provider", "ollama")


def get_context_length(model: str) -> int:
    return int(MODEL_CONFIGS.get(model, {}).get("context_length", 120000))


def get_model_choices() -> list[str]:
    return list(MODEL_CONFIGS.keys())


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
