import os
import sys
import argparse
import logging
from sqlalchemy import create_engine, inspect

# プロジェクトのルートディレクトリをPythonのパスに追加し、
# 他のモジュール（例: database.models）をインポートできるようにします。
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, project_root)

from database.models import Base

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

def migrate_database(db_path: str):
    """
    データベースのスキーマを現在のモデル定義と比較し、不足しているテーブルを作成します。
    """
    if not os.path.exists(db_path):
        logging.error(f"データベースファイルが見つかりません: {db_path}")
        return

    logging.info(f"データベーススキーマをチェック中: {db_path}")
    DATABASE_URL = f"sqlite:///{db_path}"
    engine = create_engine(DATABASE_URL)

    try:
        inspector = inspect(engine)
        existing_tables = inspector.get_table_names()
        logging.info(f"既存のテーブル: {existing_tables}")

        tables_to_create = [table for table in Base.metadata.sorted_tables if table.name not in existing_tables]

        if not tables_to_create:
            logging.info("データベーススキーマは最新です。マイグレーションは不要です。")
            return

        logging.warning(f"不足しているテーブルが見つかりました。次のテーブルを作成します: {[t.name for t in tables_to_create]}")
        Base.metadata.create_all(bind=engine, tables=tables_to_create)
        logging.info("マイグレーションが完了しました。")

    except Exception as e:
        logging.error(f"マイグレーション中にエラーが発生しました: {e}", exc_info=True)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="SAIVerse データベース マイグレーションツール")
    parser.add_argument("--db", required=True, help="SQLiteデータベースへのパス (例: database/city_A.db)")
    args = parser.parse_args()
    
    migrate_database(args.db)