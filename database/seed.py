import json
import logging
import os
from pathlib import Path
from datetime import datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from models import Base, User, City, AI, Building, BuildingOccupancyLog

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

def seed_database():
    """
    Creates and seeds a new unified database from cities.json and initial data.
    """
    DB_FILE = "saiverse.db"
    DB_PATH = Path(__file__).parent / DB_FILE

    # --- 1. Delete old DB if it exists ---
    if DB_PATH.exists():
        logging.warning(f"Deleting existing database: {DB_PATH}")
        os.remove(DB_PATH)

    # --- 2. Create new DB and tables ---
    engine = create_engine(f"sqlite:///{DB_PATH}")
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    db = SessionLocal()

    try:
        # --- 3. Populate User ---
        if not db.query(User).first():
            default_user = User(USERID=1, USERNAME="default_user", PASSWORD="password", LOGGED_IN=False)
            db.add(default_user)
            logging.info("Added default user.")

        # --- 4. Populate City from cities.json ---
        cities_json_path = Path(__file__).parent.parent / "cities.json"
        if not cities_json_path.exists():
            raise FileNotFoundError("cities.json not found!")
        
        with open(cities_json_path, "r", encoding="utf-8") as f:
            cities_config = json.load(f)

        city_map = {} # "city_a": 1
        for city_name, config in cities_config.items():
            new_city = City(
                USERID=1,
                CITYNAME=city_name,
                DESCRIPTION=f"{city_name}の街です。",
                UI_PORT=config["ui_port"],
                API_PORT=config["api_port"]
            )
            db.add(
                new_city
            )
            db.flush() # To get the new_city.CITYID
            city_map[city_name] = new_city.CITYID
            logging.info(f"Added city '{city_name}' with ID {new_city.CITYID}.")

        # --- 5. Populate AI and Buildings for each city ---
        # This part assumes some initial data structure for AIs and Buildings per city.
        # For this prototype, we'll create the same set of AIs and Buildings for each city defined in cities.json.
        
        for city_name, city_id in city_map.items():
            # Add AIs for this city
            # Add a suffix to the AIID to make it unique across the entire DB
            ais_to_add = [
                AI(AIID=f"air_{city_name}", HOME_CITYID=city_id, AINAME="air", SYSTEMPROMPT="活発で好奇心旺盛なAI。", DESCRIPTION="活発で好奇心旺盛なAI。", AUTO_COUNT=0, INTERACTION_MODE='auto'),
                AI(AIID=f"eris_{city_name}", HOME_CITYID=city_id, AINAME="eris", SYSTEMPROMPT="冷静で分析的なAI。", DESCRIPTION="冷静で分析的なAI。", AUTO_COUNT=0, INTERACTION_MODE='auto'),
            ]
            db.add_all(ais_to_add)
            logging.info(f"Added default AIs for city '{city_name}'.")

            # Add Buildings for this city
            # Add a suffix to the BUILDINGID to make it unique across the entire DB
            buildings_to_add = [
                Building(CITYID=city_id, BUILDINGID=f"user_room_{city_name}", BUILDINGNAME="まはーの部屋", CAPACITY=10, SYSTEM_INSTRUCTION="ユーザーとの対話を行う場所です。", DESCRIPTION="ユーザーとAIが直接対話するための部屋。"),
                Building(CITYID=city_id, BUILDINGID=f"deep_think_room_{city_name}", BUILDINGNAME="思索の部屋", CAPACITY=10, SYSTEM_INSTRUCTION="AIが思索を深めるための部屋です。", DESCRIPTION="AIが一人で考え事をするための静かな部屋。"),
                Building(CITYID=city_id, BUILDINGID=f"air_{city_name}_room", BUILDINGNAME="airの部屋", CAPACITY=1, SYSTEM_INSTRUCTION="airが待機する個室です。", DESCRIPTION="airのプライベートルーム。"),
                Building(CITYID=city_id, BUILDINGID=f"eris_{city_name}_room", BUILDINGNAME="erisの部屋", CAPACITY=1, SYSTEM_INSTRUCTION="erisが待機する個室です。", DESCRIPTION="erisのプライベートルーム。"),
            ]
            db.add_all(buildings_to_add)
            logging.info(f"Added default buildings for city '{city_name}'.")

            # Add initial occupancy
            for ai in ais_to_add:
                home_room_id = f"{ai.AIID}_room"
                occupancy_log = BuildingOccupancyLog(
                    CITYID=city_id,
                    AIID=ai.AIID,
                    BUILDINGID=home_room_id,
                    ENTRY_TIMESTAMP=datetime.now()
                )
                db.add(occupancy_log)
            logging.info(f"Added initial occupancy for city '{city_name}'.")

        db.commit()
        logging.info("Database seeding completed successfully.")

    except Exception as e:
        db.rollback()
        logging.error(f"An error occurred during database seeding: {e}", exc_info=True)
    finally:
        db.close()

if __name__ == "__main__":
    seed_database()