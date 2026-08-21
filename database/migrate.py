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


# 既知のカラムリネーム: {table_name: {old_col: new_col}}。
# リネームを放置すると diff 上「削除 + 追加」に見えて全書換に落ち、
# 全書換のデータ移行は列名一致でコピーするため旧列のデータが失われる。
# additive / 全書換どちらのパスでも、差分検出の前にここで RENAME COLUMN を当てる。
KNOWN_COLUMN_RENAMES = {
    # 2026-06-12: 控室 (game 専用語) → 入口 (Region 汎用仕様)。docs/intent/region.md
    "region": {"LOBBY_BUILDING_ID": "ENTRANCE_BUILDING_ID"},
    # 2026-08-14: City の名前欄を意味で分離 (docs/intent/city_identity.md)。
    # 旧 CITYNAME は表示名ではなく内部の識別子 (起動引数・user_room の BUILDINGID・
    # ペルソナ ID・ログ保存先フォルダ・二重起動チェックの鍵の材料) だったので
    # CITY_SLUG へ改名し、空いた CITYNAME を表示名の列として新設する。
    # 冪等性: 移行後の DB は CITYNAME (表示名) と CITY_SLUG の両方を持つので
    # 「新列が無いときだけ」の条件によりこの rename は二度と発火しない。
    "city": {"CITYNAME": "CITY_SLUG"},
}


def apply_known_column_renames(db_path: str) -> None:
    """KNOWN_COLUMN_RENAMES に従い ALTER TABLE RENAME COLUMN を適用する。

    旧列が存在し新列が存在しない場合のみ発行する冪等な操作。
    ALTER 系なので生きた DB にも安全に当たる (additive パスと同じ理屈)。
    """
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        insp = inspect(engine)
        for table_name, renames in KNOWN_COLUMN_RENAMES.items():
            if not insp.has_table(table_name):
                continue
            db_cols = {c["name"] for c in insp.get_columns(table_name)}
            for old_col, new_col in renames.items():
                if old_col in db_cols and new_col not in db_cols:
                    with engine.begin() as conn:
                        conn.execute(text(
                            f'ALTER TABLE "{table_name}" RENAME COLUMN "{old_col}" TO "{new_col}"'
                        ))
                    logging.info(
                        "カラムリネーム: %s.%s -> %s", table_name, old_col, new_col
                    )
    finally:
        engine.dispose()


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
    apply_known_column_renames(db_path)
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
        raise FileNotFoundError(f"マイグレーション対象DBが存在しません: {db_path}")

    # リネームを先に解消しておかないと「削除 + 追加」として扱われ、
    # 列名一致コピーのデータ移行で旧列のデータが失われる
    apply_known_column_renames(db_path)

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

            # フィード 3 テーブルは新スキーマで unique 制約 + NOT NULL 付きに
            # なるため、開発期 DB の重複行・NULL 入り行をそのままコピーすると
            # INSERT が IntegrityError → migration 全体がロールバックして
            # 起動不能になる。コピーの SELECT 自体を「勝者行のみ + 既定値
            # backfill」の決定論フィルタに置き換えて塞ぐ
            # (_feed_copy_filter_select)。ソース = バックアップには一切
            # 書かない — 後続の移行失敗時のロールバックが「無傷の元」を
            # 復元できることがバックアップの存在意義のため。
            missing_feed_keys = _feed_copy_missing_key_columns(
                source_inspector, table_name
            )
            if missing_feed_keys:
                # フィルタ SQL が参照するキー列を欠く部分スキーマの野生 DB。
                # 存在しない列を含む SQL は生成しない (実行した時点で
                # "no such column" → migration 全体が落ちる — 二十二巡目 Z1)。
                # 空表なら 0 行コピーで続行、行があるなら重複・孤児の判定
                # 自体が不能 = 修復不能な部分スキーマとして明示的に止める。
                with source_engine.connect() as src_conn:
                    partial_rows = src_conn.execute(text(
                        f'SELECT COUNT(*) FROM "{table_name}"'
                    )).scalar() or 0
                if partial_rows:
                    # AA2 (二十三巡目): feed_item の id 欠落は「判定不能」
                    # ではなく「コピー時の自動採番で id が振り直され、配送
                    # カーソルの座標を復元できない」のが停止理由 — 指針に
                    # それを書く ("id" は feed_item のキー列にのみ含まれる)
                    if "id" in missing_feed_keys:
                        reason = (
                            "id 列の無い記事表はコピー時の自動採番で id が"
                            "振り直され、配送カーソルの座標を復元できない"
                        )
                    else:
                        reason = "重複・孤児の判定ができない"
                    raise RuntimeError(
                        f"テーブル '{table_name}' はキー列 "
                        f"{', '.join(missing_feed_keys)} を欠く部分スキーマの"
                        f"まま {partial_rows} 行を持っており、{reason}ため"
                        "移行できません。移行は中止され元 DB が復元されます "
                        "— 当該テーブルの行を退避・削除するか、キー列を"
                        "補ってから再実行してください。"
                    )
                logging.warning(
                    "テーブル '%s' はキー列 %s を欠く部分スキーマですが空の"
                    "ため、0 行コピーで続行します (新スキーマで空のまま"
                    "作り直されます)",
                    table_name, ", ".join(missing_feed_keys),
                )
                continue
            feed_filter_sql = _feed_copy_filter_select(
                table_name,
                source_inspector=source_inspector,
                dialect=source_engine.dialect,
            )

            try:
                # ソーステーブルからデータを読み取る
                with source_engine.connect() as src_conn:
                    total_rows = None
                    if feed_filter_sql is not None:
                        total_rows = src_conn.execute(text(
                            f'SELECT COUNT(*) FROM "{table_name}"'
                        )).scalar() or 0
                    result = src_conn.execute(text(
                        feed_filter_sql
                        if feed_filter_sql is not None
                        else f'SELECT * FROM "{table_name}"'
                    ))
                    source_columns = list(result.keys())
                    rows = result.fetchall()

                if total_rows is not None and total_rows > len(rows):
                    # 黙って間引かない: 何件をコピー対象から外したか表明する
                    logging.warning(
                        "テーブル '%s' の重複・親なし・必須キー NULL の"
                        "フィード行 %d 件をコピー対象から除外しました "
                        "(バックアップ側には全行が残っています)",
                        table_name, total_rows - len(rows),
                    )

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

        # Post-migration: convert legacy AI state columns into AUTONOMY_ENABLED.
        # The column-copy phase above only carries over columns that exist in the
        # new schema, so when INTERACTION_MODE / ACTIVITY_STATE are dropped their
        # values are lost without an explicit translation step. Two historical
        # source columns are possible depending on DB age (INTERACTION_MODE was
        # replaced by ACTIVITY_STATE, which is in turn replaced by
        # AUTONOMY_ENABLED here, 2026-07-14) — run both; each no-ops if its
        # source column is absent. Order: legacy INTERACTION_MODE first, then
        # the more recent ACTIVITY_STATE so it wins if a DB somehow has both.
        _migrate_interaction_mode_to_autonomy_enabled(source_engine, target_engine)
        _migrate_activity_state_to_autonomy_enabled(source_engine, target_engine)

        # Post-migration: assign slot numbers to existing items (in order of CREATED_AT)
        _assign_initial_slot_numbers(target_engine)

        # Post-migration: assign short_ids to existing Tracks that don't have one.
        _backfill_track_short_ids(target_engine)

        # Post-migration: assign world-global short_ids to existing items.
        _backfill_item_short_ids(target_engine)

        # Post-migration: City の表示名 (新設 CITYNAME) を DESCRIPTION から復元する。
        # 全書換の列コピーは列名一致でしか運ばないので、旧 DESCRIPTION → 新 CITYNAME
        # の移送はここで行う。docs/intent/city_identity.md §6。
        _backfill_city_display_names(target_engine)

        # Post-migration: feed_item の採番高水位をソース (剪定前の真の値) と
        # 配送カーソルから継承する — コピーだけでは sequence が現存行の最大
        # id までしか進まず、剪定済みの高 id を指すカーソルを下回る (Y1)。
        _inherit_feed_item_sequence_high_water(source_engine, target_engine)

        # Post-migration: demote legacy STATUS_WAITING Tracks to 'pending'.
        # waiting_for / waiting_timeout_at columns are dropped by the schema
        # copy phase above (they no longer exist in the new schema), but the
        # status string is preserved as-is. Surface those Tracks as pending so
        # the persona can re-evaluate them via meta judgment instead of being
        # stuck in a now-undefined state. See
        # docs/intent/persona_cognition/handoff_waiting_track_removal.md.
        _demote_legacy_waiting_tracks(target_engine)

        # Post-migration: convert legacy ActionTrack.tasks_json checklists into
        # unified persona_task rows (track_id-bound). The column-copy phase only
        # carries columns present in the new schema, so once tasks_json is
        # dropped its data would be lost without this explicit translation.
        # See docs/intent/persona_cognition/unified_task_model.md §3.1.
        _migrate_track_tasks_json_to_persona_task(source_engine, target_engine)

    except Exception as e:
        logging.error(f"マイグレーション中にエラーが発生しました: {e}", exc_info=True)
        logging.info("ロールバックを試みます...")
        # DB接続を一度閉じてからファイル操作を行う
        source_engine.dispose()
        target_engine.dispose()
        rollback_error = None
        try:
            if os.path.exists(backup_path):
                if os.path.exists(db_path):
                    os.remove(db_path)
                shutil.move(backup_path, db_path)
                logging.info("ロールバックが完了しました。元のデータベースが復元されました。")
        except Exception as rb_e:
            logging.error(f"ロールバックに失敗しました: {rb_e}", exc_info=True)
            rollback_error = rb_e

        if rollback_error is not None:
            raise RuntimeError(
                "DBマイグレーションとロールバックの両方に失敗しました。"
                f" original={db_path}, backup={backup_path}, "
                f"migration_error={type(e).__name__}: {e}, "
                f"rollback_error={type(rollback_error).__name__}: {rollback_error}"
            ) from rollback_error
        raise RuntimeError(
            f"DBマイグレーションに失敗し、元DBへロールバックしました: {db_path}"
        ) from e
    finally:
        source_engine.dispose()
        target_engine.dispose()

