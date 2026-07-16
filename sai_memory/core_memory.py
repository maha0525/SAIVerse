"""コア記憶 (記憶アーキv2 ゾーン A) のストレージ層。

コア記憶＝ペルソナが**自分で選んで刻む恒常知識**。head (システムプロンプト部) に
常駐し、Metabolism 時のみ更新が反映される。編集主体はペルソナ自身 (memory_write /
memory_delete スペル。宛先 core / core:N でコア記憶を指す。旧専用スペル
core_memory_add / core_memory_update / core_memory_remove は P2c-4a で撤去)。
システムは容量目安を超過しても絶対に切り詰めない (通知のみ)。

ペルソナへの提示時は ``c:{id}`` 形式で参照する (Memopedia の ``m:N`` と同じ操作感)。

【P3a 物理統合 (2026-07-11)】このモジュールの公開 API (関数シグネチャ・戻り値の
形・意味) は変えず、格納先を ``memopedia_pages`` に統合した
(docs/intent/concept_consolidation.md「P3 物理統合」)。コア記憶 1 件は
trunk ``root_core`` (category ``core``・is_trunk・title「コア記憶」) 配下の
子ページで、``c:N`` の N は metadata JSON の ``core_id`` (Memopedia の ``m:N``
採番とは独立の連番、max+1)。soft-delete は memopedia の ``is_deleted`` と
metadata の ``deleted_at`` を両方刻む (memopedia:N 経由で触られた場合の整合のため)。
ページ作成・更新は memopedia storage の生成 diff / 編集来歴 (page_edit_history)
を経由し、``edit_source="core_memory"`` で識別できるようにする。

旧 ``core_memories`` テーブルは ``init_core_memory_table`` 内の一回きり冪等
migration でページへ写し切ってから DROP する (``sai_memory/clips.py`` の
marks→clips と同じ流儀。旧 path は残さない)。

テーブルはペルソナの memory.db に同居する (Memopedia / Chronicle と同じ conn)。
``init_core_memory_table`` は SAIMemoryAdapter の初期化時に冪等に呼ばれる。

詳細設計: docs/intent/memory_architecture_v2.md §5 / docs/intent/concept_consolidation.md
「P3a: コア記憶 → 常時開ページ」
"""
from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class CoreMemory:
    """コア記憶 1 件。

    ``kind`` は項目種別。今回実装するのは 'note' (書き下ろしテキスト) のみだが、
    直後の増分で 'scene' (実会話の切り抜き＝口調・性格のアンカー) が入る予定。
    ``metadata`` は scene の由来参照 (元 message_id 群・日付等) などが将来入る余白。
    """
    id: int
    content: str
    created_at: int
    updated_at: int
    kind: str = "note"
    metadata: Optional[str] = None
    confirmed: int = 1
    deleted_at: Optional[int] = None

    @property
    def ref(self) -> str:
        """ペルソナ提示用の参照 (例: ``core:3``)。

        書式は統一グラマー (``saiverse/references.py``) が単一真実源。
        """
        from saiverse import references

        return references.to_short_ref("core", self.id)


# ---------------------------------------------------------------------------
# 物理格納: memopedia_pages (trunk root_core 配下の子ページ)
# ---------------------------------------------------------------------------

ROOT_CORE_ID = "root_core"
CATEGORY_CORE = "core"

# コア記憶固有のメタ情報キー。add_core_memory の ``metadata`` 引数 (呼び出し側が
# 渡す任意 JSON、例: gold_panning の {"source": "gold_panning"} や scene の
# {"anchor_id":..., "message_ids":..., "date_range":...}) はこれらのキーと
# フラットにマージしてページの metadata 列へ入れる。CoreMemory.metadata へ
# 復元する際はこの reserved keys を除いた残りを再直列化する。
_RESERVED_META_KEYS = {"core_id", "kind", "confirmed", "deleted_at"}

_CORE_PAGE_COLS = "id, title, summary, content, created_at, updated_at, metadata, is_deleted"


