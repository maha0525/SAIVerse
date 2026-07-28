"""歪み世代 Chronicle の削除 (あらすじのレベル制への移行修復)。

docs/issues/air_aifi_memory_repair.md の手順の実装。エリスの修復
(2026-07-29) で確立した選定・検算・削除をペルソナ汎用にしたもの。

既定は **dry-run (読み取りのみ)**。実行は --execute (要ユーザー承認)。

選定の規則:
- 境界 = 問題コード (W4 恒等転写) の本番初走行 = そのペルソナの
  digest_origin が batch/identity の最初の created_at
- 上限 = 新コード (あらすじのレベル制) の本番稼働開始。既定は
  2026-07-28 23:04 (サーバー再起動)。**これ以降に作られたエントリは
  新方式の正当な産物なので削除しない** (エリスの再生成分・air の
  23:34 の初編纂分を巻き込まないための線)
- 削除 = 境界〜上限の間に作成された Chronicle エントリ。ただし「大昔の
  取りこぼし拾い」(被覆開始が旧世代の被覆末尾より前) は残す
- 連動 = 削除エントリ産の Fragment / それだけで生まれたページ /
  付随 embeddings / 実行台帳の編纂マーク (metabolism.run) / world DB の
  宙に浮く digest_ref

使い方:
    python scripts/arasuji/persona_chronicle_cleanup.py <persona_id>
    python scripts/arasuji/persona_chronicle_cleanup.py <persona_id> \
        --execute --expect 101,4,111,21   # dry-run の数字で検算してから実行
"""

from __future__ import annotations

import argparse
import datetime
import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

#: 新コード (あらすじのレベル制、コミット 9075a7f) の本番稼働開始 —
#: 2026-07-28 23:04 のサーバー再起動。これ以降の作成分は正当な産物。
DEFAULT_CUTOVER = int(datetime.datetime(2026, 7, 28, 23, 4, 0).timestamp())


def _d(ts) -> str:
    if ts is None:
        return "-"
    return datetime.datetime.fromtimestamp(int(ts)).strftime("%m/%d %H:%M")


def _memory_db(persona_id: str) -> Path:
    import os
    home = Path(os.getenv("SAIVERSE_HOME", Path.home() / ".saiverse"))
    return home / "personas" / persona_id / "memory.db"


def _world_db() -> Path:
    import os
    home = Path(os.getenv("SAIVERSE_HOME", Path.home() / ".saiverse"))
    return home / "user_data" / "database" / "saiverse.db"