def _migrate_interaction_mode_to_autonomy_enabled(source_engine, target_engine) -> None:
    """Map ultra-legacy AI.INTERACTION_MODE values directly to AI.AUTONOMY_ENABLED.

    Historical chain: INTERACTION_MODE (retired) -> ACTIVITY_STATE (retired
    2026-07-14) -> AUTONOMY_ENABLED (current). ACTIVITY_STATE no longer exists
    in the target schema, so a DB old enough to still carry INTERACTION_MODE
    is translated straight to AUTONOMY_ENABLED (skipping the now-removed
    intermediate column) using the same equivalence the old two-step mapping
    implied ('auto' was the only mode that mapped to the equivalent of
    ACTIVITY_STATE='Active'; everything else mapped to a non-Active state):

    - 'auto'          -> True  (自律行動含めて全動作)
    - 'user'/'manual'/'sleep'/other -> False

    Runs only when the source DB still has INTERACTION_MODE; otherwise no-op
    (the mainstream case today: real DBs carry ACTIVITY_STATE instead — see
    :func:`_migrate_activity_state_to_autonomy_enabled`).
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

        with target_engine.begin() as tgt:
            converted = 0
            for ai_id, legacy_mode in rows:
                enabled = (legacy_mode or "").strip() == "auto"
                tgt.execute(
                    text('UPDATE "AI" SET AUTONOMY_ENABLED = :enabled WHERE AIID = :id'),
                    {"enabled": enabled, "id": ai_id},
                )
                converted += 1
        logging.info(
            "INTERACTION_MODE -> AUTONOMY_ENABLED 変換完了: %d 件のレコードを更新しました。",
            converted,
        )
    except Exception as exc:
        logging.error(
            "INTERACTION_MODE -> AUTONOMY_ENABLED 変換に失敗しました: %s",
            exc,
            exc_info=True,
        )
        raise


def _migrate_activity_state_to_autonomy_enabled(source_engine, target_engine) -> None:
    """Map legacy AI.ACTIVITY_STATE values to AI.AUTONOMY_ENABLED (2026-07-14).

    ACTIVITY_STATE was a 4-state column ('Stop'/'Sleep'/'Idle'/'Active') but
    every real gate in the codebase only ever tested ``== 'Active'`` /
    ``!= 'Active'`` — Stop/Sleep/Idle were never actually distinguished, and
    'Stop' (meant to mean "fully halted") was never implemented. Replacing the
    column with a plain boolean makes that reality explicit:

    - 'Active'               -> True
    - 'Stop'/'Sleep'/'Idle'/other/NULL -> False

    This is the mainstream migration path today (real DBs carry
    ACTIVITY_STATE, not the older INTERACTION_MODE — see
    :func:`_migrate_interaction_mode_to_autonomy_enabled` for that older
    case). Runs only when the source DB still has ACTIVITY_STATE; otherwise
    no-op. Without this step, the column-copy phase in
    :func:`migrate_database_in_place` would silently leave every persona's
    AUTONOMY_ENABLED at the new column's default (True) — turning every
    previously-Idle persona's autonomous behavior on.
    """
    try:
        source_inspector = inspect(source_engine)
        if not source_inspector.has_table("AI"):
            return
        source_cols = {c["name"] for c in source_inspector.get_columns("AI")}
        if "ACTIVITY_STATE" not in source_cols:
            logging.info("ACTIVITY_STATE が source DB に存在しないため、変換をスキップします。")
            return

        with source_engine.connect() as src:
            rows = src.execute(text('SELECT AIID, ACTIVITY_STATE FROM "AI"')).fetchall()

        if not rows:
            return

        with target_engine.begin() as tgt:
            converted = 0
            for ai_id, legacy_state in rows:
                enabled = (legacy_state or "").strip() == "Active"
                tgt.execute(
                    text('UPDATE "AI" SET AUTONOMY_ENABLED = :enabled WHERE AIID = :id'),
                    {"enabled": enabled, "id": ai_id},
                )
                converted += 1
        logging.info(
            "ACTIVITY_STATE -> AUTONOMY_ENABLED 変換完了: %d 件のレコードを更新しました。",
            converted,
        )
    except Exception as exc:
        logging.error(
            "ACTIVITY_STATE -> AUTONOMY_ENABLED 変換に失敗しました: %s",
            exc,
            exc_info=True,
        )
        raise


def _migrate_track_tasks_json_to_persona_task(source_engine, target_engine) -> None:
    """Convert legacy ``ActionTrack.tasks_json`` checklists into ``persona_task`` rows.

    Each ``{title, done}`` entry becomes a track_id-bound persona_task
    (``parent_kind='track'``). ``done=True`` -> status 'completed', else 'pending'.
    ``goal`` reuses the title (the lightweight checklist had no goal field).

    Idempotent: skips any track that already has persona_task rows in the target
    (so re-running the migration does not duplicate). Reads tasks_json from the
    **source** (backup) DB because the new schema no longer carries the column.
    No-op when the source has no tasks_json column (already migrated).
    """
    import json as _json
    import uuid as _uuid
    from datetime import datetime as _dt

    try:
        source_inspector = inspect(source_engine)
        if not source_inspector.has_table("action_track"):
            return
        src_cols = {c["name"] for c in source_inspector.get_columns("action_track")}
        if "tasks_json" not in src_cols:
            logging.info("action_track.tasks_json が source DB に無いため、track_task 移行をスキップします。")
            return

        with source_engine.connect() as src:
            rows = src.execute(text(
                'SELECT track_id, persona_id, tasks_json FROM "action_track" '
                'WHERE tasks_json IS NOT NULL AND tasks_json != ""'
            )).fetchall()
        if not rows:
            logging.info("移行対象の tasks_json を持つ Track はありませんでした。")
            return

        now = _dt.now()
        migrated_tracks = 0
        migrated_tasks = 0
        # persona ごとの次 short_id (既存 MAX から継続。物理削除しない不変条件下で単調)。
        persona_next_short: dict = {}

        def _next_short(tgt, pid: str) -> int:
            if pid not in persona_next_short:
                cur = tgt.execute(text(
                    'SELECT MAX(short_id) FROM "persona_task" WHERE persona_id = :pid'
                ), {"pid": pid}).scalar()
                persona_next_short[pid] = (cur or 0) + 1
            val = persona_next_short[pid]
            persona_next_short[pid] = val + 1
            return val

        with target_engine.begin() as tgt:
            for track_id, persona_id, tasks_json in rows:
                # 冪等: 既に persona_task 行がある Track はスキップ
                existing = tgt.execute(text(
                    'SELECT 1 FROM "persona_task" WHERE track_id = :tid LIMIT 1'
                ), {"tid": track_id}).fetchone()
                if existing is not None:
                    continue
                try:
                    items = _json.loads(tasks_json)
                except (TypeError, ValueError) as exc:
                    raise ValueError(
                        f"Track {track_id} の tasks_json を解析できません"
                    ) from exc
                if not isinstance(items, list):
                    raise ValueError(
                        f"Track {track_id} の tasks_json は配列ではありません"
                    )
                for item_index, item in enumerate(items):
                    if not isinstance(item, dict):
                        raise ValueError(
                            f"Track {track_id} の tasks_json[{item_index}] はobjectではありません"
                        )
                    title = (item.get("title") or "").strip()
                    if not title:
                        raise ValueError(
                            f"Track {track_id} の tasks_json[{item_index}] にtitleがありません"
                        )
                    done = bool(item.get("done"))
                    status = "completed" if done else "pending"
                    tgt.execute(text(
                        'INSERT INTO "persona_task" ('
                        'id, persona_id, short_id, parent_kind, note_id, track_id, '
                        'title, goal, summary, notes, status, priority, origin, '
                        'active_step_id, due_at, created_at, updated_at, completed_at, '
                        'version, last_actor'
                        ') VALUES ('
                        ':id, :pid, :short_id, :pk, NULL, :tid, '
                        ':title, :goal, :summary, NULL, :status, :priority, :origin, '
                        'NULL, NULL, :created, :updated, :completed, '
                        '0, :actor)'
                    ), {
                        "id": _uuid.uuid4().hex,
                        "pid": persona_id,
                        "short_id": _next_short(tgt, persona_id),
                        "pk": "track",
                        "tid": track_id,
                        "title": title,
                        "goal": title,
                        "summary": "",
                        "status": status,
                        "priority": "normal",
                        "origin": "migration",
                        "created": now,
                        "updated": now,
                        "completed": now if done else None,
                        "actor": "migration",
                    })
                    migrated_tasks += 1
                migrated_tracks += 1
        logging.info(
            "track_task -> persona_task 移行完了: %d Track / %d タスクを移行しました。",
            migrated_tracks, migrated_tasks,
        )
    except Exception as exc:
        logging.error(
            "track_task -> persona_task 移行に失敗しました: %s",
            exc,
            exc_info=True,
        )
        raise


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
        logging.error("legacy waiting Track の降ろし処理に失敗しました: %s", exc)
        raise


def _backfill_track_short_ids(engine) -> None:
    """既存 Track にペルソナ単位の short_id を作成日時順で割り当てる。"""
    try:
        with engine.begin() as conn:
            rows = conn.execute(text(
                'SELECT track_id, persona_id, created_at '
                'FROM action_track '
                'WHERE short_id IS NULL '
                'ORDER BY persona_id, created_at'
            )).fetchall()

            if not rows:
                return

            from collections import defaultdict
            persona_max: dict = defaultdict(int)

            existing = conn.execute(text(
                'SELECT persona_id, MAX(short_id) '
                'FROM action_track '
                'WHERE short_id IS NOT NULL '
                'GROUP BY persona_id'
            )).fetchall()
            for persona_id, max_sid in existing:
                if max_sid is not None:
                    persona_max[persona_id] = max_sid

            for track_id, persona_id, _created_at in rows:
                persona_max[persona_id] += 1
                conn.execute(
                    text('UPDATE action_track SET short_id = :sid WHERE track_id = :tid'),
                    {"sid": persona_max[persona_id], "tid": track_id},
                )

            logging.info("short_id を %d 件の Track に割り当てました。", len(rows))
    except Exception as e:
        logging.error("Track short_id バックフィルに失敗しました: %s", e)
        raise


def backfill_track_short_ids(db_path: str) -> None:
    """追加系マイグレーション後に呼ぶ standalone エントリポイント。"""
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        _backfill_track_short_ids(engine)
    finally:
        engine.dispose()


def _backfill_item_short_ids(engine) -> None:
    """既存 item に世界全体の連番 short_id を作成日時順で割り当てる。

    Track と違い item は world スコープ (saiverse.db に世界で1つ) なので、ペルソナ
    単位ではなく世界全体の単一カウンタ。参照アドレッシング統一 (item:N) の同一性キー。
    """
    try:
        with engine.begin() as conn:
            rows = conn.execute(text(
                'SELECT ITEM_ID FROM item '
                'WHERE SHORT_ID IS NULL '
                'ORDER BY CREATED_AT'
            )).fetchall()

            if not rows:
                return

            current_max = conn.execute(text(
                'SELECT COALESCE(MAX(SHORT_ID), 0) FROM item'
            )).scalar() or 0

            for (item_id,) in rows:
                current_max += 1
                conn.execute(
                    text('UPDATE item SET SHORT_ID = :sid WHERE ITEM_ID = :iid'),
                    {"sid": current_max, "iid": item_id},
                )

            logging.info("short_id を %d 件の item に割り当てました。", len(rows))
    except Exception as e:
        logging.error("Item short_id バックフィルに失敗しました: %s", e)
        raise


def backfill_item_short_ids(db_path: str) -> None:
    """追加系マイグレーション後に呼ぶ standalone エントリポイント。"""
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        _backfill_item_short_ids(engine)
    finally:
        engine.dispose()


def _backfill_schedule_instance_tokens(engine) -> None:
    """persona_schedule の NULL INSTANCE_TOKEN に行一生トークンを採番する (冪等)。

    W3 Codex 第三陣: 台帳冪等キー ``{schedule_id}:{instance_token}:{occurrence}``
    の instance_token は行作成時に書き手が採番するが、列追加 (追加系 ALTER)
    直後の既存行は NULL のまま。NULL 行は読み手が "legacy" として扱うため
    動作はするが、legacy 同士では SCHEDULE_ID 再利用の分離が効かない —
    ここで一括採番して塞ぐ。randomblob(6) は SQLite が行ごとに評価するので
    各行に異なる 12 hex 文字が入る。対象行が無ければ no-op なので起動ごとに
    無条件で呼んで問題ない。
    """
    try:
        with engine.begin() as conn:
            result = conn.execute(text(
                "UPDATE persona_schedule "
                "SET INSTANCE_TOKEN = lower(hex(randomblob(6))) "
                "WHERE INSTANCE_TOKEN IS NULL"
            ))
            if result.rowcount:
                logging.info(
                    "persona_schedule の INSTANCE_TOKEN を %d 行に採番しました。",
                    result.rowcount,
                )
    except Exception as e:
        # NULL のままでも "legacy" fallback で動作は継続する (次回起動で再試行)。
        logging.warning("INSTANCE_TOKEN バックフィルに失敗しました（スキップ）: %s", e)


def _backfill_city_display_names(engine) -> None:
    """City の表示名 (CITYNAME) を埋める (冪等)。docs/intent/city_identity.md §6。

    旧スキーマでは CITYNAME が内部の識別子で、表示名は DESCRIPTION に置かれて
    いた (チュートリアルの「City名」の書き込み先がそこだった)。改名 ALTER で
    識別子は CITY_SLUG へ退避し、新設の CITYNAME は空文字で始まるので、ここで
    表示名を復元する。

    移送元を DESCRIPTION にするのは、**チュートリアルで街の名前を入力した人の
    入力を落とさない**ため。DESCRIPTION が seed の説明文のままだった世界では
    表示名が一度だけ説明文になるが、それはマップ画面の編集で直せる (取り返しが
    つく側の不都合を選ぶ)。まはー裁定 (2026-08-14) により DESCRIPTION 自体は
    消さず両方に残す。

    DESCRIPTION も空の世界では CITY_SLUG を入れる (表示名が空でも UI は
    CITY_SLUG へフォールバックするが、編集の初期値として実体があった方がよい)。
    空の CITYNAME だけを対象にするので起動ごとに無条件で呼んでよい。
    """
    try:
        with engine.begin() as conn:
            result = conn.execute(text(
                "UPDATE city "
                "SET CITYNAME = CASE "
                "  WHEN DESCRIPTION IS NOT NULL AND trim(DESCRIPTION) != '' THEN DESCRIPTION "
                "  ELSE CITY_SLUG END "
                "WHERE CITYNAME IS NULL OR trim(CITYNAME) = ''"
            ))
            if result.rowcount:
                logging.info(
                    "City の表示名 (CITYNAME) を %d 行に復元しました。",
                    result.rowcount,
                )
    except Exception as e:
        # 表示名が空でも UI は CITY_SLUG へフォールバックするので運転は継続する。
        logging.warning("City 表示名のバックフィルに失敗しました（スキップ）: %s", e)


def backfill_city_display_names(db_path: str) -> None:
    """City 表示名の復元を単体で走らせるエントリポイント。"""
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        _backfill_city_display_names(engine)
    finally:
        engine.dispose()


def backfill_schedule_instance_tokens(db_path: str) -> None:
    """INSTANCE_TOKEN 採番を単体で走らせるエントリポイント。"""
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        _backfill_schedule_instance_tokens(engine)
    finally:
        engine.dispose()


def _backfill_day_plan_refs(engine) -> None:
    """persona_day_plan.slots_json のコマ ref を ``desire:N`` → ``task:N`` に統合する。

    参照アドレッシング統一 (Q2=A): 欲求とタスクは同一 short_id 空間なので、
    prefix を task: に畳むだけで実体は変わらない。移行対象は構造化データのみで、
    過去の自然文は触らない (reference_addressing.md §5)。冪等なので毎起動で呼んでも
    害はない (desire: を含む行だけを LIKE で拾って書き換える)。schema 変更を伴わない
    データ移行なので needs_migration では拾えず、起動時に無条件で呼ぶ。
    """
    import json
    try:
        with engine.begin() as conn:
            rows = conn.execute(text(
                "SELECT persona_id, plan_date, slots_json FROM persona_day_plan "
                "WHERE slots_json LIKE '%desire:%'"
            )).fetchall()
            if not rows:
                return
            changed = 0
            for persona_id, plan_date, slots_json in rows:
                try:
                    slots = json.loads(slots_json)
                except (json.JSONDecodeError, TypeError):
                    continue
                dirty = False
                for slot in slots:
                    ref = slot.get("ref") if isinstance(slot, dict) else None
                    if isinstance(ref, str) and ref.startswith("desire:"):
                        slot["ref"] = "task:" + ref[len("desire:"):]
                        dirty = True
                if dirty:
                    conn.execute(
                        text("UPDATE persona_day_plan SET slots_json = :s "
                             "WHERE persona_id = :p AND plan_date = :d"),
                        {"s": json.dumps(slots, ensure_ascii=False),
                         "p": persona_id, "d": plan_date},
                    )
                    changed += 1
            if changed:
                logging.info("day_plan の desire: 参照を %d 行で task: に統合しました。", changed)
    except Exception as e:
        logging.warning("day_plan ref バックフィルに失敗しました（スキップ）: %s", e)


def backfill_day_plan_refs(db_path: str) -> None:
    """slots_json の desire: → task: 統合を単体で走らせるエントリポイント。"""
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        _backfill_day_plan_refs(engine)
    finally:
        engine.dispose()


def _backfill_desire_stage_normalization(engine) -> None:
    """desire 正規化 (P3c-0): stage の物理刻印 + note_id 親バインドの撤去 + desire ノート削除。

    concept_consolidation.md P3c-0「stage を読み出し時導出から書き込み時刻印へ」の
    main DB 側移行。schema 変更を伴わないデータ移行なので needs_migration では
    拾えず、起動ごとに無条件で呼んで問題ない (各ステップとも実行後は対象行が
    残らないため冪等)。

    **順序 (a)→(b) は不変条件**: 先に parent_kind='note' を外すと、stage 導出の
    根拠 (parent_kind='note' → candidate) が消えて誤って adopted に刻まれる。

    (a) stage IS NULL の全行に derive_stage() 相当を CASE で刻印
    (a2) stage='candidate' で帳簿無し (desire_state IS NULL) の行に帳簿を
        バックフィルする — 帳簿カラム導入前に生まれた古い候補行が対象。
        「stage=candidate ⇒ 帳簿を持つ」の不変条件 (create_task が新規行で
        保証) を既存行にも揃える。鮮度の起点は既存の created_at (decay の
        フォールバックと同じ基準なので減衰挙動は変わらない)
    (b) parent_kind='note' の行を親なし (parent_kind/note_id を NULL) にする
    (c) note_type='desire' の Note 行と、それを参照する note_page/note_message/
        track_open_note を削除する (desire ノートは title と定型 description
        しか持たない器 — 中身の候補 Task 自体は (a)(b) で既に正規化済み)
    """
    try:
        with engine.begin() as conn:
            # (a) stage の物理刻印 (derive_stage() と同じ規則)
            result_a = conn.execute(text("""
                UPDATE persona_task SET stage = CASE
                    WHEN status = 'completed' THEN 'completed'
                    WHEN status = 'cancelled' AND parent_kind = 'note'
                         AND desire_state = 'expired' THEN 'dormant'
                    WHEN status = 'cancelled' THEN 'aborted'
                    WHEN parent_kind = 'note' THEN 'candidate'
                    ELSE 'adopted'
                END
                WHERE stage IS NULL
            """))
            if result_a.rowcount:
                logging.info(
                    "[desire正規化] persona_task.stage を %d 行に刻印しました。",
                    result_a.rowcount,
                )

            # (a2) 古い候補行への帳簿バックフィル (stage=candidate ⇒ 帳簿あり)
            result_a2 = conn.execute(text("""
                UPDATE persona_task SET
                    desire_state = 'fresh',
                    last_touched_at = COALESCE(last_touched_at, created_at),
                    touch_count = COALESCE(touch_count, 0)
                WHERE stage = 'candidate' AND desire_state IS NULL
            """))
            if result_a2.rowcount:
                logging.info(
                    "[desire正規化] 帳簿無しの候補 %d 行に帳簿をバックフィルしました。",
                    result_a2.rowcount,
                )

            # (b) note_id 親バインドの撤去 (候補は親なしが正規形)
            result_b = conn.execute(text("""
                UPDATE persona_task SET parent_kind = NULL, note_id = NULL
                WHERE parent_kind = 'note'
            """))
            if result_b.rowcount:
                logging.info(
                    "[desire正規化] persona_task の note 親バインドを %d 行で解除しました。",
                    result_b.rowcount,
                )

            # (c) desire ノートの削除 (関連する多対多リンクも先に消す)。
            # P3c① で note テーブル自体が database.models から削除された
            # (Note はテーマノードページへ物理統合済み) ため、Base.metadata
            # から作られた新規 DB にはこのテーブルが無い — 存在確認してから
            # 触る (無ければこのステップは何もしない、他ステップの結果は
            # 巻き戻さない)。
            note_table_exists = conn.execute(text(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='note'"
            )).fetchone() is not None
            desire_note_ids = [
                row[0] for row in conn.execute(
                    text("SELECT note_id FROM note WHERE note_type = 'desire'")
                ).fetchall()
            ] if note_table_exists else []
            if desire_note_ids:
                placeholders = ", ".join(f":id{i}" for i in range(len(desire_note_ids)))
                params = {f"id{i}": nid for i, nid in enumerate(desire_note_ids)}
                for table_name in ("note_page", "note_message", "track_open_note"):
                    conn.execute(
                        text(f"DELETE FROM {table_name} WHERE note_id IN ({placeholders})"),
                        params,
                    )
                conn.execute(
                    text(f"DELETE FROM note WHERE note_id IN ({placeholders})"), params,
                )
                logging.info(
                    "[desire正規化] desire ノート %d 件と関連リンクを削除しました。",
                    len(desire_note_ids),
                )
    except Exception as e:
        logging.error("desire 正規化マイグレーションに失敗しました: %s", e)
        raise


def backfill_desire_stage_normalization(db_path: str) -> None:
    """desire 正規化 (P3c-0) を単体で走らせるエントリポイント。"""
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        _backfill_desire_stage_normalization(engine)
    finally:
        engine.dispose()


def _drop_empty_legacy_note_tables(engine) -> None:
    """note (+ note_page/note_message/track_open_note) を空になったら DROP する (P3c①)。

    concept_consolidation.md「Note → テーマノード移行」。Note の物理格納先は
    per-persona memory.db 側の memopedia ページ (trunk root_theme) へ移った
    (``saiverse/note_theme_migration.py``、呼び出し元は
    ``SAIVerseManager._on_persona_registered``)。main DB の note テーブルは
    1 枚に全ペルソナの行が同居する単一テーブルで、移行はペルソナ単位の
    扇形移行 (persona registration のたびにそのペルソナの行だけ移す) なので、
    全ペルソナが一度は起動してこの移行を経るまで note テーブルの行はゼロに
    ならない (docs/handoff/2026-07-11_p3c_purpose_note_audit.md §0-6)。

    本関数は「note が存在してかつ空」の時だけ4テーブルを DROP する冪等ステップ
    で、未移行データが残っている間は何もしない (models.py から Note 系クラスを
    削除したため、Base.metadata 経由の schema diff (_schema_diff/needs_migration)
    はこれらのテーブルの存在に気づかない — 明示的な DROP が必要)。
    テーブル不存在は正常系 (新規 DB ではそもそも作られない)。
    """
    try:
        with engine.begin() as conn:
            exists = conn.execute(text(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='note'"
            )).fetchone()
            if exists is None:
                return
            count = conn.execute(text("SELECT COUNT(*) FROM note")).scalar()
            if count:
                logging.info(
                    "[note退役] note テーブルに未移行の行が %d 件残っているため "
                    "DROP をスキップします。", count,
                )
                return
            for table_name in ("note_page", "note_message", "track_open_note", "note"):
                conn.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
            logging.info(
                "[note退役] note/note_page/note_message/track_open_note を DROP しました。"
            )
    except Exception as e:
        logging.warning("note テーブルの DROP に失敗しました（スキップ）: %s", e)


def drop_empty_legacy_note_tables(db_path: str) -> None:
    """note 系テーブルの空 DROP (P3c①) を単体で走らせるエントリポイント。"""
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        _drop_empty_legacy_note_tables(engine)
    finally:
        engine.dispose()


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
        logging.error("スロット番号の割り当てに失敗しました: %s", e)
        raise


def _ensure_execution_ledger_tables(engine) -> None:
    """実行台帳 2 テーブル (execution_ledger / execution_outbox) を軽量パスで揃える。

    docs/intent/execution_ledger.md Phase 0。新規テーブルは needs_migration →
    try_additive_migration の汎用パス (missing_tables → CREATE TABLE) でも作られる
    が、Building Memory (ensure_building_memory_tables) と同様に「テーブル追加は
    素早く確実に適用したい」ため、CREATE TABLE IF NOT EXISTS 相当の冪等な
    軽量シンク経路を別途持つ (schema_sync.ensure_table_columns_indexes に委譲:
    未作成なら CREATE / 列不足なら ALTER / インデックス不足なら CREATE INDEX)。
    """
    try:
        from database.schema_sync import ensure_table_columns_indexes
        from database.models import ExecutionLedgerEntry, ExecutionOutboxItem
        # FK (execution_outbox → execution_ledger) があるため ledger を先に作る
        ensure_table_columns_indexes(engine, ExecutionLedgerEntry.__table__)
        ensure_table_columns_indexes(engine, ExecutionOutboxItem.__table__)
    except Exception as e:
        logging.error("実行台帳テーブルの作成に失敗しました: %s", e, exc_info=True)
        raise


def ensure_execution_ledger_tables(db_path: str) -> None:
    """実行台帳テーブルの軽量シンクを単体で走らせるエントリポイント。"""
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        _ensure_execution_ledger_tables(engine)
    finally:
        engine.dispose()


def _ensure_episode_inheritance_table(engine) -> None:
    """継承エッジ (episode_inheritance) を軽量パスで揃える。

    experience_structure.md §3.3 / 完了計画書 W13。新規テーブルは
    needs_migration → try_additive_migration の汎用パスでも作られるが、実行台帳・
    Building Memory と同様「テーブル追加は素早く確実に適用したい」ため
    CREATE TABLE IF NOT EXISTS 相当の冪等な軽量シンク経路を別途持つ
    (schema_sync.ensure_table_columns_indexes に委譲)。既存 DB に対しては
    テーブル追加のみ (既存行に触れず、エッジ 0 本で無害)。
    """
    try:
        from database.schema_sync import ensure_table_columns_indexes
        from database.models import EpisodeInheritance
        ensure_table_columns_indexes(engine, EpisodeInheritance.__table__)
    except Exception as e:
        logging.error("継承エッジテーブルの作成に失敗しました: %s", e, exc_info=True)
        raise


def ensure_episode_inheritance_table(db_path: str) -> None:
    """継承エッジテーブルの軽量シンクを単体で走らせるエントリポイント。"""
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        _ensure_episode_inheritance_table(engine)
    finally:
        engine.dispose()


def _ensure_task_book_table(engine) -> None:
    """タスク帳 (task_book) を軽量パスで揃える。

    autonomous_behavior_v3.md §4.1-2 (相手のある一件の台帳)。新規テーブルは
    needs_migration → try_additive_migration の汎用パスでも作られるが、実行台帳・
    継承エッジと同様「テーブル追加は素早く確実に適用したい」ため
    CREATE TABLE IF NOT EXISTS 相当の冪等な軽量シンク経路を別途持つ
    (schema_sync.ensure_table_columns_indexes に委譲)。既存 DB に対しては
    テーブル追加のみ (既存行に触れず、行 0 件で無害)。列の後付け
    (IDEM_KEY 等) と UNIQUE インデックス (uq_task_book_idem) も同じ委譲で
    ALTER / CREATE INDEX により追従される。CHECK 制約 (ck_task_book_due_at_integer)
    は SQLite が既存テーブルへ後付けできないが、テーブル未作成時の table.create()
    が制約込みの完全形で作るため、CHECK 無しの task_book は存在しない (本テーブルは
    制約込みの定義で一括出荷され、それ以前のリリースに存在しない)。書き込み経路の
    型検査 (saiverse/task_book.py の _validate_epoch) が同じ不変条件を二重に守る。
    """
    try:
        from database.schema_sync import ensure_table_columns_indexes
        from database.models import TaskBookEntry
        ensure_table_columns_indexes(engine, TaskBookEntry.__table__)
        # REVISION (楽観ロック版数) は後付け列だと NULL で入る (schema_sync の
        # ADD COLUMN は default を付けない)。NULL のままだと遷移 UPDATE の
        # WHERE REVISION = ? が既存行に一致せず遷移不能になるため 0 へ埋める
        # (冪等 — 埋めた後は 0 件更新)。
        from sqlalchemy import text as _text
        with engine.begin() as conn:
            conn.execute(_text(
                "UPDATE task_book SET REVISION = 0 WHERE REVISION IS NULL"
            ))
    except Exception as e:
        logging.error("タスク帳テーブルの作成に失敗しました: %s", e, exc_info=True)
        raise


def ensure_task_book_table(db_path: str) -> None:
    """タスク帳テーブルの軽量シンクを単体で走らせるエントリポイント。"""
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        _ensure_task_book_table(engine)
    finally:
        engine.dispose()


def _due_at_to_local_epoch(value):
    """``persona_task.due_at`` を epoch 秒 (整数) へ。解釈できなければ None。

    ``due_at`` は**タイムゾーンを持たないローカル時刻**として書かれている
    (``persona/tasks/store.py`` の ``_coerce_due_at`` は ISO 文字列から ``Z`` を
    剥がして naive の datetime にする)。写し先のタスク帳 ``DUE_AT`` も
    ローカル解釈の epoch で読み書きされている (``sea/sluice.py`` は
    ``int(dt.timestamp())`` で書き、``datetime.fromtimestamp()`` で表示する)。
    naive な datetime の ``timestamp()`` は Python がローカルとして解釈するので、
    それが両者を一致させる唯一の変換になる。

    SQLite 側の ``strftime('%s', ...)`` は引数を UTC として解釈するため使えない
    (Asia/Tokyo では 9 時間ずれる — 2026-08-22 Codex 指摘 4)。

    ``value`` は生 SQL 経由だと SQLAlchemy の型変換を通らず文字列で来る
    (``'2026-08-24 12:00:00.000000'``) ので、datetime と文字列の両方を受ける。
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError:
            return None
    else:
        return None
    try:
        return int(parsed.timestamp())
    except (ValueError, OSError, OverflowError):
        # 環境の timestamp() が受けない日付 (Windows の 0001-01-01 等)。
        return None


