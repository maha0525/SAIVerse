#!/usr/bin/env python
"""Import all playbooks from sea/playbooks/ directory into the database.

This script safely imports playbooks without affecting any other data.
It will:
  • Import all playbooks from sea/playbooks/public/
  • Update existing playbooks with new definitions
  • Preserve all personas, conversations, and other data

Usage:
  python scripts/import_all_playbooks.py
  python scripts/import_all_playbooks.py --directory sea/playbooks/public
  python scripts/import_all_playbooks.py --force  # Skip existing playbooks
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

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from database.paths import default_db_path
from database.models import Base, Playbook

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)


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
                router_callable = data.get("router_callable", False)

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
                            existing.schema_json = json.dumps(schema_payload, ensure_ascii=False)
                            existing.nodes_json = json.dumps(data, ensure_ascii=False)
                            existing.router_callable = router_callable
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
                        scope="public",
                        created_by_persona_id=None,
                        building_id=None,
                        schema_json=schema_json,
                        nodes_json=nodes_json,
                        router_callable=router_callable,
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


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Import all playbooks from sea/playbooks/ directory",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Import all playbooks from default directory
  python scripts/import_all_playbooks.py

  # Update existing playbooks
  python scripts/import_all_playbooks.py --force

  # Dry run to see what would be imported
  python scripts/import_all_playbooks.py --dry-run

  # Import from specific directory
  python scripts/import_all_playbooks.py --directory sea/playbooks/custom
"""
    )
    parser.add_argument(
        "--directory",
        type=Path,
        default=ROOT / "sea" / "playbooks" / "public",
        help="Directory containing playbook JSON files (default: sea/playbooks/public)"
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
    args = parser.parse_args()

    directory = args.directory
    if not directory.is_absolute():
        directory = ROOT / directory

    logging.info(f"Importing playbooks from: {directory}")

    imported, updated, skipped = import_playbooks_from_directory(
        directory,
        force_update=args.force,
        dry_run=args.dry_run
    )

    print("\n" + "=" * 60)
    if args.dry_run:
        print("DRY RUN SUMMARY")
    else:
        print("IMPORT SUMMARY")
    print("=" * 60)
    print(f"Imported: {imported}")
    print(f"Updated:  {updated}")
    print(f"Skipped:  {skipped}")
    print(f"Total:    {imported + updated + skipped}")
    print("=" * 60)

    if args.dry_run:
        print("\nNo changes were made. Remove --dry-run to actually import.")
    elif imported > 0 or updated > 0:
        print("\n✓ Playbooks successfully imported!")
    elif skipped > 0:
        print("\n✓ All playbooks already exist. Use --force to update them.")


if __name__ == "__main__":
    main()
