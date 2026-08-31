"""Load MCP server definitions from SAIVerse data sources."""
from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, Optional

LOGGER = logging.getLogger(__name__)

_PLACEHOLDER_RE = re.compile(r"\$\{([^}]+)\}")
_CONFIG_FILENAME = "mcp_servers.json"


def _resolve_addon_param(
    addon_name: str,
    key: str,
    persona_id: Optional[str] = None,
) -> Optional[str]:
    """Resolve one addon parameter via saiverse.addon_config.get_params.

    Returns ``None`` when the addon is not registered, the key is missing,
    or an unexpected error occurs. Callers should log a warning and keep
    the original placeholder text when ``None`` is returned.
    """
    try:
        from saiverse.addon_config import get_params
    except ImportError as exc:
        LOGGER.warning(
            "MCP config: saiverse.addon_config unavailable — cannot resolve addon '%s.%s' (%s)",
            addon_name,
            key,
            exc,
        )
        return None

    try:
        params = get_params(addon_name, persona_id=persona_id)
    except Exception as exc:
        LOGGER.warning(
            "MCP config: failed to load addon params for '%s' (persona=%s): %s",
            addon_name,
            persona_id or "<global>",
            exc,
        )
        return None

    if key not in params:
        LOGGER.warning(
            "MCP config: addon '%s' has no parameter '%s' (persona=%s)",
            addon_name,
            key,
            persona_id or "<global>",
        )
        return None

    value = params[key]
    if value is None:
        LOGGER.warning(
            "MCP config: addon '%s.%s' is set to None (persona=%s)",
            addon_name,
            key,
            persona_id or "<global>",
        )
        return None
    return str(value)


def _resolve_runtime(key: str) -> Optional[str]:
    """Resolve a ``runtime.<key>`` placeholder.

    Runtime placeholders are values that SAIVerse computes on the fly at
    placeholder-resolution time (= MCP subprocess spawn / reconnect),
    instead of reading from DB / env. Used to avoid forcing users to
    re-enter values that the host machine already knows (e.g. its own
    LAN IP after a Wi-Fi switch).

    Supported keys:
      * ``lan_ip`` -- outward-facing IPv4 of this host (see saiverse.lan_ip)
    """
    if key == "lan_ip":
        from saiverse.lan_ip import get_local_ip
        ip = get_local_ip()
        if ip is None:
            LOGGER.warning(
                "MCP config: ${runtime.lan_ip} could not be detected "
                "(offline / sandbox?); placeholder left unresolved",
            )
        return ip
    LOGGER.warning("MCP config: unknown runtime placeholder '${runtime.%s}'", key)
    return None


def _resolve_placeholder(
    placeholder: str,
    persona_id: Optional[str] = None,
    instance_context: Optional[Dict[str, str]] = None,
) -> Optional[str]:
    """Resolve a single ``${...}`` placeholder body.

    Supported forms:
      * ``env.VAR_NAME``                    -- OS environment variable (explicit form)
      * ``addon.<addon_name>.<key>``        -- AddonConfig (global) via get_params
      * ``persona.addon.<addon_name>.<key>`` -- AddonPersonaConfig via get_params
      * ``runtime.<key>``                    -- runtime-computed value (e.g. lan_ip)
      * ``instance.<key>``                   -- per-instance context passed at
        named-instance launch time (e.g. ``${instance.ws_port}``). See
        ``docs/intent/mcp_addon_integration.md`` 設計 G.
      * ``VAR_NAME``                         -- legacy OS environment variable form
    """
    parts = placeholder.split(".")

    if len(parts) == 2 and parts[0] == "env":
        var_name = parts[1]
        value = os.getenv(var_name)
        if value is None:
            LOGGER.warning("MCP config: environment variable '%s' is not set", var_name)
        return value

    if len(parts) == 2 and parts[0] == "runtime":
        return _resolve_runtime(parts[1])

    if len(parts) == 2 and parts[0] == "instance":
        key = parts[1]
        if instance_context is None:
            # 名前付きインスタンス外の interpolate (テンプレート一覧・グローバル
            # reload 等) では ${instance.*} が未解決なのは正常 — WARNING だと
            # 呼ばれるたびに error.log を洪水させる (2026-07-19 実測 11,181 行で
            # 実エラーが埋没)。実起動時の鍵違いは下の WARNING が引き続き担う。
            LOGGER.debug(
                "MCP config: placeholder '${%s}' requires named-instance "
                "context but none provided",
                placeholder,
            )
            return None
        value = instance_context.get(key)
        if value is None:
            LOGGER.warning(
                "MCP config: named-instance context has no key '%s' "
                "(placeholder '${%s}')",
                key,
                placeholder,
            )
            return None
        return str(value)

    if len(parts) == 3 and parts[0] == "addon":
        _, addon_name, key = parts
        return _resolve_addon_param(addon_name, key, persona_id=None)

    if len(parts) == 4 and parts[0] == "persona" and parts[1] == "addon":
        _, _, addon_name, key = parts
        if persona_id is None:
            LOGGER.warning(
                "MCP config: placeholder '${%s}' requires persona context but none provided",
                placeholder,
            )
            return None
        return _resolve_addon_param(addon_name, key, persona_id=persona_id)

    if len(parts) == 1:
        var_name = parts[0]
        value = os.getenv(var_name)
        if value is None:
            LOGGER.warning("MCP config: environment variable '%s' is not set", var_name)
        return value

    LOGGER.warning("MCP config: unknown placeholder format '${%s}'", placeholder)
    return None


