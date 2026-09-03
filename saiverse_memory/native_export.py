"""SAIMemory native export/import: full-fidelity thread round-trip.

Exports and imports threads with all metadata preserved,
enabling external editing (e.g., find-replace) and re-import.

Import の意味論 (記憶・人格境界監査 2026-07-12 第5片、まはー裁定 2026-07-16):

- **復元 (restore, 既定)**: 同一 persona への書き戻し。archive の persona_id /
  全 thread_id prefix が target と一致しない場合は**書き込む前に拒否**する。
  別人格名義の土地を人格 DB に同居させない。
- **移植 (transplant, 明示フラグ)**: 別 persona への移し替え。thread ID /
  resource ID / Stelis parent ID の persona prefix を**原子的に** target へ
  写像し、元の identity は各 message の ``metadata.transplanted_from`` に
  provenance として保持する (target DB 内の全 identity は target に閉じる)。
- **replace は全成功時のみ**: 事前検証 → 単一トランザクションで全 thread を
  書き、成功時に一度だけ commit する。途中失敗は rollback され、既存の
  target は行単位で不変 (旧「message 単位 commit で部分適用が残る」の根治)。
- **embedding は commit 後の派生工程**: 再構築可能なデータなので import の
  成否には関与しない。persona ID を thread ID から推測せず、embedder だけを
  非生成的に構築する (別 persona の memory.db を作らない)。
"""
from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from saiverse.data_paths import get_persona_memory_db
from sai_memory.memory.storage import (
    get_stelis_thread,
    init_db,
)

LOGGER = logging.getLogger(__name__)

FORMAT_VERSION = "saiverse_saimemory_v1"


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

def _resolve_thread_ids(
    conn: sqlite3.Connection,
    persona_id: str,
    thread_suffixes: Optional[Iterable[str]],
) -> List[str]:
    """Resolve thread suffixes to full thread IDs."""
    if thread_suffixes:
        resolved: List[str] = []
        for item in thread_suffixes:
            item = item.strip()
            if not item:
                continue
            if ":" in item:
                resolved.append(item)
            else:
                resolved.append(f"{persona_id}:{item}")
        return resolved

    cur = conn.execute(
        "SELECT DISTINCT thread_id FROM messages WHERE thread_id LIKE ? ORDER BY thread_id",
        (f"{persona_id}:%",),
    )
    return [row[0] for row in cur.fetchall()]