def _build_page_metadata(
    core_id: int,
    kind: str,
    confirmed: int,
    deleted_at: Optional[int],
    extra_metadata_json: Optional[str],
) -> Dict[str, Any]:
    """呼び出し側の任意 metadata (JSON文字列) とコア記憶固有情報をマージする。"""
    meta: Dict[str, Any] = {}
    if extra_metadata_json:
        try:
            parsed = json.loads(extra_metadata_json)
            if isinstance(parsed, dict):
                meta.update(parsed)
        except (json.JSONDecodeError, TypeError):
            pass
    meta["core_id"] = core_id
    meta["kind"] = kind
    meta["confirmed"] = confirmed
    meta["deleted_at"] = deleted_at
    return meta


def _extract_origin_metadata(meta: Dict[str, Any]) -> Optional[str]:
    """reserved keys を除いた呼び出し側由来の metadata を JSON 文字列に復元する。"""
    extra = {k: v for k, v in meta.items() if k not in _RESERVED_META_KEYS}
    if not extra:
        return None
    return json.dumps(extra, ensure_ascii=False)


def _ensure_root_core(conn: sqlite3.Connection) -> None:
    """trunk root_core ページを冪等に用意する (無ければ作る)。"""
    row = conn.execute(
        "SELECT id FROM memopedia_pages WHERE id = ?", (ROOT_CORE_ID,)
    ).fetchone()
    if row is not None:
        return
    from sai_memory.memopedia.storage import create_page

    create_page(
        conn,
        parent_id=None,
        title="コア記憶",
        category=CATEGORY_CORE,
        is_trunk=True,
        page_id=ROOT_CORE_ID,
    )
    conn.execute(
        "UPDATE memopedia_pages SET is_important = 1 WHERE id = ?", (ROOT_CORE_ID,)
    )
    conn.commit()


def _migrate_legacy_core_memories_table(conn: sqlite3.Connection) -> None:
    """旧 ``core_memories`` テーブルが存在すれば、全行をページへ写して DROP する。

    一回きり・冪等 (旧テーブルが無ければ即 return)。id → metadata.core_id を保存し、
    kind/metadata/confirmed/deleted_at/created_at/updated_at を忠実に写す。
    """
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='core_memories'"
    ).fetchone()
    if exists is None:
        return

    from sai_memory.memopedia.storage import create_page

    rows = conn.execute(
        "SELECT id, content, created_at, updated_at, kind, metadata, confirmed, deleted_at "
        "FROM core_memories ORDER BY id ASC"
    ).fetchall()
    for old_id, content, created_at, updated_at, kind, metadata, confirmed, deleted_at in rows:
        meta = _build_page_metadata(
            int(old_id),
            kind or "note",
            int(confirmed) if confirmed is not None else 1,
            int(deleted_at) if deleted_at is not None else None,
            metadata,
        )
        page = create_page(
            conn,
            parent_id=ROOT_CORE_ID,
            title=f"コア記憶 core:{old_id}",
            content=content or "",
            category=CATEGORY_CORE,
            is_trunk=False,
            metadata=meta,
        )
        conn.execute(
            "UPDATE memopedia_pages SET created_at = ?, updated_at = ?, is_important = 1, "
            "is_deleted = ? WHERE id = ?",
            (
                int(created_at), int(updated_at),
                1 if deleted_at is not None else 0,
                page.id,
            ),
        )
    conn.commit()
    conn.execute("DROP TABLE core_memories")
    conn.commit()


def init_core_memory_table(conn: sqlite3.Connection) -> None:
    """コア記憶の物理格納 (memopedia_pages) を初期化する (冪等)。

    1. Memopedia テーブルを保証 (memopedia_pages 未初期化の生 conn からの
       直接呼び出しにも耐える — test_core_memory_storage.py のように
       init_memopedia_tables を経由せず本関数だけを呼ぶ既存呼び出し元がある)。
    2. trunk root_core ページを冪等に用意する。
    3. 旧 core_memories テーブルが存在すれば一回きり移行して DROP する。
    """
    from sai_memory.memopedia.storage import init_memopedia_tables

    init_memopedia_tables(conn)
    _ensure_root_core(conn)
    _migrate_legacy_core_memories_table(conn)


