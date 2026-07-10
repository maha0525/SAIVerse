"""Memory Atlas ファサード — ref (``m:N`` / ``core`` / ``c:N`` / ``ch:N``) を
既存4ストレージ (Memopedia / core_memory / Chronicle / 目的の木) へディスパッチ
する読み側 API。

concept_consolidation.md「土地と地図帳モデル」「P2: Atlas ファサード + 統一
スペル」(実装分割 P2a) の実装。ペルソナ向けスペル (``builtin_data/tools/
memory_read.py`` 等) から呼ばれる薄い変換層で、地図の実体には触れず、既存
ストレージ API への委譲とテキスト整形だけを行う (strangler-fig 戦略: 物理統合
は P3 で1枚ずつ)。

**対応する ref 形式**:

- ``m:N``  — Memopedia ページ (short_id)
- ``core`` — コア記憶全件 (常時開・机の予算外の特殊ページ)
- ``c:N``  — コア記憶 1 件
- ``ch:N`` — Chronicle エントリ (short_id)
- ``task:N`` — 目的の地図 (P2b で対応。現状は案内メッセージを返す stub)

この ``m:N`` / ``c:N`` / ``ch:N`` という軽量プレフィックス表記は、
``saiverse/references.py`` の RefKind システム (``track:2`` のようなフルワード
短縮参照や ``saiverse://`` URI grammar) とは別の系列。m:N / c:N は元々
RefKind に未登録のまま各ストレージモジュール内で扱われてきた慣行で
(``sai_memory/memopedia/storage.py`` の ``resolve_page_ref`` 等)、ch:N も
その慣行に揃える — Atlas の ref はこのモジュールが直接文字列パースする。

**開閉 (机の物理)**: ``open_page`` / ``close_page`` は ``sai_memory/desk.py``
に委譲する。コア記憶 (``core`` / ``c:N``) は常時開のシステム常設ピンなので
机の対象外 — open は「既に開いている」、close は「閉じられない」を返す。
``task:N`` (目的の地図) の開閉も P2b 対応まで stub。
"""
from __future__ import annotations

import logging
from typing import List, Optional, Tuple

from sai_memory import desk
from sai_memory.core_memory import get_core_memory, list_core_memories
from sai_memory.photos import Photo, list_photos_pasted_to

LOGGER = logging.getLogger(__name__)


class AtlasRefError(ValueError):
    """ref の形式が不正、または対応する地図が存在しない場合。"""


# ----- ref 解析 -----


def _parse_ref(ref: str) -> Tuple[str, Optional[str]]:
    """Atlas の ref 文字列を ``(kind, key)`` に分解する。

    kind: ``core_all`` / ``core_one`` / ``memopedia`` / ``chronicle`` / ``task``
    """
    text = (ref or "").strip()
    if not text:
        raise AtlasRefError("ref が空です")
    if text.lower() == "core":
        return ("core_all", None)
    if text.startswith("m:"):
        return ("memopedia", text[2:].strip())
    if text.startswith("ch:"):
        return ("chronicle", text[3:].strip())
    if text.startswith("c:"):
        return ("core_one", text[2:].strip())
    if text.startswith("task:"):
        return ("task", text[5:].strip())
    raise AtlasRefError(f"認識できない ref 形式です: {ref!r}")


def _parse_int(key: Optional[str]) -> Optional[int]:
    if key is None:
        return None
    text = key.strip()
    if not text.isdigit():
        return None
    value = int(text)
    return value if value > 0 else None


def _format_photos(photos: List[Photo]) -> str:
    if not photos:
        return ""
    lines = ["### 貼られた写真"]
    for p in photos:
        if p.is_range:
            label = p.quote or f"{p.message_id}〜{p.message_id_end}"
            lines.append(f"- [写真] 範囲: {label}")
        else:
            lines.append(f"- [写真] 引用: 「{p.quote}」")
    return "\n".join(lines)


