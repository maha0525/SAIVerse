#!/usr/bin/env python
"""Migrate existing SAIVerse data to new user_data directory structure.

This script migrates:
- database/data/* -> user_data/database/
- assets/avatars/* -> user_data/icons/
- Updates avatar paths in the database

Usage:
    python scripts/migrate_to_user_data.py
    python scripts/migrate_to_user_data.py --dry-run  # Preview changes
"""

from __future__ import annotations

import argparse
import logging
import re
import shutil
import sys
from pathlib import Path

# Add project root to path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
LOGGER = logging.getLogger(__name__)


def migrate_directory(src: Path, dest: Path, dry_run: bool = False) -> int:
    """Migrate files from source to destination directory.
    
    Returns:
        Number of files migrated
    """
    if not src.exists():
        LOGGER.info("Source directory does not exist: %s", src)
        return 0
    
    dest.mkdir(parents=True, exist_ok=True)
    count = 0
    
    for item in src.iterdir():
        if item.name.startswith(".git"):
            continue
        
        dest_path = dest / item.name
        
        if item.is_file():
            if dry_run:
                LOGGER.info("[DRY RUN] Would copy: %s -> %s", item, dest_path)
            else:
                shutil.copy2(item, dest_path)
                LOGGER.info("Copied: %s -> %s", item, dest_path)
            count += 1
        elif item.is_dir():
            if dry_run:
                LOGGER.info("[DRY RUN] Would copy directory: %s -> %s", item, dest_path)
            else:
                if dest_path.exists():
                    # Merge with existing
                    for sub_item in item.rglob("*"):
                        if sub_item.is_file():
                            rel_path = sub_item.relative_to(item)
                            sub_dest = dest_path / rel_path
                            sub_dest.parent.mkdir(parents=True, exist_ok=True)
                            shutil.copy2(sub_item, sub_dest)
                            count += 1
                else:
                    shutil.copytree(item, dest_path)
                count += 1
    
    return count


def update_avatar_paths_in_db(dry_run: bool = False) -> int:
    """Update avatar paths in database from assets/avatars to user_data/icons.
    
    Returns:
        Number of records updated
    """
    try:
        from sqlalchemy import create_engine, text
        from database.paths import default_db_path
        
        db_path = default_db_path()
        if not db_path.exists():
            LOGGER.warning("Database not found: %s", db_path)
            return 0
        
        engine = create_engine(f"sqlite:///{db_path}")
        
        with engine.connect() as conn:
            # Find all avatar paths that need updating
            result = conn.execute(text(
                "SELECT AIID, AVATAR_IMAGE FROM ai WHERE AVATAR_IMAGE LIKE 'assets/avatars/%'"
            ))
            rows = result.fetchall()
            
            if not rows:
                LOGGER.info("No avatar paths need updating")
                return 0
            
            count = 0
            for row in rows:
                ai_id, old_path = row
                # Replace assets/avatars with user_data/icons
                new_path = re.sub(r'^assets[/\\]avatars[/\\]', 'user_data/icons/', old_path)
                
                if dry_run:
                    LOGGER.info("[DRY RUN] Would update %s: %s -> %s", ai_id, old_path, new_path)
                else:
                    conn.execute(text(
                        "UPDATE ai SET AVATAR_IMAGE = :new_path WHERE AIID = :ai_id"
                    ), {"new_path": new_path, "ai_id": ai_id})
                    LOGGER.info("Updated %s: %s -> %s", ai_id, old_path, new_path)
                count += 1
            
            if not dry_run:
                conn.commit()
            
            return count
    
    except Exception as exc:
        LOGGER.error("Failed to update database: %s", exc)
        return 0


def main():
    parser = argparse.ArgumentParser(
        description="Migrate SAIVerse data to new user_data directory structure"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview changes without making them"
    )
    args = parser.parse_args()
    
    dry_run = args.dry_run
    if dry_run:
        LOGGER.info("=== DRY RUN MODE - No changes will be made ===")
    
    print("\n" + "=" * 60)
    print("SAIVerse Data Migration")
    print("=" * 60)
    
    # 1. Migrate database
    db_src = ROOT / "database" / "data"
    db_dest = ROOT / "user_data" / "database"
    LOGGER.info("\n--- Migrating database ---")
    db_count = migrate_directory(db_src, db_dest, dry_run)
    
    # 2. Migrate avatars
    avatar_src = ROOT / "assets" / "avatars"
    avatar_dest = ROOT / "user_data" / "icons"
    LOGGER.info("\n--- Migrating avatars ---")
    avatar_count = migrate_directory(avatar_src, avatar_dest, dry_run)
    
    # 3. Update database paths
    LOGGER.info("\n--- Updating database avatar paths ---")
    db_update_count = update_avatar_paths_in_db(dry_run)
    
    # Summary
    print("\n" + "=" * 60)
    if dry_run:
        print("DRY RUN SUMMARY")
    else:
        print("MIGRATION SUMMARY")
    print("=" * 60)
    print(f"Database files migrated: {db_count}")
    print(f"Avatar files migrated:   {avatar_count}")
    print(f"DB records updated:      {db_update_count}")
    print("=" * 60)
    
    if dry_run:
        print("\nNo changes were made. Remove --dry-run to apply migration.")
    else:
        print("\n✓ Migration completed!")
        print("\nNote: Original files were copied, not moved.")
        print("After verifying the migration, you can manually delete:")
        print(f"  - {db_src}")
        print(f"  - {avatar_src}")


if __name__ == "__main__":
    main()
