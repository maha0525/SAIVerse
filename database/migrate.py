import os
import sys
import argparse
import logging
import shutil
from datetime import datetime
from sqlalchemy import create_engine, inspect, text

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


def _schema_diff(db_path: str):
    """現行スキーマと DB の差分を返す。

    Returns:
        (missing_by_table, extra_by_table, missing_tables)
        - missing_by_table: {table_name: {col, ...}}  モデルにあって DB に無い列
        - extra_by_table:   {table_name: {col, ...}}  DB にあってモデルに無い列
        - missing_tables:   [Table, ...]              DB に存在しないテーブル
    """
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        insp = inspect(engine)
        missing_by_table: dict = {}
        extra_by_table: dict = {}
        missing_tables: list = []
        for table in Base.metadata.sorted_tables:
            if not insp.has_table(table.name):
                missing_tables.append(table)
                continue
            db_cols = {c["name"] for c in insp.get_columns(table.name)}
            model_cols = {c.name for c in table.columns}
            missing = model_cols - db_cols
            extra = db_cols - model_cols
            if missing:
                missing_by_table[table.name] = missing
            if extra:
                extra_by_table[table.name] = extra
        return missing_by_table, extra_by_table, missing_tables
    finally:
        engine.dispose()


def _render_default_sql(column) -> "str | None":
    """列の Python 側 default を ALTER 文に埋める SQL リテラルへ変換する。

    スカラー default のみ対応 (callable / SQL 式 default は None を返す)。
    """
    default = getattr(column, "default", None)
    if default is None or not getattr(default, "is_scalar", False):
        return None
    val = default.arg
    if isinstance(val, bool):
        return "1" if val else "0"
    if isinstance(val, (int, float)):
        return str(val)
    if isinstance(val, str):
        escaped = val.replace("'", "''")
        return f"'{escaped}'"
    return None