def _ensure_chronicle_ready(conn) -> None:
    """Chronicle (arasuji_entries) テーブルの存在を保証する (冪等)。

    SAIMemoryAdapter は Memopedia/core_memory/photos と違い arasuji テーブルを
    __init__ で eager 初期化しない (Chronicle に触れる各呼び出し元が都度
    ``init_arasuji_tables`` を呼ぶ既存の遅延初期化パターン — 例:
    ``api/routes/people/recall.py`` / ``sea/session_lifecycle.py``)。
    Atlas の ch:N 経路もこの流儀に合わせ、触る直前に一度呼ぶ。
    """
    from sai_memory.arasuji.storage import init_arasuji_tables

    init_arasuji_tables(conn)


def _task_stub_message(key: Optional[str]) -> str:
    # TODO(P2b): 目的の地図 (task:N) の解決は目的の木 (persona_task) の Atlas
    # 統合と合わせて実装する (concept_consolidation.md P2 実装分割 P2b)。
    ref = f"task:{key}" if key else "task:N"
    return f"目的ノード ({ref}) の閲覧は今後 (P2b) 対応予定です。"


# ----- read_page -----


def read_page(adapter, ref: str) -> str:
    """ref の内容を読む。読んだ内容は会話の流れに残るだけで机の場所は取らない。

    対象が机に開かれている場合は touch する (touch の定義 = read/write/clip が
    触った扱い、sai_memory/desk.py)。読んでいる最中のページが LRU に追い出され
    るのを防ぐ。
    """
    kind, key = _parse_ref(ref)
    conn = adapter.conn
    if kind == "core_all":
        return _read_core_all(conn)
    if kind == "core_one":
        return _read_core_one(conn, key)
    if kind == "memopedia":
        text = _read_memopedia(adapter, key)
        _touch_if_open(adapter, kind, key)
        return text
    if kind == "chronicle":
        text = _read_chronicle(conn, key)
        _touch_if_open(adapter, kind, key)
        return text
    if kind == "task":
        return _task_stub_message(key)
    raise AtlasRefError(f"未対応の ref kind: {kind}")


def _touch_if_open(adapter, kind: str, key: Optional[str]) -> None:
    """机に開かれていれば touch する (開いていなければ何もしない)。

    touch は本文読み出しの副作用なので、失敗しても読み出し自体は成立させる。
    """
    try:
        norm_ref = _normalize_ref_for_desk(adapter, kind, key)
        if norm_ref is None:
            return
        with adapter._db_lock:
            desk.touch_item(adapter.conn, norm_ref)
    except Exception:
        LOGGER.warning(
            "memory_atlas: failed to touch desk item (kind=%s key=%s)",
            kind, key, exc_info=True,
        )


def _read_core_all(conn) -> str:
    items = list_core_memories(conn)
    if not items:
        return "コア記憶はまだありません。"
    lines = [f"# コア記憶 (常時開・全 {len(items)} 件)"]
    for cm in items:
        lines.append(f"\n## {cm.ref}")
        lines.append(cm.content)
    return "\n".join(lines)


def _read_core_one(conn, key: Optional[str]) -> str:
    mid = _parse_int(key)
    if mid is None:
        return f"コア記憶の参照が不正です: c:{key}"
    cm = get_core_memory(conn, mid)
    if cm is None:
        return f"コア記憶が見つかりません: c:{mid}"
    lines = [f"# {cm.ref}", "", cm.content]
    photos_text = _format_photos(list_photos_pasted_to(conn, cm.ref))
    if photos_text:
        lines.append("")
        lines.append(photos_text)
    return "\n".join(lines)