def _next_core_id(conn: sqlite3.Connection) -> int:
    """新規コア記憶の次の core_id (MAX + 1。soft-delete 済みも含めて never-reuse)。"""
    row = conn.execute(
        "SELECT COALESCE(MAX(CAST(json_extract(metadata, '$.core_id') AS INTEGER)), 0) "
        "FROM memopedia_pages WHERE parent_id = ? AND category = ?",
        (ROOT_CORE_ID, CATEGORY_CORE),
    ).fetchone()
    return int(row[0]) + 1 if row and row[0] is not None else 1


def _fetch_core_row_by_core_id(
    conn: sqlite3.Connection, core_id: int, *, include_deleted: bool,
) -> Optional[tuple]:
    row = conn.execute(
        f"SELECT {_CORE_PAGE_COLS} FROM memopedia_pages "
        "WHERE parent_id = ? AND category = ? "
        "AND CAST(json_extract(metadata, '$.core_id') AS INTEGER) = ?",
        (ROOT_CORE_ID, CATEGORY_CORE, core_id),
    ).fetchone()
    if row is None:
        return None
    if not include_deleted and bool(row[7]):
        return None
    return row


def _parse_core_row(row: tuple):
    page_id, title, summary, content, created_at, updated_at, metadata_json, is_deleted = row
    try:
        meta = json.loads(metadata_json) if metadata_json else {}
    except (json.JSONDecodeError, TypeError):
        meta = {}
    return page_id, title, summary, content, created_at, updated_at, meta


def _core_memory_from_row(row: tuple) -> CoreMemory:
    _page_id, _title, _summary, content, created_at, updated_at, meta = _parse_core_row(row)
    confirmed = meta.get("confirmed")
    deleted_at = meta.get("deleted_at")
    return CoreMemory(
        id=int(meta.get("core_id")),
        content=content or "",
        created_at=int(created_at),
        updated_at=int(updated_at),
        kind=meta.get("kind") or "note",
        metadata=_extract_origin_metadata(meta),
        confirmed=int(confirmed) if confirmed is not None else 1,
        deleted_at=int(deleted_at) if deleted_at is not None else None,
    )


def add_core_memory(
    conn: sqlite3.Connection,
    content: str,
    *,
    kind: str = "note",
    metadata: Optional[str] = None,
    confirmed: int = 1,
) -> int:
    """コア記憶を1件追加し、採番された id (core_id) を返す。

    ``kind`` / ``metadata`` は 'scene' (実会話の切り抜き) / 由来参照用。
    ``confirmed`` は 0 で「未確認 (自動採取)」= ユーザーの確認待ち。ペルソナ自身や
    ユーザーの手動追加は 1 (確認済み)。gold_panning の自動採取だけ 0 で書く。
    既存呼び出し (省略) は後方互換で confirmed=1 のまま動く。
    """
    from sai_memory.memopedia.storage import create_page, generate_diff, record_page_edit

    _ensure_root_core(conn)
    core_id = _next_core_id(conn)
    meta = _build_page_metadata(core_id, kind, confirmed, None, metadata)
    page = create_page(
        conn,
        parent_id=ROOT_CORE_ID,
        title=f"コア記憶 c:{core_id}",
        content=content,
        category=CATEGORY_CORE,
        is_trunk=False,
        metadata=meta,
    )
    conn.execute("UPDATE memopedia_pages SET is_important = 1 WHERE id = ?", (page.id,))
    conn.commit()

    diff_text = generate_diff("", content)
    record_page_edit(
        conn,
        page_id=page.id,
        diff_text=diff_text,
        edit_type="create",
        edit_source="core_memory",
        before_title="",
        before_summary="",
        before_content="",
    )
    return core_id


