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
from typing import TYPE_CHECKING

from .data_paths import (
    BUILTIN_DATA_DIR,
    LAYER_BUILTIN,
    LAYER_EXPANSION,
    LAYER_USER_DATA,
    PROVIDERS_DIR,
    USER_DATA_DIR,
    iter_file_candidates_with_layer,
)
from .provider_protocols import SUPPORTED_PROVIDER_PROTOCOLS

if TYPE_CHECKING:
    from .persona_model_selection import ReapplyResult

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


def _provider_shape_error(config: object, default_id: str) -> str | None:
    """Check the loader/API shape without changing credential policy.

    Keep omitted display fields compatible with the API's existing defaults,
    but never turn an explicit null/wrong type into a usable declaration.
    Errors name only fields and types, never their possibly sensitive values.
    """
    if not isinstance(config, dict):
        return "JSON root must be an object"
    provider_id = config.get("id", default_id)
    if not isinstance(provider_id, str) or not _SAFE_ID_PATTERN.fullmatch(provider_id):
        return "id must be a non-empty safe identifier"
    if not isinstance(config.get("display_name", provider_id), str):
        return "display_name must be a string"
    protocol = config.get("protocol")
    if not isinstance(protocol, str) or protocol not in SUPPORTED_PROVIDER_PROTOCOLS:
        return "protocol must name a supported protocol"
    if "api_key_env_alternates" in config:
        alternates = config["api_key_env_alternates"]
        if not isinstance(alternates, list) or any(
            not isinstance(name, str) or not name.strip() for name in alternates
        ):
            return "api_key_env_alternates must be a list of non-empty strings"
    optional_types = {
        "base_url": str,
        "api_key_env": str,
        "api_key_required": bool,
        "default_request_kwargs": dict,
        "default_convert_system_to_user": bool,
        "default_supports_images": bool,
        "default_max_image_bytes": int,
    }
    for field, expected_type in optional_types.items():
        value = config.get(field)
        # Exact type matters: JSON true is not an integer byte count, and
        # strings such as "false" must not become truthy model defaults.
        if value is not None and type(value) is not expected_type:
            return f"{field} must be {expected_type.__name__} or null"
    return None


def _invalid_provider(provider_id: str, error: dict[str, str]) -> dict:
    """A visible disabled entry, containing no unvalidated connection values."""
    return {
        "id": provider_id, "display_name": provider_id, "protocol": "invalid",
        "source": error["source"], "config_error": error,
    }


def config_error_message(error: dict[str, str]) -> str:
    """Safe diagnostic for connection failures (never raw JSON/exception text)."""
    return (
        "Provider configuration is invalid: "
        f"path={error['path']!r} source={error['source']} reason={error['reason']}"
    )


def load_configs() -> dict[str, dict]:
    """Select each highest-priority definition, including visible invalid ones.

    Invalid overrides claim their filename and ID just like valid overrides.
    A lower file with that name is read only to discover its ID, so references
    to a differently named ID are blocked too. Its connection is never used.
    """
    configs: dict[str, dict] = {}
    seen_names: dict[str, dict[str, str] | None] = {}
    name_priority: dict[str, int] = {}
    id_priority: dict[str, int] = {}
    candidates = iter_file_candidates_with_layer(PROVIDERS_DIR, "*.json")
    for index, (config_file, layer) in enumerate(candidates):
        if config_file.name in seen_names and seen_names[config_file.name] is None:
            continue
        provider_id = config_file.stem
        config_data = None
        try:
            config_data = json.loads(config_file.read_text(encoding="utf-8"))
            if isinstance(config_data, dict):
                declared_id = config_data.get("id", provider_id)
                if isinstance(declared_id, str) and _SAFE_ID_PATTERN.fullmatch(declared_id):
                    provider_id = declared_id
            reason = _provider_shape_error(config_data, config_file.stem)
        except (OSError, UnicodeError, ValueError, RecursionError) as exc:
            reason = type(exc).__name__  # exception text may contain raw values

        if config_file.name in seen_names:
            error = seen_names[config_file.name]
            priority = name_priority[config_file.name]
            if provider_id not in configs or priority < id_priority[provider_id]:
                configs[provider_id] = _invalid_provider(provider_id, error)
                id_priority[provider_id] = priority
            continue

        error = None if reason is None else {
            "path": str(config_file.absolute()), "source": layer, "reason": reason,
        }
        seen_names[config_file.name] = error
        name_priority[config_file.name] = index
        if provider_id in configs:
            continue
        id_priority[provider_id] = index
        if error:
            configs[provider_id] = _invalid_provider(provider_id, error)
            LOGGER.warning("%s; no lower-layer fallback", config_error_message(error))
            continue

        # Both trust and error markers belong to the loader, never the JSON.
        config_data.pop("builtin", None)
        config_data.pop("config_error", None)
        config_data["source"] = layer
        configs[provider_id] = config_data
        LOGGER.debug("Loaded provider config: %r from %r (source=%s)",
                     provider_id, str(config_file.absolute()), layer)

    LOGGER.info("Loaded %d provider configurations", len(configs))
    return configs


