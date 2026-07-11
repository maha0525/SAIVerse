"""テーマノードページ — 旧 Note (person/project/vocation) の物理格納。

concept_consolidation.md「P3c①② 設計 v0.1」①の実装。旧 ``note`` テーブル
(main DB) の行を、ペルソナの memory.db 側 memopedia ページ (trunk
``root_theme``、category ``theme``) へ移す。

テーマページは通常の Memopedia ページと同じ扱いなので、参照は m:N (memopedia
の通常 short_id) — コア記憶の ``c:N`` / Chronicle の ``ch:N`` のような専用
参照子は持たない。目的ノード (persona_task) 自体の物理格納は main DB のまま
(concept_consolidation.md「P3c 設計提案 v0.1」X 案) — テーマページは「意味記憶
としての Note」の後継であって、目的の地図 (task:N) とは別の実体。

trunk ``root_theme`` の子ページは移行専用の入口。移行後の新規テーマページ
作成は memory_write の対象カテゴリに含めない (P3c①② 設計 v0.1 決定 (a):
「そもそも意味記憶と目的記憶で別枠」— 新規テーマの命名は将来の代謝 [P4] の
領分)。

呼び出し元: ``saiverse/note_theme_migration.py`` (main DB の Note 行を読み、
persona ごとにこのモジュールでページ化する)。

詳細設計: docs/intent/concept_consolidation.md「P3c①② 設計 v0.1」
"""
from __future__ import annotations

import sqlite3
from typing import Any, Dict, Optional

from sai_memory.memopedia.storage import CATEGORY_THEME

ROOT_THEME_ID = "root_theme"

__all__ = ["ROOT_THEME_ID", "CATEGORY_THEME", "ensure_root_theme",
           "find_theme_page_by_legacy_note_id", "migrate_note_to_theme_page",
           "create_theme_page"]


def ensure_root_theme(conn: sqlite3.Connection) -> None:
    """trunk root_theme ページを冪等に用意する (無ければ作る)。

    Memopedia テーブル自体の初期化 (``init_memopedia_tables``) は呼び出し側の
    責務 — 本関数は P3a の ``_ensure_root_core`` / P3b の ``_ensure_root_chronicle``
    と同じ流儀で、既に memopedia_pages が存在する前提で動く。
    """
    row = conn.execute(
        "SELECT id FROM memopedia_pages WHERE id = ?", (ROOT_THEME_ID,)
    ).fetchone()
    if row is not None:
        return
    from sai_memory.memopedia.storage import create_page

    create_page(
        conn,
        parent_id=None,
        title="テーマ",
        category=CATEGORY_THEME,
        is_trunk=True,
        page_id=ROOT_THEME_ID,
    )
    conn.commit()


def find_theme_page_by_legacy_note_id(
    conn: sqlite3.Connection, note_id: str,
) -> Optional[str]:
    """旧 note_id から既に移行済みのテーマページ id を引く (冪等性チェック用)。

    page id は旧 note_id (UUID) をそのまま継承しているため id 一致で直接引ける
    (metadata.legacy_note_id 参照はページ id 変更に備えた冗長化)。
    """
    row = conn.execute(
        "SELECT id FROM memopedia_pages WHERE parent_id = ? AND category = ? "
        "AND (id = ? OR json_extract(metadata, '$.legacy_note_id') = ?)",
        (ROOT_THEME_ID, CATEGORY_THEME, note_id, note_id),
    ).fetchone()
    return row[0] if row else None


