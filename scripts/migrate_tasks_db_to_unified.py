"""Migrate legacy per-persona ``tasks.db`` into the unified ``persona_task`` table.

旧 standalone Task (``~/.saiverse/personas/<id>/tasks.db``) を main DB
(saiverse.db) の統合テーブル ``persona_task`` / ``persona_task_step`` /
``persona_task_history`` へ移す。

設計: docs/intent/persona_cognition/unified_task_model.md §3.1

旧 standalone はリリース版に同梱されており、他ユーザーの手元にデータが
在りうる。**取りこぼし不可**のため、id・ステップ・履歴・タイムスタンプを
保ったまま移送する (親なし = ``parent_kind`` NULL の未所属タスクとして)。

安全:
- 実行前に main DB を ``.bak`` へバックアップする (--dry-run 時はしない)。
- 冪等: 既に ``persona_task`` に同じ id がある行はスキップする。
- ``--dry-run`` で書き込まずに移行件数だけ表示する。

使い方::

    python scripts/migrate_tasks_db_to_unified.py --dry-run
    python scripts/migrate_tasks_db_to_unified.py
    python scripts/migrate_tasks_db_to_unified.py --persona air_city_a
"""
from __future__ import annotations

import argparse
import logging
import os
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import List, Optional

project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, project_root)

from database.models import PersonaTask, PersonaTaskHistory, PersonaTaskStep  # noqa: E402
from database.paths import default_db_path  # noqa: E402
from saiverse.data_paths import get_saiverse_home  # noqa: E402
from sqlalchemy import create_engine, inspect  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

_ISO_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"


def _parse_dt(value: Optional[str]) -> Optional[datetime]:
    """標準フォーマット (ISO + Z) を datetime に。失敗時は fromisoformat / None。"""
    if not value:
        return None
    try:
        return datetime.strptime(value, _ISO_FORMAT)
    except (TypeError, ValueError):
        pass
    try:
        return datetime.fromisoformat(value.replace("Z", ""))
    except (TypeError, ValueError):
        return None


def _list_persona_task_dbs(only_persona: Optional[str]) -> List[Path]:
    personas_root = get_saiverse_home() / "personas"
    if not personas_root.exists():
        return []
    result: List[Path] = []
    for persona_dir in sorted(personas_root.iterdir()):
        if not persona_dir.is_dir():
            continue
        if only_persona and persona_dir.name != only_persona:
            continue
        db_path = persona_dir / "tasks.db"
        if db_path.exists():
            result.append(db_path)
    return result


def _read_legacy(db_path: Path) -> dict:
    """tasks.db から tasks / task_steps / task_history を読む。"""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        def _rows(table: str) -> list:
            try:
                return [dict(r) for r in conn.execute(f"SELECT * FROM {table}").fetchall()]
            except sqlite3.OperationalError:
                return []
        return {
            "tasks": _rows("tasks"),
            "task_steps": _rows("task_steps"),
            "task_history": _rows("task_history"),
        }
    finally:
        conn.close()