def _export_thread(
    conn: sqlite3.Connection,
    thread_id: str,
    start_epoch: Optional[int] = None,
    end_epoch: Optional[int] = None,
) -> Dict[str, Any]:
    """Export a single thread with all metadata."""
    # Thread-level info
    # export は init_db を通さず生の接続で読む (208/234 行目) ので、新しい
    # init_db でまだ開かれていない DB には title 列が無いことがある。その場合は
    # 列なしで読む。
    try:
        cur = conn.execute(
            "SELECT resource_id, overview, overview_updated_at, title FROM threads WHERE id=?",
            (thread_id,),
        )
        thread_row = cur.fetchone()
    except sqlite3.OperationalError as exc:
        # 列が無いときだけ縮退する。"database is locked" 等の一時的な失敗まで
        # ここで拾うと、列がある DB から題名だけが静かに欠けたエクスポートになる。
        if "no such column" not in str(exc):
            raise
        cur = conn.execute(
            "SELECT resource_id, overview, overview_updated_at, NULL FROM threads WHERE id=?",
            (thread_id,),
        )
        thread_row = cur.fetchone()
    resource_id = thread_row[0] if thread_row else None
    overview = thread_row[1] if thread_row else None
    overview_updated_at = thread_row[2] if thread_row else None
    title = thread_row[3] if thread_row else None

    # Stelis info
    stelis_data = None
    stelis = get_stelis_thread(conn, thread_id)
    if stelis:
        stelis_data = {
            "parent_thread_id": stelis.parent_thread_id,
            "depth": stelis.depth,
            "window_ratio": stelis.window_ratio,
            "status": stelis.status,
            "chronicle_prompt": stelis.chronicle_prompt,
            "chronicle_summary": stelis.chronicle_summary,
            "created_at": stelis.created_at,
            "completed_at": stelis.completed_at,
            "label": stelis.label,
        }

    # Messages (raw, no role conversion or content expansion)
    query_parts = [
        "SELECT id, role, content, resource_id, created_at, metadata,"
        " origin_track_id, line_role, line_id, scope, paired_action_text,"
        " pulse_id, thought_signature, spell_origin_id, spell_seq, rowid",
        "FROM messages WHERE thread_id=?",
    ]
    params: List[Any] = [thread_id]
    if start_epoch is not None:
        query_parts.append("AND created_at >= ?")
        params.append(start_epoch)
    if end_epoch is not None:
        query_parts.append("AND created_at <= ?")
        params.append(end_epoch)
    # 正典順 (created_at, rowid) で書き出す — 移植先は export 順に INSERT する
    # ため、ここが同一秒内で不定だと移植先の rowid 順 (= 正典 tie-breaker) が
    # 元 DB の履歴順と食い違う。
    query_parts.append("ORDER BY created_at ASC, rowid ASC")

    rows = conn.execute(" ".join(query_parts), params).fetchall()
    messages: List[Dict[str, Any]] = []
    for (mid, role, content, res_id, created_at, meta_raw,
         origin_track_id, line_role, line_id, scope, paired_action_text,
         pulse_id, thought_signature, spell_origin_id, spell_seq,
         source_rowid) in rows:
        meta = None
        if meta_raw:
            try:
                meta = json.loads(meta_raw) if isinstance(meta_raw, str) else meta_raw
            except (json.JSONDecodeError, TypeError):
                meta = None
        msg: Dict[str, Any] = {
            "id": mid,
            "role": role,
            "content": content,
            "resource_id": res_id,
            "created_at": int(created_at) if created_at is not None else None,
            "metadata": meta,
            # 元 DB のグローバル挿入順 (rowid)。import 側が全 thread 横断の
            # 正典順 (created_at, seq) で INSERT し直すための順序キー —
            # thread 単位の並びだけでは同一秒のスレッド交互順序を復元できない。
            "seq": int(source_rowid),
        }
        if origin_track_id is not None:
            msg["origin_track_id"] = origin_track_id
        if line_role is not None:
            msg["line_role"] = line_role
        if line_id is not None:
            msg["line_id"] = line_id
        if scope is not None and scope != "committed":
            msg["scope"] = scope
        if paired_action_text is not None:
            msg["paired_action_text"] = paired_action_text
        if pulse_id is not None:
            msg["pulse_id"] = pulse_id
        if thought_signature is not None:
            msg["thought_signature"] = thought_signature
        if spell_origin_id is not None:
            msg["spell_origin_id"] = spell_origin_id
        if spell_seq is not None:
            msg["spell_seq"] = spell_seq
        messages.append(msg)

    result: Dict[str, Any] = {
        "thread_id": thread_id,
        "resource_id": resource_id,
        "overview": overview,
        "overview_updated_at": overview_updated_at,
        "stelis": stelis_data,
        "messages": messages,
    }
    # title は任意項目 (2026-09-03 追加)。旧形式の読み手は未知キーを無視し、
    # 旧形式のファイルには無いだけなので、FORMAT_VERSION は上げない。
    if title:
        result["title"] = title
    return result


def export_threads_native(
    persona_id: str,
    thread_suffixes: Optional[Iterable[str]] = None,
    start: Optional[str] = None,
    end: Optional[str] = None,
) -> Dict[str, Any]:
    """Export threads to native format dict.

    Args:
        persona_id: Persona identifier.
        thread_suffixes: Thread suffixes or full IDs to export. None = all.
        start: Start ISO timestamp filter (inclusive).
        end: End ISO timestamp filter (inclusive).

    Returns:
        Dict in saiverse_saimemory_v1 format.
    """
    db_path = get_persona_memory_db(persona_id)
    if not db_path.exists():
        raise FileNotFoundError(f"memory.db not found for persona {persona_id}: {db_path}")

    start_epoch = int(datetime.fromisoformat(start).timestamp()) if start else None
    end_epoch = int(datetime.fromisoformat(end).timestamp()) if end else None

    conn = sqlite3.connect(str(db_path))
    try:
        thread_ids = _resolve_thread_ids(conn, persona_id, thread_suffixes)
        threads = [
            _export_thread(conn, tid, start_epoch, end_epoch)
            for tid in thread_ids
        ]
        return {
            "format": FORMAT_VERSION,
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "persona_id": persona_id,
            "threads": threads,
        }
    finally:
        conn.close()


