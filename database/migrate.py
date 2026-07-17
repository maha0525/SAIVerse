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
