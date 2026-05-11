"""Provider configuration management for SAIVerse.

Providers describe how to connect to an LLM backend (protocol, base URL,
API key environment variable, default request kwargs). Models reference
providers via the ``provider_ref`` field; the provider's defaults are
inherited when the model JSON does not specify them directly.

Loads provider configurations from:
    1. ~/.saiverse/user_data/providers/  (highest priority)
    2. expansion_data/<addon>/providers/  (middle priority)
    3. builtin_data/providers/             (lowest priority)

Builtin providers are immutable. Editing a builtin from the UI creates a
user_data override with the same id, which then takes priority on next reload.
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from .data_paths import (
    BUILTIN_DATA_DIR,
    PROVIDERS_DIR,
    USER_DATA_DIR,
    iter_files,
)

LOGGER = logging.getLogger(__name__)

_SAFE_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_.\-]+$")


def _is_builtin_path(path: Path) -> bool:
    """Return True if path is under builtin_data/."""
    try:
        path.resolve().relative_to(BUILTIN_DATA_DIR.resolve())
        return True
    except ValueError:
        return False


def load_configs() -> dict[str, dict]:
    """Load provider configurations from all sources, respecting priority.

    Returns:
        Dict mapping provider_id -> provider config dict.
        Configs loaded from builtin_data automatically get ``builtin: True``.
    """
    configs: dict[str, dict] = {}
    seen_keys: set[str] = set()

    for config_file in iter_files(PROVIDERS_DIR, "*.json"):
        try:
            config_data = json.loads(config_file.read_text(encoding="utf-8"))
        except Exception as exc:
            LOGGER.warning(
                "Failed to load provider config from %s: %s",
                config_file.name, exc,
            )
            continue

        provider_id = config_data.get("id") or config_file.stem
        if not isinstance(provider_id, str) or not provider_id:
            LOGGER.warning(
                "Provider config %s missing valid 'id', skipping",
                config_file.name,
            )
            continue

        if provider_id in seen_keys:
            continue

        if _is_builtin_path(config_file):
            config_data["builtin"] = True

        configs[provider_id] = config_data
        seen_keys.add(provider_id)
        LOGGER.debug(
            "Loaded provider config: %s from %s", provider_id, config_file,
        )

    LOGGER.info("Loaded %d provider configurations", len(configs))
    return configs


PROVIDER_CONFIGS: dict[str, dict] = load_configs()


def reload_configs() -> dict[str, dict]:
    """Reload provider configurations from disk and refresh the global cache."""
    global PROVIDER_CONFIGS
    PROVIDER_CONFIGS = load_configs()
    LOGGER.info(
        "Provider configurations reloaded: %d providers",
        len(PROVIDER_CONFIGS),
    )
    return PROVIDER_CONFIGS


def get_provider(provider_id: str) -> dict | None:
    """Get a provider configuration by id, or None if not found."""
    return PROVIDER_CONFIGS.get(provider_id)


def is_builtin(provider_id: str) -> bool:
    """Return True if the active provider config came from builtin_data.

    Note: if a user_data override exists with the same id, this returns False
    even if a builtin with that id also exists — the override is what's active.
    """
    config = PROVIDER_CONFIGS.get(provider_id)
    if config is None:
        return False
    return bool(config.get("builtin"))


def save_provider(provider_id: str, config: dict) -> None:
    """Save a provider configuration to user_data/providers/<id>.json.

    The provider is always saved to user_data/, even when a builtin with the
    same id exists. The user_data version takes priority on next reload,
    effectively overriding the builtin.

    Args:
        provider_id: Unique provider identifier (filename stem).
        config: Provider configuration dict.

    Raises:
        ValueError: If provider_id contains characters unsafe for filenames.
    """
    if not provider_id or not _SAFE_ID_PATTERN.match(provider_id):
        raise ValueError(f"Invalid provider id: {provider_id!r}")

    target_dir = USER_DATA_DIR / PROVIDERS_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    target_file = target_dir / f"{provider_id}.json"

    # Strip auto-injected fields; user_data files are never marked builtin
    save_data = {k: v for k, v in config.items() if k != "builtin"}
    save_data["id"] = provider_id  # Ensure id is consistent with filename

    target_file.write_text(
        json.dumps(save_data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    LOGGER.info("Saved provider %s to %s", provider_id, target_file)
    reload_configs()


def delete_provider(provider_id: str) -> None:
    """Delete a provider's user_data override.

    Only user_data providers can be deleted. Builtin providers are immutable;
    attempting to delete one without a user_data override raises ValueError.
    If a user_data override is deleted while a builtin with the same id exists,
    the builtin becomes visible again after reload.

    Raises:
        FileNotFoundError: If no user_data provider with this id exists.
        ValueError: If the provider exists only in builtin_data.
    """
    target_file = USER_DATA_DIR / PROVIDERS_DIR / f"{provider_id}.json"
    if not target_file.exists():
        builtin_file = BUILTIN_DATA_DIR / PROVIDERS_DIR / f"{provider_id}.json"
        if builtin_file.exists():
            raise ValueError(
                f"Cannot delete builtin provider {provider_id!r}. "
                f"Builtin providers are read-only."
            )
        raise FileNotFoundError(f"Provider not found: {provider_id}")

    target_file.unlink()
    LOGGER.info("Deleted provider %s (file: %s)", provider_id, target_file)
    reload_configs()


def list_models_using_provider(provider_id: str) -> list[str]:
    """List model config keys that reference this provider via provider_ref.

    Used to warn the user before deleting a provider that is in use.
    """
    # Lazy import to avoid circular dependency with model_configs
    from .model_configs import MODEL_CONFIGS

    using = [
        model_key
        for model_key, model_config in MODEL_CONFIGS.items()
        if model_config.get("provider_ref") == provider_id
    ]
    return sorted(using)


def list_provider_choices() -> list[tuple[str, str]]:
    """Get list of (provider_id, display_name) tuples for UI dropdowns."""
    return [
        (pid, config.get("display_name", pid))
        for pid, config in PROVIDER_CONFIGS.items()
    ]


__all__ = [
    "PROVIDER_CONFIGS",
    "load_configs",
    "reload_configs",
    "get_provider",
    "is_builtin",
    "save_provider",
    "delete_provider",
    "list_models_using_provider",
    "list_provider_choices",
]
