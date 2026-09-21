"""実験 2: 新規ペルソナ B のディレクトリに、別ペルソナ A の memory.db を
そのまま置いたら、B の読み出し経路で A の記憶が返ってくるか。

まはーの原文の質問「新規ペルソナを作って memory.db だけ差し替えれば動くか」。

- SAIVERSE_HOME は scratchpad の一時ディレクトリ。~/.saiverse/ には触れない。
- 人格 ID は合成 (probe_a_testcity / probe_b_testcity)。
- LLM は呼ばない。埋め込みはローカル ONNX snapshot のみ。
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from probe_env import guard_not_production, setup_home, table_counts  # noqa: E402

HOME = setup_home("exp2")
guard_not_production()

from saiverse_memory.adapter import SAIMemoryAdapter  # noqa: E402
from sai_memory.core_memory import add_core_memory, list_core_memories  # noqa: E402

A = "probe_a_testcity"
B = "probe_b_testcity"
BUILDING = "probe_building_hall"

OUT: dict = {"home": str(HOME)}


def make_adapter(pid: str) -> SAIMemoryAdapter:
    """本番 (persona/bootstrap.py:60) と同じ引数の形で作る。"""
    return SAIMemoryAdapter(
        persona_id=pid,
        persona_dir=HOME / "personas" / pid,
        resource_id=pid,
        startup_backup=False,
        recover_orphaned_thread=False,
    )


def seed_persona_a() -> dict:
    a = make_adapter(A)
    info = {}
    # 1) ペルソナ自身のスレッド (__persona__) — Pulse の内省・自律の書き込み先
    a.append_persona_message({
        "role": "user",
        "content": "PROBE-A-PERSONA-1 まはーが工房の棚を作り直した話",
        "metadata": {"tags": ["conversation"]},
    })
    a.append_persona_message({
        "role": "assistant",
        "content": "PROBE-A-PERSONA-2 棚は三段で、上段に定規を置くことにした",
        "metadata": {"tags": ["conversation"]},
    })
    # 2) 建物スレッド — ユーザーとの会話の書き込み先
    a.append_building_message(BUILDING, {
        "role": "user",
        "content": "PROBE-A-BUILDING-1 今日の天気はどうだった",
        "metadata": {"tags": ["conversation"]},
    })
    a.append_building_message(BUILDING, {
        "role": "assistant",
        "content": "PROBE-A-BUILDING-2 午後から晴れて、窓から光が入った",
        "metadata": {"tags": ["conversation"]},
    })
    # 3) 名前付きスレッド (会話の題名つき)
    a.append_persona_message({
        "role": "assistant",
        "content": "PROBE-A-NAMED-1 別スレッドの記録",
        "metadata": {"tags": ["conversation"]},
    }, thread_suffix="probe_named_thread")
    a.set_thread_title("probe_named_thread", "プローブ用の名前つきスレッド")
    # 4) コア記憶 (memopedia_pages 上)
    cid = add_core_memory(a.conn, "PROBE-A-CORE-1 まはーの誕生日は 1 月 14 日", kind="note")
    info["core_id"] = cid
    # 5) 手帳 / Chronicle (memopedia の trunk ページ) — 直接 API で足す
    from sai_memory.memopedia.storage import create_page
    page = create_page(
        a.conn,
        parent_id=None,
        title="PROBE-A-PAGE タイトル",
        content="PROBE-A-PAGE-1 手帳のページ本文",
        category="note",
        is_trunk=False,
    )
    info["memopedia_page_id"] = page.id

    a.conn.commit()
    info["threads"] = [r[0] for r in a.conn.execute(
        "SELECT id FROM threads ORDER BY id").fetchall()]
    info["message_count"] = a.conn.execute(
        "SELECT COUNT(*) FROM messages").fetchone()[0]
    info["embedding_count"] = a.conn.execute(
        "SELECT COUNT(*) FROM message_embeddings").fetchone()[0]
    info["can_embed"] = a.can_embed()
    a.close()
    return info


def seed_persona_b() -> dict:
    b = make_adapter(B)
    b.append_persona_message({
        "role": "assistant",
        "content": "PROBE-B-OWN-1 B 自身の記憶 (差し替えで消えるはず)",
        "metadata": {"tags": ["conversation"]},
    })
    b.conn.commit()
    info = {
        "threads": [r[0] for r in b.conn.execute(
            "SELECT id FROM threads ORDER BY id").fetchall()],
        "message_count": b.conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0],
    }
    b.close()
    return info


def read_as_b(label: str, a_threads: list) -> dict:
    """B の adapter を作り直して、製品の読み出し関数を実際に呼ぶ。"""
    b = make_adapter(B)
    res: dict = {}

    # (1) 会話の続き — 現在のペルソナスレッド
    r1 = b.recent_persona_messages(max_chars=100000)
    res["recent_persona_messages"] = {
        "thread_id_queried": b._thread_id(None),
        "count": len(r1),
        "contents": [m.get("content", "")[:60] for m in r1],
    }

    # (2) 件数指定版
    r2 = b.recent_persona_messages_by_count(50)
    res["recent_persona_messages_by_count"] = {
        "count": len(r2),
        "contents": [m.get("content", "")[:60] for m in r2],
    }

    # (3) 建物の会話履歴
    r3 = b.recent_messages(BUILDING, max_chars=100000)
    res["recent_messages(building)"] = {
        "thread_id_queried": b._thread_id(BUILDING),
        "count": len(r3),
        "contents": [m.get("content", "")[:60] for m in r3],
    }

    # (4) スレッド一覧 (Memory Settings UI のブラウズ)
    r4 = b.list_thread_summaries()
    res["list_thread_summaries"] = [
        {"thread_id": s["thread_id"], "title": s["title"],
         "message_count": s["message_count"], "active": s["active"]}
        for s in r4
    ]

    # (5) A のスレッド ID を直接指定した取得
    direct = {}
    for tid in a_threads:
        msgs = b.get_thread_messages(tid)
        direct[tid] = {
            "count": len(msgs),
            "contents": [m.get("content", "")[:60] for m in msgs],
        }
    res["get_thread_messages(A の thread_id 直指定)"] = direct

    # (6) 意味検索による想起 (recall_snippet)
    res["can_embed"] = b.can_embed()
    snippet = b.recall_snippet(BUILDING, query_text="棚と定規の話", max_chars=2000)
    res["recall_snippet"] = {
        "length": len(snippet),
        "text": snippet[:800],
    }
    snippet2 = b.recall_snippet(None, query_text="天気と光の話", max_chars=2000)
    res["recall_snippet(persona thread)"] = {
        "length": len(snippet2),
        "text": snippet2[:800],
    }

    # (7) コア記憶
    cores = list_core_memories(b.conn)
    res["list_core_memories"] = [c.content[:60] for c in cores]

    # (8) memopedia ページ
    pages = b.conn.execute(
        "SELECT id, title, substr(content,1,60) FROM memopedia_pages ORDER BY id"
    ).fetchall()
    res["memopedia_pages"] = [list(p) for p in pages]

    # (9) working_memory (persona_id をキーにするテーブル)
    wm = b.conn.execute("SELECT persona_id, data FROM working_memory").fetchall()
    res["working_memory_rows"] = [[r[0], (r[1] or "")[:40]] for r in wm]

    # (10) 現在のスレッド (active_state.json 由来)
    res["get_current_thread"] = b.get_current_thread()

    b.close()
    return {label: res}


def main() -> None:
    OUT["A_seed"] = seed_persona_a()
    OUT["B_seed"] = seed_persona_b()

    a_db = HOME / "personas" / A / "memory.db"
    b_db = HOME / "personas" / B / "memory.db"

    OUT["before_swap"] = {
        "A_tables": table_counts(a_db),
        "B_tables": table_counts(b_db),
    }
    OUT.update(read_as_b("read_as_B_BEFORE_swap", OUT["A_seed"]["threads"]))

    # --- 差し替え: B のディレクトリに A の memory.db をそのまま置く ---
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(b_db) + suffix)
        if p.exists():
            p.unlink()
    shutil.copy2(a_db, b_db)
    OUT["swap"] = {"copied": f"{a_db} -> {b_db}"}
    OUT["after_swap_B_tables"] = table_counts(b_db)

    OUT.update(read_as_b("read_as_B_AFTER_swap", OUT["A_seed"]["threads"]))

    out_path = Path(__file__).with_name("exp2_result.json")
    out_path.write_text(json.dumps(OUT, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(OUT, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
