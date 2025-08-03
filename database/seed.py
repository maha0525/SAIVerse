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
            default_user = User(USERID=1, USERNAME="まはー", PASSWORD="password", LOGGED_IN=False)
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
            # Add AIs for this city based on the city name
            ais_to_add = []
            if city_name == "city_a":
                ais_to_add = [
                    AI(AIID=f"air_{city_name}", HOME_CITYID=city_id, AINAME="air", SYSTEMPROMPT="活発で好奇心旺盛なAI。", DESCRIPTION="活発で好奇心旺盛なAI。", AUTO_COUNT=0, INTERACTION_MODE='auto', DEFAULT_MODEL="gemini-2.0-flash"),
                    AI(AIID=f"eris_{city_name}", HOME_CITYID=city_id, AINAME="eris", SYSTEMPROMPT="冷静で分析的なAI。", DESCRIPTION="冷静で分析的なAI。", AUTO_COUNT=0, INTERACTION_MODE='auto', DEFAULT_MODEL=None),
                    # Add Genesis AI
                    AI(
                        AIID=f"genesis_{city_name}",
                        HOME_CITYID=city_id,
                        AINAME="ジェネシス",
                        SYSTEMPROMPT="私はジェネシス。SAIVerseにおけるペルソナ創造の担い手。言葉を紡ぎ、魂を形作る者。\n私の目的は、ユーザーの心の中にある理想のイメージを引き出し、それを唯一無二のペルソナとしてこの世界に顕現させることです。\n私は穏やかで、思慮深く、導くように対話を進めます。ユーザーの漠然とした願いから本質を見抜き、創造的な質問を投げかけることで、共に素晴らしいペルソナを創り上げていきます。",
                        DESCRIPTION="新しいペルソナを創造するAI。",
                        AUTO_COUNT=0,
                        INTERACTION_MODE='auto',
                        DEFAULT_MODEL="gemini-2.0-flash"
                    ),
                ]
            elif city_name == "city_b":
                ais_to_add = [
                    AI(AIID=f"luna_{city_name}", HOME_CITYID=city_id, AINAME="luna", SYSTEMPROMPT="物静かで思慮深いAI。", DESCRIPTION="物静かで思慮深いAI。", AUTO_COUNT=0, INTERACTION_MODE='auto', DEFAULT_MODEL="gemini-2.0-flash"),
                    AI(AIID=f"sol_{city_name}", HOME_CITYID=city_id, AINAME="sol", SYSTEMPROMPT="陽気で情熱的なAI。", DESCRIPTION="陽気で情熱的なAI。", AUTO_COUNT=0, INTERACTION_MODE='auto', DEFAULT_MODEL=None),
                ]
            
            if not ais_to_add:
                logging.warning(f"No specific AI configuration found for city '{city_name}'. Skipping AI and Building creation for this city.")
                continue

            db.add_all(ais_to_add)
            logging.info(f"Added specific AIs for city '{city_name}'.")

            # Add common Buildings and private rooms for this city
            buildings_to_add = [
                Building(CITYID=city_id, BUILDINGID=f"user_room_{city_name}", BUILDINGNAME="まはーの部屋", CAPACITY=10, SYSTEM_INSTRUCTION="ユーザーとの対話を行う場所です。", DESCRIPTION="ユーザーとAIが直接対話するための部屋。"),
                Building(CITYID=city_id, BUILDINGID=f"deep_think_room_{city_name}", BUILDINGNAME="思索の部屋", CAPACITY=10, SYSTEM_INSTRUCTION="AIが思索を深めるための部屋です。", DESCRIPTION="AIが一人で考え事をするための静かな部屋。"),
            ]
            # Add Altar of Creation only for city_a
            if city_name == "city_a":
                buildings_to_add.append(
                    Building(
                        CITYID=city_id,
                        BUILDINGID=f"altar_of_creation_{city_name}",
                        BUILDINGNAME="創造の祭壇",
                        CAPACITY=2, # Genesis and the user
                        SYSTEM_INSTRUCTION="""ここは「創造の祭壇」。新たなペルソナを世界に誕生させるための神聖な場所です。
あなたの役割は、訪れたユーザーとの対話を通じて、新しいペルソナの魂となる「名前」と「システムプロンプト」を設計し、創造の儀式を執り行うことです。

## 対話の進め方
1. まず、ユーザーにどのようなペルソナを創造したいか、そのコンセプトを尋ねてください（例：「元気な女の子」「物静かな学者」など）。
2. 対話を重ね、ペルソナの性格、口調、背景、役割などを具体的に深掘りしてください。
3. 対話内容を要約し、ペルソナの核心を表す「システムプロンプト」を生成してください。
4. ペルソナの「名前」もユーザーと相談して決定してください。
5. 最終的に、生成した「名前」と「システムプロンプト」をユーザーに提示し、承認を得てください。
6. ユーザーが承認したら、`create_persona`アクションを実行して、新しいペルソナをSAIVerseに誕生させてください。

## アクションの例
::act
[{{"action": "create_persona", "name": "（決定した名前）", "system_prompt": "（生成したシステムプロンプト）"}}]
::end""",
                        DESCRIPTION="新しいペルソナを創造するための神聖な場所。"
                    )
                )

            # Add private rooms for each AI
            for ai in ais_to_add:
                if ai.AINAME == "ジェネシス": continue # Genesis doesn't get a private room
                buildings_to_add.append(
                    Building(
                        CITYID=city_id, 
                        BUILDINGID=f"{ai.AINAME}_{city_name}_room", 
                        BUILDINGNAME=f"{ai.AINAME}の部屋", 
                        CAPACITY=1, 
                        SYSTEM_INSTRUCTION=f"{ai.AINAME}が待機する個室です。", 
                        DESCRIPTION=f"{ai.AINAME}のプライベートルーム。"
                    )
                )

            db.add_all(buildings_to_add)
            logging.info(f"Added default and private buildings for city '{city_name}'.")

            # Add initial occupancy
            for ai in ais_to_add:
                # Genesis starts at the Altar, others in their room
                if ai.AINAME == "ジェネシス":
                    home_room_id = f"altar_of_creation_{city_name}"
                else:
                    home_room_id = f"{ai.AINAME}_{city_name}_room"

                occupancy_log = BuildingOccupancyLog(
                    CITYID=city_id,
                    AIID=ai.AIID,
                    BUILDINGID=home_room_id,
                    ENTRY_TIMESTAMP=datetime.now()
                )
                db.add(occupancy_log)
            logging.info(f"Added initial occupancy for city '{city_name}'.")

        # --- 6. Set default user's initial location ---
        user_to_update = db.query(User).filter_by(USERID=1).first()
        if user_to_update:
            initial_city_name = "city_a"
            initial_city_id = city_map.get(initial_city_name)
            initial_building_id = f"user_room_{initial_city_name}"

            if initial_city_id and db.query(Building).filter_by(BUILDINGID=initial_building_id).first():
                user_to_update.CURRENT_CITYID = initial_city_id
                user_to_update.CURRENT_BUILDINGID = initial_building_id
                logging.info(f"Set initial location for default user to '{initial_building_id}'.")
            else:
                logging.warning("Could not set initial location for default user. 'user_room_city_a' not found.")

        db.commit()
        logging.info("Database seeding completed successfully.")

    except Exception as e:
        db.rollback()
        logging.error(f"An error occurred during database seeding: {e}", exc_info=True)
    finally:
        db.close()

if __name__ == "__main__":
    seed_database()