def _interpolate_env(
    value: str,
    persona_id: Optional[str] = None,
    instance_context: Optional[Dict[str, str]] = None,
) -> str:
    """Replace all ``${...}`` placeholders in a string with resolved values.

    Unresolved placeholders are left intact so that callers (or the tests)
    can detect missing configuration instead of silently substituting empty strings.
    """

    def _replace(match: re.Match[str]) -> str:
        placeholder = match.group(1)
        resolved = _resolve_placeholder(
            placeholder, persona_id=persona_id, instance_context=instance_context
        )
        if resolved is None:
            return match.group(0)
        return resolved

    return _PLACEHOLDER_RE.sub(_replace, value)


def _interpolate_value(
    value: Any,
    persona_id: Optional[str] = None,
    instance_context: Optional[Dict[str, str]] = None,
) -> Any:
    if isinstance(value, str):
        return _interpolate_env(
            value, persona_id=persona_id, instance_context=instance_context
        )
    if isinstance(value, list):
        return [
            _interpolate_value(
                item, persona_id=persona_id, instance_context=instance_context
            )
            for item in value
        ]
    if isinstance(value, dict):
        return {
            key: _interpolate_value(
                item, persona_id=persona_id, instance_context=instance_context
            )
            for key, item in value.items()
        }
    return value