def update_core_memory(
    conn: sqlite3.Connection, memory_id: int, content: str, *, confirmed: Optional[int] = None,
) -> bool:
    """既存のコア記憶を書き換える。対象が存在すれば True。

    ``confirmed`` を渡すと確認フラグも更新する (gold_panning の自動 update は
    confirmed=0 で「未確認」に戻し、ユーザーの再確認を促す)。省略時は現状維持。
    削除済み (ごみ箱) 対象への更新も許容する (旧 core_memories 実装と同じ挙動)。
    """
    from sai_memory.memopedia.storage import generate_diff, record_page_edit

    row = _fetch_core_row_by_core_id(conn, memory_id, include_deleted=True)
    if row is None:
        return False
    page_id, title, summary, old_content, _created_at, _updated_at, meta = _parse_core_row(row)

    if confirmed is not None:
        meta["confirmed"] = int(confirmed)
    now = int(time.time())
    conn.execute(
        "UPDATE memopedia_pages SET content = ?, metadata = ?, updated_at = ? WHERE id = ?",
        (content, json.dumps(meta, ensure_ascii=False), now, page_id),
    )
    conn.commit()

    diff_text = generate_diff(old_content or "", content)
    if diff_text:
        record_page_edit(
            conn,
            page_id=page_id,
            diff_text=diff_text,
            edit_type="update",
            edit_source="core_memory",
            before_title=title,
            before_summary=summary,
            before_content=old_content,
        )
    return True


def remove_core_memory(conn: sqlite3.Connection, memory_id: int) -> bool:
    """コア記憶を soft-delete する (ごみ箱へ移す)。生存中の対象があれば True。

    物理削除しないのは、gold_panning の自動 remove やペルソナの誤削除を
    ユーザーが後から復元できるようにするため (restore_core_memory)。
    memopedia の ``is_deleted`` と metadata の ``deleted_at`` を両方刻む
    (memopedia:N 経由でこのページに触れた場合の整合を保つ)。
    """
    row = _fetch_core_row_by_core_id(conn, memory_id, include_deleted=False)
    if row is None:
        return False
    page_id, _title, _summary, _content, _created_at, _updated_at, meta = _parse_core_row(row)

    now = int(time.time())
    meta["deleted_at"] = now
    conn.execute(
        "UPDATE memopedia_pages SET is_deleted = 1, metadata = ? WHERE id = ?",
        (json.dumps(meta, ensure_ascii=False), page_id),
    )
    conn.commit()
    return True


def get_core_memory(
    conn: sqlite3.Connection, memory_id: int, *, include_deleted: bool = False,
) -> Optional[CoreMemory]:
    """id 指定で 1 件取得する。無ければ None。

    既定では生存中 (deleted_at IS NULL) のみ。``include_deleted=True`` でごみ箱も対象。
    訂正導線の通知 (仮想センサー) が「何が変わったか」を得るための読み口。
    """
    row = _fetch_core_row_by_core_id(conn, memory_id, include_deleted=include_deleted)
    return _core_memory_from_row(row) if row else None


def list_core_memories(conn: sqlite3.Connection) -> List[CoreMemory]:
    """生存中 (未削除) の全コア記憶を id 昇順で返す。"""
    rows = conn.execute(
        f"SELECT {_CORE_PAGE_COLS} FROM memopedia_pages "
        "WHERE parent_id = ? AND category = ? AND (is_deleted = 0 OR is_deleted IS NULL) "
        "ORDER BY CAST(json_extract(metadata, '$.core_id') AS INTEGER) ASC",
        (ROOT_CORE_ID, CATEGORY_CORE),
    ).fetchall()
    return [_core_memory_from_row(row) for row in rows]


