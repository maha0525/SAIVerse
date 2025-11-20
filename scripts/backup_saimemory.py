#!/usr/bin/env python3
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sai_memory.backup import BackupError, run_backup

load_dotenv()

DEFAULT_BACKUP_ROOT = Path.home() / ".saiverse" / "backups" / "saimemory_rdiff"
DEFAULT_PERSONA_ROOT = Path.home() / ".saiverse" / "personas"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Create full or incremental SAIMemory backups using rdiff-backup.")
    parser.add_argument("personas", nargs="+", help="Persona IDs (maps to ~/.saiverse/personas/<persona>/memory.db)")
    parser.add_argument(
        "--output-dir",
        default=str(DEFAULT_BACKUP_ROOT),
        help=f"Backup repository root (default: {DEFAULT_BACKUP_ROOT})",
    )
    parser.add_argument(
        "--full",
        action="store_true",
        help="Rotate the existing repository and create a fresh full backup.",
    )
    parser.add_argument(
        "--rdiff-path",
        help="Optional explicit path to rdiff-backup binary.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable verbose logging output.",
    )
    return parser.parse_args()


def _persona_db_path(persona: str) -> Path:
    return DEFAULT_PERSONA_ROOT / persona / "memory.db"


def main() -> None:
    args = parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s: %(message)s",
    )

    output_root = Path(args.output_dir).expanduser()
    output_root.mkdir(parents=True, exist_ok=True)

    status = 0
    for persona in args.personas:
        db_path = _persona_db_path(persona)
        try:
            repo = run_backup(
                persona_id=persona,
                db_path=db_path,
                output_root=output_root,
                rdiff_path=args.rdiff_path,
                force_full=args.full,
            )
            logging.info("Backup completed: persona=%s repo=%s", persona, repo)
        except BackupError as exc:
            logging.error("Failed to back up %s: %s", persona, exc)
            status = 1
    sys.exit(status)


if __name__ == "__main__":
    main()
