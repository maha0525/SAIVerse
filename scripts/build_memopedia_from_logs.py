"""Build Memopedia knowledge base from existing chat logs.

Processes messages from oldest to newest, extracting entities and their
knowledge in batches, then reflecting them directly to Memopedia pages.

Usage:
    python scripts/build_memopedia_from_logs.py <persona_id> [options]

Examples:
    # Full run with defaults
    python scripts/build_memopedia_from_logs.py eris_city_a

    # Dry run (show what would be extracted, no DB writes)
    python scripts/build_memopedia_from_logs.py eris_city_a --dry-run

    # Process only first 500 messages
    python scripts/build_memopedia_from_logs.py eris_city_a --limit 500

    # Resume from a specific timestamp
    python scripts/build_memopedia_from_logs.py eris_city_a --start-after 1711900000
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

#: 再構築を打ち切る連続失敗数 (API 側の _MAX_CONSECUTIVE_BATCH_FAILURES と同値)。
MAX_CONSECUTIVE_BATCH_FAILURES = 3


def _init_db(persona_id: str, *, dry_run: bool = False):
    """接続を開く。下見 (--dry-run) では**読み取り専用**で開く。

    通常の ``init_db`` はテーブルの用意・列の追加・既存行の補完まで行う書き込み。
    下見の約束は「データを一切変更しない」なので、下見では SQLite の read-only
    接続にして、書ける口をそもそも与えない (Codex 六巡 #1 / 七巡 #1)。
    """
    import sqlite3

    from sai_memory.memory.storage import init_db
    from sai_memory.arasuji import init_arasuji_tables

    db_path = Path(os.getenv("SAIVERSE_HOME") or Path.home() / ".saiverse") / "personas" / persona_id / "memory.db"
    if not db_path.exists():
        print(f"Database not found: {db_path}")
        sys.exit(1)

    if dry_run:
        uri = f"file:{db_path.as_posix()}?mode=ro"
        return sqlite3.connect(uri, uri=True, check_same_thread=False)

    conn = init_db(str(db_path), check_same_thread=False)
    init_arasuji_tables(conn)
    return conn


def _init_llm(model_name: str = None):
    from saiverse.model_defaults import BUILTIN_DEFAULT_LITE_MODEL
    from saiverse.model_configs import find_model_config
    from llm_clients.factory import get_llm_client

    model_name = model_name or os.getenv("MEMORY_WEAVE_MODEL", BUILTIN_DEFAULT_LITE_MODEL)
    resolved_model_id, model_config = find_model_config(model_name)
    if not resolved_model_id:
        print(f"Model '{model_name}' not found")
        sys.exit(1)

    provider = model_config.get("provider", "gemini")
    context_length = model_config.get("context_length", 128000)
    client = get_llm_client(resolved_model_id, provider, context_length, config=model_config)
    print(f"Using model: {model_config.get('model', resolved_model_id)} / {provider}")
    return client


def _init_memopedia(conn, *, dry_run: bool = False):
    """Memopedia を開く。下見では ``None`` を返す (コンストラクタが書くため)。

    ``Memopedia.__init__`` はテーブルの用意と root ページの投入を行う =
    書き込み。下見では作らず、既存ページ一覧は :func:`_dry_run_page_list` が
    読み取りだけで組む。
    """
    from sai_memory.memopedia import Memopedia, init_memopedia_tables

    if dry_run:
        return None

    init_memopedia_tables(conn)
    return Memopedia(conn)


def _dry_run_page_list(conn) -> str:
    """下見用の既存ページ一覧 (読み取りだけ)。

    本実行と同じ材料をプロンプトへ渡すため、下見でも既存ページは見せる。
    テーブルがまだ無い DB だけが空一覧。それ以外の読み取り失敗は下見を止める
    —— 読めていない一覧で抽出を走らせると、実際とは違う下見を見せたうえに
    LLM の課金だけ発生する (Codex 八巡 #8)。
    """
    import sqlite3

    from sai_memory.memopedia.storage import build_tree, category_keys

    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'memopedia_pages'"
    ).fetchone()
    if not exists:
        print("(下見: Memopedia がまだ無い DB のため、既存ページ一覧なしで進めます)")
        return ""
    try:
        tree = build_tree(conn)
    except sqlite3.Error as exc:
        print(f"既存ページ一覧を読めませんでした: {exc}")
        print("(下見を中止します — 読めていない一覧で抽出すると、実際と違う結果を見せます)")
        sys.exit(1)
    lines = []
    for category_key in category_keys("extractable"):
        pages = tree.get(category_key, [])
        if not pages:
            continue
        lines.append(f"[{category_key}]")
        for page in pages:
            lines.append(f"  - {page.title}")
            for child in page.children:
                lines.append(f"    - {child.title}")
    return "\n".join(lines)


def _fetch_messages(
    conn, *, limit: int = 0, start_after: float = 0, start_after_rowid: int = 0,
):
    """再開位置より先のメッセージを取る。位置は (時刻, 行番号) の組。

    時刻だけだと同じ秒のメッセージの順序を表せない —— 「その時刻より後」だと
    同秒の行を取りこぼし、「その時刻から」だと同じバッチを何度も処理し続ける
    (API 側と同じ設計、Codex 四巡 #1)。

    Returns:
        ``(messages, rowid_of)``。``rowid_of`` はメッセージ id → 行番号。
    """
    from sai_memory.memory.storage import Message

    query = """
        SELECT rowid, id, thread_id, role, content, resource_id, created_at, metadata
        FROM messages
        WHERE thread_id NOT IN (SELECT thread_id FROM stelis_threads)
    """
    params = []

    if start_after > 0:
        query += " AND (created_at > ? OR (created_at = ? AND rowid > ?))"
        params.extend([start_after, start_after, start_after_rowid])

    query += " ORDER BY created_at ASC, rowid ASC"

    if limit > 0:
        query += " LIMIT ?"
        params.append(limit)

    cur = conn.execute(query, params)

    messages = []
    rowid_of = {}
    for row in cur.fetchall():
        row_id, msg_id, tid, role, content, resource_id, created_at, metadata_raw = row
        metadata = {}
        if metadata_raw:
            try:
                metadata = json.loads(metadata_raw)
            except Exception:
                pass
        rowid_of[msg_id] = row_id
        messages.append(Message(
            id=msg_id, thread_id=tid, role=role, content=content,
            resource_id=resource_id, created_at=created_at, metadata=metadata,
        ))

    return messages, rowid_of


def main():
    parser = argparse.ArgumentParser(
        description="Build Memopedia knowledge base from chat logs",
    )
    parser.add_argument("persona_id", help="Persona ID (e.g., eris_city_a)")
    parser.add_argument("--limit", type=int, default=0, help="Max messages to process (0=all)")
    parser.add_argument("--batch-size", type=int, default=20, help="Messages per extraction batch (default: 20)")
    parser.add_argument("--model", type=str, default=None, help="Model to use for extraction")
    parser.add_argument("--start-after", type=float, default=0, help="Process messages after this timestamp (for resuming)")
    parser.add_argument(
        "--start-after-rowid", type=int, default=0,
        help="Row number to pair with --start-after (同じ秒のメッセージの順序を"
             "表すため。前回の実行が最後に出力した値をそのまま渡す)",
    )
    parser.add_argument("--dry-run", action="store_true", help="Preview only, no DB writes")
    args = parser.parse_args()

    conn = _init_db(args.persona_id, dry_run=args.dry_run)
    client = _init_llm(args.model)
    memopedia = _init_memopedia(conn, dry_run=args.dry_run)

    messages, rowid_of = _fetch_messages(
        conn, limit=args.limit, start_after=args.start_after,
        start_after_rowid=args.start_after_rowid,
    )
    if not messages:
        print("No messages found")
        conn.close()
        return

    print(f"Found {len(messages)} messages to process")
    print(f"  Oldest: {time.strftime('%Y-%m-%d %H:%M', time.localtime(messages[0].created_at))}")
    print(f"  Newest: {time.strftime('%Y-%m-%d %H:%M', time.localtime(messages[-1].created_at))}")
    print(f"  Batch size: {args.batch_size}")
    print()

    from sai_memory.memory.entity_extractor import (
        ExtractionFailed,
        extract_entities,
        reflect_to_memopedia,
        _format_page_list,
    )
    from sai_memory.arasuji.context import get_episode_context_for_timerange

    total_entities = 0
    total_notes = 0
    total_new_pages = 0
    total_updated_pages = 0
    total_deduped = 0
    batch_count = 0
    failed_batches = 0
    consecutive_failures = 0
    processed_messages = 0
    skipped_messages = 0
    # 次回の再開位置 (時刻, 行番号)。「連続して成功したところまで」で止める ——
    # 先へ進めると、失敗した範囲が次回の取得対象から外れ、二度と拾えない
    checkpoint_ts = args.start_after
    checkpoint_rowid = args.start_after_rowid

    for i in range(0, len(messages), args.batch_size):
        batch = messages[i:i + args.batch_size]
        # 小さすぎる末尾は次回に回す。ただし**先頭のバッチは飛ばさない** ——
        # 飛ばすと再開位置が一歩も進まず、同じ範囲を毎回取り直す (API 側と同じ条件)
        if len(batch) < args.batch_size // 2 and i > 0:
            print(f"  Skipping small final batch ({len(batch)} messages)")
            skipped_messages += len(batch)
            continue

        batch_count += 1
        start_time = min(m.created_at for m in batch)
        end_time = max(m.created_at for m in batch)
        time_str = time.strftime("%Y-%m-%d %H:%M", time.localtime(start_time))

        # Episode context
        ep_ctx = ""
        try:
            ep_ctx = get_episode_context_for_timerange(
                conn, start_time=start_time, end_time=end_time, max_entries=10,
            )
        except Exception:
            pass

        # Existing pages (refreshed each batch to include newly created pages)
        existing_pages = (
            _format_page_list(memopedia) if memopedia
            else _dry_run_page_list(conn)
        )

        print(f"[Batch {batch_count}] {time_str} | msgs {i+1}-{i+len(batch)}/{len(messages)}", end="")

        # 抽出と反映を**ひとつの try** で捕まえる。反映側 (DB ロック / スキーマ)
        # の例外が外へ抜けると、そのバッチだけでなく再構築全体が止まる
        # (Codex 六巡 #3)
        try:
            entities = extract_entities(
                client, batch,
                episode_context=ep_ctx,
                existing_pages=existing_pages,
                persona_id=args.persona_id,
            )

            if entities:
                print(f" → {len(entities)} entities")
                for ent in entities:
                    print(f"    [{ent.category}] {ent.name}:")
                    for note in ent.notes:
                        print(f"      - {note}")
            else:
                print(" → 0 entities")

            if entities and not args.dry_run:
                results = reflect_to_memopedia(
                    entities, memopedia,
                    source_time=int(end_time),
                )
                for r in results:
                    status = "NEW" if r.is_new_page else "UPDATE"
                    print(f"    → [{status}] {r.entity_name} ({r.notes_appended} notes)")
                total_new_pages += sum(1 for r in results if r.is_new_page)
                total_updated_pages += sum(1 for r in results if not r.is_new_page)
                total_deduped += sum(r.notes_deduped for r in results)
        except Exception as exc:
            # 一つのバッチの失敗で全体を止めない。ただし数えて最後に出す。
            # 再開位置はここで止める (先へ進めると、この範囲は次回に取得され
            # ないため二度と拾えない)
            failed_batches += 1
            consecutive_failures += 1
            print(f" → このバッチは失敗しました: {exc}")
            if consecutive_failures >= MAX_CONSECUTIVE_BATCH_FAILURES:
                # たまたま落ちた 1 バッチと、DB が壊れている状態は別物。
                # 続けて落ちるなら後者なので、途中経過の顔で終わらせずに止める
                print(
                    f"\n{consecutive_failures} バッチ続けて失敗したため中断しました"
                    "（データベースかモデルの側に継続的な問題があります）"
                )
                break
            continue

        consecutive_failures = 0
        processed_messages += len(batch)
        # 再開位置を進めるのは**反映まで終わってから**。抽出だけ済んだ時点で
        # 進めると、反映で落ちた範囲が次回に取得されない
        if failed_batches == 0 and not args.dry_run:
            checkpoint_ts = batch[-1].created_at
            checkpoint_rowid = rowid_of.get(batch[-1].id, checkpoint_rowid)

        total_entities += len(entities)
        total_notes += sum(len(e.notes) for e in entities)

    # Summary
    print(f"\n{'='*60}")
    print("Done!")
    print(f"  Messages processed: {processed_messages} (in {batch_count - failed_batches} batches)")
    if skipped_messages:
        print(f"  Messages skipped (small final batch): {skipped_messages}")
    print(f"  Entities found: {total_entities}")
    print(f"  Notes extracted: {total_notes}")
    if failed_batches:
        print(f"  Batches that failed extraction: {failed_batches}")
    if not args.dry_run:
        print(f"  New pages created: {total_new_pages}")
        print(f"  Existing pages updated: {total_updated_pages}")
        if total_deduped:
            print(f"  Notes already recorded (not duplicated): {total_deduped}")
    if args.dry_run:
        # 下見は何も反映していない。再開位置を進めて案内すると、下見した範囲を
        # そのまま読み飛ばす本実行を勧めることになる (Codex 六巡 #1)
        print("  (dry-run mode, nothing saved — 再開位置も進めていません)")
    elif messages:
        # 再開位置は「実際に反映したところまで」。失敗した範囲も、小さすぎて
        # 飛ばした末尾も、再開位置の向こう側へ置き去りにしない
        print(f"  Last message timestamp: {messages[-1].created_at}")
        resume = f"--start-after {checkpoint_ts} --start-after-rowid {checkpoint_rowid}"
        if failed_batches:
            print(f"  (Use {resume} to resume — 最初の失敗の手前から。失敗した範囲をやり直します)")
        else:
            print(f"  (Use {resume} to resume)")
    print(f"{'='*60}")

    conn.close()


if __name__ == "__main__":
    main()
