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
- ``p:N``  — 写真 (short_id)。「写真を読む＝その写真が写す土地を見に行く」—
  範囲写真は区間の生ログ全文、点写真は対象メッセージ本文＋引用箇所が tail に
  流れる (机にも head にも触らない。concept_consolidation.md「clip と写真の見え方」)
- ``task:N`` — 目的の地図 (目的ノード)。read は解決済み (P2c-1)、開閉も
  P3c①②で対応済み — 実体は main DB (persona_task) なので ``manager``
  (world 文脈) を追加で受ける。書く (memory_write) / 撮って貼る
  (memory_clip) は引き続き未対応 (purpose 動詞の領分)

**書く (memory_write) / 撮って貼る (memory_clip)**: 書き先は Memopedia 本文
への追記 (編集来歴が残る) / コア記憶の新規・上書き。clip は**参照貼り**のみ
(photos に pasted_to 付きで保存) — 本文への転写は既存 SCENE
(core_memory_add_scene) の役割のままで、本モジュールは触らない。

**貼られた写真の描画は常に抜粋** (写真は参照であって内容の運搬手段ではない):
点=引用全文 (元々短い) / 範囲=先頭数行＋「全Nメッセージ・M字、前後省略 —
memory_read p:N で全文」。折り畳み状態のような新しい状態は作らない。

