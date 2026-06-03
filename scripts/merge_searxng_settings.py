#!/usr/bin/env python3
"""Generate searxng_settings.yml by merging three layers:

1. SearXNG base settings  (from .searxng-src/searx/settings.yml)
2. SAIVerse engine defaults (searxng_engine_defaults.yml, git tracked)
3. User engine overrides   (searxng_user_engines.yml, gitignored)

Priority: user override > SAIVerse default > SearXNG base

Run this before every SearXNG server start to ensure the latest
SAIVerse defaults and user preferences are applied.
"""

from __future__ import annotations

import os
import re
import secrets
import sys
from pathlib import Path

import yaml

PROBLEMATIC_ENGINES = {"ahmia", "torch", "wikidata", "radiobrowser"}


def main() -> None:
    script_dir = Path(__file__).resolve().parent
    workspace_root = script_dir.parent

    src_dir = _resolve(os.getenv("SEARXNG_SRC_DIR") or "scripts/.searxng-src", workspace_root)
    base_path = src_dir / "searx" / "settings.yml"

    defaults_path = script_dir / "searxng_engine_defaults.yml"
    user_engines_path = script_dir / "searxng_user_engines.yml"

    settings_path = _resolve(
        os.getenv("SEARXNG_SETTINGS_PATH") or "scripts/searxng_settings.yml",
        workspace_root,
    )

    if not base_path.exists():
        print(f"[ERROR] SearXNG base settings not found: {base_path}", file=sys.stderr)
        print("[HINT] Run setup_searxng first.", file=sys.stderr)
        sys.exit(1)

    data = yaml.safe_load(base_path.read_text(encoding="utf-8"))

    _apply_base_patches(data, settings_path)
    _remove_problematic_engines(data)

    saiverse_defaults = _load_yaml_dict(defaults_path)
    user_overrides = _load_yaml_dict(user_engines_path)

    engines = data.get("engines", [])
    for eng in engines:
        name = eng.get("name", "")
        if eng.get("inactive"):
            continue

        user_val = user_overrides.get(name)
        saiverse_val = saiverse_defaults.get(name)

        if user_val is True:
            eng.pop("disabled", None)
        elif user_val is False:
            eng["disabled"] = True
        elif saiverse_val is True:
            eng.pop("disabled", None)
        elif saiverse_val is False:
            eng["disabled"] = True

    if not user_engines_path.exists():
        _generate_user_engines(engines, user_engines_path)
        print(f"[INFO] Generated {user_engines_path}")

    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(
        yaml.safe_dump(data, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    print(f"[OK] Generated {settings_path}")


def _resolve(raw: str, root: Path) -> Path:
    p = Path(raw)
    return p if p.is_absolute() else root / p


def _load_yaml_dict(path: Path) -> dict:
    if not path.exists():
        return {}
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    return raw if isinstance(raw, dict) else {}


def _apply_base_patches(data: dict, settings_path: Path) -> None:
    search = data.setdefault("search", {})
    formats = search.get("formats") or []
    if "json" not in formats:
        formats.append("json")
    search["formats"] = formats
    search.setdefault("safe_search", 0)

    server = data.setdefault("server", {})
    if server.get("bind_address") == "127.0.0.1":
        server["bind_address"] = "0.0.0.0"
    server["limiter"] = False

    existing_secret = None
    if settings_path.exists():
        try:
            existing = yaml.safe_load(settings_path.read_text(encoding="utf-8"))
            existing_secret = (existing or {}).get("server", {}).get("secret_key")
        except Exception:
            pass

    env_secret = os.getenv("SEARXNG_SECRET_KEY")
    if env_secret:
        server["secret_key"] = env_secret
    elif existing_secret:
        server["secret_key"] = existing_secret
    else:
        default_secret = "ultrasecretkey"
        if not server.get("secret_key") or server["secret_key"] == default_secret:
            server["secret_key"] = secrets.token_hex(32)


def _remove_problematic_engines(data: dict) -> None:
    normalize = lambda name: re.sub(r"[\s_-]+", "", name.lower())
    data["engines"] = [
        e for e in data.get("engines", [])
        if normalize(e.get("name", "")) not in PROBLEMATIC_ENGINES
    ]


def _generate_user_engines(engines: list, path: Path) -> None:
    lines = [
        "# SearXNG エンジン設定",
        "# default: SAIVerse推奨に従う（バージョンアップで変わる可能性あり）",
        "# true:    有効（バージョンアップで変わらない）",
        "# false:   無効（バージョンアップで変わらない）",
        "",
    ]
    for eng in engines:
        name = eng.get("name", "")
        if not name:
            continue
        if any(c in name for c in " :#{}[]|>&*!,\"'"):
            key = f'"{name}"'
        else:
            key = name
        lines.append(f"{key}: default")

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