def list_deleted_core_memories(conn: sqlite3.Connection) -> List[CoreMemory]:
    """ごみ箱 (soft-delete 済み) のコア記憶を削除の新しい順に返す。"""
    rows = conn.execute(
        f"SELECT {_CORE_PAGE_COLS} FROM memopedia_pages "
        "WHERE parent_id = ? AND category = ? AND is_deleted = 1 "
        "ORDER BY CAST(json_extract(metadata, '$.deleted_at') AS INTEGER) DESC",
        (ROOT_CORE_ID, CATEGORY_CORE),
    ).fetchall()
    return [_core_memory_from_row(row) for row in rows]


def confirm_core_memory(conn: sqlite3.Connection, memory_id: int) -> bool:
    """未確認 (自動採取) のコア記憶をユーザーが確認済みにする。生存中の対象があれば True。"""
    row = _fetch_core_row_by_core_id(conn, memory_id, include_deleted=False)
    if row is None:
        return False
    page_id, _title, _summary, _content, _created_at, _updated_at, meta = _parse_core_row(row)
    meta["confirmed"] = 1
    conn.execute(
        "UPDATE memopedia_pages SET metadata = ? WHERE id = ?",
        (json.dumps(meta, ensure_ascii=False), page_id),
    )
    conn.commit()
    return True


def restore_core_memory(conn: sqlite3.Connection, memory_id: int) -> bool:
    """ごみ箱から復元する (deleted_at をクリア)。対象が削除済みなら True。"""
    row = _fetch_core_row_by_core_id(conn, memory_id, include_deleted=True)
    if row is None:
        return False
    page_id, _title, _summary, _content, _created_at, _updated_at, meta = _parse_core_row(row)
    if meta.get("deleted_at") is None:
        return False
    meta["deleted_at"] = None
    conn.execute(
        "UPDATE memopedia_pages SET is_deleted = 0, metadata = ? WHERE id = ?",
        (json.dumps(meta, ensure_ascii=False), page_id),
    )
    conn.commit()
    return True


def count_unconfirmed_core_memories(conn: sqlite3.Connection) -> int:
    """未確認 (confirmed=0) かつ生存中の件数。チャットの「N件更新」バッジ用。"""
    row = conn.execute(
        "SELECT COUNT(*) FROM memopedia_pages "
        "WHERE parent_id = ? AND category = ? AND (is_deleted = 0 OR is_deleted IS NULL) "
        "AND CAST(json_extract(metadata, '$.confirmed') AS INTEGER) = 0",
        (ROOT_CORE_ID, CATEGORY_CORE),
    ).fetchone()
    return int(row[0]) if row else 0


def total_core_memory_chars(conn: sqlite3.Connection) -> int:
    """全コア記憶の本文文字数の合計を返す (容量目安判定に使う)。"""
    row = conn.execute(
        "SELECT COALESCE(SUM(LENGTH(content)), 0) FROM memopedia_pages "
        "WHERE parent_id = ? AND category = ? AND (is_deleted = 0 OR is_deleted IS NULL)",
        (ROOT_CORE_ID, CATEGORY_CORE),
    ).fetchone()
    return int(row[0]) if row and row[0] is not None else 0


# ---------------------------------------------------------------------------
# Scene (実会話の切り抜き) — スペルと UI API の共通ロジック
# ---------------------------------------------------------------------------
#
# scene の窓切り出し・トランスクリプト整形・保存はスペル
# (builtin_data/tools/memory_clip.py mode='transcribe' paste_to='core'、旧
# core_memory_add_scene) と REST API
# (api/routes/people/core_memory.py) の両方から呼ばれる。ロジックの複製を避け、
# 手本の劣化 (言い換えドリフト) を防ぐ「参照によるコピー」の一点管理をここに集約する。
# 詳細: docs/intent/memory_architecture_v2.md §5


DEFAULT_SCENE_ROUNDS = 3

# 会話メッセージで persona 応答とみなす role。'model' (Gemini 系呼称) と
# 'assistant' (OpenAI 系呼称・インポートログ) の両方が実データに存在する
# (実 DB 実査で確認、2026-07-04)。それ以外 (user 等) は user ラベルにする。
_PERSONA_ROLES = ("model", "assistant")