def select_targets(
    conn: sqlite3.Connection, cutover: int, *, strict_window: bool = False,
) -> dict:
    """選定 (読み取りのみ)。dry-run と実行が共有する唯一のロジック。

    strict_window=True は「取りこぼし拾い」の例外温存をやめ、境界〜上限の
    窓内を全て削除対象にする (ユーザー裁定 2026-07-29: 旧コードで編纂された
    ものは内容が本物でも新版で回し直す)。
    """
    boundary = conn.execute(
        "SELECT MIN(created_at) FROM memopedia_pages WHERE category='chronicle' "
        "AND json_extract(metadata,'$.digest_origin') IN ('batch','identity') "
        "AND created_at < ?", (cutover,)
    ).fetchone()[0]
    healthy = conn.execute(
        "SELECT COUNT(*) FROM memopedia_pages WHERE category='chronicle' "
        "AND created_at >= ?", (cutover,)
    ).fetchone()[0]
    if boundary is None:
        return {"boundary": None, "healthy": healthy}
    tail = conn.execute(
        "SELECT MAX(json_extract(metadata,'$.end_time')) FROM memopedia_pages "
        "WHERE category='chronicle' AND created_at < ?", (boundary,)
    ).fetchone()[0]

    rows = conn.execute(
        "SELECT id, json_extract(metadata,'$.level'), "
        "json_extract(metadata,'$.digest_origin'), created_at, "
        "json_extract(metadata,'$.start_time'), json_extract(metadata,'$.end_time'), "
        "json_extract(metadata,'$.source_ids'), substr(content,1,40) "
        "FROM memopedia_pages WHERE category='chronicle' "
        "AND created_at >= ? AND created_at < ? "
        "ORDER BY created_at", (boundary, cutover)
    ).fetchall()
    keep, delete = [], []
    for r in rows:
        st = r[4]
        if (not strict_window and tail is not None and st is not None
                and int(st) < int(tail)):
            keep.append(r)
        else:
            delete.append(r)
    delete_ids = [r[0] for r in delete]
    ph = ",".join("?" for _ in delete_ids)

    # 巻き添え解除: 削除される親に食われた、残る子 (旧世代エントリ等)
    child_ids = set()
    for r in delete:
        try:
            child_ids.update(json.loads(r[6]) if r[6] else [])
        except (json.JSONDecodeError, TypeError):
            pass
    release = []
    for cid in child_ids:
        if cid in set(delete_ids):
            continue
        row = conn.execute(
            "SELECT id, json_extract(metadata,'$.is_consolidated') "
            "FROM memopedia_pages WHERE id=? AND category='chronicle'", (cid,)
        ).fetchone()
        if row and row[1]:
            release.append(row[0])

    frag_ids = [r[0] for r in conn.execute(
        f"SELECT id FROM memopedia_fragments WHERE chronicle_entry_id IN ({ph})",
        tuple(delete_ids),
    )] if delete_ids else []

    # 空になるページ = 削除 Fragment のページのうち、全 Fragment が消え、
    # かつ境界以降に生まれたもの
    page_ids = []
    if delete_ids:
        for (pid,) in conn.execute(
            f"SELECT DISTINCT entity_id FROM memopedia_fragments "
            f"WHERE chronicle_entry_id IN ({ph})", tuple(delete_ids),
        ):
            total = conn.execute(
                "SELECT COUNT(*) FROM memopedia_fragments WHERE entity_id=?", (pid,)
            ).fetchone()[0]
            deleted_here = conn.execute(
                f"SELECT COUNT(*) FROM memopedia_fragments WHERE entity_id=? "
                f"AND chronicle_entry_id IN ({ph})", (pid,) + tuple(delete_ids),
            ).fetchone()[0]
            create_edit = conn.execute(
                "SELECT edited_at FROM memopedia_page_edit_history WHERE page_id=? "
                "AND edit_type='create' ORDER BY edited_at LIMIT 1", (pid,)
            ).fetchone()
            if total == deleted_here and create_edit and create_edit[0] >= boundary:
                page_ids.append(pid)

    # 境界以降の「既存ページの概要書き換え」— エリスではゼロだったが、
    # ペルソナごとに要確認。ゼロでなければロールバック工程の設計が要る。
    summary_edits = conn.execute(
        "SELECT COUNT(*) FROM memopedia_page_edit_history h "
        "JOIN memopedia_pages p ON p.id = h.page_id "
        "WHERE h.edited_at >= ? AND p.category != 'chronicle' "
        "AND h.edit_source IN ('entity_extractor','memory_organize') "
        "AND h.edit_type IN ('append','update')", (boundary,)
    ).fetchone()[0]

    # 双子検査 (エリスの教訓): 例外 (取りこぼし拾い) の source に、同一内容の
    # 重複行が既に他のあらすじへ編纂されていないか。居れば余剰コピーの残骸で
    # あり、その例外は残す価値がない (UI から個別削除を推奨)。
    twins = []
    for r in keep:
        try:
            sids = json.loads(r[6]) if r[6] else []
        except (json.JSONDecodeError, TypeError):
            sids = []
        for mid in sids:
            m = conn.execute(
                "SELECT thread_id, role, content, created_at FROM messages "
                "WHERE id=?", (mid,)
            ).fetchone()
            if not m:
                continue
            for (tw,) in conn.execute(
                "SELECT id FROM messages WHERE thread_id=? AND role=? "
                "AND content=? AND created_at=? AND id != ?", m + (mid,)
            ):
                covered = conn.execute(
                    "SELECT 1 FROM memopedia_pages p, "
                    "json_each(p.metadata,'$.source_ids') s "
                    "WHERE p.category='chronicle' AND s.value=? LIMIT 1", (tw,)
                ).fetchone()
                if covered:
                    twins.append((r[0], r[7]))

    return {
        "boundary": boundary, "tail": tail, "healthy": healthy,
        "cutover": cutover,
        "delete": delete, "delete_ids": delete_ids, "keep": keep,
        "release": release, "frag_ids": frag_ids, "page_ids": page_ids,
        "summary_edits": summary_edits, "twins": twins,
    }


