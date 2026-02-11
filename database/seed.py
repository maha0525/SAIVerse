import json
import logging
import os
import sys
from pathlib import Path
from datetime import datetime

# Ensure project root is on sys.path so saiverse package is importable
# (when run as `python database/seed.py`, sys.path[0] is database/ not project root)
_project_root = str(Path(__file__).resolve().parent.parent)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from models import Base, User, City, AI, Building, BuildingOccupancyLog, Blueprint, Tool, Playbook

try:  # pragma: no cover - supports running as script or module
    from .paths import default_db_path, ensure_data_dir
except ImportError:
    from paths import default_db_path, ensure_data_dir  # type: ignore

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')


def import_initial_playbooks() -> None:
    """Import all playbooks from builtin_data/playbooks/public/ directory."""
    playbooks_dir = Path(__file__).parent.parent / "builtin_data" / "playbooks" / "public"
    if not playbooks_dir.exists():
        logging.warning(f"Playbooks directory not found: {playbooks_dir}")
        return

    db_path = default_db_path()
    engine = create_engine(f"sqlite:///{db_path}")
    Session = sessionmaker(bind=engine)

    imported_count = 0
    with Session() as session:
        for json_path in sorted(playbooks_dir.glob("*.json")):
            try:
                data = json.loads(json_path.read_text(encoding="utf-8"))
                name = data.get("name")
                if not name:
                    logging.warning(f"Skipping {json_path.name}: missing 'name' field")
                    continue

                # Check if already imported
                existing = session.query(Playbook).filter(Playbook.name == name).first()
                if existing:
                    logging.info(f"Playbook '{name}' already exists, skipping.")
                    continue

                description = data.get("description", "")
                router_callable = data.get("router_callable", False)
                schema_payload = {
                    "name": name,
                    "description": description,
                    "input_schema": data.get("input_schema", []),
                    "start_node": data.get("start_node"),
                }
                nodes_json = json.dumps(data, ensure_ascii=False)
                schema_json = json.dumps(schema_payload, ensure_ascii=False)

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
                logging.info(f"Imported playbook '{name}' (router_callable={router_callable}).")
            except Exception as exc:
                logging.error(f"Failed to import {json_path.name}: {exc}")

        session.commit()

    logging.info(f"Playbook import completed: {imported_count} playbooks imported.")


# ---------------------------------------------------------------------------
# Seed data helpers
# ---------------------------------------------------------------------------

def _load_seed_data() -> dict:
    """Load seed data from builtin_data/seed_data.json."""
    seed_path = Path(__file__).parent.parent / "builtin_data" / "seed_data.json"
    if not seed_path.exists():
        raise FileNotFoundError(f"Seed data not found: {seed_path}")
    return json.loads(seed_path.read_text(encoding="utf-8"))


def _expand(template: str, *, city: str, username: str) -> str:
    """Expand {city} and {username} placeholders in a template string."""
    return template.replace("{city}", city).replace("{username}", username)


def _create_user(db, user_config: dict) -> None:
    """Create the default user if not exists."""
    if not db.query(User).first():
        db.add(User(
            USERID=user_config["userid"],
            USERNAME=user_config["username"],
            PASSWORD=user_config["password"],
            LOGGED_IN=False,
        ))
        logging.info("Added default user.")


def _create_cities(db, cities_json_path: Path, seed_data: dict) -> dict:
    """Create City records from cities.json. Returns city_map {name: id}."""
    if not cities_json_path.exists():
        raise FileNotFoundError("builtin_data/cities.json not found!")

    with open(cities_json_path, "r", encoding="utf-8") as f:
        cities_config = json.load(f)

    city_map = {}
    for city_name, config in cities_config.items():
        timezone_name = config.get("timezone", "UTC")
        # Look up online mode from seed_data; default to False
        city_seed = seed_data.get("cities", {}).get(city_name, {})
        online_mode = city_seed.get("start_in_online_mode", False)

        new_city = City(
            USERID=1,
            CITYNAME=city_name,
            DESCRIPTION=f"{city_name}の街です。",
            UI_PORT=config["ui_port"],
            API_PORT=config["api_port"],
            START_IN_ONLINE_MODE=online_mode,
            TIMEZONE=timezone_name,
        )
        db.add(new_city)
        db.flush()
        city_map[city_name] = new_city.CITYID
        logging.info(f"Added city '{city_name}' with ID {new_city.CITYID}.")

    return city_map