def _migrate_deadline_tasks_to_task_book(engine) -> None:
    """締め切りつきタスク (persona_task) をタスク帳 (task_book) へ機械写しする。

    autonomous_behavior_v3.md §9-8「判断なしの機械写し」の中央 DB 側。統合タスク
    テーブルのうち、v3 のタスク帳に席があるのは**期限のある生きた一件だけ**
    (§13.6「残る仕事は『締め切りの一件』だけ」)。写す条件はそれをそのまま書いた
    もの: ``due_at IS NOT NULL`` かつ ``status IN (pending, active, paused)``。

    **相手 (COUNTERPART) は NULL で入れる。** persona_task に相手を表す列は無く、
    'user' を既定にすると「ユーザーとの約束」という**存在しなかった事実**を発明
    することになる (捏造)。期限があるので §4.1-2 の合法形の三形②「期限つきの
    自分だけの一件」に当たり、相手なしのまま正当に載る。副次的な効き目として、
    完遂時の成果物参照の必須 (相手のある一件だけの縛り、§9-5) を偽って課さない。

    ``DUE_AT`` は epoch 秒の整数 (persona_task の ``due_at`` は DateTime なので
    変換する)。変換は **Python 側でローカルタイムとして** 行い、パースできない値の
    行は落とす (NULL の DUE_AT で入れると相手も期限も無い不正な行になる)。

    ⚠ **SQLite の ``strftime('%s', ...)`` は使えない** (2026-08-22 Codex 指摘 4)。
    あれは引数を UTC として解釈するが、``persona_task.due_at`` はタイムゾーンを
    持たないローカル時刻の DateTime である。書き込み側 (``persona/tasks/store.py``
    の ``_coerce_due_at`` は ISO 文字列の ``Z`` を剥がして naive の datetime にする)
    も、写し先の読み手 (``sea/sluice.py`` は ``int(dt.timestamp())`` で epoch 化し、
    ``datetime.fromtimestamp(due_at)`` で表示する) も、どちらもローカル解釈で
    一貫している。UTC 解釈で写すと Asia/Tokyo では期限が 9 時間ずれる。
    naive の datetime に対する Python の ``timestamp()`` はローカル解釈なので、
    それがそのまま正しい変換になる。

    冪等: ``IDEM_KEY = 'migration:persona_task:<id>'`` と
    ``uq_task_book_idem (PERSONA_ID, IDEM_KEY)`` の組で、二周目は
    ``WHERE NOT EXISTS`` に弾かれて 0 件。**写し元は無傷で残す** (v3 §9-8 ①)。
    """
    import uuid

    from sqlalchemy import text as _text

    try:
        with engine.begin() as conn:
            exists = conn.execute(_text(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name IN ('persona_task', 'task_book') "
                "GROUP BY 1 HAVING COUNT(*) = 2"
            )).fetchone()
            if exists is None:
                return
            rows = conn.execute(_text(
                """
                SELECT t.id, t.persona_id, t.title, t.due_at
                FROM persona_task AS t
                WHERE t.due_at IS NOT NULL
                  AND t.status IN ('pending', 'active', 'paused')
                  AND t.title IS NOT NULL
                  AND trim(t.title) != ''
                """
            )).fetchall()

            created_at = int(datetime.now().timestamp())
            copied = 0
            for task_id, persona_id, title, due_raw in rows:
                due_epoch = _due_at_to_local_epoch(due_raw)
                if due_epoch is None:
                    logging.warning(
                        "[v3機械写し] persona_task %s の due_at (%r) を epoch へ "
                        "変換できないため、この行は写しませんでした",
                        task_id, due_raw,
                    )
                    continue
                idem_key = f"migration:persona_task:{task_id}"
                result = conn.execute(
                    _text(
                        """
                        INSERT INTO task_book (
                            TASK_ID, PERSONA_ID, CONTENT, DUE_AT, COUNTERPART,
                            ORIGIN, ORIGIN_REF, STATUS, ARTIFACT_REF, OUTCOME,
                            CREATED_AT, CLOSED_AT, META_JSON, IDEM_KEY, REVISION
                        )
                        SELECT
                            :task_book_id, :persona_id, :content, :due_at, NULL,
                            'migration', :origin_ref, 'open', NULL, NULL,
                            :created_at, NULL, NULL, :idem_key, 0
                        WHERE NOT EXISTS (
                            SELECT 1 FROM task_book AS b
                            WHERE b.PERSONA_ID = :persona_id
                              AND b.IDEM_KEY = :idem_key
                        )
                        """
                    ),
                    {
                        "task_book_id": str(uuid.uuid4()),
                        "persona_id": persona_id,
                        "content": title,
                        "due_at": due_epoch,
                        "origin_ref": f"task:{task_id}",
                        "created_at": created_at,
                        "idem_key": idem_key,
                    },
                )
                copied += result.rowcount or 0
            if copied:
                logging.info(
                    "[v3機械写し] 締め切りつきタスク %d 件をタスク帳へ写しました "
                    "(写し元の persona_task は無傷で残ります)",
                    copied,
                )
    except Exception as e:
        logging.error(
            "締め切りつきタスクのタスク帳への写しに失敗しました: %s", e, exc_info=True,
        )
        raise