def resolve_config_placeholders(
    config: Dict[str, Any],
    persona_id: Optional[str] = None,
    instance_context: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Public entry point: resolve all placeholders in a single server config.

    Intended for the MCP client to call at process-launch time with the
    appropriate persona context, after loading raw configs via
    :func:`load_mcp_configs_raw`.

    ``instance_context`` carries per-named-instance values (e.g. token / port
    for one of several gateway instances spawned from the same server
    definition). See ``docs/intent/mcp_addon_integration.md`` 設計 G.
    """
    return _interpolate_value(
        config, persona_id=persona_id, instance_context=instance_context
    )


def is_server_enabled(server_name: str, cfg: Dict[str, Any]) -> bool:
    """Whether a server definition is switched on.

    **Strict boolean.** A bare truthiness test reads the string ``"false"`` as
    enabled, so a typo would silently keep running a server the author meant to
    switch off. Non-boolean values are treated as disabled — "I tried to turn
    this off" is the intent worth honouring.

    Shared by the startup load (:func:`load_mcp_configs`) and the addon
    hot-load path (``tools.mcp_client._reload_addon_mcp_config``) so the two
    cannot disagree about whether a server runs. They did disagree once: the
    strict check landed in the startup path only, which made
    ``"enabled": "false"`` mean *off* at boot but *on* after re-enabling the
    addon — the same definition, two answers.
    """
    flag = cfg.get("enabled", True)
    if not isinstance(flag, bool):
        LOGGER.warning(
            "MCP config: server '%s' has a non-boolean 'enabled' (%r); "
            "treating it as disabled",
            server_name,
            flag,
        )
        return False
    return flag


def _load_config_file(path: Path) -> Dict[str, Dict[str, Any]]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        LOGGER.warning("MCP config: failed to read %s: %s", path, exc)
        return {}

    servers = data.get("mcpServers", {})
    if not isinstance(servers, dict):
        LOGGER.warning("MCP config: 'mcpServers' in %s is not an object", path)
        return {}

    result: Dict[str, Dict[str, Any]] = {}
    for server_name, cfg in servers.items():
        if not isinstance(cfg, dict):
            LOGGER.warning("MCP config: server '%s' in %s is not an object", server_name, path)
            continue
        result[server_name] = dict(cfg)
    return result


def _iter_candidate_paths() -> list[Path]:
    from saiverse.data_paths import BUILTIN_DATA_DIR, EXPANSION_DATA_DIR, USER_DATA_DIR

    paths: list[Path] = []

    # Highest priority: explicit user-level config file
    paths.append(USER_DATA_DIR / _CONFIG_FILENAME)

    # Project-based user configs: ~/.saiverse/user_data/<project>/mcp_servers.json
    if USER_DATA_DIR.exists():
        for project_dir in sorted(USER_DATA_DIR.iterdir()):
            if not project_dir.is_dir() or project_dir.name.startswith(("_", ".")):
                continue
            paths.append(project_dir / _CONFIG_FILENAME)

    # Expansion/addon packs: <repo>/expansion_data/<pack>/mcp_servers.json
    if EXPANSION_DATA_DIR.exists():
        for project_dir in sorted(EXPANSION_DATA_DIR.iterdir()):
            if not project_dir.is_dir() or project_dir.name.startswith(("_", ".")):
                continue
            paths.append(project_dir / _CONFIG_FILENAME)

    # Lowest priority: builtin_data/mcp_servers.json
    paths.append(BUILTIN_DATA_DIR / _CONFIG_FILENAME)

    return paths


def _extract_addon_name_from_path(path: Path) -> Optional[str]:
    """Return the addon folder name if ``path`` lives under EXPANSION_DATA_DIR.

    Example: ``expansion_data/saiverse-elyth-addon/mcp_servers.json``
             -> ``"saiverse-elyth-addon"``.

    Returns ``None`` for user_data / builtin_data / anything outside expansion_data.
    """
    from saiverse.data_paths import EXPANSION_DATA_DIR

    try:
        path_abs = path.resolve()
        exp_abs = EXPANSION_DATA_DIR.resolve()
    except OSError as exc:
        LOGGER.debug("MCP config: path resolve failed for '%s': %s", path, exc)
        return None
    try:
        rel = path_abs.relative_to(exp_abs)
    except ValueError:
        return None
    parts = rel.parts
    if len(parts) < 2:
        return None
    return parts[0]


def load_mcp_configs() -> Dict[str, Dict[str, Any]]:
    """Load enabled MCP server configs with SAIVerse priority rules.

    Priority:
      1. ``~/.saiverse/user_data/mcp_servers.json``
      2. ``~/.saiverse/user_data/<project>/mcp_servers.json``
      3. ``expansion_data/<pack>/mcp_servers.json``
      4. ``builtin_data/mcp_servers.json``

    Servers declared under ``expansion_data/<addon_name>/`` have their keys
    **automatically prefixed** with ``<addon_name>__`` to isolate them from
    other addons and from user/builtin definitions. See
    ``docs/intent/mcp_addon_integration.md`` §B for the rationale.

    User-level and builtin configs are treated as privileged and use
    whatever names they declare.
    """
    merged: Dict[str, Dict[str, Any]] = {}

    for path in _iter_candidate_paths():
        addon_name = _extract_addon_name_from_path(path)
        for server_name, cfg in _load_config_file(path).items():
            qualified_name = (
                f"{addon_name}__{server_name}" if addon_name else server_name
            )
            if qualified_name in merged:
                LOGGER.debug(
                    "MCP config: '%s' from %s ignored by higher-priority source",
                    qualified_name,
                    path,
                )
                continue
            cfg_copy = _interpolate_value(cfg)
            cfg_copy["_source_path"] = str(path)
            if addon_name:
                cfg_copy["_addon_name"] = addon_name
                cfg_copy["_original_server_name"] = server_name
            merged[qualified_name] = cfg_copy
            LOGGER.debug("MCP config: loaded '%s' from %s", qualified_name, path)

    enabled: Dict[str, Dict[str, Any]] = {}
    for server_name, cfg in merged.items():
        if not is_server_enabled(server_name, cfg):
            LOGGER.info("MCP config: server '%s' is disabled", server_name)
            continue
        enabled[server_name] = cfg

    LOGGER.info("MCP config: loaded %d enabled server(s)", len(enabled))
    return enabled