def try_additive_migration(db_path: str) -> bool:
    """追加系 (新規テーブル / 新規列) のみのスキーマ差分を ALTER/CREATE で適用する。

    全書換 (ファイル move) と違い、生きた DB に対して直接 ALTER TABLE ADD COLUMN /
    CREATE TABLE を発行するため、 他コネクションがファイルを開いていても
    (Windows の WinError 32 を踏まずに) 適用できる。 これがマイグレーションの大半
    (列追加) を占めるので、 全書換より先にこちらを試す。

    Returns:
        True  — 追加系のみで差分を完全に解消した (= 全書換不要)
        False — 列削除 / 型変更など破壊的差分があり全書換が必要、 または NOT NULL
                かつ既定値が無く安全に ALTER 追加できない列がある。 この場合 DB は
                一切変更しない (部分適用しない)。
    """
    missing_by_table, extra_by_table, missing_tables = _schema_diff(db_path)

    # DB にあってモデルに無い列 = 削除/リネーム → 全書換が必要
    if extra_by_table:
        logging.info(
            "全書換が必要な差分を検出 (削除/リネーム列): %s", extra_by_table
        )
        return False

    if not missing_by_table and not missing_tables:
        return True  # 差分なし (needs_migration と矛盾するが安全側)

    # --- 事前検証: 全 ADD COLUMN が安全に発行できるか先に確認 (部分適用を防ぐ) ---
    table_by_name = {t.name: t for t in Base.metadata.sorted_tables}
    planned: list = []  # [(table_name, ddl), ...]
    for table_name, cols in missing_by_table.items():
        table = table_by_name[table_name]
        engine = create_engine(f"sqlite:///{db_path}")
        try:
            dialect = engine.dialect
            for col_name in cols:
                column = table.columns[col_name]
                col_type = column.type.compile(dialect=dialect)
                ddl = f'ALTER TABLE "{table_name}" ADD COLUMN "{col_name}" {col_type}'
                default_sql = _render_default_sql(column)
                if not column.nullable:
                    if default_sql is None:
                        # NOT NULL かつ既定値なし → 既存行を埋められないので ALTER 不可
                        logging.info(
                            "列 %s.%s は NOT NULL だが既定値が無く ALTER 追加不可。全書換に切替",
                            table_name, col_name,
                        )
                        return False
                    ddl += f" NOT NULL DEFAULT {default_sql}"
                elif default_sql is not None:
                    ddl += f" DEFAULT {default_sql}"
                planned.append((table_name, ddl))
        finally:
            engine.dispose()

    # --- 適用 ---
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        for table in missing_tables:
            table.create(bind=engine)
            logging.info("テーブル %s を新規作成しました", table.name)
        if planned:
            with engine.begin() as conn:
                for table_name, ddl in planned:
                    conn.execute(text(ddl))
                    logging.info("追加系マイグレーション: %s", ddl)
        return True
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
        # ここで黙って return すると、 呼び出し側 (main.py) は「migration completed」と
        # 誤認してマイグレーション未適用の DB のまま起動を続行し、 後段の AI クエリが
        # `no such column` で落ちる。 失敗を握り潰さず raise して起動を明示的に止める。
        logging.error(
            f"バックアップの作成に失敗しました (DB がロックされている可能性): {e}",
            exc_info=True,
        )
        raise RuntimeError(
            f"DB マイグレーションのバックアップに失敗しました: {db_path}。 "
            "他プロセスが DB を開いていないか確認してください。"
        ) from e

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
                # ソーステーブルからデータを読み取る
                with source_engine.connect() as src_conn:
                    result = src_conn.execute(text(f'SELECT * FROM "{table_name}"'))
                    source_columns = list(result.keys())
                    rows = result.fetchall()

                if not rows:
                    logging.info(f"  - テーブル '{table_name}' は空なので、スキップします。")
                    continue

                target_columns_info = target_inspector.get_columns(table_name)
                target_columns = [c['name'] for c in target_columns_info]

                # ターゲットにしか存在しない新しいカラムを見つける
                new_columns = set(target_columns) - set(source_columns)
                model_table = Base.metadata.tables[table_name]

                # 新しいNOT NULLカラムのデフォルト値を収集
                new_col_defaults = {}
                for col_name in new_columns:
                    column = model_table.columns.get(col_name)
                    if column is not None and column.default is not None and not column.nullable:
                        default_value = column.default.arg
                        logging.info(f"  - 新しいNOT NULLカラム '{col_name}' にデフォルト値 '{default_value}' を設定します。")
                        new_col_defaults[col_name] = default_value
                    elif column is not None and not column.nullable:
                        logging.warning(f"  - 警告: 新しいNOT NULLカラム '{col_name}' にデフォルト値がありません。移行に失敗する可能性があります。")

                # 移行対象のカラムを決定（ソースに存在しターゲットにもあるカラム + デフォルト値付き新カラム）
                cols_from_source = [c for c in source_columns if c in target_columns]
                cols_with_defaults = sorted(new_col_defaults.keys())
                all_insert_cols = cols_from_source + cols_with_defaults

                # INSERT文を構築
                col_names = ", ".join(f'"{c}"' for c in all_insert_cols)
                placeholders = ", ".join([":" + c for c in all_insert_cols])
                insert_sql = text(f'INSERT INTO "{table_name}" ({col_names}) VALUES ({placeholders})')

                # データを新しいテーブルに書き込む
                with target_engine.begin() as tgt_conn:
                    for row in rows:
                        row_dict = dict(zip(source_columns, row))
                        # ソースカラムのうちターゲットに存在するもの + 新カラムのデフォルト値
                        insert_values = {c: row_dict[c] for c in cols_from_source}
                        for c in cols_with_defaults:
                            insert_values[c] = new_col_defaults[c]
                        tgt_conn.execute(insert_sql, insert_values)

                logging.info(f"  - {len(rows)} 件のレコードを '{table_name}' に移行しました。")

            except Exception as e:
                logging.error(f"テーブル '{table_name}' の移行中にエラーが発生しました: {e}", exc_info=True)
                raise

        logging.info("すべてのテーブルのデータ移行が正常に完了しました。")

        # Post-migration: convert legacy INTERACTION_MODE values to ACTIVITY_STATE.
        # The column-copy phase above only carries over columns that exist in the
        # new schema, so when INTERACTION_MODE is dropped its values are lost
        # without an explicit translation step.
        _migrate_interaction_mode_to_activity_state(source_engine, target_engine)

        # Post-migration: assign slot numbers to existing items (in order of CREATED_AT)
        _assign_initial_slot_numbers(target_engine)

        # Post-migration: demote legacy STATUS_WAITING Tracks to 'pending'.
        # waiting_for / waiting_timeout_at columns are dropped by the schema
        # copy phase above (they no longer exist in the new schema), but the
        # status string is preserved as-is. Surface those Tracks as pending so
        # the persona can re-evaluate them via meta judgment instead of being
        # stuck in a now-undefined state. See
        # docs/intent/persona_cognition/handoff_waiting_track_removal.md.
        _demote_legacy_waiting_tracks(target_engine)

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