def migrate_deadline_tasks_to_task_book(db_path: str) -> None:
    """締め切りつきタスク → タスク帳の機械写しを単体で走らせるエントリポイント。"""
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        _migrate_deadline_tasks_to_task_book(engine)
    finally:
        engine.dispose()


def _ensure_feed_tables(engine) -> None:
    """フィード取り込み 3 テーブル (feed_subscription / feed_item / feed_read_cursor)
    を軽量パスで現行スキーマへ収束させる。

    docs/intent/rss_feed_intake.md。新規テーブルは needs_migration →
    try_additive_migration の汎用パス (missing_tables → CREATE TABLE) でも作られる
    が、実行台帳・Building Memory と同様「テーブル追加は素早く確実に適用したい」
    ため冪等な軽量シンク経路を別途持つ。既存 DB へはテーブル追加 (購読 0 本で
    無害) に加え、欠落列の補修・既存 NULL の既定値埋め (二十二巡目 Z3)・
    重複修復・feed_item の AUTOINCREMENT 再構築・採番高水位の継承・一意
    index 補修で現行スキーマへ収束させる。

    全工程を**単一 transaction** で行う (二十一巡目 Y2)。以前は列補修を
    schema_sync (engine 単位・独立 commit) に委ねていたが、nullable・default
    なしの ALTER が先に確定すると、後段の再構築 INSERT SELECT が NOT NULL 列の
    NULL で失敗 → rollback しても ALTER だけが残り、次回起動も同じ地点で失敗
    する「起動不能の固定化」になる。単一 transaction なら途中で何が失敗しても
    DB は手つかずの旧形に戻り、原因解消後の再実行で最初からやり直せる。

    一意 index 3 本の明示補修 (models.py の uq_feed_sub_fixture_url /
    uq_feed_item_sub_guid / uq_feed_cursor_persona_sub) について:
    UniqueConstraint は既存テーブルに ALTER で足せないため、既存 dev 環境にも
    効くよう UNIQUE INDEX で適用する。現行モデルの CREATE TABLE には制約が
    入っているため通常は既在で no-op — 制約なしの旧形テーブルへの収束用。

    index 作成の前に、同一キーの重複行を決定論で削除修復する。野生の重複は
    この機能の開発期の DB にのみ存在しうる。記事・カーソルは再取得で再生
    する消耗データなので、決定論の削除修復が安全。重複を例外で表明して
    migration を止めると Web UI が起動できず、「UI から削除して再実行」の
    案内自体が実行不能になる (2026-08-03 裁定)。

    修復後の index 作成失敗は migration 失敗として例外で表明する (続行
    しない) — 修復済みの DB で作成が失敗する正当な理由は無い。index 無しで
    走ると add_subscription の IntegrityError 収束 (同時 POST の
    get-or-create) が効かず、重複購読を黙って作る。一時ロック等の一過性の
    失敗も、握り潰すのではなく migration の再実行で解決するのが正しい。
    """
    from database.models import FeedSubscription, FeedItem, FeedReadCursor
    try:
        with engine.begin() as conn:
            # pysqlite (既定の legacy isolation) は BEGIN の発行を最初の DML
            # まで遅延し、それより前の DDL は engine.begin() の中でも
            # autocommit で即確定する (rollback で戻らない — 実測)。冒頭で
            # 明示 BEGIN を発行し、以降の CREATE / ALTER / 再構築 / index
            # 作成を全て同一 transaction に収める。
            raw = conn.connection.dbapi_connection
            if raw is not None and not getattr(raw, "in_transaction", False):
                conn.exec_driver_sql("BEGIN")
            # FK (feed_item / feed_read_cursor → feed_subscription) があるため
            # 購読を先に作る
            for table in (
                FeedSubscription.__table__,
                FeedItem.__table__,
                FeedReadCursor.__table__,
            ):
                _sync_feed_table_schema(conn, table)
            # 列補修の直後・重複修復より前に、既存行の NULL を既定値で埋める
            # (二十二巡目 Z3)。NULL の LAST_ITEM_ID は重複カーソル修復の
            # 大小比較を壊すため、修復より前であることに意味がある
            _backfill_feed_null_columns(conn)
            _repair_duplicate_feed_rows(conn)
            # 重複修復の後・index 補修の前に呼ぶ — 新テーブルは unique 制約
            # 付きのため、重複が残っていると再構築のコピー INSERT が
            # IntegrityError になる (関数 docstring 参照)
            _rebuild_feed_item_with_autoincrement(conn)
            # 採番の高水位をカーソルから継承する (二十一巡目 Y1)。再構築の
            # 有無に関わらず毎回検査する冪等な補修 — 過去に低く戻った
            # sequence もここで治る
            _bump_feed_item_sequence(conn)
            conn.execute(text(
                'CREATE UNIQUE INDEX IF NOT EXISTS uq_feed_sub_fixture_url '
                'ON feed_subscription ("FIXTURE_ID", "FEED_URL")'
            ))
            conn.execute(text(
                'CREATE UNIQUE INDEX IF NOT EXISTS uq_feed_item_sub_guid '
                'ON feed_item ("SUBSCRIPTION_ID", "GUID")'
            ))
            conn.execute(text(
                'CREATE UNIQUE INDEX IF NOT EXISTS uq_feed_cursor_persona_sub '
                'ON feed_read_cursor ("PERSONA_ID", "SUBSCRIPTION_ID")'
            ))
    except Exception as e:
        logging.error(
            "フィードテーブルの整備に失敗しました (全工程を rollback): %s",
            e, exc_info=True,
        )
        raise


