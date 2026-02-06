#!/usr/bin/env python3
"""Setup script for SAIVerse test environment.

This script creates a clean test environment with:
- test_data/.saiverse/ - Replaces ~/.saiverse for testing
- test_data/user_data/ - Replaces PROJECT_ROOT/user_data for testing

Usage:
    python test_fixtures/setup_test_env.py           # Full setup
    python test_fixtures/setup_test_env.py --clean   # Delete and recreate
    python test_fixtures/setup_test_env.py --reset-db       # Reset DB only
    python test_fixtures/setup_test_env.py --reset-memory   # Reset SAIMemory only

Environment variables set by this script (for start_test_server.sh):
    SAIVERSE_HOME=test_data/.saiverse
    SAIVERSE_USER_DATA_DIR=test_data/user_data
"""

import argparse
import json
import logging
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

# Add project root to path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from database.models import Base, User, City, AI, Building, BuildingOccupancyLog, Playbook

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
LOGGER = logging.getLogger(__name__)

# Paths
TEST_DATA_DIR = PROJECT_ROOT / "test_data"
TEST_SAIVERSE_HOME = TEST_DATA_DIR / ".saiverse"
TEST_USER_DATA = TEST_DATA_DIR / "user_data"
TEST_DB_PATH = TEST_USER_DATA / "database" / "saiverse.db"
DEFINITIONS_PATH = PROJECT_ROOT / "test_fixtures" / "definitions" / "test_data.json"


def load_definitions() -> dict:
    """Load test data definitions from JSON."""
    with open(DEFINITIONS_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def create_directory_structure():
    """Create the test_data directory structure."""
    LOGGER.info("Creating directory structure...")

    # .saiverse directories
    dirs = [
        TEST_SAIVERSE_HOME / "personas",
        TEST_SAIVERSE_HOME / "cities",
        TEST_SAIVERSE_HOME / "buildings",
        TEST_SAIVERSE_HOME / "image",
        TEST_SAIVERSE_HOME / "documents",
        TEST_SAIVERSE_HOME / "qdrant",
        TEST_SAIVERSE_HOME / "backups",
        # user_data directories
        TEST_USER_DATA / "database",
        TEST_USER_DATA / "tools",
        TEST_USER_DATA / "playbooks",
        TEST_USER_DATA / "models",
        TEST_USER_DATA / "prompts",
        TEST_USER_DATA / "icons",
    ]

    for d in dirs:
        d.mkdir(parents=True, exist_ok=True)
        LOGGER.debug(f"Created: {d}")

    LOGGER.info(f"Directory structure created at {TEST_DATA_DIR}")


def seed_database(definitions: dict):
    """Create and seed the test database."""
    LOGGER.info(f"Creating test database at {TEST_DB_PATH}...")

    # Ensure parent directory exists
    TEST_DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    # Remove existing DB
    if TEST_DB_PATH.exists():
        TEST_DB_PATH.unlink()

    # Create engine and tables
    engine = create_engine(f"sqlite:///{TEST_DB_PATH}")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)

    with Session() as session:
        # Add user
        user_data = definitions["user"]
        user = User(**user_data)
        session.add(user)
        LOGGER.info(f"Added user: {user_data['USERNAME']}")

        # Add city
        city_data = definitions["city"]
        city = City(USERID=user_data["USERID"], **city_data)
        session.add(city)
        LOGGER.info(f"Added city: {city_data['CITYNAME']}")

        # Add buildings
        for bldg_data in definitions["buildings"]:
            building = Building(CITYID=city_data["CITYID"], **bldg_data)
            session.add(building)
            LOGGER.info(f"Added building: {bldg_data['BUILDINGNAME']}")

        # Flush to ensure buildings exist before adding personas
        session.flush()

        # Add personas
        for persona_data in definitions["personas"]:
            start_building = persona_data.pop("start_building", None)
            persona = AI(HOME_CITYID=city_data["CITYID"], **persona_data)
            session.add(persona)
            LOGGER.info(f"Added persona: {persona_data['AINAME']}")

            # Create persona directories
            persona_dir = TEST_SAIVERSE_HOME / "personas" / persona_data["AIID"]
            persona_dir.mkdir(parents=True, exist_ok=True)

            # Add initial occupancy if start_building specified
            if start_building:
                log = BuildingOccupancyLog(
                    CITYID=city_data["CITYID"],
                    BUILDINGID=start_building,
                    AIID=persona_data["AIID"],
                    ENTRY_TIMESTAMP=datetime.now(),
                    EXIT_TIMESTAMP=None,
                )
                session.add(log)

        session.commit()

    LOGGER.info("Database seeded successfully.")