@dataclass(frozen=True)
class SceneResult:
    """scene 作成の結果 (スペルの文言生成・API レスポンス双方が参照する)。"""
    memory_id: int
    transcript: str
    message_count: int
    char_count: int          # この切り抜き単体の文字数
    total_chars: int         # 追加後のコア記憶合計文字数
    date_start: str          # YYYY-MM-DD
    date_end: str            # YYYY-MM-DD
    message_ids: List[str]
    anchor_id: str


def format_scene_transcript(messages, persona_name: str) -> str:
    """会話メッセージ列を ``[時刻] [ラベル]: 原文`` 形式に整形する。content は無改変。

    記法は複数メッセージ表示の既存慣行 (chronicle_context_down 等) に揃える
    (2026-07-07 まはー指定。旧 ``ラベル「原文」`` 形式はカギカッコが content 内の
    カギカッコと衝突してメッセージ境界が読み取れなかった)。

    persona 応答の role は 'model' / 'assistant' 両方が実データに存在するため
    ``_PERSONA_ROLES`` で判定する。それ以外は user ラベル。
    """
    lines = []
    for msg in messages:
        label = persona_name if msg.role in _PERSONA_ROLES else "user"
        ts = getattr(msg, "created_at", None)
        ts_label = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else "?"
        lines.append(f"[{ts_label}] [{label}]: {msg.content}")
    return "\n".join(lines)


def create_scene_core_memory(
    conn: sqlite3.Connection,
    anchor_id: str,
    *,
    rounds: int = DEFAULT_SCENE_ROUNDS,
    persona_name: str,
) -> Optional[SceneResult]:
    """アンカー中心の会話窓を切り抜き、scene としてコア記憶に刻む (決定論・LLM 不使用)。

    - ``conn``: 呼び出し側で db lock を取得済みの memory.db 接続。
    - ``anchor_id``: 生の message_id (URI 剥がしは呼び出し側の責務)。
    - ``rounds``: アンカー前後に含めるおおよその往復数。
    - ``persona_name``: トランスクリプトの persona 応答ラベルに使う表示名。

    窓が取れない (アンカー不在・アンカー自体が除外対象・周辺に会話なし) 場合は
    ``None`` を返す。スペル/API 側でそれぞれのエラー文言に変換する。
    """
    from sai_memory.memory.storage import get_conversation_window_around

    window = get_conversation_window_around(conn, anchor_id, rounds=rounds)
    if not window:
        return None

    transcript = format_scene_transcript(window, persona_name)
    date_start = datetime.fromtimestamp(window[0].created_at).strftime("%Y-%m-%d")
    date_end = datetime.fromtimestamp(window[-1].created_at).strftime("%Y-%m-%d")
    message_ids = [m.id for m in window]

    metadata = json.dumps(
        {
            "anchor_id": anchor_id,
            "message_ids": message_ids,
            "date_range": [date_start, date_end],
        },
        ensure_ascii=False,
    )

    new_id = add_core_memory(conn, transcript, kind="scene", metadata=metadata)

    # 由来参照を範囲クリップとして切り出し、このコア記憶へ貼る (土地参照の統一
    # プリミティブ、concept_consolidation.md「クリップ」)。正リンクはクリップ側の
    # pasted_to = "c:{id}" 一本。クリップが撮れなくても scene 本体は生かす
    # (由来参照は本体より優先度が低い — adapter.add_clips と同じ姿勢)。
    try:
        from sai_memory.clips import add_clip
        add_clip(
            conn,
            message_id=message_ids[0],
            message_id_end=message_ids[-1],
            pasted_to=f"core:{new_id}",
        )
    except Exception:
        pass

    total = total_core_memory_chars(conn)

    return SceneResult(
        memory_id=new_id,
        transcript=transcript,
        message_count=len(window),
        char_count=len(transcript),
        total_chars=total,
        date_start=date_start,
        date_end=date_end,
        message_ids=message_ids,
        anchor_id=anchor_id,
    )