def _sync_feed_table_schema(conn, table) -> None:
    """feed 表 1 枚の CREATE / 欠落列 ALTER / index 補修を、呼び出し元の
    transaction 内 (connection 上) で行う。

    schema_sync.ensure_table_columns_indexes と同じ収束 (未作成なら CREATE /
    列不足なら ALTER / index 不足なら CREATE INDEX) の feed 専用版。共有の
    schema_sync は engine 単位で ALTER を独立 commit するため、後段の再構築が
    失敗して rollback しても ALTER だけが残る (_ensure_feed_tables docstring の
    「起動不能の固定化」)。feed 表は列補修〜再構築〜index 補修を単一
    transaction に収める必要があり、connection を受ける自前実装を持つ。

    ALTER の列定義は try_additive_migration と同じ規則: スカラー default を
    持つ列は DEFAULT を付ける (NOT NULL ならそれも付け、既存行は default 値で
    埋まる)。それ以外 (server_default = CURRENT_TIMESTAMP 等は SQLite の
    ADD COLUMN に載せられない) は素の nullable 列として足す — feed_item の
    既存行に残る NULL は _rebuild_feed_item_with_autoincrement のコピーが
    COALESCE で埋める。
    """
    from sqlalchemy import inspect as sa_inspect
    insp = sa_inspect(conn)
    if not insp.has_table(table.name):
        # 現行モデル DDL そのまま (unique 制約 + AUTOINCREMENT + index 込み)
        table.create(bind=conn)
        logging.info("%s テーブルを新規作成しました", table.name)
        return
    existing_cols = {c["name"] for c in insp.get_columns(table.name)}
    for col in table.columns:
        if col.name in existing_cols:
            continue
        ddl = (
            f'ALTER TABLE {table.name} ADD COLUMN "{col.name}" '
            f'{col.type.compile(dialect=conn.dialect)}'
        )
        default_sql = _render_default_sql(col)
        if default_sql is not None:
            if not col.nullable:
                ddl += " NOT NULL"
            ddl += f" DEFAULT {default_sql}"
        conn.execute(text(ddl))
        logging.info("ALTER TABLE %s: 列追加 %s", table.name, col.name)
    existing_idx = {idx["name"] for idx in insp.get_indexes(table.name)}
    for idx in table.indexes:
        if idx.name not in existing_idx:
            idx.create(bind=conn)
            logging.info("CREATE INDEX: %s", idx.name)


def _backfill_feed_null_columns(conn) -> None:
    """フィード 3 表の「モデル上 NOT NULL かつ安全な既定値を持つ列」に残る
    NULL を既定値で埋める UPDATE (冪等・軽量、毎起動実行)。

    _ensure_feed_tables の同一 transaction 内、列補修の直後・重複修復の前に
    呼ばれる。過去の補修が残した NULL — 例えば AUTOINCREMENT 化済みの
    feed_item は再構築 (COALESCE backfill 込み) がもう走らないため、旧版の
    補修が nullable ALTER で足した列の NULL が残存し続ける — を放置すると、
    後日の全書換 migration のコピーで NOT NULL 違反として表面化する
    (二十二巡目 Z3)。また NULL の LAST_ITEM_ID は重複カーソル修復
    (_repair_duplicate_feed_rows) の大小比較を NULL にして修復漏れ →
    一意 index 作成失敗を起こすため、修復より前に埋める。

    nullability の DDL (NOT NULL 制約の付け直し) までは行わない — SQLite の
    制約変更は表の再構築が必要で、NULL さえ残っていなければ実害が無い
    (将来の全書換 migration が新 DDL の表を作って正す)。安全な既定値の無い
    参照キー列 (GUID 等) の NULL もここでは触らない — 行の削除は重複・孤児
    と違い「消耗データの再生」で正当化できず、軽量シンクの越権になる。
    全書換時はコピーフィルタが当該行をスキップする (Z2)。
    """
    from database.models import FeedSubscription, FeedItem, FeedReadCursor
    repaired = []
    for table in (
        FeedSubscription.__table__,
        FeedItem.__table__,
        FeedReadCursor.__table__,
    ):
        for col in table.columns:
            if col.nullable or col.primary_key:
                continue
            backfill_sql = _feed_not_null_backfill_sql(col, conn.dialect)
            if backfill_sql is None:
                continue
            count = conn.execute(text(
                f'UPDATE {table.name} SET "{col.name}" = {backfill_sql} '
                f'WHERE "{col.name}" IS NULL'
            )).rowcount
            if count:
                repaired.append(f"{table.name}.{col.name} = {count} 行")
    if repaired:
        # 黙って直さない: どの列を何行埋めたか表明する
        logging.warning(
            "フィード表の NOT NULL 列に残っていた NULL をモデル既定値で"
            "埋めました: %s", " / ".join(repaired),
        )


def _bump_feed_item_sequence(conn, extra_high: int = 0) -> None:
    """feed_item の採番 (sqlite_sequence) を配送カーソルの高水位以上へ進める。

    配送カーソル (FeedReadCursor.LAST_ITEM_ID) の座標系は「過去に採番された
    どの id も再利用されない」が前提だが、剪定・購読削除は最大 id の行を普通に
    消すため、**現存行だけからは剪定済みの高水位を復元できない**。再構築や
    全書換のコピーで sqlite_sequence が「現存行の最大 id」まで戻ると、剪定済み
    の高 id を指すカーソルより小さい id が新規記事に振られ、`id > LAST_ITEM_ID`
    の配送から永久に漏れる (空表なら採番が 1 から再開して非再利用も崩れる —
    二十一巡目 Y1)。現存 feed_item.id の最大・全カーソルの LAST_ITEM_ID の
    最大・既存 sequence 値・呼び出し元が知る追加の高水位 (extra_high、全書換の
    ソース側 sequence 値) の最大まで明示的に進める。下げる方向には触らない。
    """
    if conn.execute(text(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' "
        "AND name = 'sqlite_sequence'"
    )).fetchone() is None:
        # AUTOINCREMENT 表が 1 つも無い DB には sqlite_sequence 自体が無い。
        # feed_item は直前の CREATE / 再構築で AUTOINCREMENT 化済みのはずで
        # 通常は到達しない (防御のみ)
        return
    max_id = conn.execute(
        text('SELECT MAX(id) FROM feed_item')
    ).scalar() or 0
    max_cursor = conn.execute(
        text('SELECT MAX("LAST_ITEM_ID") FROM feed_read_cursor')
    ).scalar() or 0
    row = conn.execute(text(
        "SELECT seq FROM sqlite_sequence WHERE name = 'feed_item'"
    )).fetchone()
    cur_seq = int(row[0]) if row is not None and row[0] is not None else 0
    high = max(int(max_id), int(max_cursor), int(extra_high))
    if high <= cur_seq:
        return
    if row is None:
        conn.execute(
            text("INSERT INTO sqlite_sequence (name, seq) "
                 "VALUES ('feed_item', :high)"),
            {"high": high},
        )
    else:
        conn.execute(
            text("UPDATE sqlite_sequence SET seq = :high "
                 "WHERE name = 'feed_item'"),
            {"high": high},
        )
    logging.info(
        "feed_item の採番高水位を %d から %d へ進めました "
        "(配送カーソルの前提 = id 非再利用の維持)", cur_seq, high,
    )


def _repair_duplicate_feed_rows(conn) -> None:
    """一意 index 作成前の、フィード 3 テーブルの重複行の決定論修復。

    _ensure_feed_tables の同一 transaction 内で呼ばれる (3 表とも存在)。
    移行済み (または軽量シンク済み) の DB 自身に対する操作であり、全書換
    migration のソース = バックアップには触れない (そちらはコピー時フィルタ
    _feed_copy_filter_select が同じ決定論規則で間引く。ただし孤児の子行の
    扱いだけ異なる — 修復は既存 DB の行を残す = 消すのは重複解消に必要な
    分だけ、コピーは新 DB へ運ばない)。修復規則:

    - feed_subscription: 同一 (FIXTURE_ID, FEED_URL) は最古の 1 行
      (rowid 最小) を残して後発を削除。従属する feed_item /
      feed_read_cursor も同一 transaction で道連れ削除。
    - feed_item: 同一 (SUBSCRIPTION_ID, GUID) は rowid 最小を残す。
    - feed_read_cursor: 同一 (PERSONA_ID, SUBSCRIPTION_ID) は既読が
      最も進んだ行 (LAST_ITEM_ID 最大、同値なら rowid 最小) を残す —
      巻き戻すと配送済み記事の重複配送になるため。

    何行消したかは WARNING ログで表明する (黙って消さない)。
    """
    # 後発の重複購読 (rowid 最小でない行) を特定する述語
    loser_subs = (
        'SELECT "SUBSCRIPTION_ID" FROM feed_subscription WHERE rowid NOT IN ('
        'SELECT MIN(rowid) FROM feed_subscription '
        'GROUP BY "FIXTURE_ID", "FEED_URL")'
    )
    orphan_items = conn.execute(text(
        f'DELETE FROM feed_item WHERE "SUBSCRIPTION_ID" IN ({loser_subs})'
    )).rowcount
    orphan_cursors = conn.execute(text(
        f'DELETE FROM feed_read_cursor WHERE "SUBSCRIPTION_ID" IN ({loser_subs})'
    )).rowcount
    dup_subs = conn.execute(text(
        'DELETE FROM feed_subscription WHERE rowid NOT IN ('
        'SELECT MIN(rowid) FROM feed_subscription '
        'GROUP BY "FIXTURE_ID", "FEED_URL")'
    )).rowcount
    dup_items = conn.execute(text(
        'DELETE FROM feed_item WHERE rowid NOT IN ('
        'SELECT MIN(rowid) FROM feed_item GROUP BY "SUBSCRIPTION_ID", "GUID")'
    )).rowcount
    dup_cursors = conn.execute(text(
        'DELETE FROM feed_read_cursor AS c WHERE EXISTS ('
        'SELECT 1 FROM feed_read_cursor AS k '
        'WHERE k."PERSONA_ID" = c."PERSONA_ID" '
        'AND k."SUBSCRIPTION_ID" = c."SUBSCRIPTION_ID" '
        'AND (k."LAST_ITEM_ID" > c."LAST_ITEM_ID" '
        'OR (k."LAST_ITEM_ID" = c."LAST_ITEM_ID" AND k.rowid < c.rowid)))'
    )).rowcount
    if dup_subs or dup_items or dup_cursors or orphan_items or orphan_cursors:
        logging.warning(
            "フィードの重複行を修復しました: 重複購読 %d 行 (従属記事 %d 行・"
            "従属カーソル %d 行を道連れ削除) / 重複記事 %d 行 / "
            "重複カーソル %d 行",
            dup_subs, orphan_items, orphan_cursors, dup_items, dup_cursors,
        )


