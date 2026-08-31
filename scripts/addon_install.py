"""アドオンインストーラの CLI 検証用ツール (Phase 1 / Phase 2 用)。

UI を介さずに saiverse.addon_installer をローカルから叩いて、
インストール / 更新 / アンインストールの動作確認を行うためのもの。

Usage:
    python scripts/addon_install.py install <repo_url> --commit <sha> [--id <addon_id>]
    python scripts/addon_install.py update <addon_id> --commit <sha>
    python scripts/addon_install.py uninstall <addon_id> [--delete-data]
    python scripts/addon_install.py inspect <addon_id>

例:
    # Elyth (commit pin は適宜差し替え)
    python scripts/addon_install.py install \\
        https://github.com/maha0525/saiverse-elyth-addon.git \\
        --commit abc123def

    # voice-tts のアンインストール (永続データは残す)
    python scripts/addon_install.py uninstall saiverse-voice-tts

    # 検査 (addon.json の v2 manifest を validate するだけ)
    python scripts/addon_install.py inspect saiverse-elyth-addon
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

# Allow running as `python scripts/addon_install.py` from repo root
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from saiverse.addon_installer import (  # noqa: E402
    AddonInstallError,
    AddonManifestError,
    ProgressEvent,
    install_addon,
    uninstall_addon,
    update_addon,
)
from saiverse.addon_manifest import AddonManifest  # noqa: E402
from saiverse.data_paths import EXPANSION_DATA_DIR  # noqa: E402


def _print_progress(event: ProgressEvent) -> None:
    prefix = f"[{event.phase}]"
    if event.step_index is not None and event.step_total is not None:
        prefix += f"({event.step_index}/{event.step_total})"
    print(f"{prefix} {event.message}", flush=True)


def _cmd_install(args: argparse.Namespace) -> int:
    try:
        manifest = install_addon(
            repo_url=args.repo_url,
            commit=args.commit,
            addon_id=args.id,
            progress_callback=_print_progress,
        )
    except AddonInstallError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    print(f"\n[OK] Installed: {manifest.name} v{manifest.version}")
    return 0


def _cmd_update(args: argparse.Namespace) -> int:
    try:
        manifest = update_addon(
            addon_id=args.addon_id,
            new_commit=args.commit,
            progress_callback=_print_progress,
        )
    except AddonInstallError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    print(f"\n[OK] Updated: {manifest.name} → v{manifest.version}")
    return 0


def _cmd_uninstall(args: argparse.Namespace) -> int:
    try:
        uninstall_addon(
            addon_id=args.addon_id,
            delete_data=args.delete_data,
            progress_callback=_print_progress,
        )
    except AddonInstallError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    print(f"\n[OK] Uninstalled: {args.addon_id}")
    return 0


def _cmd_inspect(args: argparse.Namespace) -> int:
    """addon.json を validate して manifest を表示するだけ (DRY-RUN)。"""
    import json
    from pydantic import ValidationError

    addon_dir = EXPANSION_DATA_DIR / args.addon_id
    manifest_path = addon_dir / "addon.json"
    if not manifest_path.exists():
        print(f"ERROR: addon.json not found at {manifest_path}", file=sys.stderr)
        return 1
    try:
        with open(manifest_path, encoding="utf-8") as f:
            data = json.load(f)
        manifest = AddonManifest.model_validate(data)
    except (json.JSONDecodeError, ValidationError) as e:
        print(f"ERROR: validation failed:\n{e}", file=sys.stderr)
        return 1

    print(f"[OK] {args.addon_id} addon.json is valid")
    print(f"  name:             {manifest.name}")
    print(f"  display_name:     {manifest.display_name}")
    print(f"  version:          {manifest.version}")
    print(f"  manifest_version: {manifest.manifest_version}")
    print(f"  setup_version:    {manifest.setup_version}")
    if manifest.setup:
        print(f"  setup.steps:      {len(manifest.setup.steps)} step(s)")
        for i, step in enumerate(manifest.setup.steps, 1):
            print(f"    [{i}] {step.type}: {step.name}")
    else:
        print("  setup:            (none)")
    if manifest.uninstall:
        print(f"  uninstall.steps:  {len(manifest.uninstall.steps)} step(s)")
    return 0


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    parser = argparse.ArgumentParser(description="SAIVerse addon installer CLI")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_install = sub.add_parser("install", help="Install a new addon")
    p_install.add_argument("repo_url", help="Git repository URL")
    p_install.add_argument("--commit", required=True, help="Commit SHA to checkout")
    p_install.add_argument("--id", default=None, help="Addon ID (default: inferred from repo URL)")
    p_install.set_defaults(func=_cmd_install)

    p_update = sub.add_parser("update", help="Update an installed addon to a new commit")
    p_update.add_argument("addon_id", help="Addon ID (= expansion_data/<id>/)")
    p_update.add_argument("--commit", required=True, help="New commit SHA")
    p_update.set_defaults(func=_cmd_update)

    p_uninstall = sub.add_parser("uninstall", help="Uninstall an addon")
    p_uninstall.add_argument("addon_id", help="Addon ID")
    p_uninstall.add_argument(
        "--delete-data", action="store_true",
        help="Also delete persistent data (~/.saiverse/user_data/addon_data/<id>/)",
    )
    p_uninstall.set_defaults(func=_cmd_uninstall)

    p_inspect = sub.add_parser("inspect", help="Validate addon.json without installing")
    p_inspect.add_argument("addon_id", help="Addon ID (must be already present in expansion_data/)")
    p_inspect.set_defaults(func=_cmd_inspect)

    args = parser.parse_args()
    try:
        return args.func(args)
    except AddonManifestError as e:
        print(f"MANIFEST ERROR: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