def export_thread_by_id(
    persona_id: str,
    thread_id: str,
) -> Dict[str, Any]:
    """Export a single thread by its full thread_id. Used by API."""
    db_path = get_persona_memory_db(persona_id)
    if not db_path.exists():
        raise FileNotFoundError(f"memory.db not found for persona {persona_id}: {db_path}")

    conn = sqlite3.connect(str(db_path))
    try:
        thread_data = _export_thread(conn, thread_id)
        return {
            "format": FORMAT_VERSION,
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "persona_id": persona_id,
            "threads": [thread_data],
        }
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------

def _validate_native_format(data: Dict[str, Any]) -> None:
    """Validate the native format structure **before any write**.

    replace が「全成功時のみ」であるための第一関門: ここを通った archive は
    書き込み中に形式起因で失敗しない (metadata の JSON 化可能性まで検証する)。
    """
    fmt = data.get("format")
    if fmt != FORMAT_VERSION:
        raise ValueError(
            f"Unsupported format: {fmt!r} (expected {FORMAT_VERSION!r})"
        )
    threads = data.get("threads")
    if not isinstance(threads, list):
        raise ValueError("'threads' must be a list")
    for i, thread in enumerate(threads):
        if not isinstance(thread, dict):
            raise ValueError(f"threads[{i}] must be a dict")
        thread_id = thread.get("thread_id")
        if not thread_id or not isinstance(thread_id, str):
            raise ValueError(f"threads[{i}] missing 'thread_id'")
        messages = thread.get("messages")
        if not isinstance(messages, list):
            raise ValueError(f"threads[{i}] 'messages' must be a list")
        stelis = thread.get("stelis")
        if stelis is not None and not isinstance(stelis, dict):
            raise ValueError(f"threads[{i}] 'stelis' must be a dict or null")
        for j, msg in enumerate(messages):
            if not isinstance(msg, dict):
                raise ValueError(f"threads[{i}].messages[{j}] must be a dict")
            metadata = msg.get("metadata")
            if metadata is not None:
                try:
                    json.dumps(metadata, ensure_ascii=False)
                except (TypeError, ValueError) as exc:
                    raise ValueError(
                        f"threads[{i}].messages[{j}] metadata is not "
                        f"JSON-serializable: {exc}"
                    ) from exc


def _thread_prefix_of(thread_id: str) -> Optional[str]:
    """thread_id の persona prefix (``persona:suffix`` のコロン前) を返す。"""
    return thread_id.split(":", 1)[0] if ":" in thread_id else None


def _ensure_restore_identity(target_persona: str, data: Dict[str, Any]) -> None:
    """復元 (restore) の同一性検証: source/target 不一致を書き込む前に拒否する。

    監査 M4 の根治: 別人格名義の thread/resource が target の人格 DB に
    同居する事故は、警告ではなく拒否で塞ぐ。別 persona へ移すのは明示的な
    移植 (transplant=True) だけ。
    """
    source = data.get("persona_id")
    if source and source != target_persona:
        raise ValueError(
            f"restore rejected: archive persona '{source}' != target "
            f"'{target_persona}'. 別 persona へ移すには transplant "
            "(移植) を明示指定してください。"
        )
    for i, thread in enumerate(data.get("threads", [])):
        prefix = _thread_prefix_of(thread["thread_id"])
        if prefix is not None and prefix != target_persona:
            raise ValueError(
                f"restore rejected: threads[{i}] '{thread['thread_id']}' は "
                f"target '{target_persona}' の thread ではありません。"
            )


