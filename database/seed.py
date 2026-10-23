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

# The same .env main.py loads, before the database path is resolved: with a
# SAIVERSE_HOME written there, main.py uses the database under it, and this
# script must create (or replace) that one, not the one under ~/.saiverse.
from scripts.update_engine import load_saiverse_dotenv  # noqa: E402

load_saiverse_dotenv(Path(_project_root))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from models import Base, User, City, AI, Building, BuildingOccupancyLog, Blueprint, Tool, Playbook
from saiverse.i18n_utils import split_i18n_columns

try:  # pragma: no cover - supports running as script or module
    from .paths import default_db_path, ensure_data_dir
except ImportError:
    from paths import default_db_path, ensure_data_dir  # type: ignore

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')


def import_initial_playbooks(db_path: Path | None = None) -> None:
    """Import all playbooks from builtin_data/playbooks/public/ directory into
    ``db_path`` (default: ``default_db_path()``)."""
    playbooks_dir = Path(__file__).parent.parent / "builtin_data" / "playbooks" / "public"
    if not playbooks_dir.exists():
        logging.warning(f"Playbooks directory not found: {playbooks_dir}")
        return

    db_path = db_path or default_db_path()
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

                # description / display_name may be a language dict ({ja, en});
                # the DB columns are strings, so split into base + ``_en``.
                description, description_en = split_i18n_columns(
                    data.get("description"), alt_en=data.get("description_en"),
                )
                description = description or ""
                display_name, display_name_en = split_i18n_columns(
                    data.get("display_name"), alt_en=data.get("display_name_en"),
                )
                router_callable = data.get("router_callable", False)
                user_selectable = data.get("user_selectable", False)
                dev_only = data.get("dev_only", False)
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
                    description_en=description_en,
                    display_name=display_name,
                    display_name_en=display_name_en,
                    scope="public",
                    created_by_persona_id=None,
                    building_id=None,
                    schema_json=schema_json,
                    nodes_json=nodes_json,
                    router_callable=router_callable,
                    user_selectable=user_selectable,
                    dev_only=dev_only,
                )
                session.add(record)
                imported_count += 1
                logging.info(f"Imported playbook '{name}' (router_callable={router_callable}).")
            except Exception as exc:
                logging.error(f"Failed to import {json_path.name}: {exc}")

        session.commit()
    # Close the pooled SQLite connection too: seed_database_if_missing moves or
    # removes the file right after, which Windows refuses while it is open.
    engine.dispose()

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

        # 表示名 (CITYNAME) と説明文 (DESCRIPTION) は別物。旧 seed_data は
        # description に表示名を入れていたので、display_name が無い定義では
        # そちらへフォールバックする (docs/intent/city_identity.md §3)。
        city_display_name = (
            city_seed.get("display_name")
            or city_seed.get("description")
            or city_name
        )
        city_description = city_seed.get("description", "")

        new_city = City(
            USERID=1,
            CITY_SLUG=city_name,
            CITYNAME=city_display_name,
            DESCRIPTION=city_description,
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
            # 旧 ACTIVITY_STATE 時代の既定 "Idle" (自律 OFF) を踏襲。列自体の
            # 既定は True (docs: database/models.py) だが、新規 seed 世界で
            # 全ペルソナが自律稼働を始めてしまわないよう明示的に False にする。
            # 既定 ON (owner 裁定 2026-07-14)。旧実装は ACTIVITY_STATE='Idle' 固定
            # (= 自律 OFF) で作っていたが、ライフ (PersonaSchedule の起床・就寝) が
            # 未設定のペルソナは自律 ON でも発火しない (watchdog が "no day_open
            # schedule" で skip / confirm_life_for_today がライフを作らない) ため、
            # ON で作っても暴走しない。実質の起動条件はライフの設定側にある。
            AUTONOMY_ENABLED=p_def.get("autonomy_enabled", True),
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

    populate_database(DB_PATH)


def populate_database(DB_PATH: Path) -> bool:
    """Create the tables in ``DB_PATH`` and fill in the initial world and
    playbooks. Never removes or replaces anything; the caller decides that the
    file may be written (``seed_database`` after its backup, or
    ``seed_database_if_missing`` after creating the file itself). Returns
    whether the world was committed (a failure is logged, as it always was).
    """
    # --- 2. Load seed data ---
    seed_data = _load_seed_data()
    username = seed_data["user"]["username"]

    # --- 3. Create new DB and tables ---
    engine = create_engine(f"sqlite:///{DB_PATH}")
    try:
        Base.metadata.create_all(engine)
        SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
        db = SessionLocal()
    except Exception:
        # The file must not stay open: seed_database_if_missing moves it away.
        engine.dispose()
        raise

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
        committed = True

    except Exception as e:
        db.rollback()
        logging.error(f"An error occurred during database seeding: {e}", exc_info=True)
        committed = False
    finally:
        db.close()
        engine.dispose()

    if not committed:
        return False
    # Import initial playbooks after DB seeding
    try:
        import_initial_playbooks(DB_PATH)
    except Exception as e:
        logging.error(f"An error occurred during playbook import: {e}", exc_info=True)
    return committed

DB_MISSING = "missing"
DB_READY = "ready"
DB_EMPTY = "empty"
DB_INCOMPLETE = "incomplete"
DB_UNREADABLE = "unreadable"


def database_state(db_path: Path) -> tuple[str, str]:
    """What ``seed_database_if_missing`` finds at ``db_path``, and why.

    Opened read-only, so the check itself never creates or changes the file.
    ``DB_READY``: has a ``city`` table with at least one city (SQLite table
    names are matched without regard to case, as SQLite itself does).
    ``DB_EMPTY``: a SQLite file with no table at all (a 0-byte file) -- a setup
    that stopped right after creating it, or one creating it right now.
    ``DB_INCOMPLETE``: has tables but no city (no ``city`` table, as after a
    partial restore, or an empty one, as after a setup that stopped while it
    filled the file) -- the other tables may hold the user's world.
    ``DB_UNREADABLE``: cannot be read as SQLite (damaged, not a database, no
    permission) -- it may still hold the user's world too. Only ``DB_MISSING``
    lets setup create a database; every other state but ``DB_READY`` stops it.
    """
    import sqlite3

    if not db_path.exists():
        return DB_MISSING, f"{db_path} does not exist"
    try:
        conn = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)
        try:
            tables = [row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
            city_table = next((str(name) for name in tables if str(name).lower() == "city"), None)
            cities = (
                conn.execute(f'SELECT COUNT(*) FROM "{city_table}"').fetchone()[0]
                if city_table is not None else 0
            )
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return DB_UNREADABLE, f"{db_path} cannot be read as a SQLite database ({exc})"
    if city_table is not None and cities > 0:
        return DB_READY, f"{db_path} is in place"
    if not tables:
        return DB_EMPTY, (
            f"{db_path} is an empty database file (a setup that stopped right after creating it, "
            "or one running now)"
        )
    if city_table is not None:
        return DB_INCOMPLETE, f"{db_path} has a city table without any city (a setup that stopped part-way)"
    return DB_INCOMPLETE, f"{db_path} has tables ({', '.join(sorted(map(str, tables))[:10])}) but no city table"


def _stop(db_path: Path, detail: str) -> int:
    print(f"[ERROR] {detail}.", flush=True)
    print("[ERROR] The database was left as it is. Check the file (or move it away to start "
          "a new world), then run setup again.", flush=True)
    print(f"[ERROR] データベース {db_path} はそのままにしてあります。ファイルを確かめるか "
          "(新しい世界で始めるならファイルを別の場所へ移してから)、setup をもう一度実行してください。",
          flush=True)
    return 2


def _remove_own_file(path: Path) -> None:
    """Remove a file this run created, with the SQLite files beside it."""
    for candidate in (path, *(path.with_name(path.name + suffix) for suffix in ("-journal", "-wal", "-shm"))):
        try:
            candidate.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            print(f"[WARN] Could not remove {candidate} ({exc}); delete it yourself.", flush=True)


def _publish_without_overwriting(source: Path, target: Path) -> None:
    """Give ``source`` the name ``target`` only if ``target`` does not exist,
    as one step: ``FileExistsError`` otherwise, and ``target`` is untouched.

    On Windows ``os.rename`` already refuses an existing target. On Mac /
    Linux it would replace it, so a hard link is made instead (``os.link``
    refuses an existing target) and the source name removed after. Where hard
    links are not supported (FAT / exFAT drives, for instance), ``target`` is
    created with ``O_EXCL`` -- which also refuses an existing one -- and the
    source copied into it through that file descriptor: once created, the name
    is this run's, and a file another process might put there afterwards
    replaces the name, never the bytes written to this run's file. Never
    "check that it is missing, then rename": something could be put there in
    between, and the rename would replace it.
    """
    if os.name == "nt":
        os.rename(source, target)
        return
    try:
        os.link(source, target)
    except FileExistsError:
        raise
    except OSError:
        _copy_into_new_file(source, target)
    source.unlink()


def _copy_into_new_file(source: Path, target: Path) -> None:
    """Create ``target`` (``FileExistsError`` if it exists) and copy ``source``
    into it through the new file's own descriptor."""
    fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with open(source, "rb") as src, os.fdopen(fd, "wb", closefd=False) as dst:
            while True:
                chunk = src.read(1024 * 1024)
                if not chunk:
                    break
                dst.write(chunk)
            dst.flush()
            os.fsync(fd)
    except BaseException:
        os.close(fd)
        # The file is this run's (created above): remove the partial copy.
        try:
            target.unlink()
        except OSError:
            pass
        raise
    os.close(fd)


def seed_database_if_missing() -> int:
    """What setup.bat / setup.sh run: create the database SAIVerse uses only when
    there is none, without ever moving, removing or replacing a file at its
    path. Returns the exit code.

    The database is the one main.py uses (``default_db_path`` after the same
    .env), resolved once. setup used to look at a fixed ``~/.saiverse`` path
    and so missed a SAIVERSE_HOME set elsewhere.

    - In place (a city) -> nothing is changed, 0.
    - Missing -> the world is made in a file of this run's own beside it
      (``<name>.creating-<pid>``, created with ``O_EXCL``), filled in
      completely (``populate_database``), then given the database's name
      without overwriting (``_publish_without_overwriting``). A database that
      appeared at the name meanwhile -- another setup, a restore -- is left as
      it is, this run's file removed, and 2 returned. The file at the
      database's name is never opened for writing by this run.
    - Empty, incomplete or unreadable -> nothing is changed and 2 is returned,
      so setup stops instead of touching a file that another setup may be
      filling, or that may hold the user's world. The message asks the user to
      check it (or move it away to start a new world).

    When filling in fails -- returned or raised -- only this run's own file is
    removed and 1 is returned.
    """
    db_path = default_db_path()
    ensure_data_dir()
    state, detail = database_state(db_path)
    if state == DB_READY:
        print(f"[OK] Database already exists: {db_path}", flush=True)
        return 0
    if state != DB_MISSING:
        return _stop(db_path, detail)
    work = db_path.with_name(f"{db_path.name}.creating-{os.getpid()}")
    try:
        fd = os.open(work, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except OSError as exc:
        print(f"[ERROR] Could not create {work} to make the database in ({exc}). Run setup again.",
              flush=True)
        return 1
    os.close(fd)
    print(f"[SETUP] Initializing database: {db_path}", flush=True)
    try:
        committed = populate_database(work)
    except Exception:
        logging.exception("The database could not be initialized")
        committed = False
    if not committed or database_state(work)[0] != DB_READY:
        _remove_own_file(work)
        print("[ERROR] The database could not be initialized (see above). Run setup again.", flush=True)
        print("[ERROR] データベースを作れませんでした (上の表示を見てください)。setup をもう一度実行してください。",
              flush=True)
        return 1
    try:
        _publish_without_overwriting(work, db_path)
    except FileExistsError:
        _remove_own_file(work)
        return _stop(db_path, f"{db_path} appeared while setup was creating the database")
    except OSError as exc:
        _remove_own_file(work)
        print(f"[ERROR] The new database could not be put at {db_path} ({exc}). Run setup again.",
              flush=True)
        return 1
    print(f"[OK] Database initialized: {db_path}", flush=True)
    return 0


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
    parser.add_argument(
        "--if-missing",
        action="store_true",
        help="Create the database only when there is none (what setup runs); never replace one in place",
    )
    args = parser.parse_args()

    if args.if_missing:
        sys.exit(seed_database_if_missing())
    seed_database(force=args.force)