def migrate(dry_run: bool, only_persona: Optional[str]) -> None:
    db_path = default_db_path()
    if not db_path.exists():
        logging.error("main DB が見つかりません: %s", db_path)
        return

    # 統合テーブルが無ければ schema 移行が先。依存順序を明示して止める。
    pre_engine = create_engine(f"sqlite:///{db_path}")
    try:
        if not inspect(pre_engine).has_table("persona_task"):
            logging.error(
                "統合テーブル persona_task が main DB にありません。先に "
                "`python database/migrate.py` を実行してスキーマを移行してください。"
            )
            return
    finally:
        pre_engine.dispose()

    task_dbs = _list_persona_task_dbs(only_persona)
    if not task_dbs:
        logging.info("移行対象の tasks.db はありませんでした。")
        return

    # --- バックアップ (dry-run 以外) ---
    if not dry_run:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = db_path.with_name(f"{db_path.name}_pretaskmig_{ts}.bak")
        shutil.copy2(db_path, backup_path)
        logging.info("main DB をバックアップしました: %s", backup_path)

    engine = create_engine(f"sqlite:///{db_path}")
    SessionLocal = sessionmaker(bind=engine)
    session = SessionLocal()

    total_tasks = 0
    total_steps = 0
    total_history = 0
    skipped = 0
    try:
        existing_ids = {row[0] for row in session.query(PersonaTask.id).all()}
        # 本実行で新規追加したタスク id のみ。ステップ/履歴はこれでゲートする
        # (起動時に既存だったタスクのステップ/履歴を再挿入して UNIQUE 違反になるのを防ぐ)。
        newly_added: set = set()
        # persona ごとの次 short_id (既存 MAX から継続。物理削除しない不変条件下で単調)。
        from sqlalchemy import func as _sa_func
        persona_next_short: dict = {}

        def _next_short(pid: str) -> int:
            if pid not in persona_next_short:
                cur = (
                    session.query(_sa_func.max(PersonaTask.short_id))
                    .filter(PersonaTask.persona_id == pid)
                    .scalar()
                )
                persona_next_short[pid] = (cur or 0) + 1
            val = persona_next_short[pid]
            persona_next_short[pid] = val + 1
            return val

        for task_db in task_dbs:
            persona_id = task_db.parent.name
            data = _read_legacy(task_db)
            logging.info(
                "[%s] tasks=%d steps=%d history=%d",
                persona_id, len(data["tasks"]), len(data["task_steps"]), len(data["task_history"]),
            )
            for t in data["tasks"]:
                if t["id"] in existing_ids:
                    skipped += 1
                    continue
                # tasks.db は persona_id 列を持つが、無い古い DB もありうるので
                # ディレクトリ名を優先フォールバックにする。
                pid = t.get("persona_id") or persona_id
                session.add(PersonaTask(
                    id=t["id"],
                    persona_id=pid,
                    short_id=_next_short(pid),
                    parent_kind=None,  # 未所属 (standalone は親を持たなかった)
                    note_id=None,
                    track_id=None,
                    title=t.get("title") or "",
                    goal=t.get("goal") or "",
                    summary=t.get("summary") or "",
                    notes=t.get("notes"),
                    status=t.get("status") or "pending",
                    priority=t.get("priority") or "normal",
                    origin=t.get("origin") or "auto",
                    active_step_id=t.get("active_step_id"),
                    due_at=_parse_dt(t.get("due_at")),
                    created_at=_parse_dt(t.get("created_at")) or datetime.now(),
                    updated_at=_parse_dt(t.get("updated_at")) or datetime.now(),
                    completed_at=_parse_dt(t.get("completed_at")),
                    version=t.get("version") or 0,
                    last_actor=t.get("last_actor"),
                ))
                existing_ids.add(t["id"])
                newly_added.add(t["id"])
                total_tasks += 1

            for s in data["task_steps"]:
                if s["task_id"] not in newly_added:
                    continue
                session.add(PersonaTaskStep(
                    id=s["id"],
                    task_id=s["task_id"],
                    position=s.get("position") or 0,
                    title=s.get("title") or "",
                    description=s.get("description"),
                    status=s.get("status") or "pending",
                    notes=s.get("notes"),
                    created_at=_parse_dt(s.get("created_at")) or datetime.now(),
                    updated_at=_parse_dt(s.get("updated_at")) or datetime.now(),
                    completed_at=_parse_dt(s.get("completed_at")),
                    version=s.get("version") or 0,
                ))
                total_steps += 1

            for h in data["task_history"]:
                if h["task_id"] not in newly_added:
                    continue
                session.add(PersonaTaskHistory(
                    id=h["id"],
                    task_id=h["task_id"],
                    step_id=h.get("step_id"),
                    event_type=h.get("event_type") or "legacy",
                    payload=h.get("payload"),
                    actor=h.get("actor"),
                    created_at=_parse_dt(h.get("created_at")) or datetime.now(),
                ))
                total_history += 1

        if dry_run:
            session.rollback()
            logging.info(
                "[DRY-RUN] 移行予定: tasks=%d steps=%d history=%d (skipped=%d) — 書き込みませんでした。",
                total_tasks, total_steps, total_history, skipped,
            )
        else:
            session.commit()
            logging.info(
                "移行完了: tasks=%d steps=%d history=%d (skipped=%d)。",
                total_tasks, total_steps, total_history, skipped,
            )
    except Exception:
        session.rollback()
        logging.error("移行中にエラーが発生したためロールバックしました。", exc_info=True)
        raise
    finally:
        session.close()
        engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="書き込まずに移行件数だけ表示")
    parser.add_argument("--persona", default=None, help="特定ペルソナのみ移行 (persona_id)")
    args = parser.parse_args()
    migrate(dry_run=args.dry_run, only_persona=args.persona)


if __name__ == "__main__":
    main()