def _remap_archive_for_transplant(
    data: Dict[str, Any], target_persona: str
) -> Dict[str, Any]:
    """移植 (transplant): 全 identity を target へ原子的に写像した archive を返す。

    - thread_id / Stelis parent_thread_id: ``source:suffix`` → ``target:suffix``
    - resource_id (thread / message): source persona そのもの → target persona
    - 各 message の ``metadata.transplanted_from`` に元 identity
      (source persona / 元 thread_id) を provenance として保持する
    - 写像できない thread (source prefix を持たない等) が 1 つでもあれば
      archive 全体を拒否する — 部分的に写像された中間状態を作らない

    入力 dict は変更しない (写像済みの deep copy を返す)。
    source == target の場合は写像不要なのでそのまま返す (復元と等価)。
    """
    source = data.get("persona_id")
    if not source:
        raise ValueError(
            "transplant rejected: archive に persona_id がありません "
            "(写像元の persona を特定できない)。"
        )
    if source == target_persona:
        return data

    src_prefix = f"{source}:"

    def _remap_thread_id(thread_id: str, where: str) -> str:
        if not thread_id.startswith(src_prefix):
            raise ValueError(
                f"transplant rejected: {where} '{thread_id}' は source "
                f"'{source}' の prefix を持たず写像できません。"
            )
        return f"{target_persona}:{thread_id[len(src_prefix):]}"

    remapped_threads: List[Dict[str, Any]] = []
    for thread in data.get("threads", []):
        original_thread_id = thread["thread_id"]
        new_thread = dict(thread)
        new_thread["thread_id"] = _remap_thread_id(original_thread_id, "thread")
        if new_thread.get("resource_id") == source:
            new_thread["resource_id"] = target_persona

        stelis = thread.get("stelis")
        if isinstance(stelis, dict):
            new_stelis = dict(stelis)
            parent = new_stelis.get("parent_thread_id")
            if parent:
                new_stelis["parent_thread_id"] = _remap_thread_id(
                    parent, f"stelis parent of '{original_thread_id}'"
                )
            new_thread["stelis"] = new_stelis

        new_messages: List[Dict[str, Any]] = []
        for msg in thread.get("messages", []):
            new_msg = dict(msg)
            if new_msg.get("resource_id") == source:
                new_msg["resource_id"] = target_persona
            metadata = new_msg.get("metadata")
            metadata = dict(metadata) if isinstance(metadata, dict) else {}
            metadata["transplanted_from"] = {
                "persona_id": source,
                "thread_id": original_thread_id,
            }
            new_msg["metadata"] = metadata
            new_messages.append(new_msg)
        new_thread["messages"] = new_messages
        remapped_threads.append(new_thread)

    remapped = dict(data)
    remapped["persona_id"] = target_persona
    remapped["threads"] = remapped_threads
    return remapped


