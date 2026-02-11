import os
import sys
import argparse
import logging
import shutil
from datetime import datetime
from sqlalchemy import create_engine, inspect
import pandas as pd

# プロジェクトのルートディレクトリをPythonのパスに追加し、
# 他のモジュール（例: database.models）をインポートできるようにします。
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, project_root)

from database.models import Base
from database.paths import default_db_path

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')


def needs_migration(db_path: str) -> bool:
    """Check if the database schema differs from the current models.

    Compares columns in each table between the existing DB and the model
    definitions. Returns True if any table has missing or extra columns.
    """
    if not os.path.exists(db_path):
        return False

    engine = create_engine(f"sqlite:///{db_path}")
    try:
        db_inspector = inspect(engine)
        for table in Base.metadata.sorted_tables:
            if not db_inspector.has_table(table.name):
                # New table that doesn't exist yet
                return True
            db_columns = {c["name"] for c in db_inspector.get_columns(table.name)}
            model_columns = {c.name for c in table.columns}
            if db_columns != model_columns:
                return True
        return False
    finally:
        engine.dispose()


def migrate_database_in_place(db_path: str):
    """
    指定されたデータベースファイルをその場でマイグレーションします。
    1. 既存DBをタイムスタンプ付きのバックアップファイルにリネームします。
    2. 新しいスキーマで空のDBを元の名前で作成します。
    3. バックアップから新DBへデータを移行します。カラムの追加・削除に自動で対応します。
    4. 成功すればバックアップはそのまま残し、失敗すればロールバックを試みます。
    """
    # --- 1. Validate paths and create backup ---
    if not os.path.exists(db_path):
        logging.error(f"データベースファイルが見つかりません: {db_path}")
        logging.info("データベースファイルが存在しないため、マイグレーションは不要です。")
        return

    db_dir = os.path.dirname(db_path)
    db_name = os.path.basename(db_path)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = os.path.join(db_dir, f"{db_name}_{timestamp}.bak")
    
    logging.info(f"マイグレーションを開始します: {db_path}")
    
    try:
        # 元のファイルをバックアップパスに移動
        shutil.move(db_path, backup_path)
        logging.info(f"データベースをバックアップしました: {backup_path}")
    except Exception as e:
        logging.error(f"バックアップの作成に失敗しました: {e}")
        return

    # --- 2. Setup engines and create new schema ---
    source_engine = create_engine(f"sqlite:///{backup_path}")
    target_engine = create_engine(f"sqlite:///{db_path}")

    try:
        Base.metadata.create_all(target_engine)
        logging.info(f"新しいスキーマでデータベースを作成しました: {db_path}")

        # --- 3. Migrate data ---
        source_inspector = inspect(source_engine)
        target_inspector = inspect(target_engine)
        
        # Base.metadata.sorted_tables は外部キーの依存関係に基づいてソートされている
        for table in Base.metadata.sorted_tables:
            table_name = table.name
            logging.info(f"テーブル '{table_name}' のデータ移行を開始...")

            if not source_inspector.has_table(table_name):
                logging.warning(f"  - ソースにテーブル '{table_name}' が存在しないため、スキップします。")
                continue

            try:
                df = pd.read_sql_table(table_name, source_engine)
                if df.empty:
                    logging.info(f"  - テーブル '{table_name}' は空なので、スキップします。")
                    continue

                source_columns = df.columns.tolist()
                target_columns_info = target_inspector.get_columns(table_name)
                target_columns = [c['name'] for c in target_columns_info]

                # ターゲットにしか存在しない新しいカラムを見つける
                new_columns = set(target_columns) - set(source_columns)
                model_table = Base.metadata.tables[table_name]

                for col_name in new_columns:
                    column = model_table.columns.get(col_name)
                    # デフォルト値が設定されているNOT NULLカラムにデフォルト値を設定
                    if column is not None and column.default is not None and not column.nullable:
                        default_value = column.default.arg
                        logging.info(f"  - 新しいNOT NULLカラム '{col_name}' にデフォルト値 '{default_value}' を設定します。")
                        df[col_name] = default_value
                    elif column is not None and not column.nullable:
                        logging.warning(f"  - 警告: 新しいNOT NULLカラム '{col_name}' にデフォルト値がありません。移行に失敗する可能性があります。")

                # ターゲットテーブルに存在するカラムのみを移行対象とする
                df_to_load = df[[col for col in df.columns if col in target_columns]]

                # データを新しいテーブルに書き込む
                df_to_load.to_sql(table_name, target_engine, if_exists='append', index=False)
                logging.info(f"  - {len(df_to_load)} 件のレコードを '{table_name}' に移行しました。")

            except Exception as e:
                logging.error(f"テーブル '{table_name}' の移行中にエラーが発生しました: {e}", exc_info=True)
                # このテーブルでエラーが発生しても、他のテーブルの移行を試みる場合はここにロジックを追加できます。
                # 今回は、いずれかのテーブルで失敗したら全体をロールバックします。
                raise # エラーを再送出して、外側のtry-exceptブロックでキャッチさせる

        logging.info("すべてのテーブルのデータ移行が正常に完了しました。")
        
    except Exception as e:
        logging.error(f"マイグレーション中にエラーが発生しました: {e}", exc_info=True)
        logging.info("ロールバックを試みます...")
        # DB接続を一度閉じてからファイル操作を行う
        source_engine.dispose()
        target_engine.dispose()
        try:
            if os.path.exists(backup_path):
                if os.path.exists(db_path):
                    os.remove(db_path)
                shutil.move(backup_path, db_path)
                logging.info("ロールバックが完了しました。元のデータベースが復元されました。")
        except Exception as rb_e:
            logging.error(f"ロールバックに失敗しました: {rb_e}", exc_info=True)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="SAIVerse データベース マイグレーションツール")
    parser.add_argument(
        "--db",
        default=None,
        help="SQLiteデータベースへのパス（省略時は ~/.saiverse/user_data/database/saiverse.db）",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="スキーマ差分がなくてもマイグレーションを実行する",
    )
    args = parser.parse_args()

    db_path = args.db or str(default_db_path())
    logging.info(f"対象データベース: {db_path}")

    if not args.force and not needs_migration(db_path):
        logging.info("スキーマに変更はありません。マイグレーションは不要です。")
    else:
        migrate_database_in_place(db_path)