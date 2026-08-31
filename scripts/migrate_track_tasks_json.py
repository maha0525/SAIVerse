"""Forward-migrate live ``ActionTrack.tasks_json`` into unified ``persona_task`` rows.

Track 内タスク (旧 track_task) は統合 persona_task テーブルへ一本化された
(unified_task_model.md §5 step 4)。読み取り経路が persona_task を見るように
切り替わったため、既存の ``action_track.tasks_json`` チェックリストを事前に
persona_task 行へ移しておく必要がある。

``database/migrate.py`` の post-migration フックは ``tasks_json`` 列ドロップ
(step 6) で全書換が走った時にしか発火しない。本スクリプトは列がまだ存在する
状態で **その場で順方向に** 移行するための一発ツール。同じフック関数を
source=target=live DB に対して呼ぶ (冪等: 既に persona_task 行がある Track は
スキップ)。

前提: 先に ``python database/migrate.py`` で persona_task テーブルが作成済みで
あること (未作成なら明示して止まる)。

使い方::

    python database/migrate.py            # persona_task テーブルを作る (additive)
    python scripts/migrate_track_tasks_json.py
"""
from __future__ import annotations

import logging
import os
import sys

project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, project_root)

from database.migrate import _migrate_track_tasks_json_to_persona_task  # noqa: E402
from database.paths import default_db_path  # noqa: E402
from sqlalchemy import create_engine, inspect  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")


def main() -> None:
    db_path = default_db_path()
    if not db_path.exists():
        logging.error("main DB が見つかりません: %s", db_path)
        return

    engine = create_engine(f"sqlite:///{db_path}")
    try:
        insp = inspect(engine)
        if not insp.has_table("persona_task"):
            logging.error(
                "統合テーブル persona_task が main DB にありません。先に "
                "`python database/migrate.py` を実行してください。"
            )
            return
        if not insp.has_table("action_track"):
            logging.info("action_track テーブルがありません。移行不要です。")
            return
        # source=target=live DB。フックが tasks_json を読み persona_task へ書く (冪等)。
        _migrate_track_tasks_json_to_persona_task(engine, engine)
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
