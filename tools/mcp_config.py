"""MCP server configuration loader.

Loads MCP server definitions from ``mcp_servers.json`` files found in the
three-tier priority system (user_data > expansion_data > builtin_data).

Configuration format (Claude Desktop compatible)::

    {
      "mcpServers": {
        "server_name": {
          "command": "npx",
          "args": ["-y", "@modelcontextprotocol/server-filesystem", "/path"],
          "env": {"KEY": "${ENV_VAR}"},
          "enabled": true
        }
      }
    }

Transport detection:
- ``command`` key present → stdio transport
- ``url`` key present → remote transport (``transport`` field: "streamable_http" or "sse")
"""
from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Dict

LOGGER = logging.getLogger(__name__)

_ENV_VAR_RE = re.compile(r"\$\{([^}]+)\}")


def _interpolate_env(value: str) -> str:
    """Replace ``${ENV_VAR}`` placeholders with environment variable values."""

    def _replacer(match: re.Match) -> str:
        var_name = match.group(1)
        env_val = os.environ.get(var_name)
        if env_val is None:
            LOGGER.warning("MCP config: environment variable '%s' is not set", var_name)
            return match.group(0)  # keep placeholder as-is
        return env_val

    return _ENV_VAR_RE.sub(_replacer, value)


def _interpolate_env_dict(env: Dict[str, str]) -> Dict[str, str]:
    """Interpolate environment variables in an env dict."""
    return {k: _interpolate_env(v) for k, v in env.items()}


def _load_single_config(path: Path) -> Dict[str, Dict[str, Any]]:
    """Load a single mcp_servers.json and return server_name -> config dict."""
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        LOGGER.warning("MCP config: failed to read %s: %s", path, exc)
        return {}

    servers = data.get("mcpServers", {})
    if not isinstance(servers, dict):
        LOGGER.warning("MCP config: 'mcpServers' in %s is not a dict, skipping", path)
        return {}

    result: Dict[str, Dict[str, Any]] = {}
    for name, cfg in servers.items():
        if not isinstance(cfg, dict):
            LOGGER.warning("MCP config: server '%s' in %s has non-dict config, skipping", name, path)
            continue
        result[name] = cfg

    return result


def load_mcp_configs() -> Dict[str, Dict[str, Any]]:
    """Load MCP server configurations from all data paths.

    Priority: user_data > expansion_data > builtin_data.
    Later sources do NOT override earlier ones (higher-priority wins).

    Returns:
        Dict mapping server_name to its configuration dict.
        Only enabled servers are included.
    """
    from saiverse.data_paths import USER_DATA_DIR, EXPANSION_DATA_DIR, BUILTIN_DATA_DIR

    merged: Dict[str, Dict[str, Any]] = {}
    config_filename = "mcp_servers.json"

    # 1. User data (highest priority)
    user_config = USER_DATA_DIR / config_filename
    for name, cfg in _load_single_config(user_config).items():
        if name not in merged:
            merged[name] = cfg
            LOGGER.debug("MCP config: loaded server '%s' from user_data", name)

    # 2. Expansion data (middle priority) — scan all project subdirs
    if EXPANSION_DATA_DIR.exists():
        for project_dir in sorted(EXPANSION_DATA_DIR.iterdir()):
            if not project_dir.is_dir() or project_dir.name.startswith(("_", ".")):
                continue
            exp_config = project_dir / config_filename
            for name, cfg in _load_single_config(exp_config).items():
                if name not in merged:
                    merged[name] = cfg
                    LOGGER.debug("MCP config: loaded server '%s' from expansion_data/%s", name, project_dir.name)

    # 3. Builtin data (lowest priority)
    builtin_config = BUILTIN_DATA_DIR / config_filename
    for name, cfg in _load_single_config(builtin_config).items():
        if name not in merged:
            merged[name] = cfg
            LOGGER.debug("MCP config: loaded server '%s' from builtin_data", name)

    # Filter out disabled servers and interpolate env vars
    result: Dict[str, Dict[str, Any]] = {}
    for name, cfg in merged.items():
        if not cfg.get("enabled", True):
            LOGGER.info("MCP config: server '%s' is disabled, skipping", name)
            continue

        # Interpolate environment variables in the env dict
        if "env" in cfg and isinstance(cfg["env"], dict):
            cfg["env"] = _interpolate_env_dict(cfg["env"])

        result[name] = cfg

    LOGGER.info("MCP config: loaded %d server(s) from config files", len(result))
    return result
