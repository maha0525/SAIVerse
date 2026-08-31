"""個別テーブルのスキーマ追従 helper。

main.py の needs_migration → migrate_database_in_place 経路 (全 schema rebuild) を
待たずに、 単一テーブル単位で 「テーブル未作成なら CREATE / カラム不足なら ALTER /
インデックス不足なら CREATE INDEX」 を行う軽量パス。

migration script や addon 起動時のテーブル整備に使う。
"""
from __future__ import annotations

import logging
from typing import Any, Optional


def ensure_table_columns_indexes(
    bound_engine: Any,
    table: Any,
    logger: Optional[logging.Logger] = None,
) -> None:
    """``table`` (= SQLAlchemy Table) が DB に揃っていることを保証する。

    - テーブル未作成 → CREATE TABLE
    - 列不足 → ALTER TABLE ADD COLUMN
    - index 不足 → CREATE INDEX
    """
    from sqlalchemy import inspect, text
    log = logger or logging.getLogger(__name__)
    insp = inspect(bound_engine)
    table_name = table.name
    if not insp.has_table(table_name):
        table.create(bind=bound_engine)
        log.info("%s テーブルを新規作成しました", table_name)
        return

    existing_cols = {c["name"] for c in insp.get_columns(table_name)}
    expected = {c.name: c for c in table.columns}
    missing_cols = [name for name in expected if name not in existing_cols]
    if missing_cols:
        with bound_engine.begin() as conn:
            for col_name in missing_cols:
                col = expected[col_name]
                col_type = col.type.compile(dialect=bound_engine.dialect)
                conn.execute(text(
                    f'ALTER TABLE {table_name} ADD COLUMN "{col_name}" {col_type}'
                ))
                log.info("ALTER TABLE %s: 列追加 %s %s", table_name, col_name, col_type)

    existing_idx_names = {idx["name"] for idx in insp.get_indexes(table_name)}
    for idx in table.indexes:
        if idx.name in existing_idx_names:
            continue
        idx.create(bind=bound_engine)
        log.info("CREATE INDEX: %s", idx.name)


def ensure_building_memory_tables(bound_engine: Any) -> None:
    """Phase 2+3 で追加された Building Memory 関連テーブルを起動時に揃える。

    main.py 起動時の needs_migration → migrate_database_in_place 経路でも
    結局これらは作成されるが、 そちらは全 schema rebuild という重い処理。
    軽量パスとしてこちらを使う。
    """
    from database.models import BuildingMessage, PersonaPulseCursor
    ensure_table_columns_indexes(bound_engine, BuildingMessage.__table__)
    ensure_table_columns_indexes(bound_engine, PersonaPulseCursor.__table__)