def print_dry_run(persona_id: str, sel: dict, world: sqlite3.Connection) -> None:
    print(f"=== {persona_id} 歪み世代 Chronicle ドライラン (読み取りのみ) ===")
    if sel["boundary"] is None:
        print("問題コード産のエントリ (batch/identity) が無い — 対応不要。")
        print(f"(新コード稼働後の正当な産物: {sel['healthy']} 件)")
        return
    print(f"境界 (問題コード初走行): {_d(sel['boundary'])} (epoch {sel['boundary']})")
    print(f"上限 (新コード稼働開始): {_d(sel['cutover'])} — これ以降の "
          f"{sel['healthy']} 件は正当な産物として対象外")
    print(f"旧世代の被覆末尾 (再生成の起点): {_d(sel['tail'])}")
    c = Counter((f"lv{r[1]}", r[2] or "-") for r in sel["delete"])
    print(f"\n削除対象: {len(sel['delete'])} 件 — "
          + ", ".join(f"{k[0]}/{k[1]}={v}" for k, v in sorted(c.items())))
    for r in sel["delete"]:
        print(f"  {r[0][:8]} lv{r[1]} {r[2] or '-':8s} 作成{_d(r[3])} "
              f"被覆{_d(r[4])}〜{_d(r[5])} 「{r[7]}…」")
    print(f"\n例外として残す (取りこぼし拾い): {len(sel['keep'])} 件")
    for r in sel["keep"]:
        print(f"  {r[0][:8]} lv{r[1]} {r[2] or '-'} 被覆{_d(r[4])} 「{r[7]}…」")
    if sel["twins"]:
        print("\n⚠️ 双子あり (同一発言の別コピーが編纂済み = 余剰コピーの残骸。"
              "残す価値なし — UI から個別削除を推奨):")
        for eid, head in sel["twins"]:
            print(f"  {eid[:8]} 「{head}…」")
    print(f"\n統合解除する子 (削除される親に食われていた残存エントリ): {len(sel['release'])} 件")
    print(f"Fragment 削除: {len(sel['frag_ids'])} 件")
    print(f"ページ削除 (Fragment 全滅 + 境界以降生まれ): {len(sel['page_ids'])} 件")
    if sel["summary_edits"]:
        print(f"⚠️ 境界以降の既存ページ概要の書き換え: {sel['summary_edits']} 件 — "
              "エリスではゼロだった工程。ロールバック要否を個別判断すること")
    n_ledger = world.execute(
        "SELECT COUNT(*) FROM execution_ledger WHERE KIND='metabolism.run' "
        "AND PERSONA_ID=? AND CREATED_AT >= ? AND CREATED_AT < ?",
        (persona_id, sel["boundary"], sel["cutover"]),
    ).fetchone()[0]
    print(f"実行台帳の編纂マーク削除 (境界以降): {n_ledger} 件")
    danglers = [eid for (eid, ref) in world.execute(
        "SELECT SHORT_ID, DIGEST_REF FROM episodes WHERE PERSONA_ID=? "
        "AND DIGEST_REF LIKE 'chronicle:%'", (persona_id,),
    ) if ref.split(":", 1)[1] in set(sel["delete_ids"])]
    print(f"world DB の宙に浮く digest_ref (NULL 化): {len(danglers)} 件 {danglers[:10]}")
    print(f"\n実行するには: --execute --expect "
          f"{len(sel['delete'])},{len(sel['keep'])},{len(sel['frag_ids'])},{len(sel['page_ids'])}")


