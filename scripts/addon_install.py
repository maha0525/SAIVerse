"""アドオンインストーラの CLI 検証用ツール。

UI を介さずに saiverse.addon_installer をローカルから叩いて、
インストール / 更新 / アンインストールの動作確認を行うためのもの。

導入と更新は UI と同じく二段構え (prepare → confirm) で動く。CLI は対話しない
ので、導入時の質問への答えは ``--answers`` に JSON で渡す。渡さなかった質問は
既定の選択肢で進む。質問の一覧は prepare の結果として表示する。

Usage:
    python scripts/addon_install.py install <repo_url> --commit <sha> [--id <addon_id>] [--answers JSON]
    python scripts/addon_install.py update <addon_id> --commit <sha> [--repo-url <url>] [--answers JSON]
    python scripts/addon_install.py uninstall <addon_id> [--delete-data]
    python scripts/addon_install.py inspect <addon_id>

    --answers の形: '{"engines": ["cloud", "gpt_sovits"]}' (一つだけ選ぶ質問も要素 1 の一覧)
    update の --repo-url を省略すると、カタログ (registry) の repo_url から取得する。

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
import json
import logging
import sys
from pathlib import Path
from typing import Optional

# Allow running as `python scripts/addon_install.py` from repo root
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from saiverse.addon_installer import (  # noqa: E402
    AddonInstallError,
    AddonManifestError,
    PreparedSetup,
    ProgressEvent,
    cancel_install,
    cancel_update,
    confirm_install,
    confirm_update,
    prepare_install,
    prepare_update,
    uninstall_addon,
)
from saiverse.addon_manifest import AddonManifest  # noqa: E402
from saiverse.data_paths import EXPANSION_DATA_DIR  # noqa: E402
from saiverse.i18n_utils import resolve_i18n_text  # noqa: E402


def _print_progress(event: ProgressEvent) -> None:
    prefix = f"[{event.phase}]"
    if event.step_index is not None and event.step_total is not None:
        prefix += f"({event.step_index}/{event.step_total})"
    print(f"{prefix} {event.message}", flush=True)


def _parse_answers(raw: Optional[str]) -> dict:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise AddonInstallError(f"--answers is not valid JSON: {e}") from e
    if not isinstance(data, dict):
        raise AddonInstallError("--answers must be a JSON object")
    return data


def _print_prepared(prepared: PreparedSetup) -> None:
    if prepared.questions:
        print("\n導入時の質問 (--answers で答える。省略した質問は * の既定値):")
        for q in prepared.questions:
            kind = "複数選択" if q["multiple"] else "一つ選択"
            print(f"  {q['id']} ({kind}): {resolve_i18n_text(q['question'])}")
            for c in q["choices"]:
                mark = "*" if c["default"] else " "
                sel = " [選択済み]" if c.get("selected") else ""
                print(f"    {mark} {c['id']}: {resolve_i18n_text(c['label'])}{sel}")
    if prepared.steps:
        print("\nこの OS で実行の候補になる step:")
        for s in prepared.steps:
            cond = f" when={s['when']}" if s["when"] else ""
            env = f" env={s['env']}" if s["env"] else ""
            print(f"  - {s['type']}: {s['name']}{cond}{env}")
    print()


def _cmd_install(args: argparse.Namespace) -> int:
    try:
        answers = _parse_answers(args.answers)
        prepared = prepare_install(
            repo_url=args.repo_url,
            commit=args.commit,
            addon_id=args.id,
            progress_callback=_print_progress,
        )
        _print_prepared(prepared)
        try:
            manifest = confirm_install(
                prepared.addon_id, answers, progress_callback=_print_progress,
            )
        except AddonInstallError:
            # 答えの検証で止まった (rollback 前) なら、prepare の残りを片付ける
            try:
                cancel_install(prepared.addon_id)
            except AddonInstallError:
                pass
            raise
    except AddonInstallError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    print(f"\n[OK] Installed: {manifest.name} v{manifest.version}")
    return 0


def _registry_repo_url(addon_id: str) -> str:
    from saiverse.addon_registry import fetch_registry

    try:
        registry = fetch_registry()
    except RuntimeError as e:
        raise AddonInstallError(f"registry fetch failed (pass --repo-url): {e}") from e
    entry = registry.get_addon(addon_id)
    if entry is None:
        raise AddonInstallError(f"{addon_id} is not in the registry (pass --repo-url)")
    return entry.repo_url


def _cmd_update(args: argparse.Namespace) -> int:
    try:
        answers = _parse_answers(args.answers)
        repo_url = args.repo_url or _registry_repo_url(args.addon_id)
        prepared = prepare_update(
            addon_id=args.addon_id,
            repo_url=repo_url,
            new_commit=args.commit,
            progress_callback=_print_progress,
        )
        if prepared.needs_setup:
            _print_prepared(prepared)
        try:
            manifest = confirm_update(
                args.addon_id, answers, progress_callback=_print_progress,
            )
        except AddonInstallError:
            try:
                cancel_update(args.addon_id)
            except AddonInstallError:
                pass
            raise
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
        print(f"  setup.options:    {len(manifest.setup.options)} question(s)")
        for opt in manifest.setup.options:
            print(f"    - {opt.id}: {', '.join(opt.choice_ids())}")
        print(f"  setup.steps:      {len(manifest.setup.steps)} step(s)")
        for i, step in enumerate(manifest.setup.steps, 1):
            extra = ""
            if step.when:
                extra += f" when={step.when}"
            if step.os:
                extra += f" os={step.os}"
            env = getattr(step, "env", None)  # env は一部の step 種別だけが持つ
            if env:
                extra += f" env={env}"
            print(f"    [{i}] {step.type}: {step.name}{extra}")
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

    answers_help = (
        'Answers to the setup questions as JSON, e.g. \'{"engines": ["cloud"]}\'. '
        "Questions left out use their default choices."
    )

    p_install = sub.add_parser("install", help="Install a new addon")
    p_install.add_argument("repo_url", help="Git repository URL")
    p_install.add_argument("--commit", required=True, help="Commit SHA to checkout")
    p_install.add_argument("--id", default=None, help="Addon ID (default: inferred from repo URL)")
    p_install.add_argument("--answers", default=None, help=answers_help)
    p_install.set_defaults(func=_cmd_install)

    p_update = sub.add_parser("update", help="Update an installed addon to a new commit")
    p_update.add_argument("addon_id", help="Addon ID (= expansion_data/<id>/)")
    p_update.add_argument("--commit", required=True, help="New commit SHA")
    p_update.add_argument(
        "--repo-url", default=None,
        help="Repository to fetch from (default: the registry's repo_url for this addon)",
    )
    p_update.add_argument("--answers", default=None, help=answers_help)
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
