"""実験 3: JSON エクスポート → 移植インポート (別 ID) / 復元インポート (同一 ID)。

- `saiverse_memory.native_export.export_threads_native` / `import_threads_native`
  を実際に呼ぶ。
- 移植 (transplant=True) と 同一 ID への復元 (transplant=False) の差を記録する。
- A の memory.db の全テーブル行数と、import 後の移植先の全テーブル行数を突き合わせ、
  **運ばれなかったテーブルを名指しする**。
- skip_embed=True (埋め込みは作らない)。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from probe_env import guard_not_production, setup_home, table_counts  # noqa: E402

HOME = setup_home("exp3")
guard_not_production()

from saiverse_memory.adapter import SAIMemoryAdapter  # noqa: E402
from saiverse_memory.native_export import (  # noqa: E402
    export_threads_native,
    import_threads_native,
)
from sai_memory.core_memory import add_core_memory, list_core_memories  # noqa: E402

A = "probe_a_testcity"
C = "probe_c_testcity"   # 移植先 (別 ID)
BUILDING = "probe_building_hall"

OUT: dict = {"home": str(HOME)}


def make_adapter(pid: str) -> SAIMemoryAdapter:
    return SAIMemoryAdapter(
        persona_id=pid,
        persona_dir=HOME / "personas" / pid,
        resource_id=pid,
        startup_backup=False,
        recover_orphaned_thread=False,
    )


def seed_a() -> dict:
    a = make_adapter(A)
    info = {}
    a.append_persona_message({
        "role": "user", "content": "PROBE-A-PERSONA-1 まはーが工房の棚を作り直した話",
        "metadata": {"tags": ["conversation"]}, "embedding_chunks": 0})
    a.append_persona_message({
        "role": "assistant", "content": "PROBE-A-PERSONA-2 棚は三段で、上段に定規を置くことにした",
        "metadata": {"tags": ["conversation"]}, "embedding_chunks": 0})
    a.append_building_message(BUILDING, {
        "role": "user", "content": "PROBE-A-BUILDING-1 今日の天気はどうだった",
        "metadata": {"tags": ["conversation"]}, "embedding_chunks": 0})
    a.append_building_message(BUILDING, {
        "role": "assistant", "content": "PROBE-A-BUILDING-2 午後から晴れて、窓から光が入った",
        "metadata": {"tags": ["conversation"]}, "embedding_chunks": 0})
    a.append_persona_message({
        "role": "assistant", "content": "PROBE-A-NAMED-1 別スレッドの記録",
        "metadata": {"tags": ["conversation"]}, "embedding_chunks": 0},
        thread_suffix="probe_named_thread")
    a.set_thread_title("probe_named_thread", "プローブ用の名前つきスレッド")

    # コア記憶 (memopedia_pages)
    info["core_id"] = add_core_memory(
        a.conn, "PROBE-A-CORE-1 まはーの誕生日は 1 月 14 日", kind="note")

    # 手帳ページ (memopedia)
    from sai_memory.memopedia.storage import create_page
    page = create_page(a.conn, parent_id=None, title="PROBE-A-PAGE タイトル",
                       content="PROBE-A-PAGE-1 手帳のページ本文",
                       category="note", is_trunk=False)
    info["page_id"] = page.id

    # 作業記憶 (working_memory)
    a.save_working_memory({"probe": "PROBE-A-WORKING-1"})

    # 知覚バッファ / 机 / クリップ / メモ など「相乗り」テーブルにも 1 行ずつ置く
    from sai_memory.desk import open_item
    try:
        open_item(a.conn, f"memopedia:{page.id}")
        info["desk"] = "ok"
    except Exception as exc:  # noqa: BLE001
        info["desk"] = f"skipped: {exc}"

    seeded: dict = {}
    msg_ids = [r[0] for r in a.conn.execute(
        "SELECT id FROM messages ORDER BY rowid")]
    info["message_ids"] = msg_ids

    # Chronicle (arasuji) エントリ — memopedia_pages 上
    try:
        from sai_memory.arasuji.storage import create_entry
        e = create_entry(
            a.conn, level=1, content="PROBE-A-CHRONICLE-1 あらすじ本文",
            source_ids=msg_ids[:2], source_count=2, message_count=2)
        seeded["arasuji_entry"] = e.id
    except Exception as exc:  # noqa: BLE001
        seeded["arasuji_entry"] = f"skipped: {exc}"

    # クリップ (土地参照)
    try:
        from sai_memory.clips import add_clip
        c = add_clip(a.conn, message_id=msg_ids[0], quote="PROBE-A-PERSONA-1")
        seeded["clip"] = c.clip_id
    except Exception as exc:  # noqa: BLE001
        seeded["clip"] = f"skipped: {exc}"

    # 目的タグ
    try:
        seeded["purpose_tag"] = a.add_purpose_tag(
            f"message:{msg_ids[0]}", "purpose:probe", 2)
    except Exception as exc:  # noqa: BLE001
        seeded["purpose_tag"] = f"skipped: {exc}"

    # 覚え書き (memory_notes)
    try:
        notes = a.add_memory_notes(["PROBE-A-NOTE-1 覚え書き"])
        seeded["memory_notes"] = len(notes)
    except Exception as exc:  # noqa: BLE001
        seeded["memory_notes"] = f"skipped: {exc}"

    # Pulse ログ
    try:
        seeded["pulse_log"] = a.append_pulse_log(
            "probe-pulse-1", f"{A}:__persona__", "assistant",
            "PROBE-A-PULSELOG-1 内省のログ")
    except Exception as exc:  # noqa: BLE001
        seeded["pulse_log"] = f"skipped: {exc}"

    # 知覚バッファ
    try:
        seeded["perception"] = a.push_perception(
            kind="probe", content="PROBE-A-PERCEPTION-1")
    except Exception as exc:  # noqa: BLE001
        seeded["perception"] = f"skipped: {exc}"

    # スルース未通過区間
    try:
        from sai_memory.memory.storage import record_sluice_skipped_span
        record_sluice_skipped_span(a.conn, msg_ids[0], msg_ids[1])
        seeded["sluice_skipped_span"] = "ok"
    except Exception as exc:  # noqa: BLE001
        seeded["sluice_skipped_span"] = f"skipped: {exc}"

    info["extra_seeded"] = seeded

    # 現在のスレッド (active_state.json)
    a.set_active_thread(f"{A}:probe_named_thread")

    a.conn.commit()
    info["threads"] = [r[0] for r in a.conn.execute("SELECT id FROM threads ORDER BY id")]
    a.close()
    return info


def read_as(pid: str, label: str, source_threads: list) -> dict:
    ad = make_adapter(pid)
    res = {
        "recent_persona_messages": {
            "thread_id_queried": ad._thread_id(None),
            "count": len(ad.recent_persona_messages(100000)),
            "contents": [m["content"][:50] for m in ad.recent_persona_messages(100000)],
        },
        "recent_messages(building)": {
            "thread_id_queried": ad._thread_id(BUILDING),
            "contents": [m["content"][:50] for m in ad.recent_messages(BUILDING, 100000)],
        },
        "list_thread_summaries": [
            {"thread_id": s["thread_id"], "title": s["title"],
             "message_count": s["message_count"]} for s in ad.list_thread_summaries()
        ],
        "list_core_memories": [c.content[:50] for c in list_core_memories(ad.conn)],
        "working_memory": ad.load_working_memory(),
        "memopedia_page_titles": [
            r[0] for r in ad.conn.execute(
                "SELECT title FROM memopedia_pages ORDER BY id")
        ],
        "transplanted_from_sample": [
            r[0] for r in ad.conn.execute(
                "SELECT metadata FROM messages WHERE metadata LIKE '%transplanted_from%' LIMIT 2")
        ],
    }
    ad.close()
    return {label: res}


def main() -> None:
    OUT["A_seed"] = seed_a()
    a_db = HOME / "personas" / A / "memory.db"
    OUT["A_tables"] = table_counts(a_db)

    # --- export ---
    archive = export_threads_native(A)
    arc_path = Path(__file__).with_name("exp3_archive_A.json")
    arc_path.write_text(json.dumps(archive, ensure_ascii=False, indent=2), encoding="utf-8")
    OUT["export"] = {
        "path": str(arc_path),
        "top_level_keys": sorted(archive.keys()),
        "thread_count": len(archive["threads"]),
        "thread_ids": [t["thread_id"] for t in archive["threads"]],
        "per_thread_keys": sorted(archive["threads"][0].keys()),
        "message_count": sum(len(t["messages"]) for t in archive["threads"]),
        "size_bytes": arc_path.stat().st_size,
    }

    # --- 別 ID への移植 (transplant=True) ---
    # 移植先は「新規に作ったペルソナ」を想定 — adapter を一度作って空 DB を用意する
    c = make_adapter(C)
    c.append_persona_message({
        "role": "assistant", "content": "PROBE-C-OWN-1 C 自身の記憶",
        "metadata": {"tags": ["conversation"]}, "embedding_chunks": 0})
    c.conn.commit()
    c.close()
    OUT["C_before_tables"] = table_counts(HOME / "personas" / C / "memory.db")

    # 復元 (transplant=False) を別 ID へ投げたらどうなるか (拒否されるはず)
    try:
        import_threads_native(C, archive, replace=True, skip_embed=True, transplant=False)
        OUT["restore_into_other_id"] = "ACCEPTED (拒否されなかった)"
    except Exception as exc:  # noqa: BLE001
        OUT["restore_into_other_id"] = f"REJECTED: {type(exc).__name__}: {exc}"

    result = import_threads_native(C, archive, replace=True, skip_embed=True, transplant=True)
    OUT["transplant_result"] = result
    OUT["C_after_tables"] = table_counts(HOME / "personas" / C / "memory.db")
    OUT.update(read_as(C, "read_as_C_after_transplant", OUT["A_seed"]["threads"]))

    # 運ばれなかったテーブルの名指し
    a_tables = OUT["A_tables"]
    c_after = OUT["C_after_tables"]
    not_carried = {}
    for name, count in a_tables.items():
        if not isinstance(count, int) or count == 0:
            continue
        got = c_after.get(name)
        if not isinstance(got, int) or got < count:
            not_carried[name] = {"A": count, "C_after_transplant": got}
    OUT["tables_not_carried_by_transplant"] = not_carried

    # --- 同一 ID への復元 (transplant=False) ---
    # A の memory.db を消して作り直し、archive を復元する
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(a_db) + suffix)
        if p.exists():
            p.unlink()
    fresh = make_adapter(A)     # 空の memory.db を作り直す
    fresh.close()
    OUT["A_after_wipe_tables"] = table_counts(a_db)

    restore_result = import_threads_native(A, archive, replace=True, skip_embed=True,
                                           transplant=False)
    OUT["restore_same_id_result"] = restore_result
    OUT["A_after_restore_tables"] = table_counts(a_db)
    OUT.update(read_as(A, "read_as_A_after_restore", OUT["A_seed"]["threads"]))

    not_carried_restore = {}
    for name, count in a_tables.items():
        if not isinstance(count, int) or count == 0:
            continue
        got = OUT["A_after_restore_tables"].get(name)
        if not isinstance(got, int) or got < count:
            not_carried_restore[name] = {"A_original": count, "A_after_restore": got}
    OUT["tables_not_carried_by_restore"] = not_carried_restore

    out_path = Path(__file__).with_name("exp3_result.json")
    out_path.write_text(json.dumps(OUT, ensure_ascii=False, indent=2), encoding="utf-8")
    print("WROTE", out_path)


if __name__ == "__main__":
    main()
