"""Load MCP server definitions from SAIVerse data sources."""
from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Dict

LOGGER = logging.getLogger(__name__)

_ENV_VAR_RE = re.compile(r"\$\{([^}]+)\}")
_CONFIG_FILENAME = "mcp_servers.json"


def _interpolate_env(value: str) -> str:
    """Replace ``${ENV_VAR}`` placeholders with environment variable values."""

    def _replace(match: re.Match[str]) -> str:
        var_name = match.group(1)
        resolved = os.getenv(var_name)
        if resolved is None:
            LOGGER.warning("MCP config: environment variable '%s' is not set", var_name)
            return match.group(0)
        return resolved

    return _ENV_VAR_RE.sub(_replace, value)


def _interpolate_value(value: Any) -> Any:
    if isinstance(value, str):
        return _interpolate_env(value)
    if isinstance(value, list):
        return [_interpolate_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _interpolate_value(item) for key, item in value.items()}
    return value


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


def load_mcp_configs() -> Dict[str, Dict[str, Any]]:
    """Load enabled MCP server configs with SAIVerse priority rules.

    Priority:
      1. ``~/.saiverse/user_data/mcp_servers.json``
      2. ``~/.saiverse/user_data/<project>/mcp_servers.json``
      3. ``expansion_data/<pack>/mcp_servers.json``
      4. ``builtin_data/mcp_servers.json``
    """
    merged: Dict[str, Dict[str, Any]] = {}

    for path in _iter_candidate_paths():
        for server_name, cfg in _load_config_file(path).items():
            if server_name in merged:
                LOGGER.debug("MCP config: '%s' from %s ignored by higher-priority source", server_name, path)
                continue
            cfg_copy = _interpolate_value(cfg)
            cfg_copy["_source_path"] = str(path)
            merged[server_name] = cfg_copy
            LOGGER.debug("MCP config: loaded '%s' from %s", server_name, path)

    enabled: Dict[str, Dict[str, Any]] = {}
    for server_name, cfg in merged.items():
        if not cfg.get("enabled", True):
            LOGGER.info("MCP config: server '%s' is disabled", server_name)
            continue
        enabled[server_name] = cfg

    LOGGER.info("MCP config: loaded %d enabled server(s)", len(enabled))
    return enabled
