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
import os
import re
from pathlib import Path

from .data_paths import (
    BUILTIN_DATA_DIR,
    LAYER_BUILTIN,
    LAYER_EXPANSION,
    LAYER_USER_DATA,
    PROVIDERS_DIR,
    USER_DATA_DIR,
    iter_files_with_layer,
)

LOGGER = logging.getLogger(__name__)

_SAFE_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_.\-]+$")

# Which data layer a provider definition was loaded from. Credential policy is
# decided from this (see saiverse/provider_security.py), so it is always the
# root the loader actually walked — not re-derived from the path afterwards,
# which a symlink or Windows junction could point at another layer, and not
# read out of the file, which would let a definition name its own layer.
SOURCE_BUILTIN = LAYER_BUILTIN
SOURCE_EXPANSION = LAYER_EXPANSION
SOURCE_USER_DATA = LAYER_USER_DATA
# For a config that never came through the loader (e.g. a pending API payload).
# Untrusted by default so that forgetting to stamp one fails closed.
SOURCE_UNKNOWN = "unknown"


def load_configs() -> dict[str, dict]:
    """Load provider configurations from all sources, respecting priority.

    Returns:
        Dict mapping provider_id -> provider config dict, each stamped with the
        ``source`` layer it was loaded from.
    """
    configs: dict[str, dict] = {}
    seen_keys: set[str] = set()

    for config_file, layer in iter_files_with_layer(PROVIDERS_DIR, "*.json"):
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

        # Taken from the root this file was walked from, never from its
        # contents: a definition must not be able to claim a layer it was not
        # loaded from. ``builtin`` was the previous marker, and it lived inside
        # the file — so it is dropped rather than trusted.
        config_data.pop("builtin", None)
        config_data["source"] = layer

        configs[provider_id] = config_data
        seen_keys.add(provider_id)
        LOGGER.debug(
            "Loaded provider config: %s from %s (source=%s)",
            provider_id, config_file, config_data["source"],
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
    return config.get("source") == SOURCE_BUILTIN


def reload_models_after_provider_change() -> None:
    """Re-resolve model configs after a provider definition changed.

    ``model_configs`` inlines a provider's ``base_url`` / ``api_key_env`` into
    every model that names it, once, at load time. Reloading only the providers
    would leave those copies pointing at the old endpoint — and the credential
    check compares the two, so every model on an edited provider would start
    failing until the next restart.
    """
    from .model_configs import reload_configs as reload_model_configs

    reload_model_configs()


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

    # Strip the derived layer markers; they are re-stamped from the path on load
    save_data = {
        k: v for k, v in config.items() if k not in ("source", "builtin")
    }
    save_data["id"] = provider_id  # Ensure id is consistent with filename

    # Written beside the target and moved into place, never truncated in place:
    # a crash partway through a direct write leaves half a JSON file, and a file
    # that fails to parse does not fall back to the layer underneath — it takes
    # the provider out of the list entirely (see
    # docs/issues/malformed_provider_json_breaks_provider_list.md).
    staged = target_dir / f"{provider_id}.json.tmp"
    try:
        staged.write_text(
            json.dumps(save_data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(staged, target_file)
    except Exception:
        staged.unlink(missing_ok=True)
        raise
    LOGGER.info("Saved provider %s to %s", provider_id, target_file)
    reload_configs()
    reload_models_after_provider_change()


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
    reload_models_after_provider_change()


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
    "SOURCE_BUILTIN",
    "SOURCE_EXPANSION",
    "SOURCE_USER_DATA",
    "SOURCE_UNKNOWN",
    "load_configs",
    "reload_configs",
    "reload_models_after_provider_change",
    "get_provider",
    "is_builtin",
    "save_provider",
    "delete_provider",
    "list_models_using_provider",
    "list_provider_choices",
]