def _rebuild_feed_item_with_autoincrement(conn) -> None:
    """feed_item テーブルに AUTOINCREMENT が無ければ再構築して付与する (冪等)。

    FeedItem.id は配送カーソル (FeedReadCursor.LAST_ITEM_ID より新しいか) と
    剪定の id 上限ガードの座標で、「一度使った id は再利用されない」単調性が
    前提。素の INTEGER PRIMARY KEY (rowid の別名) は最大 id の行を削除すると
    次の INSERT が同じ id を再利用する — 剪定は published 順で残す行を選ぶため
    最大 id の行 (newest-first 初回取り込みの最古記事) を普通に消すし、購読
    削除も記事を丸ごと消す。再利用が起きるとカーソルより小さい id の新着が
    永久に配送から漏れる (二十巡目 V1)。

    models.py は sqlite_autoincrement で新規 CREATE に AUTOINCREMENT を出すが
    (全書換 migration の Base.metadata.create_all 経路も同じ定義で作る)、
    それ以前に作られた既存テーブルには SQLite の制約上 ALTER で足せず、
    再構築 (rename → 新 CREATE → INSERT SELECT → DROP) でしか直せない。
    本機能は未リリースで対象は開発期 dev DB のみ。呼び出し元
    (_ensure_feed_tables) の同一 transaction 内で行い、失敗すれば丸ごと
    rollback する。行と id は保存される。コピー INSERT が進める
    sqlite_sequence は「現存行の最大 id」まで — 剪定済みの高 id を指す配送
    カーソルには届かないため、直後に _bump_feed_item_sequence が高水位を
    継承する (二十一巡目 Y1)。

    順序の前提: _repair_duplicate_feed_rows の**後**に呼ぶこと — 新テーブルは
    unique 制約 (uq_feed_item_sub_guid) 付きのため、重複行が残っていると
    コピー INSERT が IntegrityError で失敗する。

    必須キー (SUBSCRIPTION_ID / GUID — 既定値で救えない NOT NULL 列) が
    NULL の行は、全書換のコピーフィルタ (Z2) と同じ規則でコピーから
    スキップし、件数を WARNING で表明する (二十三巡目 AA1)。素通しすると
    新テーブルの NOT NULL 制約でコピー INSERT が失敗し、migration 全体が
    rollback → 毎起動同じ地点で失敗する起動不能になる。起動時 backfill
    (_backfill_feed_null_columns) は既定値のある列しか埋めないため、
    ここに必ず届きうる。
    """
    from sqlalchemy.schema import CreateTable
    from database.models import FeedItem

    row = conn.execute(text(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'feed_item'"
    )).fetchone()
    if row is None or "AUTOINCREMENT" in (row[0] or "").upper():
        return
    conn.execute(text('ALTER TABLE feed_item RENAME TO feed_item_pre_autoinc'))
    # 現行モデル定義そのままの DDL (AUTOINCREMENT + unique 制約 + FK 込み)
    conn.execute(text(str(CreateTable(FeedItem.__table__).compile(conn.engine))))
    # NOT NULL 列は COALESCE でモデル既定値へ backfill する (二十一巡目 Y2)。
    # 旧形テーブルへの列補修 (_sync_feed_table_schema) は server_default
    # (FETCHED_AT の CURRENT_TIMESTAMP) を ADD COLUMN に載せられず nullable で
    # 足すため、既存行の当該列は NULL のまま — 素通しでコピーすると新テーブル
    # の NOT NULL 制約で migration 全体が失敗する
    cols = []
    select_exprs = []
    for col in FeedItem.__table__.columns:
        cols.append(f'"{col.name}"')
        expr = f'"{col.name}"'
        if not col.nullable and not col.primary_key:
            backfill_sql = _feed_not_null_backfill_sql(col, conn.dialect)
            if backfill_sql is not None:
                expr = f'COALESCE("{col.name}", {backfill_sql})'
        select_exprs.append(expr)
    # 必須キー NULL 行のスキップ (AA1 — 規則は docstring 参照)。述語は旧表の
    # 実在列から組む (直前の _sync_feed_table_schema で列は補修済みだが、
    # 存在しない列を参照する SQL は生成しないという Z1 の規律に合わせる)
    old_cols = {
        r[1] for r in conn.execute(text(
            "PRAGMA table_info('feed_item_pre_autoinc')"
        ))
    }
    required_pred = _feed_required_key_predicate(
        "feed_item", old_cols, conn.dialect,
    )
    skipped = conn.execute(text(
        'SELECT COUNT(*) FROM feed_item_pre_autoinc '
        f'WHERE NOT ({required_pred})'
    )).scalar() or 0
    if skipped:
        # 黙って間引かない: 何行をコピー対象から外したか表明する
        logging.warning(
            "feed_item の AUTOINCREMENT 再構築で、必須キーが NULL の %d 行を"
            "コピー対象から除外しました (既定値で救えない修復不能行)",
            skipped,
        )
    conn.execute(text(
        f'INSERT INTO feed_item ({", ".join(cols)}) '
        f'SELECT {", ".join(select_exprs)} FROM feed_item_pre_autoinc '
        f'WHERE {required_pred}'
    ))
    # DROP は旧テーブルに付いていた index (idx_feed_item_sub / 補修済みの
    # uq_feed_item_sub_guid) も道連れに消す — 新テーブル側へここで作り直す
    # (uq index は直後の _ensure_feed_tables の補修が立てる)
    conn.execute(text('DROP TABLE feed_item_pre_autoinc'))
    conn.execute(text(
        'CREATE INDEX IF NOT EXISTS idx_feed_item_sub '
        'ON feed_item ("SUBSCRIPTION_ID", "id")'
    ))
    logging.info(
        "feed_item テーブルを AUTOINCREMENT 付きで再構築しました "
        "(配送カーソルの前提 = id 単調性の獲得)"
    )


def _feed_not_null_backfill_sql(column, dialect) -> "str | None":
    """フィード表の NOT NULL 列の NULL を埋める既定値 SQL (モデル定義から導出)。

    feed_item の再構築コピー・全書換コピーフィルタの COALESCE (Z2)・起動時の
    NULL 埋め UPDATE (Z3) が共用する。スカラー default (TITLE / SUMMARY /
    LINK の '') は _render_default_sql、server_default (FETCHED_AT 等の
    CURRENT_TIMESTAMP) は SQL 式としてコンパイルして返す。どちらも無い列
    (SUBSCRIPTION_ID / GUID — NULL ならその行自体が壊れている) は None =
    backfill しない (行の扱いは呼び出し元の責務)。
    """
    default_sql = _render_default_sql(column)
    if default_sql is not None:
        return default_sql
    server_default = getattr(column, "server_default", None)
    arg = getattr(server_default, "arg", None)
    if arg is None:
        return None
    try:
        return str(arg.compile(dialect=dialect))
    except Exception:
        return None


def _inherit_feed_item_sequence_high_water(source_engine, target_engine) -> None:
    """全書換 migration 後、feed_item の採番高水位をソース DB から継承する。

    全書換は新 DB (Base.metadata.create_all) を作ってから行をコピーするため、
    sqlite_sequence は「コピーされた行の最大 id」までしか進まない。剪定済みの
    高 id を指す配送カーソル (FeedReadCursor.LAST_ITEM_ID) がそれを上回って
    いると、新規記事がカーソル未満の id で採番され `id > LAST_ITEM_ID` の
    配送から永久に漏れる (二十一巡目 Y1 — 理由の詳細は
    _bump_feed_item_sequence の docstring)。コピー後のターゲット側で、現存
    最大 id・カーソル最大値に加え、ソース側 sqlite_sequence の値 (剪定前の
    真の高水位を含む) 以上へ明示的に進める。
    """
    src_seq = 0
    with source_engine.connect() as conn:
        if conn.execute(text(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' "
            "AND name = 'sqlite_sequence'"
        )).fetchone() is not None:
            src_seq = conn.execute(text(
                "SELECT seq FROM sqlite_sequence WHERE name = 'feed_item'"
            )).scalar() or 0
    with target_engine.begin() as conn:
        _bump_feed_item_sequence(conn, extra_high=int(src_seq))


# _feed_copy_filter_select が生成する SQL の参照するキー列 (自表側) に加え、
# コピーが意味を保つために欠けてはならない必須コピーキー列。部分スキーマの
# 野生 DB でこれらが欠けていると、生成した SQL 自体が "no such column" で
# 落ちるか、コピー結果が意味を失う。呼び出し元は生成前に
# _feed_copy_missing_key_columns で実列を検査する (二十二巡目 Z1)。
#
# feed_item の id はフィルタ SQL には現れないが必須コピーキー (二十三巡目
# AA2): id 列の無い表をコピーすると INSERT の自動採番で id が振り直され、
# 剪定済みの旧 id を指しうる配送カーソル (FeedReadCursor.LAST_ITEM_ID) の
# 座標系が壊れる。旧 id → 新 id の決定的な移送は実装しない — models.py は
# feed_item を常に id 付きで CREATE するため id 列の無い表はこのコード系譜
# から生まれ得ず、万一の野生 DB への防衛は明示停止 (行があれば移行エラー、
# 空なら 0 行コピー) で足りる。
_FEED_COPY_KEY_COLUMNS = {
    "feed_subscription": ("SUBSCRIPTION_ID", "FIXTURE_ID", "FEED_URL"),
    "feed_item": ("id", "SUBSCRIPTION_ID", "GUID"),
    "feed_read_cursor": ("PERSONA_ID", "SUBSCRIPTION_ID", "LAST_ITEM_ID"),
}


def _feed_copy_missing_key_columns(source_inspector, table_name):
    """フィード表のコピーフィルタが参照するキー列のうち、ソース表に実在
    しない列名の tuple。フィード表でない・表自体が無い場合は空 tuple
    (このケースの扱いは呼び出し元の責務)。"""
    key_cols = _FEED_COPY_KEY_COLUMNS.get(table_name)
    if key_cols is None or not source_inspector.has_table(table_name):
        return ()
    existing = {c["name"] for c in source_inspector.get_columns(table_name)}
    return tuple(c for c in key_cols if c not in existing)


def _feed_copy_required_not_null_columns(table_name, dialect):
    """モデル上 NOT NULL かつ安全な既定値 (backfill SQL) の無い列名リスト。

    これらの列が NULL の行は backfill でも救えない (新スキーマの NOT NULL
    制約で INSERT が失敗する) ため、コピーフィルタの WHERE で行ごとスキップ
    する (二十二巡目 Z2 — 参照キーの無い行は修復不能な消耗データ)。
    Integer PK (rowid の別名) は SQLite 上 NULL になりえないため除外。
    """
    from sqlalchemy import Integer
    table = Base.metadata.tables[table_name]
    out = []
    for col in table.columns:
        if col.nullable:
            continue
        if col.primary_key and isinstance(col.type, Integer):
            continue
        if _feed_not_null_backfill_sql(col, dialect) is None:
            out.append(col.name)
    return out