def import_playbooks(definitions: dict):
    """Import specified playbooks from builtin_data."""
    playbook_names = definitions.get("playbooks", [])
    if not playbook_names:
        LOGGER.info("No playbooks to import.")
        return

    playbooks_dir = PROJECT_ROOT / "sea" / "playbooks" / "public"
    if not playbooks_dir.exists():
        LOGGER.warning(f"Playbooks directory not found: {playbooks_dir}")
        return

    engine = create_engine(f"sqlite:///{TEST_DB_PATH}")
    Session = sessionmaker(bind=engine)

    imported = 0
    with Session() as session:
        for name in playbook_names:
            json_path = playbooks_dir / f"{name}.json"
            if not json_path.exists():
                LOGGER.warning(f"Playbook not found: {json_path}")
                continue

            try:
                data = json.loads(json_path.read_text(encoding="utf-8"))
                description = data.get("description", "")
                router_callable = data.get("router_callable", False)
                user_selectable = data.get("user_selectable", False)

                schema_payload = {
                    "name": name,
                    "description": description,
                    "input_schema": data.get("input_schema", []),
                    "start_node": data.get("start_node"),
                }

                record = Playbook(
                    name=name,
                    description=description,
                    scope="public",
                    schema_json=json.dumps(schema_payload, ensure_ascii=False),
                    nodes_json=json.dumps(data, ensure_ascii=False),
                    router_callable=router_callable,
                    user_selectable=user_selectable,
                )
                session.add(record)
                imported += 1
                LOGGER.info(f"Imported playbook: {name}")
            except Exception as e:
                LOGGER.error(f"Failed to import {name}: {e}")

        session.commit()

    LOGGER.info(f"Imported {imported} playbooks.")


def reset_database():
    """Reset only the database."""
    definitions = load_definitions()
    seed_database(definitions)
    import_playbooks(definitions)


def reset_memory():
    """Reset only SAIMemory data."""
    definitions = load_definitions()

    LOGGER.info("Resetting SAIMemory data...")
    personas_dir = TEST_SAIVERSE_HOME / "personas"

    for persona_data in definitions["personas"]:
        persona_id = persona_data["AIID"]
        persona_dir = personas_dir / persona_id

        # Remove existing memory.db if exists
        memory_db = persona_dir / "memory.db"
        if memory_db.exists():
            memory_db.unlink()
            LOGGER.info(f"Removed: {memory_db}")

        # Ensure directory exists
        persona_dir.mkdir(parents=True, exist_ok=True)

    # Reset Qdrant data
    qdrant_dir = TEST_SAIVERSE_HOME / "qdrant"
    if qdrant_dir.exists():
        shutil.rmtree(qdrant_dir)
        qdrant_dir.mkdir(parents=True, exist_ok=True)
        LOGGER.info("Reset Qdrant data.")

    LOGGER.info("SAIMemory data reset complete.")


def clean_all():
    """Delete and recreate everything."""
    LOGGER.info("Cleaning test environment...")

    if TEST_DATA_DIR.exists():
        shutil.rmtree(TEST_DATA_DIR)
        LOGGER.info(f"Removed: {TEST_DATA_DIR}")

    setup_full()


def setup_full():
    """Full setup of test environment."""
    LOGGER.info("Setting up test environment...")

    definitions = load_definitions()

    create_directory_structure()
    seed_database(definitions)
    import_playbooks(definitions)

    LOGGER.info("")
    LOGGER.info("=" * 60)
    LOGGER.info("Test environment setup complete!")
    LOGGER.info("=" * 60)
    LOGGER.info("")
    LOGGER.info("To start the test server, run:")
    LOGGER.info("")
    LOGGER.info("  ./test_fixtures/start_test_server.sh")
    LOGGER.info("")
    LOGGER.info("Or manually set environment variables:")
    LOGGER.info("")
    LOGGER.info(f"  export SAIVERSE_HOME={TEST_SAIVERSE_HOME}")
    LOGGER.info(f"  export SAIVERSE_USER_DATA_DIR={TEST_USER_DATA}")
    LOGGER.info(f"  python main.py test_city --db-file {TEST_DB_PATH}")
    LOGGER.info("")


def main():
    parser = argparse.ArgumentParser(
        description="Setup SAIVerse test environment",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__
    )
    parser.add_argument(
        "--clean",
        action="store_true",
        help="Delete and recreate everything"
    )
    parser.add_argument(
        "--reset-db",
        action="store_true",
        help="Reset database only"
    )
    parser.add_argument(
        "--reset-memory",
        action="store_true",
        help="Reset SAIMemory data only"
    )

    args = parser.parse_args()

    if args.clean:
        clean_all()
    elif args.reset_db:
        reset_database()
    elif args.reset_memory:
        reset_memory()
    else:
        setup_full()


if __name__ == "__main__":
    main()