def _read_memopedia(adapter, key: Optional[str]) -> str:
    from sai_memory.memopedia import Memopedia
    from sai_memory.memopedia.storage import resolve_page_ref

    if not key:
        return "Memopedia の参照が不正です: m:"
    resolved = resolve_page_ref(adapter.conn, key)
    memopedia = Memopedia(adapter.conn, db_lock=adapter._db_lock)
    page = memopedia.get_page(resolved) if resolved else None
    if page is None:
        return f"ページが見つかりません: m:{key}"

    ref = f"m:{page.short_id}" if page.short_id else page.id
    body = memopedia.render_page_body(page.id)
    lines = [f"# {page.title} ({ref})"]
    if page.summary:
        lines.append(f"\n*{page.summary}*")
    lines.append(f"\n{body or '(内容なし)'}")
    photos_text = _format_photos(list_photos_pasted_to(adapter.conn, ref))
    if photos_text:
        lines.append("")
        lines.append(photos_text)
    return "\n".join(lines)


def _read_chronicle(conn, key: Optional[str]) -> str:
    from sai_memory.arasuji.storage import get_entry_by_short_id

    _ensure_chronicle_ready(conn)
    sid = _parse_int(key)
    if sid is None:
        return f"Chronicle の参照が不正です: ch:{key}"
    entry = get_entry_by_short_id(conn, sid)
    if entry is None:
        return f"Chronicle エントリが見つかりません: ch:{sid}"
    ref = f"ch:{entry.short_id}"
    lines = [f"# {ref} (レベル{entry.level})", "", entry.content]
    photos_text = _format_photos(list_photos_pasted_to(conn, ref))
    if photos_text:
        lines.append("")
        lines.append(photos_text)
    return "\n".join(lines)


# ----- open_page / close_page (机の物理、desk.py に委譲) -----


def _normalize_ref_for_desk(adapter, kind: str, key: Optional[str]) -> Optional[str]:
    """desk.py の主キーに使う正規形 (``m:{short_id}`` / ``ch:{short_id}``) に揃える。

    呼び出し側が UUID や別表記の key を渡しても、常に short_id ベースの一意な
    ref に正規化する (同じページを異なる表記で開いて二重登録するのを防ぐ)。
    見つからなければ None。
    """
    if kind == "memopedia":
        if not key:
            return None
        from sai_memory.memopedia import Memopedia
        from sai_memory.memopedia.storage import resolve_page_ref

        resolved = resolve_page_ref(adapter.conn, key)
        if resolved is None:
            return None
        memopedia = Memopedia(adapter.conn, db_lock=adapter._db_lock)
        page = memopedia.get_page(resolved)
        if page is None:
            return None
        return f"m:{page.short_id}" if page.short_id else f"m:{page.id}"
    if kind == "chronicle":
        from sai_memory.arasuji.storage import get_entry_by_short_id

        _ensure_chronicle_ready(adapter.conn)
        sid = _parse_int(key)
        if sid is None:
            return None
        entry = get_entry_by_short_id(adapter.conn, sid)
        if entry is None:
            return None
        return f"ch:{entry.short_id}"
    return None


def _size_of_ref(adapter, ref: str) -> int:
    """desk の評価用サイズ解決 (ref → 現在の本文文字数)。解決できなければ 0。"""
    try:
        if ref.startswith("m:"):
            from sai_memory.memopedia import Memopedia
            from sai_memory.memopedia.storage import resolve_page_ref

            resolved = resolve_page_ref(adapter.conn, ref[2:])
            if resolved is None:
                return 0
            memopedia = Memopedia(adapter.conn, db_lock=adapter._db_lock)
            page = memopedia.get_page(resolved)
            if page is None:
                return 0
            body = memopedia.render_page_body(page.id)
            return len(page.title or "") + len(page.summary or "") + len(body or "")
        if ref.startswith("ch:"):
            from sai_memory.arasuji.storage import get_entry_by_short_id

            _ensure_chronicle_ready(adapter.conn)
            sid = _parse_int(ref[3:])
            if sid is None:
                return 0
            entry = get_entry_by_short_id(adapter.conn, sid)
            return len(entry.content) if entry else 0
    except Exception:
        LOGGER.warning(
            "memory_atlas: failed to resolve desk size for ref=%s", ref, exc_info=True,
        )
    return 0