def _feed_required_key_predicate(
    table_name, existing_columns, dialect, prefix="",
):
    """必須キー列 (_feed_copy_required_not_null_columns) が全て非 NULL の行
    だけを通す WHERE 述語 SQL。

    全書換のコピーフィルタ (二十二巡目 Z2) と feed_item の AUTOINCREMENT
    再構築コピー (二十三巡目 AA1) が「必須キー NULL 行はスキップ」の同じ
    規則を共用する。existing_columns に無い列は述語から外す (モデル改定で
    必須列が増えた直後の旧表への防衛 — 存在しない列を参照する SQL は
    生成しない)。対象列が無ければ SQL が壊れない恒真式を返す。
    """
    preds = [
        f'{prefix}"{c}" IS NOT NULL'
        for c in _feed_copy_required_not_null_columns(table_name, dialect)
        if c in existing_columns
    ]
    if not preds:
        # 現行モデルでは常に非空。空でも SQL が壊れない恒真式を返す
        return "1 = 1"
    return " AND ".join(preds)


def _feed_copy_select_exprs(table_name, source_inspector, dialect, prefix=""):
    """コピーフィルタ SELECT の列リストを、ソースに実在する列から組み立てる。

    モデル上 NOT NULL で安全な既定値を持つ列は COALESCE で backfill する
    (二十二巡目 Z2) — nullable の旧形テーブルに残った NULL を素通しで
    コピーすると、新スキーマの NOT NULL 制約で migration 全体が落ちる。
    モデルに無いソース列 (廃止列) も素通しで並べる — どの列を新 DB へ
    運ぶかは呼び出し元の列突合 (target との交差) が決める。全列に AS を
    付け、prefix (別名参照) 越しでも結果の列名が素の列名になるようにする。
    """
    model_table = Base.metadata.tables[table_name]
    exprs = []
    for info in source_inspector.get_columns(table_name):
        name = info["name"]
        ref = f'{prefix}"{name}"'
        expr = ref
        col = model_table.columns.get(name)
        if col is not None and not col.nullable and not col.primary_key:
            backfill_sql = _feed_not_null_backfill_sql(col, dialect)
            if backfill_sql is not None:
                expr = f'COALESCE({ref}, {backfill_sql})'
        exprs.append(f'{expr} AS "{name}"')
    return exprs


def _feed_copy_filter_select(table_name: str, *, source_inspector, dialect):
    """全書換 migration のコピーで使う、フィード 3 テーブルの「勝者行のみ +
    既定値 backfill」SELECT 文を返す。フィード表以外は None (通常の
    SELECT * でコピー)。

    ソース = バックアップ DB は読み取り専用のまま、コピーされる行だけを
    起動時修復 (_repair_duplicate_feed_rows) と同じ決定論規則で選ぶ
    (孤児の子行の扱いだけ修復より厳しい — 修復は残す、コピーは落とす。
    下の docstring 末尾参照):

    - feed_subscription: 同一 (FIXTURE_ID, FEED_URL) は最古 (rowid 最小) のみ。
      FEED_URL は保存時に正規化済み (FeedManager._normalize_feed_url) なので
      格納値どおりの比較でよい — 一意 index (uq_feed_sub_fixture_url) も
      格納値に対して立つため、index が通す行を余分に落とさない。
    - feed_item: 勝者購読に親を持つ行に限定し、同一
      (SUBSCRIPTION_ID, GUID) は rowid 最小のみ。
    - feed_read_cursor: 勝者購読に親を持つ行に限定し、同一 (PERSONA_ID,
      SUBSCRIPTION_ID) は既読が最も進んだ行 (LAST_ITEM_ID 最大、同値なら
      rowid 最小) のみ — 巻き戻すと配送済み記事の重複配送になるため。
      LAST_ITEM_ID の大小比較は COALESCE でモデル既定値 (0) に落として
      から行う — NULL のままだと比較が NULL になり、重複がどちらも
    「敗者にならず」に両方コピーされて unique 制約で落ちる。

    NULL の扱い (二十二巡目 Z2 — nullable の旧形テーブルへの防衛):

    - スカラー default / server default を持つ NOT NULL 列 (TITLE /
      FETCHED_AT / CREATED_AT 等) の NULL は SELECT 列側の COALESCE で
      既定値へ backfill してコピーする (_feed_copy_select_exprs)。
    - 安全な既定値の無い NOT NULL 列 (SUBSCRIPTION_ID / GUID / PERSONA_ID
      等の参照キー) が NULL の行は WHERE でスキップする — 修復不能な
      消耗データで、素通しすると新スキーマの NOT NULL で migration 全体が
      落ちる。キー NULL の購読はコピーされないため、その購読を親に持つ
      子行も勝者述語のキー非 NULL 条件で道連れに除外する (孤児を新 DB へ
      持ち込まない)。除外件数は呼び出し側が WARNING で表明する。

    敗者購読の記事・健康状態 (失敗回数等) を勝者へマージすることはしない —
    重複行は unique 制約導入前の開発期 DB にのみ存在しうる消耗データで、
    記事は再取得で再生する。

    子表 (feed_item / feed_read_cursor) のコピーは「勝者購読への EXISTS」で
    限定する — 敗者を除外する NOT IN 形だと、親購読が存在しない子行 (孤児)
    を素通しし、親表が空のときは述語が常に真になって全子行が通ってしまう
    (十二巡目 Q1)。孤児の子行は新 DB でも永遠に不活性 (取得・配送・UI の
    どの経路も購読 join で到達しない) なので、コピーしない。同じ理由で
    購読表が無い・購読表がキー列を欠く部分スキーマの野生 DB では子行を
    一切コピーしない — 親が存在しえない以上、全行が孤児確定のため
    (存在しない表・列への副問い合わせが SQLite のエラーになる事情も兼ねる)。

    前提: 自表のキー列 (_FEED_COPY_KEY_COLUMNS) は呼び出し元が
    _feed_copy_missing_key_columns で実在を確認済み (二十二巡目 Z1)。
    """
    if table_name not in _FEED_COPY_KEY_COLUMNS:
        return None
    # 参照キーが NULL の行を落とす述語 (Z2)。キー列は呼び出し元が実在を
    # 確認済みだが、モデル改定で必須列が増えた場合に備え実在列に絞る。
    # 生成は AUTOINCREMENT 再構築コピー (AA1) と共通のヘルパで行う
    existing = {c["name"] for c in source_inspector.get_columns(table_name)}

    def required_pred(prefix=""):
        return _feed_required_key_predicate(
            table_name, existing, dialect, prefix=prefix,
        )

    # 親表 = 購読表が使えるか。表が無い場合に加え、キー列を欠く部分
    # スキーマも「使えない」— 勝者述語の SQL が組めない (Z1)
    has_subscription = (
        source_inspector.has_table("feed_subscription")
        and not _feed_copy_missing_key_columns(
            source_inspector, "feed_subscription"
        )
    )
    # 勝者購読 (同一 (FIXTURE_ID, FEED_URL) の rowid 最小、かつキー非 NULL =
    # 親自身がコピーされる行) に親を持つ子行だけを通す述語。{col} には
    # 子表側の SUBSCRIPTION_ID 列参照が入る。SUBSCRIPTION_ID が NULL の
    # 親は等号比較 (w."SUBSCRIPTION_ID" = {col}) 自体が成立しない
    winner_parent_pred = (
        'EXISTS ('
        'SELECT 1 FROM feed_subscription AS w '
        'WHERE w."SUBSCRIPTION_ID" = {col} '
        'AND w."FIXTURE_ID" IS NOT NULL AND w."FEED_URL" IS NOT NULL '
        'AND w.rowid IN ('
        'SELECT MIN(rowid) FROM feed_subscription '
        'GROUP BY "FIXTURE_ID", "FEED_URL"))'
    )
    if table_name == "feed_subscription":
        exprs = _feed_copy_select_exprs(table_name, source_inspector, dialect)
        return (
            f'SELECT {", ".join(exprs)} FROM feed_subscription '
            'WHERE rowid IN ('
            'SELECT MIN(rowid) FROM feed_subscription '
            'GROUP BY "FIXTURE_ID", "FEED_URL") AND ' + required_pred()
        )
    if table_name == "feed_item":
        if not has_subscription:
            # 親表が使えない → 全子行が孤児確定 (docstring 参照)。コピーしない
            return 'SELECT * FROM feed_item WHERE 1 = 0'
        exprs = _feed_copy_select_exprs(table_name, source_inspector, dialect)
        return (
            f'SELECT {", ".join(exprs)} FROM feed_item WHERE rowid IN ('
            'SELECT MIN(rowid) FROM feed_item '
            'GROUP BY "SUBSCRIPTION_ID", "GUID") AND '
            + required_pred() + ' AND '
            + winner_parent_pred.format(col='feed_item."SUBSCRIPTION_ID"')
        )
    # feed_read_cursor
    if not has_subscription:
        # 親表が使えない → 全子行が孤児確定 (docstring 参照)。コピーしない
        return 'SELECT * FROM feed_read_cursor WHERE 1 = 0'
    exprs = _feed_copy_select_exprs(
        table_name, source_inspector, dialect, prefix="c."
    )
    last_item_default = _render_default_sql(
        Base.metadata.tables["feed_read_cursor"].columns["LAST_ITEM_ID"]
    ) or "0"
    cmp_k = f'COALESCE(k."LAST_ITEM_ID", {last_item_default})'
    cmp_c = f'COALESCE(c."LAST_ITEM_ID", {last_item_default})'
    return (
        f'SELECT {", ".join(exprs)} FROM feed_read_cursor AS c '
        'WHERE NOT EXISTS ('
        'SELECT 1 FROM feed_read_cursor AS k '
        'WHERE k."PERSONA_ID" = c."PERSONA_ID" '
        'AND k."SUBSCRIPTION_ID" = c."SUBSCRIPTION_ID" '
        f'AND ({cmp_k} > {cmp_c} '
        f'OR ({cmp_k} = {cmp_c} AND k.rowid < c.rowid))) '
        'AND ' + required_pred("c.") + ' AND '
        + winner_parent_pred.format(col='c."SUBSCRIPTION_ID"')
    )


def ensure_feed_tables(db_path: str) -> None:
    """フィードテーブルの軽量シンクを単体で走らせるエントリポイント。"""
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        _ensure_feed_tables(engine)
    finally:
        engine.dispose()


