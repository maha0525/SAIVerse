#!/usr/bin/env python
"""Import all playbooks from builtin_data/playbooks/ directory into the database.

This script safely imports playbooks without affecting any other data.
It will:
  • Import all playbooks from builtin_data/playbooks/public/
  • Update existing playbooks with new definitions
  • Prune file-originated playbooks whose source JSON no longer exists
    (default full-scan mode only; also removes their PlaybookPermission rows)
  • Preserve all personas, conversations, and other data

Usage:
  python scripts/import_all_playbooks.py
  python scripts/import_all_playbooks.py --directory builtin_data/playbooks/public
  python scripts/import_all_playbooks.py --force      # Update existing playbooks
  python scripts/import_all_playbooks.py --no-prune   # Skip orphan deletion
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

# Add project root to path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import hashlib

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from database.paths import default_db_path
from database.models import Base, Playbook, PlaybookPermission

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)


def _canonical_hash(data: dict) -> str:
    canonical = json.dumps(data, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


def import_playbooks_from_directory(
    directory: Path,
    force_update: bool = False,
    dry_run: bool = False
) -> tuple[int, int, int]:
    """Import all playbooks from a directory.

    Args:
        directory: Path to directory containing playbook JSON files
        force_update: If True, update existing playbooks
        dry_run: If True, only show what would be imported without making changes

    Returns:
        Tuple of (imported_count, updated_count, skipped_count)
    """
    if not directory.exists():
        logging.error(f"Directory not found: {directory}")
        return (0, 0, 0)

    db_path = default_db_path()
    if not db_path.exists():
        logging.error(f"Database not found: {db_path}")
        logging.error("Please run 'python database/seed.py' first to create the database.")
        return (0, 0, 0)

    engine = create_engine(f"sqlite:///{db_path}")
    Session = sessionmaker(bind=engine)

    imported_count = 0
    updated_count = 0
    skipped_count = 0

    json_files = sorted(directory.glob("*.json"))
    if not json_files:
        logging.warning(f"No JSON files found in {directory}")
        return (0, 0, 0)

    logging.info(f"Found {len(json_files)} playbook files in {directory}")

    with Session() as session:
        for json_path in json_files:
            try:
                data = json.loads(json_path.read_text(encoding="utf-8"))
                name = data.get("name")

                if not name:
                    logging.warning(f"Skipping {json_path.name}: missing 'name' field")
                    skipped_count += 1
                    continue

                description = data.get("description", "")
                display_name = data.get("display_name")
                router_callable = data.get("router_callable", False)
                user_selectable = data.get("user_selectable", False)
                dev_only = data.get("dev_only", False)
                required_credentials = data.get("required_credentials")
                required_credentials_json = (
                    json.dumps(required_credentials, ensure_ascii=False)
                    if required_credentials else None
                )
                try:
                    source_rel = str(json_path.resolve().relative_to(ROOT))
                except ValueError:
                    source_rel = str(json_path)
                source_hash = _canonical_hash(data)

                # Check if already exists
                existing = session.query(Playbook).filter(Playbook.name == name).first()

                if existing:
                    if force_update:
                        if dry_run:
                            logging.info(f"[DRY RUN] Would update playbook '{name}'")
                        else:
                            # Update existing playbook
                            schema_payload = {
                                "name": name,
                                "description": description,
                                "input_schema": data.get("input_schema", []),
                                "start_node": data.get("start_node"),
                            }
                            existing.description = description
                            existing.display_name = display_name
                            existing.schema_json = json.dumps(schema_payload, ensure_ascii=False)
                            existing.nodes_json = json.dumps(data, ensure_ascii=False)
                            existing.router_callable = router_callable
                            existing.user_selectable = user_selectable
                            existing.dev_only = dev_only
                            existing.required_credentials = required_credentials_json
                            existing.source_file = source_rel
                            existing.source_hash = source_hash
                            updated_count += 1
                            logging.info(f"Updated playbook '{name}' (router_callable={router_callable})")
                    else:
                        logging.info(f"Playbook '{name}' already exists, skipping (use --force to update)")
                        skipped_count += 1
                    continue

                # Import new playbook
                schema_payload = {
                    "name": name,
                    "description": description,
                    "input_schema": data.get("input_schema", []),
                    "start_node": data.get("start_node"),
                }
                nodes_json = json.dumps(data, ensure_ascii=False)
                schema_json = json.dumps(schema_payload, ensure_ascii=False)

                if dry_run:
                    logging.info(f"[DRY RUN] Would import playbook '{name}' (router_callable={router_callable})")
                else:
                    record = Playbook(
                        name=name,
                        description=description,
                        display_name=display_name,
                        scope="public",
                        created_by_persona_id=None,
                        building_id=None,
                        schema_json=schema_json,
                        nodes_json=nodes_json,
                        router_callable=router_callable,
                        user_selectable=user_selectable,
                        dev_only=dev_only,
                        required_credentials=required_credentials_json,
                        source_file=source_rel,
                        source_hash=source_hash,
                    )
                    session.add(record)
                    imported_count += 1
                    logging.info(f"Imported playbook '{name}' (router_callable={router_callable})")

            except Exception as exc:
                logging.error(f"Failed to import {json_path.name}: {exc}")
                skipped_count += 1

        if not dry_run:
            session.commit()

    return (imported_count, updated_count, skipped_count)


def prune_orphan_playbooks(dry_run: bool = False) -> int:
    """Delete file-originated playbooks whose source file no longer exists.

    Targets: scope='public' AND source_file IS NOT NULL AND the referenced
    file is missing on disk. Dynamically created playbooks (source_file IS
    NULL, e.g. via save_playbook tool) and personal/building scope playbooks
    are preserved.

    Also deletes matching PlaybookPermission rows by playbook_name to avoid
    stale permission records being inherited by an unrelated future playbook
    that happens to reuse the same name.
    """
    db_path = default_db_path()
    if not db_path.exists():
        logging.error(f"Database not found: {db_path}")
        return 0

    engine = create_engine(f"sqlite:///{db_path}")
    Session = sessionmaker(bind=engine)

    pruned_count = 0

    with Session() as session:
        candidates = (
            session.query(Playbook)
            .filter(Playbook.scope == "public")
            .filter(Playbook.source_file.isnot(None))
            .all()
        )

        for pb in candidates:
            src_path = Path(pb.source_file)
            if not src_path.is_absolute():
                src_path = ROOT / src_path

            if src_path.exists():
                continue

            if dry_run:
                logging.info(
                    f"[DRY RUN] Would prune orphan playbook '{pb.name}' "
                    f"(missing source: {pb.source_file})"
                )
            else:
                perms_deleted = (
                    session.query(PlaybookPermission)
                    .filter(PlaybookPermission.playbook_name == pb.name)
                    .delete(synchronize_session=False)
                )
                session.delete(pb)
                logging.info(
                    f"Pruned orphan playbook '{pb.name}' "
                    f"(missing source: {pb.source_file}, "
                    f"permissions removed: {perms_deleted})"
                )
            pruned_count += 1

        if not dry_run:
            session.commit()

    return pruned_count


def _collect_default_playbook_dirs() -> list[Path]:
    """Collect playbook directories from builtin_data and expansion_data.

    Returns directories in priority order:
        1. expansion_data/<project>/playbooks/public/  (higher priority)
        2. builtin_data/playbooks/public/               (lower priority)
    """
    dirs = []

    # Expansion data projects
    expansion_dir = ROOT / "expansion_data"
    if expansion_dir.exists():
        for project_dir in sorted(expansion_dir.iterdir()):
            if not project_dir.is_dir() or project_dir.name.startswith(("_", ".")):
                continue
            pb_dir = project_dir / "playbooks" / "public"
            if pb_dir.exists() and any(pb_dir.glob("*.json")):
                dirs.append(pb_dir)

    # Builtin data (always included)
    builtin_pb = ROOT / "builtin_data" / "playbooks" / "public"
    if builtin_pb.exists():
        dirs.append(builtin_pb)

    return dirs


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Import all playbooks from builtin_data/playbooks/ and expansion_data/",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Import all playbooks (builtin_data + expansion_data)
  python scripts/import_all_playbooks.py

  # Update existing playbooks
  python scripts/import_all_playbooks.py --force

  # Dry run to see what would be imported and pruned
  python scripts/import_all_playbooks.py --dry-run

  # Import from specific directory only (prune is automatically skipped)
  python scripts/import_all_playbooks.py --directory builtin_data/playbooks/custom

  # Skip orphan playbook deletion
  python scripts/import_all_playbooks.py --no-prune
"""
    )
    parser.add_argument(
        "--directory",
        type=Path,
        default=None,
        help="Directory containing playbook JSON files (default: auto-scan builtin_data + expansion_data)"
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Update existing playbooks instead of skipping them"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be imported without making changes"
    )
    parser.add_argument(
        "--no-prune",
        action="store_true",
        help="Skip deletion of DB playbooks whose source files no longer exist "
             "(prune runs only in default full-scan mode, never with --directory)"
    )
    args = parser.parse_args()

    total_imported = 0
    total_updated = 0
    total_skipped = 0
    total_pruned = 0

    if args.directory is not None:
        # Explicit directory specified — import from that only
        directory = args.directory
        if not directory.is_absolute():
            directory = ROOT / directory
        directories = [directory]
        full_scan = False
    else:
        # Default: scan builtin_data + expansion_data
        directories = _collect_default_playbook_dirs()
        full_scan = True

    for directory in directories:
        logging.info(f"Importing playbooks from: {directory}")
        imported, updated, skipped = import_playbooks_from_directory(
            directory,
            force_update=args.force,
            dry_run=args.dry_run
        )
        total_imported += imported
        total_updated += updated
        total_skipped += skipped

    # Prune orphan playbooks (only in default full-scan mode)
    if full_scan and not args.no_prune:
        logging.info("Pruning orphan playbooks (missing source files)...")
        total_pruned = prune_orphan_playbooks(dry_run=args.dry_run)
    elif not full_scan:
        logging.info("Skipping prune (--directory specified, partial import)")

    print("\n" + "=" * 60)
    if args.dry_run:
        print("DRY RUN SUMMARY")
    else:
        print("IMPORT SUMMARY")
    print("=" * 60)
    print(f"Imported: {total_imported}")
    print(f"Updated:  {total_updated}")
    print(f"Skipped:  {total_skipped}")
    print(f"Pruned:   {total_pruned}")
    print(f"Total:    {total_imported + total_updated + total_skipped}")
    print(f"Sources:  {len(directories)} directories")
    print("=" * 60)

    if args.dry_run:
        print("\nNo changes were made. Remove --dry-run to actually import.")
    elif total_imported > 0 or total_updated > 0 or total_pruned > 0:
        print("\nPlaybooks successfully imported!")
    elif total_skipped > 0:
        print("\nAll playbooks already exist. Use --force to update them.")


if __name__ == "__main__":
    main()