PROVIDER_CONFIGS: dict[str, dict] = load_configs()


def reload_configs() -> ReapplyResult:
    """Reload provider configurations from disk and refresh the global cache.

    Returns the result of deciding every persona's speaking model again
    (``saiverse.persona_model_selection.ReapplyResult``): the personas that
    could not be switched, which the screen that made the change shows right
    away. Read the new definitions from ``PROVIDER_CONFIGS``.
    """
    # import はここで行う: session_lifecycle / persona_model_selection 側が設定を
    # 読むため、モジュール先頭に置くと循環参照になる。
    from saiverse.persona_model_selection import (
        MODEL_SETTINGS_LOCK,
        reapply_after_config_reload,
    )
    from sea.session_lifecycle import invalidate_cold_sweep_fingerprints

    global PROVIDER_CONFIGS
    # 定義の差し替えから決め直しまでを、設定のロックの中で一続きに行う
    # (docs/intent/persona_model_selection.md 決まったこと 5)。取る順番は設定の
    # ロックが先、ペルソナの接続のロックが後。
    with MODEL_SETTINGS_LOCK:
        PROVIDER_CONFIGS = load_configs()
        LOGGER.info(
            "Provider configurations reloaded: %d providers",
            len(PROVIDER_CONFIGS),
        )

        # 見張りの素通しは「前回と同じ状態なら結果も同じ」という前提に立つ。接続先の
        # 定義を読み直したらその前提は消える。書き換えの入口は複数ある (作成・更新・
        # 削除・reload ルート) が、全部この読み直しを通るので、記録を捨てる呼び出しは
        # ここに一本だけ置く。
        invalidate_cold_sweep_fingerprints()

        # 接続先の定義が変わったら、動いているペルソナの接続を捨てて決め直す
        # (docs/intent/persona_model_selection.md 決まったこと 11)。すでに話したペルソナも
        # 次の返事から新しい接続先の設定で接続が作られる。書いている途中の返事は、
        # 始めたときの接続で最後まで書く。
        return reapply_after_config_reload()


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


def reload_models_after_provider_change() -> ReapplyResult:
    """Re-resolve model configs after a provider definition changed.

    ``model_configs`` inlines a provider's ``base_url`` / ``api_key_env`` into
    every model that names it, once, at load time. Reloading only the providers
    would leave those copies pointing at the old endpoint — and the credential
    check compares the two, so every model on an edited provider would start
    failing until the next restart.

    Returns the model reload's ``ReapplyResult`` (personas that could not be
    switched).
    """
    from .model_configs import reload_configs as reload_model_configs

    return reload_model_configs()


def reload_providers_and_models() -> ReapplyResult:
    """Reload provider definitions, then the model definitions that inline them.

    Both reloads run inside one section of the settings lock
    (``persona_model_selection.MODEL_SETTINGS_LOCK``, an RLock), so no other
    settings save or re-decide runs between them. Otherwise that save would
    decide with the new provider definitions and the old model definitions.

    Both reloads decide every persona again; the model reload runs last, so its
    result is the one that describes where every persona ended up.
    """
    from .persona_model_selection import MODEL_SETTINGS_LOCK

    with MODEL_SETTINGS_LOCK:
        reload_configs()
        return reload_models_after_provider_change()


def save_provider(provider_id: str, config: dict) -> ReapplyResult:
    """Save a provider configuration to user_data/providers/<id>.json.

    The provider is always saved to user_data/, even when a builtin with the
    same id exists. The user_data version takes priority on next reload,
    effectively overriding the builtin.

    Args:
        provider_id: Unique provider identifier (filename stem).
        config: Provider configuration dict.

    Returns:
        The personas that could not be switched to the new settings. The
        provider reload and the model reload that follows it both decide
        everyone again; the model reload runs last, so its result is the one
        that describes where every persona ended up.

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
        k: v for k, v in config.items() if k not in ("source", "builtin", "config_error")
    }
    save_data["id"] = provider_id  # Ensure id is consistent with filename

    # Written beside the target and moved into place, never truncated in place:
    # a crash partway through a direct write leaves half a JSON file. The loader
    # disables broken definitions rather than switching connection/key, so a
    # partial write would make all referencing models unavailable (see
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
    return reload_providers_and_models()


def delete_provider(provider_id: str) -> ReapplyResult:
    """Delete a provider's user_data override.

    Only user_data providers can be deleted. Builtin providers are immutable;
    attempting to delete one without a user_data override raises ValueError.
    If a user_data override is deleted while a builtin with the same id exists,
    the builtin becomes visible again after reload.

    Returns:
        The personas that could not be switched, from the model reload that
        runs last (see :func:`save_provider`).

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
    return reload_providers_and_models()


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