def _backfill_session_anchors(engine) -> None:
    """AI.METABOLISM_ANCHORS (単一 JSON) → session_anchor 行分離の移行 (冪等)。

    beat_execution_context.md §3.1 / SEA 監査 S8。旧列の JSON
    ``{model_key: {anchor_id, updated_at(iso), ttl_seconds?}}`` を 1 行 =
    1 (persona, model) に展開する。規則:

    - **既に session_anchor に行がある (persona, model) は上書きしない**
      (INSERT OR IGNORE — 新形式が正)。
    - 変換に成功した persona の AI.METABOLISM_ANCHORS は NULL にする
      (旧経路の読み口は撤去済みで、旧データを残すと二重の真実になる)。
      JSON が壊れている場合も NULL 化する (anchor はキャッシュ状態で損失許容 —
      最悪でも次の会話が一度コールドになるだけ)。
    - schema 変更を伴わないデータ移行なので needs_migration では拾えず、
      起動時に無条件で呼ぶ。変換対象 (非 NULL 行) が無ければ no-op。
    """
    import json
    from datetime import datetime as _dt
    try:
        # テーブル存在の軽量シンク (呼び出し順への依存を消す。冪等)。
        from database.schema_sync import ensure_table_columns_indexes
        from database.models import SessionAnchor
        ensure_table_columns_indexes(engine, SessionAnchor.__table__)

        with engine.begin() as conn:
            rows = conn.execute(text(
                "SELECT AIID, METABOLISM_ANCHORS FROM ai "
                "WHERE METABOLISM_ANCHORS IS NOT NULL"
            )).fetchall()
            if not rows:
                return

            converted_rows = 0
            converted_personas = 0
            for aiid, raw in rows:
                anchors = None
                try:
                    anchors = json.loads(raw)
                except (ValueError, TypeError):
                    logging.warning(
                        "session_anchor バックフィル: %s の METABOLISM_ANCHORS が "
                        "JSON として読めないため破棄します", aiid,
                    )
                if isinstance(anchors, dict):
                    for model_key, entry in anchors.items():
                        if not model_key or not isinstance(entry, dict):
                            continue
                        anchor_id = entry.get("anchor_id")
                        updated_raw = entry.get("updated_at")
                        try:
                            updated_epoch = int(_dt.fromisoformat(updated_raw).timestamp())
                        except (ValueError, TypeError):
                            logging.warning(
                                "session_anchor バックフィル: %s/%s の updated_at "
                                "(%r) が読めないためこの entry をスキップします",
                                aiid, model_key, updated_raw,
                            )
                            continue
                        ttl_raw = entry.get("ttl_seconds")
                        try:
                            ttl_seconds = int(ttl_raw) if ttl_raw else None
                        except (ValueError, TypeError):
                            ttl_seconds = None
                        conn.execute(
                            text(
                                "INSERT OR IGNORE INTO session_anchor "
                                "(PERSONA_ID, MODEL_KEY, ANCHOR_MESSAGE_ID, TTL_SECONDS, UPDATED_AT) "
                                "VALUES (:p, :m, :a, :t, :u)"
                            ),
                            {"p": aiid, "m": str(model_key), "a": anchor_id,
                             "t": ttl_seconds, "u": updated_epoch},
                        )
                        converted_rows += 1
                conn.execute(
                    text("UPDATE ai SET METABOLISM_ANCHORS = NULL WHERE AIID = :p"),
                    {"p": aiid},
                )
                converted_personas += 1

            logging.info(
                "session_anchor バックフィル: %d ペルソナ / %d entry を行分離しました。",
                converted_personas, converted_rows,
            )
    except Exception as e:
        # 失敗時は旧列を NULL 化しない (engine.begin のロールバック) ので、
        # 次回起動で再試行される。anchor はキャッシュ状態でスキップ許容。
        logging.warning("session_anchor バックフィルに失敗しました（スキップ）: %s", e)


def backfill_session_anchors(db_path: str) -> None:
    """METABOLISM_ANCHORS → session_anchor 行分離を単体で走らせるエントリポイント。"""
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        _backfill_session_anchors(engine)
    finally:
        engine.dispose()


def _backfill_session_head_snapshots(engine) -> None:
    """line_head_snapshot → session_head_snapshot の (persona, model) キー化移行 (冪等)。

    beat_execution_context.md §3.1 (§6-3b)。旧テーブルはキー (PERSONA_ID, LINE_ID)
    で LINE_ID は実質常に 'main'。新テーブルは PK=(PERSONA_ID, MODEL_KEY)。規則:

    - **model_key の解決**: 旧行の MODEL_KEY 列は実装バグで常に 'default' が
      入っていた (integration.py が存在しない persona.default_model 属性を引いて
      いた) ため、'default' / 空は ai.DEFAULT_MODEL (そのペルソナの標準 model) へ
      解決する。ai.DEFAULT_MODEL も NULL なら実行 model を特定できないので
      その行はスキップ (head は cache 状態で損失許容 — 次回 capture_all で再構築)。
    - **集約衝突** (複数の旧行が同一 (persona, model) に写る場合):
      LINE_ID='main' の行を優先、無ければ UPDATED_AT 最新。
    - **既に session_head_snapshot に行がある (persona, model) は上書きしない**
      (INSERT OR IGNORE — 新形式が正)。
    - 旧テーブルの行は残す (読み口は store から撤去済みで害がない。テーブル
      DROP ごと後続の掃除 wave で行う)。schema 変更を伴わないデータ移行なので
      needs_migration では拾えず、起動時に無条件で呼ぶ (対象が無ければ no-op)。
    """
    try:
        # テーブル存在の軽量シンク (呼び出し順への依存を消す。冪等)。
        from database.schema_sync import ensure_table_columns_indexes
        from database.models import SessionHeadSnapshot
        ensure_table_columns_indexes(engine, SessionHeadSnapshot.__table__)

        with engine.begin() as conn:
            legacy = conn.execute(text(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name='line_head_snapshot'"
            )).fetchone()
            if legacy is None:
                return
            rows = conn.execute(text(
                "SELECT PERSONA_ID, LINE_ID, LINE_ROLE, MODEL_KEY, SECTIONS_JSON, "
                "LAST_NOTIFIED_JSON, SNAPSHOT_VERSION, CAPTURED_AT, UPDATED_AT "
                "FROM line_head_snapshot"
            )).fetchall()
            if not rows:
                return

            default_models = {
                aiid: model
                for aiid, model in conn.execute(
                    text("SELECT AIID, DEFAULT_MODEL FROM ai")
                ).fetchall()
            }

            # (persona, model) ごとの勝者を選ぶ: line='main' 優先 > UPDATED_AT 最新。
            # UPDATED_AT は 'YYYY-MM-DD HH:MM:SS[.ffffff]' 形式の TEXT なので
            # 辞書順比較 = 時刻順比較になる。
            candidates: dict = {}
            skipped = 0
            for row in rows:
                raw_model = (row.MODEL_KEY or "").strip()
                if not raw_model or raw_model == "default":
                    resolved = (default_models.get(row.PERSONA_ID) or "").strip()
                    if not resolved:
                        logging.warning(
                            "session_head_snapshot バックフィル: %s/%s の実行 model を"
                            "特定できないためスキップします (MODEL_KEY=%r, "
                            "ai.DEFAULT_MODEL 未設定)",
                            row.PERSONA_ID, row.LINE_ID, row.MODEL_KEY,
                        )
                        skipped += 1
                        continue
                    model_key = resolved
                else:
                    model_key = raw_model

                key = (row.PERSONA_ID, model_key)
                prev = candidates.get(key)
                if prev is None:
                    candidates[key] = row
                    continue

                def _rank(r):
                    return (1 if r.LINE_ID == "main" else 0, str(r.UPDATED_AT or ""))

                if _rank(row) > _rank(prev):
                    candidates[key] = row

            inserted = 0
            for (persona_id, model_key), row in candidates.items():
                result = conn.execute(
                    text(
                        "INSERT OR IGNORE INTO session_head_snapshot "
                        "(PERSONA_ID, MODEL_KEY, LINE_ROLE, SECTIONS_JSON, "
                        "LAST_NOTIFIED_JSON, SNAPSHOT_VERSION, CAPTURED_AT, UPDATED_AT) "
                        "VALUES (:p, :m, :lr, :s, :n, :v, :c, :u)"
                    ),
                    {
                        "p": persona_id,
                        "m": model_key,
                        "lr": row.LINE_ROLE or "main_line",
                        "s": row.SECTIONS_JSON or "{}",
                        "n": row.LAST_NOTIFIED_JSON or "{}",
                        "v": row.SNAPSHOT_VERSION or 1,
                        "c": row.CAPTURED_AT,
                        "u": row.UPDATED_AT,
                    },
                )
                inserted += result.rowcount if result.rowcount and result.rowcount > 0 else 0

            # 旧テーブルの行は DROP まで残るため毎起動ここを通る。実際に新規行を
            # 書いた起動だけ info、以降の no-op 起動は debug に落とす。
            log = logging.info if (inserted or skipped) else logging.debug
            log(
                "session_head_snapshot バックフィル: 旧 %d 行 → %d Session 行を移行"
                "しました (新規 %d 行 / スキップ %d 行)。",
                len(rows), len(candidates), inserted, skipped,
            )
    except Exception as e:
        # head snapshot は cache 状態でスキップ許容 (次回 capture_all で再構築)。
        # 失敗しても次回起動で再試行される。
        logging.warning("session_head_snapshot バックフィルに失敗しました（スキップ）: %s", e)


def backfill_session_head_snapshots(db_path: str) -> None:
    """line_head_snapshot → session_head_snapshot 移行を単体で走らせるエントリポイント。"""
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        _backfill_session_head_snapshots(engine)
    finally:
        engine.dispose()


def _ensure_active_occupancy_unique(engine) -> None:
    """active occupancy の重複修復 + 部分一意 index (分離監査 P1-2 / W7 柱5)。

    「AIID ごとに EXIT_TIMESTAMP IS NULL は高々 1 行」を DB 制約にする。
    重複行がある状態で index を作ると CREATE が失敗するため、**修復が先**。
    index はモデル metadata に載せない設計 (理由は database/occupancy_repair.py
    冒頭) なので、全書換 migration 後もここが再作成する。冪等・毎起動呼び出し。
    """
    try:
        from database.occupancy_repair import (
            repair_duplicate_active_occupancy,
            ensure_active_occupancy_unique_index,
            record_startup_repairs,
        )
        with engine.begin() as conn:
            exists = conn.execute(text(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name='building_occupancy_log'"
            )).fetchone()
            if exists is None:
                return
            repairs = repair_duplicate_active_occupancy(conn)
            ensure_active_occupancy_unique_index(conn)
        if repairs:
            # manager の startup_warnings へ引き継ぐ (UI の起動時警告に出す)
            record_startup_repairs(repairs)
    except Exception as e:
        # 失敗しても起動は止めない (次回起動で再試行)。index が無い間も
        # move_entity 側の書き込み時仲裁 (close の条件付き UPDATE + 新行の
        # guarded INSERT [NOT EXISTS]) が二重 active 行を塞ぐため、index は
        # 防御の二重化 + 手書き SQL 等の外部書き込みに対する最終防衛。
        logging.warning("active occupancy 一意化に失敗しました（スキップ）: %s", e)


def ensure_active_occupancy_unique(db_path: str) -> None:
    """active occupancy の修復 + 一意 index を単体で走らせるエントリポイント。"""
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        _ensure_active_occupancy_unique(engine)
    finally:
        engine.dispose()


def _ensure_region_entrance_unique(engine) -> None:
    """Region 入口所有の一意性を DB でも強制する (W7 柱5 / Codex 第三巡)。

    「入口 Building は高々 1 つの Region に所有される」を部分一意 index で
    強制する (admin 層の read-before-write 検査は並行 create_region を
    排除できない)。レガシーデータで既に共有されている場合、自動修復は
    **しない** (どちらが入口を保持するかは人間の判断) — WARN で可視化し、
    解消されるまで index なしで続行する (admin 層の検査が引き続き防ぐ)。
    こちらも意図的にモデル metadata 外 (理由は uq_occupancy_active_ai と同じ)。
    """
    try:
        with engine.begin() as conn:
            exists = conn.execute(text(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name='region'"
            )).fetchone()
            if exists is None:
                return
            shared = conn.execute(text(
                "SELECT ENTRANCE_BUILDING_ID, COUNT(*) FROM region "
                "WHERE ENTRANCE_BUILDING_ID IS NOT NULL "
                "GROUP BY ENTRANCE_BUILDING_ID HAVING COUNT(*) > 1"
            )).fetchall()
            if shared:
                logging.warning(
                    "Region 入口が複数 Region で共有されています (%s)。"
                    "一意 index は作成できません — Region 編集画面で共有を"
                    "解消してください。",
                    ", ".join(f"{bid} x{cnt}" for bid, cnt in shared),
                )
                return
            conn.execute(text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_region_entrance_building "
                "ON region (ENTRANCE_BUILDING_ID) "
                "WHERE ENTRANCE_BUILDING_ID IS NOT NULL"
            ))
    except Exception as e:
        logging.warning("Region 入口一意化に失敗しました（スキップ）: %s", e)


def ensure_region_entrance_unique(db_path: str) -> None:
    """Region 入口一意 index を単体で走らせるエントリポイント。"""
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        _ensure_region_entrance_unique(engine)
    finally:
        engine.dispose()


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

    if args.db is not None and not os.path.isfile(db_path):
        parser.error(f"明示されたマイグレーション対象DBが存在しません: {db_path}")

    if not args.force and not needs_migration(db_path):
        logging.info("スキーマに変更はありません。マイグレーションは不要です。")
    else:
        migrate_database_in_place(db_path)