def open_page(adapter, ref: str, purpose_ref: Optional[str] = None) -> str:
    """ページを机に開いたままにする (Metabolism を跨いで head に残り続ける)。

    机の予算 (``desk.resolve_desk_budget_chars()``) を超えたら、最も長く触ら
    れていないページから自動で棚に戻す (LRU)。追い出しが起きた場合はその旨を
    結果テキストに含める。
    """
    kind, key = _parse_ref(ref)
    if kind in ("core_all", "core_one"):
        return "コア記憶は常時開いています。open は不要です。"
    if kind == "task":
        return _task_stub_message(key)
    if kind not in ("memopedia", "chronicle"):
        raise AtlasRefError(f"未対応の ref kind: {kind}")

    norm_ref = _normalize_ref_for_desk(adapter, kind, key)
    if norm_ref is None:
        return f"見つかりません: {ref}"

    conn = adapter.conn
    with adapter._db_lock:
        desk.open_item(conn, norm_ref, purpose_ref=purpose_ref)
        budget = desk.resolve_desk_budget_chars()
        # keep_ref: いま開いた本人は同一呼び出しでは追い出さない (「開きました」
        # と「棚に戻しました」の同居を防ぐ。desk.evict_lru docstring 参照)
        evicted = desk.evict_lru(
            conn, budget, lambda r: _size_of_ref(adapter, r), keep_ref=norm_ref,
        )

    lines = [f"{norm_ref} を机に開きました。"]
    if evicted:
        lines.append(f"机が溢れたため {'、'.join(evicted)} を棚に戻しました。")
    return "\n".join(lines)


def close_page(adapter, ref: str) -> str:
    """ページを机から閉じる (棚に戻す)。"""
    kind, key = _parse_ref(ref)
    if kind in ("core_all", "core_one"):
        return "コア記憶は閉じられません(常時開です)。"
    if kind == "task":
        return _task_stub_message(key)
    if kind not in ("memopedia", "chronicle"):
        raise AtlasRefError(f"未対応の ref kind: {kind}")

    norm_ref = _normalize_ref_for_desk(adapter, kind, key)
    if norm_ref is None:
        return f"見つかりません: {ref}"

    with adapter._db_lock:
        closed = desk.close_item(adapter.conn, norm_ref)
    if closed:
        return f"{norm_ref} を机から閉じました。"
    return f"{norm_ref} は机に開かれていません。"


# ----- search_pages -----


def search_pages(adapter, query: str, limit: int = 8) -> str:
    """地図帳をタイトル/全文検索する (Memopedia + Chronicle content の LIKE 検索)。"""
    text = (query or "").strip()
    if not text:
        return "検索語を指定してください。"

    conn = adapter.conn
    from sai_memory.memopedia import Memopedia
    from sai_memory.arasuji.storage import search_entries

    memopedia = Memopedia(conn, db_lock=adapter._db_lock)
    pages = memopedia.search(text, limit=limit)
    _ensure_chronicle_ready(conn)
    entries = search_entries(conn, text, limit=limit)

    lines: List[str] = []
    if pages:
        lines.append("## Memopedia")
        for p in pages:
            ref = f"m:{p.short_id}" if p.short_id else p.id
            preview = (p.summary or p.content or "").strip().splitlines()
            preview_text = preview[0] if preview else ""
            lines.append(f"- {p.title} ({ref}): {preview_text}")
    if entries:
        lines.append("## Chronicle" if not lines else "\n## Chronicle")
        for e in entries:
            ref = f"ch:{e.short_id}" if e.short_id else e.id
            preview = (e.content or "").strip().splitlines()
            preview_text = preview[0] if preview else ""
            lines.append(f"- {ref}: {preview_text}")

    if not lines:
        return f"「{text}」に一致するページは見つかりませんでした。"
    return "\n".join(lines)