def _create_city_data(db, city_name: str, city_id: int, city_config: dict, username: str) -> None:
    """Create personas, buildings, and initial occupancy for one city."""
    # --- Personas ---
    ai_objects = []
    for p_def in city_config.get("personas", []):
        ai_id = f"{p_def['id_suffix']}_{city_name}"
        ai = AI(
            AIID=ai_id,
            HOME_CITYID=city_id,
            AINAME=p_def["name"],
            SYSTEMPROMPT=p_def.get("system_prompt", ""),
            DESCRIPTION=p_def.get("description", ""),
            AUTO_COUNT=0,
            INTERACTION_MODE=p_def.get("interaction_mode", "auto"),
            DEFAULT_MODEL=p_def.get("default_model"),
        )
        ai_objects.append((ai, p_def))
    db.add_all([ai for ai, _ in ai_objects])
    logging.info(f"Added {len(ai_objects)} personas for city '{city_name}'.")

    # --- Explicit buildings ---
    for b_def in city_config.get("buildings", []):
        building_id = _expand(b_def["id_template"], city=city_name, username=username)
        # name_template takes priority over name
        if "name_template" in b_def:
            building_name = _expand(b_def["name_template"], city=city_name, username=username)
        else:
            building_name = b_def.get("name", "")
        db.add(Building(
            CITYID=city_id,
            BUILDINGID=building_id,
            BUILDINGNAME=building_name,
            CAPACITY=b_def.get("capacity", 10),
            SYSTEM_INSTRUCTION=b_def.get("system_instruction", ""),
            DESCRIPTION=b_def.get("description", ""),
        ))

    # --- Private rooms for personas ---
    for ai, p_def in ai_objects:
        if p_def.get("has_private_room", False):
            room_id = f"{p_def['name'].lower()}_{city_name}_room"
            ai.PRIVATE_ROOM_ID = room_id
            db.add(Building(
                CITYID=city_id,
                BUILDINGID=room_id,
                BUILDINGNAME=f"{p_def['name']}の部屋",
                CAPACITY=1,
                SYSTEM_INSTRUCTION=f"{p_def['name']}が待機する個室です。",
                DESCRIPTION=f"{p_def['name']}のプライベートルーム。",
            ))

    logging.info(f"Added buildings for city '{city_name}'.")

    # --- Initial occupancy ---
    for ai, p_def in ai_objects:
        if p_def.get("initial_building"):
            home = _expand(p_def["initial_building"], city=city_name, username=username)
        elif p_def.get("has_private_room"):
            home = ai.PRIVATE_ROOM_ID
        else:
            logging.warning(f"No initial placement for persona '{ai.AIID}', skipping.")
            continue
        db.add(BuildingOccupancyLog(
            CITYID=city_id,
            AIID=ai.AIID,
            BUILDINGID=home,
            ENTRY_TIMESTAMP=datetime.now(),
        ))
    logging.info(f"Added initial occupancy for city '{city_name}'.")


def _create_tools(db, tools_config: list) -> None:
    """Create default Tool records if none exist."""
    if not db.query(Tool).first():
        for t in tools_config:
            db.add(Tool(
                TOOLNAME=t["name"],
                DESCRIPTION=t["description"],
                MODULE_PATH=t["module_path"],
                FUNCTION_NAME=t["function_name"],
            ))
        logging.info(f"Added {len(tools_config)} default tools.")


# ---------------------------------------------------------------------------
# Main seed function
# ---------------------------------------------------------------------------