def execute(
    persona_id: str, expect: tuple, db_path: Path, cutover: int,
    *, strict_window: bool = False,
) -> int:
    """バックアップ → 選定再計算+検算 → 単一 tx 削除 → 台帳/世界 DB 掃除。"""
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    bak = db_path.with_name(f"memory.db.bak-cleanup-{stamp}")
    src = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    dst = sqlite3.connect(bak)
    src.backup(dst)
    dst.close()
    src.close()
    print(f"backup: {bak} ({bak.stat().st_size:,} bytes)")

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA busy_timeout = 30000")
    sel = select_targets(conn, cutover, strict_window=strict_window)
    if sel["boundary"] is None or not sel["delete_ids"]:
        print("削除対象なし — execute の出番はない (dry-run を確認)。"
              "台帳の stale マークだけが問題なら手順書の個別 SQL で。")
        conn.close()
        return 1
    actual = (len(sel["delete"]), len(sel["keep"]),
              len(sel["frag_ids"]), len(sel["page_ids"]))
    if actual != expect:
        print(f"!! 中止: 選定がドライランと一致しない actual={actual} expect={expect}")
        conn.close()
        return 1

    delete_ids, frag_ids, page_ids = (
        sel["delete_ids"], sel["frag_ids"], sel["page_ids"],
    )
    boundary = sel["boundary"]
    ph = ",".join("?" for _ in delete_ids)
    fph = ",".join("?" for _ in frag_ids)
    pph = ",".join("?" for _ in page_ids)
    total_before = conn.execute(
        "SELECT COUNT(*) FROM memopedia_pages WHERE category='chronicle'"
    ).fetchone()[0]
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            f"DELETE FROM memopedia_pages WHERE id IN ({ph}) "
            f"AND category='chronicle'", tuple(delete_ids))
        n_e = conn.execute("SELECT changes()").fetchone()[0]
        conn.execute(
            f"DELETE FROM arasuji_embeddings WHERE entry_id IN ({ph})",
            tuple(delete_ids))
        # 巻き添え解除 (エリスでは0件、air/aifi では起きうる)
        for cid in sel["release"]:
            row = conn.execute(
                "SELECT metadata FROM memopedia_pages WHERE id=?", (cid,)
            ).fetchone()
            meta = json.loads(row[0])
            meta["is_consolidated"] = 0
            meta.pop("parent_id", None)
            conn.execute("UPDATE memopedia_pages SET metadata=? WHERE id=?",
                         (json.dumps(meta, ensure_ascii=False), cid))
        if frag_ids:
            conn.execute(
                f"DELETE FROM memopedia_fragments WHERE id IN ({fph})",
                tuple(frag_ids))
            conn.execute(
                f"DELETE FROM memopedia_fragment_embeddings "
                f"WHERE fragment_id IN ({fph})", tuple(frag_ids))
        if page_ids:
            conn.execute(
                f"DELETE FROM memopedia_pages WHERE id IN ({pph})",
                tuple(page_ids))
            conn.execute(
                f"DELETE FROM memopedia_embeddings WHERE page_id IN ({pph})",
                tuple(page_ids))
            conn.execute(
                f"DELETE FROM memopedia_page_states WHERE page_id IN ({pph})",
                tuple(page_ids))
        total_after = conn.execute(
            "SELECT COUNT(*) FROM memopedia_pages WHERE category='chronicle'"
        ).fetchone()[0]
        oldgen_after = conn.execute(
            "SELECT COUNT(*) FROM memopedia_pages WHERE category='chronicle' "
            "AND created_at < ?", (boundary,)
        ).fetchone()[0]
        exceptions_alive = conn.execute(
            "SELECT COUNT(*) FROM memopedia_pages WHERE category='chronicle' "
            "AND created_at >= ? AND created_at < ?", (boundary, cutover)
        ).fetchone()[0]
        if not (n_e == expect[0]
                and total_after == total_before - expect[0]
                and exceptions_alive == expect[1]):
            conn.rollback()
            print(f"!! rollback: 検算不一致 entries={n_e} "
                  f"total {total_before}->{total_after} exceptions={exceptions_alive}")
            return 1
        conn.commit()
        print(f"committed: entries={n_e} release={len(sel['release'])} "
              f"fragments={len(frag_ids)} pages={len(page_ids)} | "
              f"chronicle {total_before}->{total_after} 旧世代 {oldgen_after} 無傷 "
              f"例外 {exceptions_alive} 現存")
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    # 台帳の編纂マーク + world DB の宙に浮く digest_ref (別 tx)
    world = sqlite3.connect(_world_db())
    world.execute("PRAGMA busy_timeout = 30000")
    try:
        world.execute("BEGIN IMMEDIATE")
        world.execute(
            "DELETE FROM execution_ledger WHERE KIND='metabolism.run' "
            "AND PERSONA_ID=? AND CREATED_AT >= ? AND CREATED_AT < ?",
            (persona_id, boundary, cutover))
        n_ledger = world.execute("SELECT changes()").fetchone()[0]
        n_ref = 0
        for eid, ref in world.execute(
            "SELECT EPISODE_ID, DIGEST_REF FROM episodes WHERE PERSONA_ID=? "
            "AND DIGEST_REF LIKE 'chronicle:%'", (persona_id,),
        ).fetchall():
            if ref.split(":", 1)[1] in set(delete_ids):
                world.execute(
                    "UPDATE episodes SET DIGEST_REF=NULL WHERE EPISODE_ID=?",
                    (eid,))
                n_ref += 1
        world.commit()
        print(f"world DB: 台帳マーク {n_ledger} 件削除 / digest_ref {n_ref} 件 NULL 化")
    except Exception:
        world.rollback()
        raise
    finally:
        world.close()
    print("完了。次: UI の記憶整理 (または CLI) で再編纂。完走確認はログで "
          "(UI はタイムアウト誤報を出す — docs/issues/organize_memory_ui_timeout.md)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("persona_id")
    ap.add_argument("--execute", action="store_true",
                    help="実際に削除する (既定は dry-run)。要ユーザー承認")
    ap.add_argument("--expect", default=None,
                    help="実行時の検算: dry-run が出した 'entries,keep,frags,pages'")
    ap.add_argument("--cutover", default=None,
                    help="新コード稼働開始 (YYYY-MM-DD HH:MM)。既定 = 2026-07-28 23:04")
    ap.add_argument("--strict-window", action="store_true",
                    help="「取りこぼし拾い」の例外温存をやめ、境界〜上限の窓内を"
                         "全て削除対象にする (旧コード編纂分は新版で回し直す裁定)")
    args = ap.parse_args()

    cutover = DEFAULT_CUTOVER
    if args.cutover:
        cutover = int(datetime.datetime.strptime(
            args.cutover, "%Y-%m-%d %H:%M").timestamp())

    db_path = _memory_db(args.persona_id)
    if not db_path.exists():
        print(f"memory.db が無い: {db_path}")
        return 1

    if not args.execute:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        world = sqlite3.connect(f"file:{_world_db()}?mode=ro", uri=True)
        try:
            sel = select_targets(conn, cutover, strict_window=args.strict_window)
            print_dry_run(args.persona_id, sel, world)
        finally:
            conn.close()
            world.close()
        return 0

    if not args.expect:
        print("--execute には --expect entries,keep,frags,pages (dry-run の数字) が必要")
        return 1
    expect = tuple(int(x) for x in args.expect.split(","))
    if len(expect) != 4:
        print("--expect は 4 つの数字 (entries,keep,frags,pages)")
        return 1
    return execute(args.persona_id, expect, db_path, cutover,
                   strict_window=args.strict_window)


if __name__ == "__main__":
    sys.exit(main())