def _import_stelis(conn: sqlite3.Connection, thread_id: str, stelis_data: Dict[str, Any]) -> None:
    """Restore Stelis thread info (呼び出し元のトランザクションに参加、commit しない)。"""
    # Delete existing stelis record if any
    conn.execute("DELETE FROM stelis_threads WHERE thread_id=?", (thread_id,))

    conn.execute(
        """
        INSERT INTO stelis_threads
        (thread_id, parent_thread_id, depth, window_ratio, status,
         chronicle_prompt, chronicle_summary, created_at, completed_at, label)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            thread_id,
            stelis_data.get("parent_thread_id"),
            stelis_data.get("depth", 0),
            stelis_data.get("window_ratio", 0.8),
            stelis_data.get("status", "active"),
            stelis_data.get("chronicle_prompt"),
            stelis_data.get("chronicle_summary"),
            stelis_data.get("created_at"),
            stelis_data.get("completed_at"),
            stelis_data.get("label"),
        ),
    )


def _delete_thread_in_txn(conn: sqlite3.Connection, thread_id: str) -> None:
    """既存 thread の全行削除 (呼び出し元のトランザクションに参加、commit しない)。

    storage.delete_thread と同じ削除対象 (embeddings / messages / stelis /
    thread 行) だが、あちらは内部 commit + 例外握り潰しを持つため replace の
    原子性には使えない。
    """
    msg_ids = [
        row[0] for row in conn.execute(
            "SELECT id FROM messages WHERE thread_id=?", (thread_id,)
        ).fetchall()
    ]
    if msg_ids:
        placeholders = ",".join("?" * len(msg_ids))
        conn.execute(
            f"DELETE FROM message_embeddings WHERE message_id IN ({placeholders})",
            msg_ids,
        )
        conn.execute(
            f"DELETE FROM embeddings WHERE message_id IN ({placeholders})",
            msg_ids,
        )
        conn.execute("DELETE FROM messages WHERE thread_id=?", (thread_id,))
    conn.execute("DELETE FROM stelis_threads WHERE thread_id=?", (thread_id,))
    conn.execute("DELETE FROM threads WHERE id=?", (thread_id,))


def _write_thread_scaffold_in_txn(
    conn: sqlite3.Connection,
    thread_data: Dict[str, Any],
    *,
    replace: bool,
) -> None:
    """thread 1 本ぶんの器 (replace 削除 / thread 行 / overview / Stelis) を書く。

    呼び出し元のトランザクションに参加し、commit しない。message 行は書かない —
    それは全 thread 横断フェーズ (:func:`_canonical_import_order` の順で
    :func:`_insert_message_in_txn`) の仕事。thread 単位で message まで書くと、
    同一秒にスレッド交互で記録された履歴のグローバル正典順 (created_at, rowid)
    を移植先で復元できない (Codex W8 レビュー P2)。
    """
    thread_id = thread_data["thread_id"]
    resource_id = thread_data.get("resource_id")

    if replace:
        _delete_thread_in_txn(conn, thread_id)

    # thread 行 (storage.get_or_create_thread 相当。内部 commit を避けて inline)
    row = conn.execute(
        "SELECT id FROM threads WHERE id=?", (thread_id,)
    ).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO threads(id, resource_id, overview, overview_updated_at) "
            "VALUES (?, ?, ?, ?)",
            (thread_id, resource_id, None, None),
        )

    overview = thread_data.get("overview")
    if overview:
        conn.execute(
            "UPDATE threads SET overview=?, overview_updated_at=? WHERE id=?",
            (overview, thread_data.get("overview_updated_at"), thread_id),
        )

    title = thread_data.get("title")
    if isinstance(title, str) and title.strip():
        conn.execute(
            "UPDATE threads SET title=? WHERE id=?",
            (title.strip(), thread_id),
        )

    stelis = thread_data.get("stelis")
    if stelis and isinstance(stelis, dict):
        _import_stelis(conn, thread_id, stelis)


def _canonical_import_order(
    threads: List[Dict[str, Any]],
) -> List[Tuple[str, Optional[str], Dict[str, Any]]]:
    """全 thread の message を正典順 (created_at, seq) に並べて返す。

    ``seq`` は export 元 DB の rowid (グローバル挿入順)。INSERT をこの順で
    行うことで、移植先の rowid tie-breaker が元 DB の同一秒スレッド交互
    順序まで保存する。``seq`` を持たない旧形式 archive は archive 内の
    出現順 (thread 逐次) を tie-breaker にする — 旧 import と同じ並びで、
    後退はない。created_at 欠落行は SQLite の NULL 並び (先頭側) に合わせる。
    """
    staged: List[
        Tuple[Tuple[Tuple[int, int], Tuple[int, int]], str, Optional[str], Dict[str, Any]]
    ] = []
    pos = 0
    for thread_data in threads:
        thread_id = thread_data["thread_id"]
        resource_id = thread_data.get("resource_id")
        for msg in thread_data.get("messages", []):
            try:
                ca_key = (1, int(msg.get("created_at")))
            except (TypeError, ValueError):
                ca_key = (0, 0)
            try:
                seq_key = (0, int(msg.get("seq")))
            except (TypeError, ValueError):
                seq_key = (1, pos)
            staged.append(((ca_key, seq_key), thread_id, resource_id, msg))
            pos += 1
    staged.sort(key=lambda entry: entry[0])
    return [(t, r, m) for _, t, r, m in staged]


def _insert_message_in_txn(
    conn: sqlite3.Connection,
    thread_id: str,
    thread_resource_id: Optional[str],
    msg: Dict[str, Any],
    explicit_rowid: Optional[int] = None,
) -> Tuple[str, str]:
    """message 1 行を書く (呼び出し元のトランザクションに参加、commit しない)。

    ``explicit_rowid`` 指定時は元 DB の rowid そのままで挿入する (呼び出し元が
    空きを検証済みの場合のみ)。保持スレッドの既存行と同一秒で交互だった履歴の
    相対順まで復元するにはこれが必要 — 追記挿入では再挿入行が必ず既存行より
    後の rowid になる。

    Returns:
        書き込んだ ``(message_id, content)`` (embedding 派生工程用)。
    """
    msg_id = msg.get("id") or str(uuid.uuid4())
    content = msg.get("content", "")
    metadata = msg.get("metadata")
    meta_json = json.dumps(metadata, ensure_ascii=False) if metadata else None
    rowid_col = "rowid, " if explicit_rowid is not None else ""
    rowid_ph = "?, " if explicit_rowid is not None else ""
    rowid_val: Tuple[Any, ...] = (
        (explicit_rowid,) if explicit_rowid is not None else ()
    )
    conn.execute(
        "INSERT OR REPLACE INTO messages"
        f"({rowid_col}id, thread_id, role, content, resource_id, created_at, metadata,"
        " origin_track_id, line_role, line_id, scope, paired_action_text,"
        " pulse_id, thought_signature, spell_origin_id, spell_seq)"
        f" VALUES ({rowid_ph}?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            *rowid_val,
            msg_id, thread_id, msg.get("role", "user"), content,
            msg.get("resource_id", thread_resource_id), msg.get("created_at"),
            meta_json, msg.get("origin_track_id"), msg.get("line_role"),
            msg.get("line_id"), msg.get("scope") or "committed",
            msg.get("paired_action_text"), msg.get("pulse_id"),
            msg.get("thought_signature"), msg.get("spell_origin_id"),
            msg.get("spell_seq"),
        ),
    )
    return (msg_id, content)


def _seq_rowids_if_free(
    conn: sqlite3.Connection,
    ordered: List[Tuple[str, Optional[str], Dict[str, Any]]],
) -> Optional[Dict[int, int]]:
    """archive の seq (元 rowid) を明示 rowid として使えるか検査する。

    全 message が相異なる正の int seq を持ち、かつそれらの rowid が移植先で
    全て空いている (replace 削除後の同一トランザクション内で判定) ときだけ、
    {message 位置: rowid} を返す。一つでも欠け・重複・衝突があれば None —
    その場合は追記挿入 (archive 内の正典順は保存、保持行との相対順は
    ベストエフォート) に全件フォールバックする。部分的な明示 rowid 挿入は
    中途半端な並びを作るのでやらない。
    """
    seqs: List[int] = []
    for _, _, msg in ordered:
        seq = msg.get("seq")
        if not isinstance(seq, int) or isinstance(seq, bool) or seq <= 0:
            return None
        seqs.append(seq)
    if len(set(seqs)) != len(seqs):
        return None
    chunk = 500
    for i in range(0, len(seqs), chunk):
        part = seqs[i:i + chunk]
        placeholders = ",".join("?" for _ in part)
        (occupied,) = conn.execute(
            f"SELECT COUNT(*) FROM messages WHERE rowid IN ({placeholders})",
            part,
        ).fetchone()
        if occupied:
            return None
    return {idx: seq for idx, seq in enumerate(seqs)}


def import_threads_native(
    persona_id: str,
    data: Dict[str, Any],
    *,
    replace: bool = True,
    skip_embed: bool = False,
    progress_callback: Optional[Callable[[int, int, str], None]] = None,
    transplant: bool = False,
) -> Dict[str, Any]:
    """Import threads from native format dict.

    意味論 (監査 M4/M5/M6 対応、モジュール docstring 参照):

    - ``transplant=False`` (復元): archive の persona_id / thread prefix が
      target と一致しなければ**書き込む前に** ValueError で拒否する。
    - ``transplant=True`` (移植): 全 identity を target へ写像し、元 identity
      を各 message の ``metadata.transplanted_from`` に保持する。
    - 書き込みは**単一トランザクション**: 全 thread の全行が成功した時に
      一度だけ commit する。途中失敗は rollback され、既存 target は不変。
    - embedding は commit 後の派生工程 (失敗しても import 成否に関与しない。
      結果は戻り値 ``embeddings`` で報告する)。

    Args:
        persona_id: Target persona ID.
        data: Parsed JSON dict in saiverse_saimemory_v1 format.
        replace: If True, delete existing thread before import.
        skip_embed: If True, skip embedding generation.
        progress_callback: Called with (current, total, message).
        transplant: 別 persona の archive を明示的に移植する。

    Returns:
        Dict with import summary:
        {"threads_imported", "messages_imported", "embeddings"}.
        ``embeddings`` は "generated" / "skipped" / "failed" のいずれか。
    """
    # --- 関門 1: 書き込みゼロの検証区間 (形式 + identity) ---
    _validate_native_format(data)
    if transplant:
        data = _remap_archive_for_transplant(data, persona_id)
    else:
        _ensure_restore_identity(persona_id, data)

    db_path = get_persona_memory_db(persona_id)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = init_db(str(db_path), check_same_thread=True)

    total_messages = sum(len(t.get("messages", [])) for t in data["threads"])
    imported = 0
    threads_imported = 0
    written_messages: List[Tuple[str, str]] = []

    try:
        # --- 関門 2: 単一トランザクションの書き込み区間 ---
        # ここから成功時の commit まで、他の commit 点は存在しない
        # (_write_*_in_txn / _insert_message_in_txn は commit しない契約)。
        # init_db がトランザクションを開いたまま返す場合は二重 BEGIN を
        # 避けて合流する。
        if not conn.in_transaction:
            conn.execute("BEGIN")
        try:
            # 第一段: 器 (replace 削除 / thread 行 / overview / Stelis)
            for thread_data in data["threads"]:
                if progress_callback:
                    progress_callback(
                        imported, total_messages,
                        f"Processing thread: {thread_data['thread_id']}",
                    )
                _write_thread_scaffold_in_txn(conn, thread_data, replace=replace)
                threads_imported += 1
            # 第二段: message を全 thread 横断の正典順 (created_at, seq) で
            # INSERT する — thread 逐次挿入は同一秒のスレッド交互順序を失う
            # (Codex W8 レビュー P2)。seq の元 rowid が移植先で全て空いている
            # とき (全 thread 復元・部分往復・空 DB への移植) は明示 rowid で
            # 挿入し、保持スレッドの既存行との同秒相対順まで完全復元する
            # (同三巡目 P2)。衝突があれば追記順に全件フォールバック。
            ordered = _canonical_import_order(data["threads"])
            explicit_rowids = _seq_rowids_if_free(conn, ordered)
            for idx, (thread_id, resource_id, msg) in enumerate(ordered):
                written_messages.append(
                    _insert_message_in_txn(
                        conn, thread_id, resource_id, msg,
                        explicit_rowid=(
                            explicit_rowids[idx] if explicit_rowids else None
                        ),
                    )
                )
                imported += 1
                if progress_callback and imported % 200 == 0:
                    progress_callback(
                        imported, total_messages,
                        f"Staged {imported}/{total_messages} messages",
                    )
            conn.commit()
        except Exception:
            conn.rollback()
            raise

        # --- commit 後: 再構築可能な派生工程 (import の成否には関与しない) ---
        embeddings_status = "skipped"
        if not skip_embed and written_messages:
            if progress_callback:
                progress_callback(imported, total_messages, "Generating embeddings...")
            ok = _regenerate_embeddings(
                conn, written_messages, progress_callback, imported, total_messages
            )
            embeddings_status = "generated" if ok else "failed"

        if progress_callback:
            progress_callback(total_messages, total_messages, "Import complete")

        return {
            "threads_imported": threads_imported,
            "messages_imported": imported,
            "embeddings": embeddings_status,
        }
    finally:
        conn.close()


def _regenerate_embeddings(
    conn: sqlite3.Connection,
    written_messages: List[Tuple[str, str]],
    progress_callback: Optional[Callable[[int, int, str], None]],
    base_progress: int,
    total: int,
) -> bool:
    """import 済み message の embedding を再生成する (commit 後の派生工程)。

    監査 M6 対応: persona ID を thread ID から推測して SAIMemoryAdapter を
    新規作成しない (別 persona の memory.db / persona directory を生成し得た)。
    embedder は設定から**非生成的に**構築し、書き込み先は呼び出し元が開いた
    target conn だけに閉じる。

    Returns: 全件成功で True。失敗は WARNING に落として False (再構築可能)。
    """
    try:
        from sai_memory.config import load_settings
        from sai_memory.memory.chunking import chunk_text
        from sai_memory.memory.recall import Embedder
        from sai_memory.memory.storage import replace_message_embeddings

        settings = load_settings()
        try:
            embedder = Embedder(
                model=settings.embed_model,
                local_model_path=settings.embed_model_path,
                model_dim=settings.embed_model_dim,
            )
        except Exception:
            LOGGER.warning(
                "Embedder not available, skipping embedding generation",
                exc_info=True,
            )
            return False

        count = 0
        for msg_id, content in written_messages:
            if content and content.strip():
                chunks = chunk_text(
                    content.strip(),
                    min_chars=settings.chunk_min_chars,
                    max_chars=settings.chunk_max_chars,
                )
                payload = [c.strip() for c in chunks if c and c.strip()]
                if payload:
                    vectors = embedder.embed(payload, is_query=False)
                    replace_message_embeddings(conn, msg_id, vectors)
            count += 1
            if progress_callback and count % 20 == 0:
                progress_callback(
                    base_progress + count, total + base_progress,
                    f"Embedding {count} messages...",
                )
        return True
    except Exception as exc:
        LOGGER.warning("Failed to regenerate embeddings: %s", exc, exc_info=True)
        return False
