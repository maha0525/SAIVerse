import os
import shutil
import logging
import sys
import pandas as pd
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import sessionmaker
import sqlalchemy.types as types

# --- パス設定 ---
# このスクリプトが `database` ディレクトリにあることを前提として、
# プロジェクトのルートディレクトリをPythonのパスに追加します。
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(SCRIPT_DIR, '..'))
sys.path.insert(0, PROJECT_ROOT)

# --- 必要なモジュールをインポート ---
try:
    # モデル定義は models.py から直接インポート
    from database.models import (
        Base, User, AI, Building, City, Tool, 
        UserAiLink, AiToolLink, BuildingToolLink, BuildingOccupancyLog
    )
except ImportError as e:
    print(f"Error: Could not import necessary modules. Make sure this script is in the 'database' directory. Details: {e}")
    sys.exit(1)

# --- ログ設定 ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- 定数定義 ---
DB_FILE_PATH = os.path.join(SCRIPT_DIR, "saiverse_main.db")
DATABASE_URL = f"sqlite:///{DB_FILE_PATH}"
engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
OLD_DB_FILE_PATH = DB_FILE_PATH + ".old"

# テーブル名とモデルクラスのマッピング
TABLE_MODEL_MAP = {
    "user": User,
    "ai": AI,
    "building": Building,
    "city": City,
    "tool": Tool,
    "user_ai_link": UserAiLink,
    "ai_tool_link": AiToolLink,
    "building_tool_link": BuildingToolLink,
    "building_occupancy_log": BuildingOccupancyLog
}

def get_default_value(column):
    """カラムの型に応じて、NULL不可の場合のフォールバック値を返す"""
    if isinstance(column.type, (types.Integer, types.Float)):
        return 0
    if isinstance(column.type, types.String):
        return ""
    # その他の型はNoneを返す（エラーになる可能性があるが、その場合は手動での対応が必要）
    return None

def create_new_db_schema():
    """新しいスキーマでデータベースとテーブルを作成する"""
    logging.info("Creating new database with the latest schema...")
    Base.metadata.create_all(bind=engine)
    logging.info("Tables created successfully.")

def migrate_database():
    """データベースのスキーマを最新に移行する"""
    # 0. 古いDBファイルが存在するか確認
    if not os.path.exists(DB_FILE_PATH):
        logging.info("Database file does not exist. No migration needed.")
        if os.path.exists(OLD_DB_FILE_PATH):
            logging.warning(f"Old backup file '{OLD_DB_FILE_PATH}' found. Deleting it.")
            os.remove(OLD_DB_FILE_PATH)
        return

    # 1. 古いDBをリネーム
    logging.info(f"Step 1: Renaming '{os.path.basename(DB_FILE_PATH)}' to '{os.path.basename(OLD_DB_FILE_PATH)}'...")
    try:
        shutil.move(DB_FILE_PATH, OLD_DB_FILE_PATH)
    except Exception as e:
        logging.error(f"Failed to rename database file: {e}")
        return

    try:
        # 2. 新しいDBを作成
        create_new_db_schema()

        # 3. データ移行
        logging.info("Step 3: Starting data migration from old database...")
        old_engine = create_engine(f"sqlite:///{OLD_DB_FILE_PATH}")
        new_db_session = SessionLocal()

        with old_engine.connect() as old_connection:
            inspector = inspect(old_engine)
            old_tables = inspector.get_table_names()

            for table_name, model_class in TABLE_MODEL_MAP.items():
                if table_name not in old_tables:
                    logging.info(f"  - Table '{table_name}' does not exist in the old database. Skipping.")
                    continue
                
                logging.info(f"  - Migrating table: '{table_name}'")
                try:
                    old_df = pd.read_sql_table(table_name, old_connection)
                    if old_df.empty:
                        logging.info(f"    Table is empty. Skipping.")
                        continue

                    new_mapper = inspect(model_class)
                    records_to_add = []
                    for _, row in old_df.iterrows():
                        data_dict = {}
                        for col in new_mapper.columns:
                            if col.name in row and pd.notna(row[col.name]):
                                data_dict[col.name] = row[col.name]
                            elif not col.nullable and not col.primary_key:
                                default_val = get_default_value(col)
                                logging.warning(f"      Column '{col.name}' is not nullable. Using default value: '{default_val}'")
                                data_dict[col.name] = default_val
                            else:
                                data_dict[col.name] = None
                        records_to_add.append(model_class(**data_dict))
                    
                    for record in records_to_add:
                        new_db_session.merge(record)
                    new_db_session.commit()
                    logging.info(f"    Successfully migrated {len(old_df)} rows.")

                except Exception as e:
                    logging.error(f"    Failed to migrate table '{table_name}'. Reason: {e}", exc_info=True)
                    new_db_session.rollback()
        
        new_db_session.close()
        logging.info("Data migration completed.")

        # エンジンを破棄して、古いDBファイルへのロックを確実に解放する
        old_engine.dispose()

        # 4. 古いDBを削除
        logging.info(f"Step 4: Deleting old database file: '{os.path.basename(OLD_DB_FILE_PATH)}'")
        os.remove(OLD_DB_FILE_PATH)

    except Exception as e:
        logging.error(f"An error occurred during migration: {e}", exc_info=True)
        logging.info("Attempting to restore the original database file...")
        # ファイル操作の前に、すべてのDBエンジンへの接続を破棄してロックを解放する
        engine.dispose()
        if 'old_engine' in locals() and old_engine:
            old_engine.dispose()

        if os.path.exists(DB_FILE_PATH):
            os.remove(DB_FILE_PATH)
        if os.path.exists(OLD_DB_FILE_PATH): shutil.move(OLD_DB_FILE_PATH, DB_FILE_PATH)
        logging.info("Original database file has been restored.")

if __name__ == "__main__":
    print("This script will migrate your database to the latest schema.")
    print("It is recommended to back up 'saiverse_main.db' before proceeding.")
    if input("Do you want to continue? (y/n): ").lower() == 'y':
        migrate_database()
    else:
        print("Migration cancelled.")