def _migrate_interaction_mode_to_activity_state(source_engine, target_engine) -> None:
    """Map legacy AI.INTERACTION_MODE values to AI.ACTIVITY_STATE.

    Mapping (Intent A v0.9 / Intent B v0.6):
    - 'auto'   -> 'Active' (自律行動含めて全動作)
    - 'user'   -> 'Idle'   (起きてるが自発的には行動しない)
    - 'manual' -> 'Idle'   (実装上の "auto OFF" 状態。Idle と同義)
    - 'sleep'  -> 'Sleep'  (寝てる、ユーザー発言で起きる)
    - その他    -> 'Idle'   (フォールバック)

    Runs only when the source DB still has INTERACTION_MODE; otherwise no-op.
    Existing ACTIVITY_STATE values from rows that already had a non-default
    state are overwritten — INTERACTION_MODE is the prior source of truth so
    we honor it during the one-shot migration.
    """
    try:
        source_inspector = inspect(source_engine)
        if not source_inspector.has_table("AI"):
            return
        source_cols = {c["name"] for c in source_inspector.get_columns("AI")}
        if "INTERACTION_MODE" not in source_cols:
            logging.info("INTERACTION_MODE が source DB に存在しないため、変換をスキップします。")
            return

        with source_engine.connect() as src:
            rows = src.execute(text('SELECT AIID, INTERACTION_MODE FROM "AI"')).fetchall()

        if not rows:
            return

        mapping = {
            "auto": "Active",
            "user": "Idle",
            "manual": "Idle",
            "sleep": "Sleep",
        }
        with target_engine.begin() as tgt:
            converted = 0
            for ai_id, legacy_mode in rows:
                new_state = mapping.get((legacy_mode or "").strip(), "Idle")
                tgt.execute(
                    text('UPDATE "AI" SET ACTIVITY_STATE = :state WHERE AIID = :id'),
                    {"state": new_state, "id": ai_id},
                )
                converted += 1
        logging.info(
            "INTERACTION_MODE -> ACTIVITY_STATE 変換完了: %d 件のレコードを更新しました。",
            converted,
        )
    except Exception as exc:
        logging.warning("INTERACTION_MODE -> ACTIVITY_STATE 変換に失敗しました: %s", exc, exc_info=True)


def _demote_legacy_waiting_tracks(engine) -> None:
    """Convert legacy `status='waiting'` ActionTracks to 'pending'.

    The 'waiting' status was retired in v0.31 (2026-05-09) when the
    Phase 5 deferred-tool infrastructure took over the role of "block
    a Pulse until an external event arrives". Old DBs may still hold
    Tracks in this state; pending preserves the persona's intent while
    letting the meta layer pick them up normally.
    """
    try:
        with engine.begin() as conn:
            result = conn.execute(
                text("UPDATE action_track SET status = 'pending' WHERE status = 'waiting'")
            )
            if result.rowcount:
                logging.info(
                    "legacy waiting Track を %d 件 'pending' に降ろしました。",
                    result.rowcount,
                )
    except Exception as exc:
        logging.warning(
            "legacy waiting Track の降ろし処理に失敗しました（スキップ）: %s", exc,
        )


def _assign_initial_slot_numbers(engine) -> None:
    """既存アイテムに作成日時順でスロット番号を割り当てる。"""
    try:
        with engine.begin() as conn:
            result = conn.execute(text("""
                SELECT il.LOCATION_ID, il.OWNER_KIND, il.OWNER_ID, i.CREATED_AT
                FROM item_location il
                JOIN item i ON il.ITEM_ID = i.ITEM_ID
                WHERE il.SLOT_NUMBER IS NULL
                ORDER BY il.OWNER_KIND, il.OWNER_ID, i.CREATED_AT
            """))
            rows = result.fetchall()

            from collections import defaultdict
            container_counters = defaultdict(int)

            for row in rows:
                location_id, owner_kind, owner_id, _created_at = row
                container_key = (owner_kind, owner_id)
                container_counters[container_key] += 1
                slot_num = container_counters[container_key]
                conn.execute(
                    text("UPDATE item_location SET SLOT_NUMBER = :slot WHERE LOCATION_ID = :lid"),
                    {"slot": slot_num, "lid": location_id},
                )

            logging.info("スロット番号を %d 件のアイテムに割り当てました。", len(rows))
    except Exception as e:
        logging.warning("スロット番号の割り当てに失敗しました（スキップ）: %s", e)


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