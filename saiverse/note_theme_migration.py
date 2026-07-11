"""Note (main DB) → テーマノードページ (per-persona memory.db) 移行。

concept_consolidation.md「P3c①② 設計 v0.1」①の main DB 側オーケストレーション。
旧 Note (person/project/vocation の3種。desire は P3c-0 で既に削除済み) を、
ペルソナの memory.db 側 memopedia ページ (trunk ``root_theme``、実体は
``sai_memory/theme_pages.py``) へ一回きり冪等に移す。

呼び出し元: ``SAIVerseManager._on_persona_registered``
(起動時ロード・動的作成・Blueprint spawn の全経路で通る統一フック)。

**raw SQL で読み書きする理由**: database/models.py から Note / NotePage /
NoteMessage / TrackOpenNote の ORM クラスは既に削除済み (P3c①、Note はテーマ
ノードページへ物理統合済み)。旧テーブル自体は
``database/migrate.py:_drop_empty_legacy_note_tables`` が空になるまで残る
ため、本モジュールは ORM を経由せず ``text()`` の raw SQL でこれらのテーブルに
触れる (``database/migrate.py`` の他の一回きりデータ移行と同じ流儀)。
テーブル自体が存在しない (新規 DB や、既に DROP 済みの DB) 場合は何もしない。

main DB に全ペルソナの Note が単一テーブルで同居するため、P3a/P3b の
「adapter init で一回きり冪等 migration → 旧テーブル即 DROP」パターンは
そのままは使えない (docs/handoff/2026-07-11_p3c_purpose_note_audit.md §0-6)。
本モジュールは persona 単位の扇形移行 — 呼ばれるたびに「そのペルソナの行だけ」
memory.db 側へ移し、main DB からは移行できた行だけ DELETE する。旧テーブル
自体の DROP は database/migrate.py の ``_drop_empty_legacy_note_tables``
(note テーブルが空になった時点で発火) が別途担う。

机へは自動で開かない (P3c①② 設計 v0.1 決定 (b))。track_open_note 行は
ただ消える (開き直しは本人の memory_open に委ねる)。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List

from sqlalchemy import text

LOGGER = logging.getLogger(__name__)


def _note_table_exists(db) -> bool:
    row = db.execute(
        text("SELECT 1 FROM sqlite_master WHERE type='table' AND name='note'")
    ).fetchone()
    return row is not None


def migrate_persona_notes_to_theme_pages(manager: Any, persona_id: str) -> int:
    """persona_id の Note 行をテーマノードページへ移行する (冪等)。

    移行に使う memory.db 接続は ``manager.personas[persona_id].sai_memory``
    (既に登録済みのペルソナが持つ SAIMemoryAdapter)。アダプタが無い/未準備
    なら何もしない (WARN ログのみ、起動は継続する — 個々のステップは独立に
    失敗できる、という _on_persona_registered 全体の設計方針に合わせる)。

    Returns:
        移行できた件数 (main DB 側 DELETE まで完了した数)。
    """
    session_factory = getattr(manager, "SessionLocal", None)
    if session_factory is None:
        return 0

    db = session_factory()
    try:
        if not _note_table_exists(db):
            # 新規 DB (最初から Note 系テーブルが無い) か、既に DROP 済み。
            return 0
        # desire ノートは P3c-0 (backfill_desire_stage_normalization、起動時に
        # 本モジュールより先に走る) が削除する。ここで明示除外するのは起動順序
        # への暗黙依存を防ぐ防御 — 万一残っていてもテーマページ化はしない
        # (中身の候補 Task は P3c-0 が正規化済みで、器に移す価値が無い)。
        rows = db.execute(
            text(
                "SELECT note_id, title, note_type, description, is_active, "
                "created_at, closed_at FROM note "
                "WHERE persona_id = :pid AND note_type != 'desire'"
            ),
            {"pid": persona_id},
        ).fetchall()
        if not rows:
            return 0
        note_rows: List[Dict[str, Any]] = [
            {
                "note_id": r[0],
                "title": r[1],
                "note_type": r[2],
                "description": r[3],
                "is_active": bool(r[4]),
                "created_at": r[5],
                "closed_at": r[6],
            }
            for r in rows
        ]
    except Exception:
        LOGGER.warning(
            "[note_theme_migration] failed to read note rows persona=%s",
            persona_id, exc_info=True,
        )
        return 0
    finally:
        db.close()

    persona = getattr(manager, "personas", {}).get(persona_id)
    adapter = getattr(persona, "sai_memory", None) if persona is not None else None
    if adapter is None or getattr(adapter, "conn", None) is None:
        LOGGER.warning(
            "[note_theme_migration] no memory adapter for persona=%s; "
            "skip (%d note(s) pending)",
            persona_id, len(note_rows),
        )
        return 0

    from sai_memory.theme_pages import migrate_note_to_theme_page

    migrated_ids: List[str] = []
    for row in note_rows:
        try:
            with adapter._db_lock:
                migrate_note_to_theme_page(adapter.conn, **row)
            migrated_ids.append(row["note_id"])
        except Exception:
            LOGGER.warning(
                "[note_theme_migration] failed to migrate note=%s persona=%s",
                row["note_id"], persona_id, exc_info=True,
            )

    if not migrated_ids:
        return 0

    db = session_factory()
    try:
        placeholders = ", ".join(f":id{i}" for i in range(len(migrated_ids)))
        params = {f"id{i}": nid for i, nid in enumerate(migrated_ids)}
        for table_name in ("track_open_note", "note_message", "note_page"):
            db.execute(
                text(f"DELETE FROM {table_name} WHERE note_id IN ({placeholders})"),
                params,
            )
        db.execute(
            text(f"DELETE FROM note WHERE note_id IN ({placeholders})"), params,
        )
        db.commit()
    except Exception:
        db.rollback()
        LOGGER.warning(
            "[note_theme_migration] failed to delete migrated note rows persona=%s",
            persona_id, exc_info=True,
        )
        return 0
    finally:
        db.close()

    LOGGER.info(
        "[note_theme_migration] migrated %d note(s) to theme pages for persona=%s",
        len(migrated_ids), persona_id,
    )
    return len(migrated_ids)