def migrate_note_to_theme_page(
    conn: sqlite3.Connection,
    *,
    note_id: str,
    title: str,
    description: Optional[str],
    note_type: str,
    is_active: bool,
    created_at: Any = None,
    closed_at: Any = None,
) -> str:
    """Note 1 件をテーマノードページへ移す (冪等: 既に移行済みならその page_id を返す)。

    page id は旧 note_id (UUID) を継承する (P3a/P3b の流儀 — 万一将来リンクが
    出てきても無傷)。content=description / title=title。metadata JSON に
    ``{note_type, legacy_note_id, legacy_created_at, legacy_is_active,
    legacy_closed_at}`` を刻む。created_at は旧 Note の作成時刻に刻み直す。

    机へは自動で開かない (P3c①② 設計 v0.1 決定 (b): 開きっぱなしは継承せず
    本人の memory_open に委ねる)。
    """
    ensure_root_theme(conn)

    existing = find_theme_page_by_legacy_note_id(conn, note_id)
    if existing is not None:
        return existing

    from sai_memory.memopedia.storage import create_page

    meta: Dict[str, Any] = {
        "note_type": note_type,
        "legacy_note_id": note_id,
        "legacy_created_at": _iso(created_at),
        "legacy_is_active": bool(is_active),
        "legacy_closed_at": _iso(closed_at),
    }

    page = create_page(
        conn,
        parent_id=ROOT_THEME_ID,
        title=title or "(無題)",
        content=description or "",
        category=CATEGORY_THEME,
        is_trunk=False,
        metadata=meta,
        page_id=note_id,
    )

    ts = _epoch(created_at)
    if ts is not None:
        conn.execute(
            "UPDATE memopedia_pages SET created_at = ? WHERE id = ?",
            (ts, page.id),
        )
        conn.commit()
    return page.id


def create_theme_page(
    conn: sqlite3.Connection,
    *,
    title: str,
    member_refs: list,
    origin: str = "naming",
) -> str:
    """P4-b 命名によって新規テーマページを作成する（冪等）。

    root_theme 配下に category='theme' のページを作成し、metadata に
    ``{"origin": origin, "member_refs": [...]}`` を刻む。
    content は member_refs の一覧（「- task:N」形式）と定型文。
    edit_source="naming" で編集来歴に記録する。

    同名のテーマページが root_theme 配下に既にあれば作成せず既存 id を返す（冪等）。

    返り値: 作成（または既存）ページの id。
    """
    ensure_root_theme(conn)

    # 同名の既存テーマページがあれば返す（冪等）
    existing = conn.execute(
        "SELECT id FROM memopedia_pages "
        "WHERE parent_id = ? AND category = ? AND title = ? "
        "AND COALESCE(is_deleted, 0) = 0",
        (ROOT_THEME_ID, CATEGORY_THEME, title or "(無題)"),
    ).fetchone()
    if existing is not None:
        return existing[0]

    from sai_memory.memopedia.storage import create_page, record_page_edit, generate_diff

    # content: member_refs の一覧 + 定型
    ref_lines = "\n".join(f"- {ref}" for ref in (member_refs or []))
    content = (
        f"{ref_lines}\n\n（命名により束ねられたテーマ）"
        if ref_lines
        else "（命名により束ねられたテーマ）"
    )
    meta: Dict[str, Any] = {
        "origin": origin,
        "member_refs": list(member_refs or []),
    }

    page = create_page(
        conn,
        parent_id=ROOT_THEME_ID,
        title=title or "(無題)",
        content=content,
        category=CATEGORY_THEME,
        is_trunk=False,
        metadata=meta,
    )

    # 編集来歴に刻む（edit_source="naming"）
    try:
        diff = generate_diff("", content)
        record_page_edit(
            conn,
            page_id=page.id,
            diff_text=diff,
            edit_type="create",
            edit_source=origin,
            before_title="",
            before_summary="",
            before_content="",
        )
    except Exception:
        pass  # 来歴の記録失敗はページ作成の成否に影響しない

    return page.id


def _iso(value: Any) -> Optional[str]:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _epoch(value: Any) -> Optional[int]:
    if value is None:
        return None
    if hasattr(value, "timestamp"):
        return int(value.timestamp())
    # raw SQL (sqlite3) 経由の DATETIME は '2026-04-30 20:09:38.498838' の
    # 文字列で届く — int() では変換できないので ISO パースを先に試す。
    try:
        from datetime import datetime

        return int(datetime.fromisoformat(str(value)).timestamp())
    except (TypeError, ValueError):
        pass
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