def seed_database(force: bool = False):
    """
    Creates and seeds a new unified database from seed_data.json and cities.json.

    ⚠️  WARNING: This will DELETE your existing database and ALL data! ⚠️

    This includes:
    - All personas (AI entities)
    - All conversation history
    - All building occupancy logs
    - All playbooks
    - All items and locations

    Use this ONLY for initial setup or when you want to reset everything.
    For updating playbooks only, use scripts/import_all_playbooks.py instead.

    Args:
        force: If True, skip confirmation prompt. Use with extreme caution.
    """
    DB_PATH = default_db_path()
    ensure_data_dir()

    # --- 1. Safety check: warn user about existing DB ---
    if DB_PATH.exists():
        if not force:
            print("\n" + "=" * 70)
            print("⚠️  WARNING: EXISTING DATABASE FOUND ⚠️")
            print("=" * 70)
            print(f"Database path: {DB_PATH}")
            print(f"Database size: {DB_PATH.stat().st_size / 1024:.1f} KB")
            print("\nThis operation will PERMANENTLY DELETE:")
            print("  • All personas (AI characters)")
            print("  • All conversation history")
            print("  • All playbooks")
            print("  • All items and locations")
            print("  • All building occupancy logs")
            print("\nA backup will be created, but you should manually backup important data.")
            print("=" * 70)

            response = input("\nType 'DELETE' in ALL CAPS to confirm deletion: ")
            if response != "DELETE":
                print("\n✓ Operation cancelled. Database preserved.")
                return

        # Create backup before deletion
        backup_path = DB_PATH.parent / f"{DB_PATH.name}_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.bak"
        import shutil
        shutil.copy2(DB_PATH, backup_path)
        logging.info(f"Created backup: {backup_path}")

        logging.warning(f"Deleting existing database: {DB_PATH}")
        os.remove(DB_PATH)

    # --- 2. Load seed data ---
    seed_data = _load_seed_data()
    username = seed_data["user"]["username"]

    # --- 3. Create new DB and tables ---
    engine = create_engine(f"sqlite:///{DB_PATH}")
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    db = SessionLocal()

    try:
        # --- 4. Populate User ---
        _create_user(db, seed_data["user"])

        # --- 5. Populate Cities ---
        cities_json_path = Path(__file__).parent.parent / "builtin_data" / "cities.json"
        city_map = _create_cities(db, cities_json_path, seed_data)

        # --- 6. Populate AI, Buildings, Occupancy per city ---
        for city_name, city_id in city_map.items():
            city_config = seed_data.get("cities", {}).get(city_name)
            if not city_config:
                logging.warning(
                    f"No seed data for city '{city_name}'. "
                    f"Skipping AI and Building creation."
                )
                continue
            _create_city_data(db, city_name, city_id, city_config, username)

        # --- 7. Set default user's initial location ---
        db.flush()
        initial_city_name = seed_data.get("initial_city", "city_a")
        initial_city_id = city_map.get(initial_city_name)
        initial_building_id = f"user_room_{initial_city_name}"

        user_to_update = db.query(User).filter_by(
            USERID=seed_data["user"]["userid"]
        ).first()
        if user_to_update:
            building_exists = db.query(Building).filter_by(
                BUILDINGID=initial_building_id
            ).first()
            if initial_city_id and building_exists:
                user_to_update.CURRENT_CITYID = initial_city_id
                user_to_update.CURRENT_BUILDINGID = initial_building_id
                logging.info(
                    f"Set initial location for default user to "
                    f"'{initial_building_id}' in city '{initial_city_name}'."
                )
            else:
                logging.warning(
                    f"Could not set initial location for default user. "
                    f"'{initial_building_id}' not found."
                )
        else:
            logging.warning("Default user not found.")

        # --- 8. Populate Tools ---
        _create_tools(db, seed_data.get("default_tools", []))

        db.commit()
        logging.info("Database seeding completed successfully.")

    except Exception as e:
        db.rollback()
        logging.error(f"An error occurred during database seeding: {e}", exc_info=True)
    finally:
        db.close()

    # Import initial playbooks after DB seeding
    try:
        import_initial_playbooks()
    except Exception as e:
        logging.error(f"An error occurred during playbook import: {e}", exc_info=True)

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(
        description="Initialize SAIVerse database (⚠️  DESTRUCTIVE OPERATION ⚠️)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
WARNING: This will delete your existing database and all data!

For safer operations:
  - To update playbooks only: python scripts/import_all_playbooks.py
  - To migrate schema: python database/migrate.py --db database/data/saiverse.db
"""
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Skip confirmation prompt (USE WITH EXTREME CAUTION)"
    )
    args = parser.parse_args()

    seed_database(force=args.force)