この ``m:N`` / ``c:N`` / ``ch:N`` という軽量プレフィックス表記は、
``saiverse/references.py`` の RefKind システム (``track:2`` のようなフルワード
短縮参照や ``saiverse://`` URI grammar) とは別の系列。m:N / c:N は元々
RefKind に未登録のまま各ストレージモジュール内で扱われてきた慣行で
(``sai_memory/memopedia/storage.py`` の ``resolve_page_ref`` 等)、ch:N も
その慣行に揃える — Atlas の ref はこのモジュールが直接文字列パースする。

**開閉 (机の物理)**: ``open_page`` / ``close_page`` は ``sai_memory/desk.py``
に委譲する。コア記憶 (``core`` / ``c:N``) は常時開のシステム常設ピンなので
机の対象外 — open は「既に開いている」、close は「閉じられない」を返す。
``task:N`` (目的ノード) は main DB 在住のため ``manager`` を追加で受ける
(P3c①②)。完了/中止 (TERMINAL_TASK_STATUSES) の目的ノードは soft-delete
された Memopedia ページと同じ「無い」扱いになり、机から自動で下ろされる。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import List, Optional, Tuple

from sai_memory import desk
from sai_memory.core_memory import get_core_memory, list_core_memories
from sai_memory.photos import Photo, get_photo_by_short_id, list_photos_pasted_to

LOGGER = logging.getLogger(__name__)

# 範囲写真の抜粋描画で見せる先頭行数 (トランスクリプトの行 = 発言)
RANGE_PHOTO_EXCERPT_LINES = 3


class AtlasRefError(ValueError):
    """ref の形式が不正、または対応する地図が存在しない場合。"""


# ----- ref 解析 -----


def _parse_ref(ref: str) -> Tuple[str, Optional[str]]:
    """Atlas の ref 文字列を ``(kind, key)`` に分解する。

    kind: ``core_all`` / ``core_one`` / ``memopedia`` / ``chronicle`` /
    ``photo`` / ``task``
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
    if text.startswith("p:"):
        return ("photo", text[2:].strip())
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


def _range_photo_transcript(
    conn, photo: Photo, persona_name: str
) -> Optional[Tuple[str, int]]:
    """範囲写真が写す区間の実会話を (トランスクリプト, メッセージ数) にする。

    整形は SCENE と同じ (``format_scene_transcript``)。端メッセージが失われて
    いる等で区間が取れなければ None。
    """
    from sai_memory.core_memory import format_scene_transcript
    from sai_memory.memory.storage import get_conversation_messages_between

    messages = get_conversation_messages_between(
        conn, photo.message_id, photo.message_id_end
    )
    if not messages:
        return None
    return format_scene_transcript(messages, persona_name), len(messages)


def _format_photos(conn, photos: List[Photo], persona_name: str) -> str:
    """ページに貼られた写真の一覧を**抜粋**で描画する。

    写真は参照であって内容の運搬手段ではない — 丸ごと載せるとページが写真に
    食われるため、描画は常に抜粋 (concept_consolidation.md「clip と写真の
    見え方」):

    - 点写真: 引用全文 (元々短い)
    - 範囲写真: トランスクリプト先頭数行 ＋「全Nメッセージ・M字、前後省略 —
      memory_read p:N で全文」

    全文が要るときはペルソナが ``memory_read p:N`` で写真そのものを読む。
    """
    if not photos:
        return ""
    lines = ["### 貼られた写真"]
    for p in photos:
        if not p.is_range:
            lines.append(f"- [写真 {p.ref}] 引用: 「{p.quote}」")
            continue
        label = f" ラベル: {p.quote}" if p.quote else ""
        result = _range_photo_transcript(conn, p, persona_name)
        if result is None:
            lines.append(
                f"- [写真 {p.ref}] 範囲:{label} "
                f"(区間 {p.message_id}〜{p.message_id_end} は現在読み出せません)"
            )
            continue
        transcript, message_count = result
        t_lines = transcript.splitlines()
        excerpt = t_lines[:RANGE_PHOTO_EXCERPT_LINES]
        lines.append(f"- [写真 {p.ref}] 範囲:{label}")
        for ln in excerpt:
            lines.append(f"  {ln}")
        if len(t_lines) > RANGE_PHOTO_EXCERPT_LINES:
            lines.append(
                f"  (全{message_count}メッセージ・{len(transcript):,}字、前後省略 — "
                f"memory_read {p.ref} で全文)"
            )
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


def _task_stub_message(key: Optional[str], action: str = "の閲覧") -> str:
    # read (P2c-1) と開閉 (P3c①②) は解決済み (_read_task / _normalize_ref_for_desk)。
    # write / clip の貼り先としての task:N は purpose 動詞側 (収穫) に委ねる
    # ため未対応のまま — write_page の task 分岐からのみ呼ばれる。
    ref = f"task:{key}" if key else "task:N"
    return f"目的ノード ({ref}) {action}は今後対応予定です。"


def _resolve_persona_name(adapter, persona_name: Optional[str]) -> str:
    """トランスクリプトのペルソナ応答ラベルを解決する。未指定は persona_id。"""
    return persona_name or adapter.persona_id


# ----- read_page -----


def read_page(
    adapter, ref: str, persona_name: Optional[str] = None, manager=None,
) -> str:
    """ref の内容を読む。読んだ内容は会話の流れに残るだけで机の場所は取らない。

    対象が机に開かれている場合は touch する (touch の定義 = read/write/clip が
    触った扱い、sai_memory/desk.py)。読んでいる最中のページが LRU に追い出され
    るのを防ぐ。

    ``persona_name`` は写真のトランスクリプト描画でペルソナ応答に付ける表示名
    (スペル層が AINAME を解決して渡す)。未指定は persona_id で代替。

    ``manager`` は ``task:N`` (目的ノード) の解決にのみ要る world 文脈
    (``SessionLocal`` を持つオブジェクト。目的の木は main DB 在住のため)。
    スペル層が ``get_active_manager()`` を渡す。他の ref では不要 — 省略時も
    従来の全 ref が動く (後方互換)。
    """
    kind, key = _parse_ref(ref)
    conn = adapter.conn
    name = _resolve_persona_name(adapter, persona_name)
    if kind == "core_all":
        return _read_core_all(conn, name)
    if kind == "core_one":
        return _read_core_one(conn, key, name)
    if kind == "memopedia":
        text = _read_memopedia(adapter, key, name)
        _touch_if_open(adapter, kind, key)
        return text
    if kind == "chronicle":
        text = _read_chronicle(conn, key, name)
        _touch_if_open(adapter, kind, key)
        return text
    if kind == "photo":
        # 写真を読む＝その写真が写す土地を見に行く。机にも head にも触らない
        # (貼り先ページの touch もしない — 読んだのは土地であってページではない)
        return _read_photo(conn, key, name)
    if kind == "task":
        return _read_task(adapter, key, manager, name)
    raise AtlasRefError(f"未対応の ref kind: {kind}")


def _read_task(adapter, key: Optional[str], manager, persona_name: str) -> str:
    """目的ノード (task:N) を読む — title / goal / stage / status / steps / 写真。

    実体は main DB の persona_task (PersonaTaskManager)。旧 task ツール群と同じ
    DB アクセスパターン (SessionLocal factory) を踏襲する。貼られた写真
    (pasted_to="task:N") はペルソナの memory.db 側 (adapter.conn) にある。
    """
    from saiverse.persona_task_manager import PersonaTaskManager, TaskNotFoundError

    if not key:
        return "目的ノードの参照が不正です: task:"
    if manager is None or getattr(manager, "SessionLocal", None) is None:
        return (
            f"目的ノード (task:{key}) を読むための world 文脈がありません"
            "（スペル実行の文脈でのみ読めます）。"
        )

    ptm = PersonaTaskManager(manager.SessionLocal)
    ref_text = f"task:{key}" if key.isdigit() else key
    try:
        task_id = ptm.resolve_task_ref(adapter.persona_id, ref_text)
        task = ptm.get_task(task_id, persona_id=adapter.persona_id)
    except TaskNotFoundError:
        return f"目的ノードが見つかりません: task:{key}"

    task_ref = task.get("task_ref") or ref_text
    lines = [f"# {task.get('title') or '(無題)'} ({task_ref})"]
    meta_bits = [f"段階: {task.get('stage')}", f"状態: {task.get('status')}"]
    if task.get("nature"):
        meta_bits.append(f"種別: {task['nature']}")
    if task.get("desire_type"):
        meta_bits.append(f"欲求の型: {task['desire_type']}")
    lines.append(" / ".join(meta_bits))
    if task.get("goal"):
        lines.append(f"目標: {task['goal']}")
    if task.get("desire_source"):
        # 接地の証跡 (この目的が何から生まれたか)
        lines.append(f"由来: {task['desire_source']}")
    if task.get("notes"):
        lines.append(f"メモ: {task['notes']}")

    steps = task.get("steps") or []
    if steps:
        lines.append("")
        lines.append("## ステップ")
        for idx, st in enumerate(steps, start=1):
            mark = "x" if st.get("status") == "completed" else " "
            note = f" — {st['notes']}" if st.get("notes") else ""
            lines.append(f"{idx}. [{mark}] {st.get('title')} ({st.get('status')}){note}")

    photos_text = _format_photos(
        adapter.conn,
        list_photos_pasted_to(adapter.conn, task_ref),
        persona_name,
    )
    if photos_text:
        lines.append("")
        lines.append(photos_text)
    return "\n".join(lines)


def _read_photo(conn, key: Optional[str], persona_name: str) -> str:
    """写真 1 枚を読む — 範囲写真は区間の生ログ全文、点写真は対象メッセージ本文。"""
    sid = _parse_int(key)
    if sid is None:
        return f"写真の参照が不正です: p:{key}"
    photo = get_photo_by_short_id(conn, sid)
    if photo is None:
        return f"写真が見つかりません: p:{sid}"

    if photo.is_range:
        result = _range_photo_transcript(conn, photo, persona_name)
        if result is None:
            return (
                f"写真 p:{sid} の区間 ({photo.message_id}〜{photo.message_id_end}) "
                "を読み出せませんでした。端のメッセージが失われている可能性があります。"
            )
        transcript, message_count = result
        header = [f"# 写真 p:{sid} (範囲・全{message_count}メッセージ)"]
        if photo.quote:
            header.append(f"ラベル: {photo.quote}")
        if photo.pasted_to:
            header.append(f"貼り先: {photo.pasted_to}")
        return "\n".join(header) + "\n\n" + transcript

    from sai_memory.core_memory import format_scene_transcript
    from sai_memory.memory.storage import get_message

    message = get_message(conn, photo.message_id)
    if message is None:
        return (
            f"写真 p:{sid} の対象メッセージ ({photo.message_id}) が見つかりません。"
        )
    lines = [f"# 写真 p:{sid} (点・引用)", f"引用箇所: 「{photo.quote}」"]
    if photo.pasted_to:
        lines.append(f"貼り先: {photo.pasted_to}")
    lines.append("")
    lines.append(format_scene_transcript([message], persona_name))
    return "\n".join(lines)


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


def _read_core_all(conn, persona_name: str) -> str:
    items = list_core_memories(conn)
    if not items:
        return "コア記憶はまだありません。"
    lines = [f"# コア記憶 (常時開・全 {len(items)} 件)"]
    for cm in items:
        lines.append(f"\n## {cm.ref}")
        lines.append(cm.content)
        photos_text = _format_photos(
            conn, list_photos_pasted_to(conn, cm.ref), persona_name
        )
        if photos_text:
            lines.append(photos_text)
    return "\n".join(lines)


def _read_core_one(conn, key: Optional[str], persona_name: str) -> str:
    mid = _parse_int(key)
    if mid is None:
        return f"コア記憶の参照が不正です: c:{key}"
    cm = get_core_memory(conn, mid)
    if cm is None:
        return f"コア記憶が見つかりません: c:{mid}"
    lines = [f"# {cm.ref}", "", cm.content]
    photos_text = _format_photos(
        conn, list_photos_pasted_to(conn, cm.ref), persona_name
    )
    if photos_text:
        lines.append("")
        lines.append(photos_text)
    return "\n".join(lines)


def _read_memopedia(adapter, key: Optional[str], persona_name: str) -> str:
    from sai_memory.memopedia import Memopedia
    from sai_memory.memopedia.storage import resolve_page_ref

    if not key:
        return "Memopedia の参照が不正です: m:"
    resolved = resolve_page_ref(adapter.conn, key)
    memopedia = Memopedia(adapter.conn, db_lock=adapter._db_lock)
    page = memopedia.get_page(resolved) if resolved else None
    if page is None:
        return f"ページが見つかりません: m:{key}"
    # soft-delete 済みページは「無い」扱い (resolve_page_ref / get_page は
    # is_deleted を見ないため、Atlas の読み口でも弾く。desk と同じガード)
    if _is_memopedia_page_deleted(adapter.conn, page.id):
        return f"ページが見つかりません: m:{key}"

    ref = f"m:{page.short_id}" if page.short_id else page.id
    body = memopedia.render_page_body(page.id)
    lines = [f"# {page.title} ({ref})"]
    if page.summary:
        lines.append(f"\n*{page.summary}*")
    lines.append(f"\n{body or '(内容なし)'}")
    photos_text = _format_photos(
        adapter.conn, list_photos_pasted_to(adapter.conn, ref), persona_name
    )
    if photos_text:
        lines.append("")
        lines.append(photos_text)
    return "\n".join(lines)


def _read_chronicle(conn, key: Optional[str], persona_name: str) -> str:
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
    photos_text = _format_photos(
        conn, list_photos_pasted_to(conn, ref), persona_name
    )
    if photos_text:
        lines.append("")
        lines.append(photos_text)
    return "\n".join(lines)


# ----- open_page / close_page (机の物理、desk.py に委譲) -----


def _normalize_ref_for_desk(
    adapter, kind: str, key: Optional[str], manager=None,
) -> Optional[str]:
    """desk.py の主キーに使う正規形 (``m:{short_id}`` / ``ch:{short_id}`` /
    ``task:{short_id}``) に揃える。

    呼び出し側が UUID や別表記の key を渡しても、常に short_id ベースの一意な
    ref に正規化する (同じページを異なる表記で開いて二重登録するのを防ぐ)。
    見つからなければ None。

    ``manager`` は ``task:N`` (目的ノード = main DB 在住) の実在確認にのみ要る
    world 文脈。他 kind では不要。
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
        # soft-delete (is_deleted=1) されたページは机の対象外。resolve_page_ref /
        # get_page は is_deleted を見ないため、ここで弾かないと削除済みページが
        # 机に居座って本文を head に描画し続ける (P2b テストで発見)
        if _is_memopedia_page_deleted(adapter.conn, page.id):
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
    if kind == "task":
        return _resolve_task_ref_for_desk(adapter, key, manager)
    return None


def _resolve_task_ref_for_desk(adapter, key: Optional[str], manager) -> Optional[str]:
    """task:N の実在確認 (desk 正規形は元々 ``task:{short_id}`` 一本で UUID
    表記のような別形は無いため、確認できればそのまま返すだけでよい)。

    完了/中止 (TERMINAL_TASK_STATUSES) の目的ノードは「無い」扱いにする —
    persona_task 行は不変条件により物理削除されない (short_id を二度と
    再利用しないため) が、Memopedia の soft-delete (``is_deleted=1``) が
    desk の存在チェックで「無い」扱いになるのと同じ既存規約に揃える
    (P3c①②設計 v0.1「無理に新機構を作らない」— 新しい終了検知を作らず、
    既存の存在チェックの仕組みに乗せる)。
    """
    from saiverse.persona_task_manager import (
        TERMINAL_TASK_STATUSES,
        PersonaTaskManager,
        TaskNotFoundError,
    )

    if not key or manager is None or getattr(manager, "SessionLocal", None) is None:
        return None
    ptm = PersonaTaskManager(manager.SessionLocal)
    ref_text = f"task:{key}"
    try:
        task_id = ptm.resolve_task_ref(adapter.persona_id, ref_text)
        task = ptm.get_task(task_id, persona_id=adapter.persona_id)
    except TaskNotFoundError:
        return None
    if task.get("status") in TERMINAL_TASK_STATUSES:
        return None
    return ref_text


def _is_memopedia_page_deleted(conn, page_id: str) -> bool:
    """soft-delete (is_deleted=1) 済みページか。storage の定型ガードと同じ判定。"""
    row = conn.execute(
        "SELECT is_deleted FROM memopedia_pages WHERE id = ?", (page_id,)
    ).fetchone()
    return bool(row and row[0])


def _size_of_ref(adapter, ref: str, manager=None) -> int:
    """desk の評価用サイズ解決 (ref → 現在の本文文字数)。解決できなければ 0。

    ``manager`` は ``task:N`` の解決にのみ要る world 文脈 (main DB 在住)。
    """
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
        if ref.startswith("task:"):
            key = ref[5:]
            if _resolve_task_ref_for_desk(adapter, key, manager) is None:
                return 0
            name = _resolve_persona_name(adapter, None)
            rendered = _read_task(adapter, key, manager, name)
            return len(rendered or "")
    except Exception:
        LOGGER.warning(
            "memory_atlas: failed to resolve desk size for ref=%s", ref, exc_info=True,
        )
    return 0


def open_page(
    adapter, ref: str, purpose_ref: Optional[str] = None, manager=None,
) -> str:
    """ページを机に開いたままにする (Metabolism を跨いで head に残り続ける)。

    机の予算 (``desk.resolve_desk_budget_chars()``) を超えたら、最も長く触ら
    れていないページから自動で棚に戻す (LRU)。追い出しが起きた場合はその旨を
    結果テキストに含める。

    ``manager`` は ``task:N`` (目的ノード = main DB 在住) の解決にのみ要る
    world 文脈。スペル層が ``get_active_manager()`` を渡す。
    """
    kind, key = _parse_ref(ref)
    if kind in ("core_all", "core_one"):
        return "コア記憶は常時開いています。open は不要です。"
    if kind == "photo":
        return (
            "写真は机に開けません（写真は土地への参照です）。"
            f"memory_read p:{key} でその場で読めます。"
        )
    if kind not in ("memopedia", "chronicle", "task"):
        raise AtlasRefError(f"未対応の ref kind: {kind}")

    norm_ref = _normalize_ref_for_desk(adapter, kind, key, manager=manager)
    if norm_ref is None:
        return f"見つかりません: {ref}"

    conn = adapter.conn
    with adapter._db_lock:
        desk.open_item(conn, norm_ref, purpose_ref=purpose_ref)
        budget = desk.resolve_desk_budget_chars()
        # keep_ref: いま開いた本人は同一呼び出しでは追い出さない (「開きました」
        # と「棚に戻しました」の同居を防ぐ。desk.evict_lru docstring 参照)
        evicted = desk.evict_lru(
            conn, budget, lambda r: _size_of_ref(adapter, r, manager=manager),
            keep_ref=norm_ref,
        )

    lines = [f"{norm_ref} を机に開きました。"]
    if evicted:
        lines.append(f"机が溢れたため {'、'.join(evicted)} を棚に戻しました。")
    return "\n".join(lines)


def close_page(adapter, ref: str, manager=None) -> str:
    """ページを机から閉じる (棚に戻す)。

    ``manager`` は ``task:N`` の解決にのみ要る world 文脈。
    """
    kind, key = _parse_ref(ref)
    if kind in ("core_all", "core_one"):
        return "コア記憶は閉じられません(常時開です)。"
    if kind == "photo":
        return "写真は机の対象外です（開閉はありません）。"
    if kind not in ("memopedia", "chronicle", "task"):
        raise AtlasRefError(f"未対応の ref kind: {kind}")

    norm_ref = _normalize_ref_for_desk(adapter, kind, key, manager=manager)
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
    # P3b (2026-07-11): Chronicle エントリは memopedia_pages 上のページにもなった
    # ため、素の memopedia.search() にも Chronicle が混じる。直下で search_entries()
    # により "## Chronicle" として別途表示するので、ここでの二重ヒットを除外する
    # (docs/intent/concept_consolidation.md「P3b」周辺の整合)。
    pages = [p for p in pages if p.category != "chronicle"]
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


# ----- write_page -----


# 新規ページ作成のカテゴリ → root ページ ID (memopedia INITIAL_ROOTS と対)
_WRITE_CATEGORY_ROOT_MAP = {
    "people": "root_people",
    "terms": "root_terms",
    "plans": "root_plans",
    "events": "root_events",
}


def write_page(
    adapter,
    ref: Optional[str] = None,
    content: str = "",
    *,
    title: Optional[str] = None,
    category: Optional[str] = None,
    core_budget: Optional[int] = None,
) -> str:
    """ページに書く (concept_consolidation.md P2 統一スペル動詞 memory_write)。

    - ``m:N``: Memopedia ページ本文への**追記** (編集来歴が残る
      ``Memopedia.append_to_content`` 経由)。書いたページが机に開かれていれば
      touch する (touch の定義に write が含まれる)。
    - ``core``: 新しいコア記憶を刻む (常時開の特殊ページ)。
    - ``c:N``: 既存コア記憶の**上書き**。
    - ``ref`` の代わりに ``title`` (+ ``category``): Memopedia に**新規ページ作成**
      (P2c-0 決定3)。構造編集 (移動・統合・分割・summary/keywords 更新) は
      日常動詞にしない — 庭仕事モードの遅延開示 (別仕様・後回し) の領分。
    - ``ch:N`` / ``p:N``: 書けない (Chronicle の編纂はシステム側 / 写真は参照)。
    - ``task:N``: purpose 動詞の領分 (P3c まで stub)。

    ``core_budget`` はコア記憶の容量目安 (per-persona 設定)。スペル層が解決して
    渡す。超過時は通知を添える (切り詰め・拒否は絶対にしない —
    memory_architecture_v2.md §5 の既存規律のまま)。
    """
    text = (content or "").strip()
    if not text:
        return "Error: content は空にできません。"

    has_ref = bool(ref and str(ref).strip())
    has_title = bool(title and str(title).strip())
    if has_ref == has_title:
        return (
            "Error: ref（既存ページに書く）と title（新規ページを作る）の"
            "どちらか一方だけを指定してください。"
        )
    if has_title:
        return _write_memopedia_create(adapter, str(title).strip(), category, text)

    kind, key = _parse_ref(ref)
    conn = adapter.conn

    if kind == "core_all":
        return _write_core_new(conn, text, core_budget)
    if kind == "core_one":
        return _write_core_update(conn, key, text, core_budget)
    if kind == "memopedia":
        result = _write_memopedia_append(adapter, key, text)
        _touch_if_open(adapter, kind, key)
        return result
    if kind == "chronicle":
        return (
            f"ch:{key} には書けません。"
            "時間の地図（Chronicle）の編纂はシステムが行います。"
        )
    if kind == "photo":
        return (
            f"p:{key} には書けません。写真は土地（生ログ）への参照です。"
        )
    if kind == "task":
        return _task_stub_message(key, action="への書き込み")
    raise AtlasRefError(f"未対応の ref kind: {kind}")


def _core_budget_note(conn, core_budget: Optional[int]) -> str:
    """コア記憶の現在量と目安の文言 (目安が未解決なら現在量のみ)。"""
    from sai_memory.core_memory import total_core_memory_chars

    total = total_core_memory_chars(conn)
    if core_budget is None or core_budget <= 0:
        return f"現在 {total:,} 字。"
    over = "目安を超えています。整理を検討してください。" if total > core_budget else ""
    return f"現在 {total:,} 字 / 目安 {core_budget:,} 字。{over}"


def _write_core_new(conn, text: str, core_budget: Optional[int]) -> str:
    from sai_memory.core_memory import add_core_memory

    new_id = add_core_memory(conn, text)
    note = _core_budget_note(conn, core_budget)
    return (
        f"コア記憶 c:{new_id} に書きました（常時開の特殊ページです）。{note}"
        "head への反映は次の記憶整理（Metabolism）からです。"
    )


def _write_core_update(
    conn, key: Optional[str], text: str, core_budget: Optional[int]
) -> str:
    from sai_memory.core_memory import update_core_memory

    mid = _parse_int(key)
    if mid is None:
        return f"コア記憶の参照が不正です: c:{key}"
    if not update_core_memory(conn, mid, text):
        return f"コア記憶が見つかりません: c:{mid}"
    note = _core_budget_note(conn, core_budget)
    return (
        f"コア記憶 c:{mid} を上書きしました。{note}"
        "head への反映は次の記憶整理（Metabolism）からです。"
    )


def _write_memopedia_append(adapter, key: Optional[str], text: str) -> str:
    from sai_memory.memopedia import Memopedia
    from sai_memory.memopedia.storage import resolve_page_ref
    from tools.context import get_active_message_id

    if not key:
        return "Memopedia の参照が不正です: m:"
    resolved = resolve_page_ref(adapter.conn, key)
    memopedia = Memopedia(adapter.conn, db_lock=adapter._db_lock)
    page = memopedia.get_page(resolved) if resolved else None
    if page is None:
        return f"ページが見つかりません: m:{key}"

    # 編集来歴 (PageEditHistory) が残る append 経路。由来メッセージ参照は
    # スペル実行中の contextvar から取れる場合のみ付く (CLI 直叩き等は None)。
    msg_id = None
    try:
        msg_id = get_active_message_id()
    except Exception:
        pass
    memopedia.append_to_content(
        page.id, text,
        ref_start_message_id=msg_id, ref_end_message_id=msg_id,
        edit_source="ai_conversation",
    )
    ref = f"m:{page.short_id}" if page.short_id else page.id
    return f"{ref}「{page.title}」に追記しました。"


def _active_message_id() -> Optional[str]:
    """スペル実行中の contextvar から由来メッセージ ID を取る (無ければ None)。"""
    from tools.context import get_active_message_id

    try:
        return get_active_message_id()
    except Exception:
        return None


def _write_memopedia_create(
    adapter, title: str, category: Optional[str], text: str
) -> str:
    """Memopedia に新規ページを作成する (P2c-0 決定3、編集来歴つき create 経路)。"""
    from sai_memory.memopedia import Memopedia

    cat = (category or "terms").lower().strip()
    root_id = _WRITE_CATEGORY_ROOT_MAP.get(cat)
    if root_id is None:
        return (
            f"Error: category が不正です: {category!r}"
            f"（有効: {', '.join(sorted(_WRITE_CATEGORY_ROOT_MAP))}）"
        )

    memopedia = Memopedia(adapter.conn, db_lock=adapter._db_lock)
    existing = memopedia.find_by_title(title)
    if existing is not None and not _is_memopedia_page_deleted(adapter.conn, existing.id):
        ex_ref = f"m:{existing.short_id}" if existing.short_id else existing.id
        return (
            f"同名のページが既にあります: {ex_ref}「{existing.title}」。"
            f"追記するには memory_write の ref に {ex_ref} を指定してください。"
        )

    msg_id = _active_message_id()
    page = memopedia.create_page(
        parent_id=root_id,
        title=title,
        content=text,
        ref_start_message_id=msg_id,
        ref_end_message_id=msg_id,
        edit_source="ai_conversation",
    )
    ref = f"m:{page.short_id}" if page.short_id else page.id
    return f"新しいページを作りました: {ref}「{title}」（カテゴリ: {cat}）"


# ----- delete_page -----


def delete_page(adapter, ref: str) -> str:
    """ページをごみ箱へ移す (P2c-0 決定1: memory_delete)。

    - ``c:N``: コア記憶の soft-delete (``deleted_at`` 刻印。旧 core_memory_remove
      と同じ経路 — 復元可能)
    - ``m:N``: Memopedia の soft-delete (``is_deleted``。復元は後回しのごみ箱仕様)
    - ``ch:N`` (編纂はシステム側) / ``p:N`` (写真は歴史として残す §5.1) /
      ``core`` 全体 / ``task:N`` (purpose_close の領分) は消せない

    削除したページが机に開いていたら desk からも即時クローズする (放置しても
    次の Metabolism で dropped になるが、即時の方が誠実)。
    """
    kind, key = _parse_ref(ref)
    conn = adapter.conn

    if kind == "core_all":
        return "コア記憶全体は消せません。1件ずつ c:N で指定してください。"
    if kind == "chronicle":
        return (
            f"ch:{key} は消せません。"
            "時間の地図（Chronicle）の編纂はシステムが行います。"
        )
    if kind == "photo":
        return (
            f"p:{key} は消せません。写真は撮られた歴史としてそのまま残ります。"
        )
    if kind == "task":
        return (
            f"目的ノード (task:{key}) はこのスペルでは消せません。"
            "完了・中止・休眠にするには purpose_close を使ってください。"
        )

    if kind == "core_one":
        from sai_memory.core_memory import remove_core_memory

        mid = _parse_int(key)
        if mid is None:
            return f"コア記憶の参照が不正です: c:{key}"
        with adapter._db_lock:
            ok = remove_core_memory(conn, mid)
        if not ok:
            return f"コア記憶が見つかりません: c:{mid}"
        return (
            f"コア記憶 c:{mid} をごみ箱に移しました（完全に消えるわけでは"
            "ありません）。head への反映は次の記憶整理（Metabolism）からです。"
        )

    if kind == "memopedia":
        from sai_memory.memopedia import Memopedia
        from sai_memory.memopedia.storage import resolve_page_ref

        if not key:
            return "Memopedia の参照が不正です: m:"
        resolved = resolve_page_ref(conn, key)
        memopedia = Memopedia(conn, db_lock=adapter._db_lock)
        page = memopedia.get_page(resolved) if resolved else None
        if page is None or _is_memopedia_page_deleted(conn, page.id):
            return f"ページが見つかりません: m:{key}"

        norm_ref = f"m:{page.short_id}" if page.short_id else f"m:{page.id}"
        msg_id = _active_message_id()
        if not memopedia.delete_page(
            page.id,
            ref_start_message_id=msg_id, ref_end_message_id=msg_id,
            edit_source="ai_conversation",
        ):
            return f"ページを消せませんでした: {norm_ref}"

        # 机に開いていたら即時クローズ (次の Metabolism を待たない)
        lines = [
            f"{norm_ref}「{page.title}」をごみ箱に移しました"
            "（完全に消えるわけではありません）。"
        ]
        with adapter._db_lock:
            if desk.close_item(conn, norm_ref):
                lines.append("机からも下ろしました。")
        return "\n".join(lines)

    raise AtlasRefError(f"未対応の ref kind: {kind}")


# ----- clip_photo -----


def _normalize_paste_target(adapter, paste_to: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    """clip の貼り先を検証・正規化する。戻り値は ``(正規 ref, エラーメッセージ)``。

    - None → (None, None): 貼り先なし = 土壌プールに置く
    - ``m:N``: ページの実在を確認し short_id 正規形へ
    - ``c:N``: コア記憶の実在を確認
    - ``task:N``: P2c まで未対応 (エラーメッセージを返し、写真は撮らない)
    """
    if paste_to is None or not str(paste_to).strip():
        return None, None
    text = str(paste_to).strip()
    try:
        kind, key = _parse_ref(text)
    except AtlasRefError:
        return None, f"貼り先の形式を解釈できません: {paste_to}（例: m:3 / c:2）"

    if kind == "memopedia":
        norm = _normalize_ref_for_desk(adapter, kind, key)
        if norm is None:
            return None, f"貼り先のページが見つかりません: {text}"
        return norm, None
    if kind == "core_one":
        mid = _parse_int(key)
        if mid is None or get_core_memory(adapter.conn, mid) is None:
            return None, f"貼り先のコア記憶が見つかりません: {text}"
        return f"c:{mid}", None
    if kind == "task":
        # TODO(P2c): 目的ノードへの貼り付け (収穫 = 写真 → candidate) は
        # purpose 動詞と合わせて実装する。
        return None, (
            f"目的ノード ({text}) への貼り付けは今後対応予定です。"
            "貼り先を指定しない場合は paste_to を省略してください。"
        )
    return None, f"この種類のページには貼れません: {text}"


def clip_photo(
    adapter,
    message_id: str,
    *,
    quote: Optional[str] = None,
    rounds: int = 3,
    paste_to: Optional[str] = None,
    persona_name: Optional[str] = None,
    mode: str = "attach",
    core_budget: Optional[int] = None,
) -> str:
    """写真を撮ってクリップで貼る (concept_consolidation.md P2 memory_clip)。

    - ``quote`` あり → **点写真**: 対象メッセージ本文からの逐語引用。実在検証
      (引用が本文に含まれるか) を行い、不一致なら撮らずにエラーを返す。
    - ``quote`` なし → **範囲写真**: ``message_id`` をアンカーに前後 ``rounds``
      往復の実会話窓 (SCENE と同じ窓切り出し) を範囲として撮る。

    貼り方は 2 種 (P2c-0 決定2):

    - ``mode='attach'`` (既定): **参照貼り** — photos に pasted_to 付きで保存し、
      ページには抜粋が表示される (全文は memory_read p:N)。
    - ``mode='transcribe'``: **転写** — 本文に焼き込み、常に生で見える。
      貼り先必須。core 宛の範囲転写は旧 SCENE (create_scene_core_memory) と
      同一の共有ロジックを通す。転写でも写真は撮って由来参照を残す
      (SCENE の現行流儀と同じ)。paste_to='core' のときは memory_write と同じ
      容量目安 (``core_budget``) 超過通知を添える (P2c-4a: 旧 core_memory_add_scene
      にあった budget 通知の欠落を埋める)。

    貼り先が机に開かれていれば touch する (touch の定義に clip が含まれる)。
    """
    from sai_memory.photos import add_photo

    conn = adapter.conn
    name = _resolve_persona_name(adapter, persona_name)

    if mode not in ("attach", "transcribe"):
        return (
            f"Error: mode が不正です: {mode!r}"
            "（attach = 参照貼り / transcribe = 転写）"
        )
    if mode == "transcribe":
        if paste_to is None or not str(paste_to).strip():
            return (
                "Error: 転写 (mode='transcribe') には貼り先 (paste_to) が必要です"
                "（焼き込み先の無い転写はできません）。"
            )
        return _clip_transcribe(
            adapter, message_id,
            quote=quote, rounds=rounds,
            paste_to=str(paste_to).strip(), persona_name=name,
            core_budget=core_budget,
        )

    norm_paste, paste_err = _normalize_paste_target(adapter, paste_to)
    if paste_err:
        return paste_err

    if quote is not None and str(quote).strip():
        # --- 点写真: 逐語引用の実在検証 ---
        from sai_memory.memory.storage import get_message

        quote_text = str(quote).strip()
        message = get_message(conn, message_id)
        if message is None:
            return f"メッセージが見つかりません: {message_id}"
        if quote_text not in (message.content or ""):
            return (
                "引用が対象メッセージの本文に見つかりませんでした。"
                "写真は土地（生ログ）をそのまま写すものなので、"
                "本文にある言葉を一字一句そのまま指定してください。"
            )
        photo = add_photo(
            conn, message_id=message_id, quote=quote_text, pasted_to=norm_paste,
        )
        lines = [f"写真 {photo.ref} を撮りました（点・引用）。", f"引用: 「{quote_text}」"]
    else:
        # --- 範囲写真: SCENE と同じ実会話窓の切り出し ---
        from sai_memory.memory.storage import get_conversation_window_around

        try:
            rounds_int = int(rounds)
        except (TypeError, ValueError):
            rounds_int = 3
        if rounds_int <= 0:
            rounds_int = 3
        window = get_conversation_window_around(conn, message_id, rounds=rounds_int)
        if not window:
            return (
                f"メッセージ {message_id} が見つからないか、実会話ではありません"
                "（ツール実行ログ等は切り抜き対象外です）。"
            )
        photo = add_photo(
            conn,
            message_id=window[0].id,
            message_id_end=window[-1].id,
            pasted_to=norm_paste,
        )
        result = _range_photo_transcript(conn, photo, name)
        lines = [f"写真 {photo.ref} を撮りました（範囲・全{len(window)}メッセージ）。"]
        if result is not None:
            transcript, message_count = result
            t_lines = transcript.splitlines()
            for ln in t_lines[:RANGE_PHOTO_EXCERPT_LINES]:
                lines.append(f"  {ln}")
            if len(t_lines) > RANGE_PHOTO_EXCERPT_LINES:
                lines.append(
                    f"  (全{message_count}メッセージ・{len(transcript):,}字、前後省略 — "
                    f"memory_read {photo.ref} で全文)"
                )

    if norm_paste:
        lines.append(f"{norm_paste} に貼りました。")
        if norm_paste.startswith("m:"):
            _touch_if_open(adapter, "memopedia", norm_paste[2:])
    else:
        lines.append("貼り先は未指定です（あとから貼れます）。")
    return "\n".join(lines)


def _latest_photo_ref_pasted_to(conn, pasted_to: str) -> Optional[str]:
    """貼り先 ref に貼られた最新の写真の p:N を返す (無ければ None)。

    create_scene_core_memory (共有ロジック) は写真を撮るが Photo を返さない
    ため、結果テキストに p:N を載せるための読み口。
    """
    photos = list_photos_pasted_to(conn, pasted_to)
    return photos[-1].ref if photos else None


def _clip_transcribe(
    adapter,
    message_id: str,
    *,
    quote: Optional[str],
    rounds: int,
    paste_to: str,
    persona_name: str,
    core_budget: Optional[int] = None,
) -> str:
    """転写 (mode='transcribe'): 本文に焼き込み + 写真で由来参照を残す。

    - 範囲 + ``paste_to='core'``: 旧 SCENE と**同一の共有ロジック**
      (``create_scene_core_memory``) を通す — 窓切り出し・トランスクリプト整形・
      写真 (pasted_to="c:{id}") まで全部同じ。旧 core_memory_add_scene スペルと
      挙動が割れない (言い換えドリフト防止の一点管理)。
    - 点 + ``paste_to='core'``: 引用を新しいコア記憶として刻む + 点写真。
    - ``paste_to='m:N'``: トランスクリプト / 引用をページ本文に追記
      (編集来歴つき append 経路) + 写真。
    - ``c:N`` (既存コア記憶への焼き込み) は非対応 — 共有ロジックが無く挙動が
      割れるため、新しいコア記憶として刻む ``paste_to='core'`` に誘導する。

    ``core_budget`` は ``paste_to='core'`` の 2 経路 (範囲・点) 共通の容量目安
    通知 (write_page と同じ ``_core_budget_note``)。旧 core_memory_add_scene /
    core_memory_add にあった budget 超過警告を、転写でも同じように出す。
    """
    from sai_memory.photos import add_photo

    conn = adapter.conn
    is_point = bool(quote and str(quote).strip())

    try:
        rounds_int = int(rounds)
    except (TypeError, ValueError):
        rounds_int = 3
    if rounds_int <= 0:
        rounds_int = 3

    # --- 宛先: core (常時開の特殊ページ) ---------------------------------
    if paste_to.lower() == "core":
        if not is_point:
            # 範囲転写 = 旧 SCENE と同一の共有ロジック (複製禁止)
            from sai_memory.core_memory import create_scene_core_memory

            with adapter._db_lock:
                result = create_scene_core_memory(
                    conn, message_id, rounds=rounds_int, persona_name=persona_name,
                )
            if result is None:
                return (
                    f"メッセージ {message_id} が見つからないか、実会話ではありません"
                    "（ツール実行ログ等は切り抜き対象外です）。"
                )
            photo_ref = _latest_photo_ref_pasted_to(conn, f"c:{result.memory_id}")
            lines = [
                f"会話の切り抜きをコア記憶 c:{result.memory_id} に転写しました"
                f"（{result.message_count} 発言・{result.char_count:,}字、"
                f"{result.date_start}〜{result.date_end}）。",
            ]
            if photo_ref:
                lines.append(f"由来参照として写真 {photo_ref} も貼りました。")
            note = _core_budget_note(conn, core_budget)
            lines.append(f"head への反映は次の記憶整理（Metabolism）からです。{note}")
            return "\n".join(lines)

        # 点転写 = 引用を新しいコア記憶として刻む + 点写真
        from sai_memory.core_memory import add_core_memory
        from sai_memory.memory.storage import get_message

        quote_text = str(quote).strip()
        message = get_message(conn, message_id)
        if message is None:
            return f"メッセージが見つかりません: {message_id}"
        if quote_text not in (message.content or ""):
            return (
                "引用が対象メッセージの本文に見つかりませんでした。"
                "写真は土地（生ログ）をそのまま写すものなので、"
                "本文にある言葉を一字一句そのまま指定してください。"
            )
        with adapter._db_lock:
            new_id = add_core_memory(conn, quote_text)
            photo = add_photo(
                conn, message_id=message_id, quote=quote_text,
                pasted_to=f"c:{new_id}",
            )
        note = _core_budget_note(conn, core_budget)
        return (
            f"引用をコア記憶 c:{new_id} に転写しました。\n"
            f"由来参照として写真 {photo.ref} も貼りました。\n"
            f"head への反映は次の記憶整理（Metabolism）からです。{note}"
        )

    # --- 宛先の解釈 -------------------------------------------------------
    try:
        kind, key = _parse_ref(paste_to)
    except AtlasRefError:
        return f"転写先の形式を解釈できません: {paste_to}（例: m:3 / core）"

    if kind == "core_one":
        # 既存コア記憶への焼き込みは共有ロジックが無い (SCENE は常に新規作成)。
        # 挙動が割れる独自実装はせず、新規に刻む "core" へ誘導する。
        return (
            f"{paste_to} への転写はできません。コア記憶への転写は"
            " paste_to='core'（新しいコア記憶として刻む）を使ってください。"
        )
    if kind != "memopedia":
        return (
            f"この宛先には転写できません: {paste_to}"
            "（転写先は m:N または core です）"
        )

    # --- 宛先: m:N (Memopedia ページ本文への焼き込み) ---------------------
    norm_ref = _normalize_ref_for_desk(adapter, "memopedia", key)
    if norm_ref is None:
        return f"転写先のページが見つかりません: {paste_to}"

    if is_point:
        from sai_memory.memory.storage import get_message

        quote_text = str(quote).strip()
        message = get_message(conn, message_id)
        if message is None:
            return f"メッセージが見つかりません: {message_id}"
        if quote_text not in (message.content or ""):
            return (
                "引用が対象メッセージの本文に見つかりませんでした。"
                "写真は土地（生ログ）をそのまま写すものなので、"
                "本文にある言葉を一字一句そのまま指定してください。"
            )
        burned_text = quote_text
        ref_start, ref_end = message_id, message_id
    else:
        from sai_memory.core_memory import format_scene_transcript
        from sai_memory.memory.storage import get_conversation_window_around

        window = get_conversation_window_around(conn, message_id, rounds=rounds_int)
        if not window:
            return (
                f"メッセージ {message_id} が見つからないか、実会話ではありません"
                "（ツール実行ログ等は切り抜き対象外です）。"
            )
        burned_text = format_scene_transcript(window, persona_name)
        ref_start, ref_end = window[0].id, window[-1].id

    from sai_memory.memopedia import Memopedia
    from sai_memory.memopedia.storage import resolve_page_ref

    resolved = resolve_page_ref(conn, norm_ref[2:])
    memopedia = Memopedia(conn, db_lock=adapter._db_lock)
    page = memopedia.get_page(resolved)
    if page is None:
        return f"転写先のページが見つかりません: {paste_to}"
    memopedia.append_to_content(
        page.id, burned_text,
        ref_start_message_id=ref_start, ref_end_message_id=ref_end,
        edit_source="ai_conversation",
    )
    with adapter._db_lock:
        photo = add_photo(
            conn,
            message_id=ref_start,
            quote=(str(quote).strip() if is_point else None),
            message_id_end=(None if is_point else ref_end),
            pasted_to=norm_ref,
        )
    _touch_if_open(adapter, "memopedia", norm_ref[2:])

    what = "引用" if is_point else "会話の切り抜き"
    return (
        f"{what}を {norm_ref}「{page.title}」の本文に転写しました。\n"
        f"由来参照として写真 {photo.ref} も貼りました。"
    )


# ----- snapshot_desk (head 机セクション用、Metabolism 時のみ呼ばれる) -----


@dataclass(frozen=True)
class DeskPageView:
    """机に開いている 1 ページの描画済みビュー (head 机セクションの材料)。"""

    ref: str                    # 正規形 (m:N / ch:N)
    text: str                   # read_page と同じ整形の本文 (写真は抜粋)
    purpose_ref: Optional[str]  # この開きが紐づく目的 (無ければ None)


def snapshot_desk(
    adapter, persona_name: Optional[str] = None, manager=None,
) -> Tuple[List[DeskPageView], List[str], List[str]]:
    """Metabolism (head snapshot 再構築) 用: 机の現況を確定して描画する。

    1. 予算を再評価して溢れ分を LRU 追い出す (**Metabolism 追い出しフック**。
       ページは成長するので、開いた時に収まっていても節目には溢れていることが
       ある)
    2. 実体が消えたページ (削除済み等) は机から自動で閉じる (決定論の掃除)
    3. 残った開きページを read_page と同じ整形で描画する

    **touch はしない** — 机の一覧描画は「触った」に数えない。LRU の鮮度は
    ペルソナの能動的な read / write / clip だけが動かす (Metabolism のたびに
    全ページを touch すると鮮度差が消えて LRU が壊れる)。

    ``manager`` は ``task:N`` (目的ノード = main DB 在住) の解決にのみ要る
    world 文脈。head の DeskSection.capture が ``ctx.manager`` を渡す。

    Returns:
        (pages, evicted, dropped): 描画済みページ一覧 (opened_at 昇順) と、
        この snapshot で机から下ろされた ref の 2 リスト —
        **evicted** = 机の溢れ (LRU 追い出し) / **dropped** = 実体の消失
        (ページ削除・不正 ref)。head 机セクションが diff 通知の材料に使う
        (理由が違うものを同じ「溢れたため」と言うのは嘘になるので分ける)。
    """
    name = _resolve_persona_name(adapter, persona_name)
    conn = adapter.conn
    evicted: List[str] = []
    dropped: List[str] = []
    with adapter._db_lock:
        budget = desk.resolve_desk_budget_chars()
        evicted.extend(
            desk.evict_lru(conn, budget, lambda r: _size_of_ref(adapter, r, manager=manager))
        )
        items = desk.list_open(conn)

    pages: List[DeskPageView] = []
    for item in items:
        try:
            kind, key = _parse_ref(item.ref)
        except AtlasRefError:
            kind, key = "", None
        if kind not in ("memopedia", "chronicle", "task"):
            # 机には open_page が正規形しか入れないはずだが、防御的に閉じる
            with adapter._db_lock:
                desk.close_item(conn, item.ref)
            dropped.append(item.ref)
            continue
        if _normalize_ref_for_desk(adapter, kind, key, manager=manager) is None:
            # 実体が消えている (ページ削除・完了/中止による目的ノード終了等)。
            # 机から下ろす
            with adapter._db_lock:
                desk.close_item(conn, item.ref)
            dropped.append(item.ref)
            continue
        if kind == "memopedia":
            text = _read_memopedia(adapter, key, name)
        elif kind == "chronicle":
            text = _read_chronicle(conn, key, name)
        else:
            text = _read_task(adapter, key, manager, name)
        pages.append(
            DeskPageView(ref=item.ref, text=text, purpose_ref=item.purpose_ref)
        )
    return pages, evicted, dropped
