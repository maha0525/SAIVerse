"""a1: `event_message` タグの中身と、想起の各経路がそれを拾うかどうかの実測。

## 何を確かめるものか

1. **文面**: ペルソナのコア記憶をユーザーが訂正・削除・復元したとき、本人の
   コンテキストに実際に入る文字列を、製品コード (API のルート関数 →
   知覚バッファ → 消費) をそのまま通して出す。あわせて編纂完了報告
   (`sai_memory/curation_ops.py`) とライフ境界通知 (`saiverse/day_plan.py`)
   の実文面も、それぞれの製品関数を呼んで出す。
2. **想起**: `event_message` タグを持つ行が、想起の各経路の絞り込み
   (SQL) を通り抜けるかどうかを、行を実際に置いて数える。対象は
   - `real_conversation_filter()` — 自動想起と unified_recall の message ソース
   - `_conversation_exclusion()` — scene 切り出し・会話キーワード検索
   - `get_embeddings_for_scope(required_tags=["conversation"], ...)` —
     adapter の recall / 記憶デバッグ API / memory_search_brief スペル
   - `get_embeddings_for_scope(exclude_tags=["handy_tool","spell"])` —
     `saiverse/recall_walk.py` の意味的近傍の辺と同じ引数の形
   - `chronicle_eligibility_filter()` — あらすじ編纂の材料
3. **Memopedia の差分通知**: ユーザーが Memopedia のページを作成・更新・削除
   したとき、head の差分検知 (`MemopediaIndexSection`) がペルソナへ届ける文を、
   その section を直接呼んで出す。

## どう実行するか

    cd C:/Users/shuhe/workspace/SAIVerse
    .venv/Scripts/python.exe docs/audits/2026-09-10_normal_behavior_remaining_evidence/repro/a1_event_message_shapes.py

隔離: `SAIVERSE_HOME` を一時ディレクトリへ向け、合成ペルソナ ``tester`` の
memory.db だけを作る。LLM は呼ばない (埋め込みはゼロベクトルの差し替え)。
本番の `~/.saiverse/` には触れない。

## 何が観測されたか (2026-09-10 実行)

- コア記憶の訂正 3 種は、消費時に 1 通の
  ``<system>[コア記憶の更新通知]\\n…</system>`` になる。role は user。
  **messages 行は作られない** (W14 以降、知覚は台帳 + 提示時マージ)。
- 編纂完了報告とライフ境界通知は **messages 行として残る**
  (tags = internal/event_message/curation, internal/event_message/day_plan)。
- 想起の絞り込みの通過表 (下の OBSERVED を参照):
  `event_message` を持つ 5 行は、real_conversation_filter /
  _conversation_exclusion / required_tags=["conversation"] のどれも通らない。
  唯一 `exclude_tags=["handy_tool","spell"]` だけの形 (recall_walk と同じ
  引数) では 5 行すべてが通った。あらすじ編纂の材料には 4 行が入る
  (discardable のスルース記録だけが落ちる)。
- Memopedia の差分通知は 3 種のラベルが出た —
  ``<system>[システム通知]\\nMemopedia「…」が作成/更新/削除されました</system>``。
  **誰が触ったかは文面に出ない**。
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT))

# Windows の cp932 コンソールでも日本語の文面をそのまま出す。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass


class DummyEmbedder:
    """埋め込みモデルを起動しない差し替え (LLM もモデルロードも無し)。"""

    def __init__(self, model=None, **kwargs) -> None:
        self.model_name = model

    def embed(self, texts, **kwargs):
        return [[0.0] * 3 for _ in texts]


def _make_world_db():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from database.models import AI, Base, City, User

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)
    db = session_factory()
    try:
        db.add(User(USERID=1, PASSWORD="x", USERNAME="tester"))
        db.flush()
        city = City(USERID=1, CITY_SLUG="c", UI_PORT=3001, API_PORT=8001)
        db.add(city)
        db.flush()
        db.add(AI(AIID="tester", HOME_CITYID=city.CITYID, AINAME="テスト人格"))
        db.commit()
    finally:
        db.close()
    return engine, session_factory


def part_a_core_memory_notices(adapter, manager):
    """コア記憶の訂正 3 種を、API のルート関数をそのまま呼んで文面にする。"""
    from api.routes.people.core_memory import (
        UpdateCoreMemoryRequest,
        delete_core_memory_item,
        restore_core_memory_item,
        update_core_memory_item,
    )
    from sai_memory.core_memory import add_core_memory, init_core_memory_table

    out = []
    with adapter._db_lock:
        init_core_memory_table(adapter.conn)
        edit_id = add_core_memory(
            adapter.conn, "まはーは野菜がほぼ食べられない。", confirmed=0,
        )
        del_id = add_core_memory(adapter.conn, "まはーの誕生日は 1 月 14 日。")
        res_id = add_core_memory(adapter.conn, "作業台の約束は 2026-07-08 に結んだ。")

    update_core_memory_item(
        "tester", edit_id,
        UpdateCoreMemoryRequest(content="まはーは野菜が苦手で、野菜ジュースで補っている。"),
        manager=manager,
    )
    out.append(("edit", adapter.flush_perception_buffer_payload()))

    delete_core_memory_item("tester", del_id, manager=manager)
    out.append(("delete", adapter.flush_perception_buffer_payload()))

    delete_core_memory_item("tester", res_id, manager=manager)
    adapter.flush_perception_buffer_payload()          # 削除ぶんを消費して切り分ける
    restore_core_memory_item("tester", res_id, manager=manager)
    out.append(("restore", adapter.flush_perception_buffer_payload()))
    return out


def part_a_curation_report():
    """編纂完了報告の実文面 (sai_memory/curation_ops.py:_write_curation_report)。"""
    from sai_memory.curation_ops import _write_curation_report

    captured = []
    fake_adapter = SimpleNamespace(
        append_persona_message=lambda msg: captured.append(msg),
    )
    _write_curation_report(
        adapter=fake_adapter,
        persona_id="tester",
        done_count=2,
        failed_count=1,
        report_lines=[
            "- [merge] m:5「SAIVerse」に m:31「SAIVerseの構造」を統合しました"
            "（4,120字、子ページ 2 件の付け替え）。編集来歴から差し戻せます。",
            "- [split] m:12 を 3 件の子ページに分割"
            "（まはーとの技術対話・設計の記録・道具の話）しました。編集来歴から差し戻せます。",
            "- [split] m:44 の編纂に失敗しました（LLM クライアントの初期化に失敗しました）。"
            "ページは変更されていません。",
        ],
    )
    return captured


def part_a_life_boundary(manager):
    """ライフ境界通知の実文面 (saiverse/day_plan._life_boundary_outbox_items)。"""
    from saiverse import day_plan

    return day_plan._life_boundary_outbox_items(
        manager, "tester", "（活動開始）今日は 09:00〜23:00。",
    )


#: 実行して出せない 2 種は、テンプレート文字列と埋まる値から合成する
#: (依頼元の指示「テンプレート文字列と埋まる値を追って、合成例を作る」)。
SYNTHESIZED = {
    # sea/sluice.py:2545-2557 (見出し) + :719 (採取行) + _persist_record:1360-1379
    "sluice_record": (
        "<system>記憶整理の節目 — スルースの採取判断:\n"
        "テスト人格の判断: 今日は食べ物の好みの話が続いたので、まはーの食事の"
        "傾向を一つ覚えておく。\n"
        "コア記憶 core:18 に採取: まはーは野菜がほぼ食べられない。\n"
        "手帳「野菜以外の栄養の取り方」をやりたいメモ: ビタミンゼリーの話を掘る。\n"
        "</system>"
    ),
    # builtin_data/tools/get_building_messages.py:268-272
    # (建物の host イベントを本人の記憶へ取り込む形。建物名を頭に付ける)
    "building_host_event": (
        "<system>[書斎] 🎲 Game:\n出題フェーズが始まりました</system>"
    ),
}


def part_b_recall_filters(conn):
    """想起・編纂の各絞り込みが、どの行を通すかを実測する。"""
    from sai_memory.memory.storage import (
        add_message,
        chronicle_eligibility_filter,
        get_embeddings_for_scope,
        get_or_create_thread,
        real_conversation_filter,
        replace_message_embeddings,
        _conversation_exclusion,
    )

    get_or_create_thread(conn, "main", resource_id="tester")
    rows = [
        ("R1 実会話 (ユーザー)", "user", "野菜って食べられる？",
         {"tags": ["conversation"]}, None, None),
        ("R2 実会話 (ペルソナ)", "model", "ほとんど食べられないんだ",
         {"tags": ["conversation"]}, None, None),
        ("R3 建物の host イベント取り込み", "user",
         SYNTHESIZED["building_host_event"],
         {"tags": ["internal", "event_message"]}, None, None),
        ("R4 スルースの判断ターン記録", "user", SYNTHESIZED["sluice_record"],
         {"tags": ["internal", "event_message", "sluice"]}, "main_line", "discardable"),
        ("R5 編纂完了報告", "user",
         "<system>[システム通知: 夜の間に棚の整理が行われました]\n完了: 2 件</system>",
         {"tags": ["internal", "event_message", "curation"]}, None, None),
        ("R6 ライフ境界通知", "user",
         "<system>[システム通知] （活動開始）今日は 09:00〜23:00。</system>",
         {"tags": ["internal", "event_message", "day_plan"]}, None, None),
        ("R7 旧世代のコア記憶訂正通知 (W14 以前の直挿し)", "user",
         "<system>[コア記憶の更新通知]\n"
         "ユーザーがあなたのコア記憶 core:7 を削除しました（ごみ箱へ移動。復元可能）。</system>",
         {"tags": ["internal", "event_message", "perception"]}, None, None),
    ]
    label_by_id = {}
    for label, role, content, meta, line_role, scope in rows:
        mid = add_message(
            conn, thread_id="main", role=role, content=content,
            resource_id="tester", metadata=meta, line_role=line_role, scope=scope,
        )
        label_by_id[mid] = label
        replace_message_embeddings(conn, mid, [[0.0, 0.0, 0.0]])

    def _ids(clause, params):
        cur = conn.execute(
            f"SELECT id FROM messages WHERE {clause}", params,
        )
        return {str(r[0]) for r in cur.fetchall()}

    results = {}
    results["real_conversation_filter (自動想起 / unified_recall の message)"] = _ids(
        *real_conversation_filter()
    )
    results["_conversation_exclusion (scene 切り出し / 会話検索)"] = _ids(
        *_conversation_exclusion()
    )
    results["chronicle_eligibility_filter (あらすじ編纂の材料)"] = _ids(
        *chronicle_eligibility_filter()
    )

    got = get_embeddings_for_scope(
        conn, required_tags=["conversation"], exclude_tags=["handy_tool", "spell"],
    )
    results['required_tags=["conversation"] (adapter recall / デバッグ API / スペル)'] = {
        str(m.id) for m, _v, _i in got
    }
    got2 = get_embeddings_for_scope(conn, exclude_tags=["handy_tool", "spell"])
    results['exclude_tags のみ (recall_walk の意味的近傍の辺と同じ形)'] = {
        str(m.id) for m, _v, _i in got2
    }
    return label_by_id, results


def part_c_memopedia_diff_notice(adapter):
    """ユーザーの Memopedia 編集・削除が差分通知になるかを実測する。

    `MemopediaIndexSection.capture_changes_since` + `diff_to_notifications` は
    「ツールを経由しない変化 (ユーザーの UI 編集・migration 等)」を拾う backstop
    (docs/intent/beat_execution_context.md §3.3)。ここではページを直接
    作成・更新・削除して、その backstop がどんなラベルを出すかを見る。
    """
    import time

    from sai_memory.memopedia.storage import init_memopedia_tables
    from sai_memory.perception_buffer import perception_block_text
    from sea.head_pipeline.sections.memopedia_index import (
        MemopediaIndexSection,
        MemopediaIndexSnapshot,
    )

    conn = adapter.conn
    init_memopedia_tables(conn)
    baseline = time.time()
    now = int(time.time()) + 1
    rows = [
        ("p-created", "新しく作られたページ", now, now, 0),
        ("p-updated", "ユーザーが本文を直したページ", int(baseline) - 3600, now, 0),
        ("p-deleted", "ユーザーが消したページ", int(baseline) - 3600, now, 1),
    ]
    for page_id, title, created_at, updated_at, is_deleted in rows:
        conn.execute(
            "INSERT INTO memopedia_pages "
            "(id, title, category, summary, content, created_at, updated_at, "
            "is_deleted) "
            "VALUES (?, ?, 'concept', '', '', ?, ?, ?)",
            (page_id, title, created_at, updated_at, is_deleted),
        )
    conn.commit()

    section = MemopediaIndexSection()
    ctx = SimpleNamespace(
        persona=SimpleNamespace(sai_memory=adapter),
        manager=None,
        persona_id="tester",
    )
    old = MemopediaIndexSnapshot(captured_at=baseline, pages=())
    new = section.capture_changes_since(ctx, baseline)
    labels = section.diff_to_notifications(old, new)
    return [
        (label.kind, "<system>" + perception_block_text("world_state", label.label) + "</system>")
        for label in labels
    ]


def main() -> int:
    tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
    home = Path(tmp.name)
    os.environ["SAIVERSE_HOME"] = str(home)
    os.environ["SAIVERSE_USER_DATA_DIR"] = str(home / "user_data")
    os.environ["SAIMEMORY_MEMORY"] = "1"

    persona_dir = home / "personas" / "tester"
    persona_dir.mkdir(parents=True, exist_ok=True)

    with patch("saiverse_memory.adapter.Embedder", DummyEmbedder):
        from saiverse_memory import SAIMemoryAdapter

        engine, session_factory = _make_world_db()
        adapter = SAIMemoryAdapter(
            "tester", persona_dir=persona_dir, resource_id="tester",
        )
        manager = SimpleNamespace(
            SessionLocal=session_factory,
            personas={"tester": SimpleNamespace(sai_memory=adapter)},
        )

        print("=" * 78)
        print("PART A-1: コア記憶の訂正通知 (API ルート関数 → 知覚バッファ → 消費)")
        print("=" * 78)
        for kind, payload in part_a_core_memory_notices(adapter, manager):
            print(f"\n--- {kind} ---")
            if payload is None:
                print("(消費対象なし)")
                continue
            # 消費バッチの戻り値は本文と media だけ。提示に載るときの役割とタグは
            # sea/runtime_context.py:1001-1006 が付ける。
            print('role  : user  (sea/runtime_context.py の時刻順マージが付与)')
            print('tags  : ["internal", "event_message", "perception"]  (同上)')
            print("content:")
            print(payload.get("content"))

        with adapter._db_lock:
            left = adapter.conn.execute(
                "SELECT COUNT(*) FROM messages "
                "WHERE metadata LIKE '%event_message%'"
            ).fetchone()[0]
        print(f"\n→ 訂正通知が messages に作った行数: {left} "
              "(W14 以降は 0 = 台帳に消費印だけ)")

        print()
        print("=" * 78)
        print("PART A-2: 編纂完了報告 (_write_curation_report)")
        print("=" * 78)
        for msg in part_a_curation_report():
            print("role  :", msg.get("role"))
            print("tags  :", (msg.get("metadata") or {}).get("tags"))
            print("content:")
            print(msg.get("content"))

        print()
        print("=" * 78)
        print("PART A-3: ライフ境界通知 (_life_boundary_outbox_items)")
        print("=" * 78)
        for item in part_a_life_boundary(manager):
            msg = item["payload"]["message"]
            print("target:", item["target"])
            print("role  :", msg.get("role"))
            print("tags  :", (msg.get("metadata") or {}).get("tags"))
            print("content:", msg.get("content"))

        print()
        print("=" * 78)
        print("PART A-4: 合成例 (実行では出せない 2 種。出典は SYNTHESIZED の注記)")
        print("=" * 78)
        for key, text in SYNTHESIZED.items():
            print(f"\n--- {key} ---")
            print(text)

        print()
        print("=" * 78)
        print("PART B: 想起・編纂の絞り込みが通す行")
        print("=" * 78)
        probe_dir = home / "personas" / "probe"
        probe_dir.mkdir(parents=True, exist_ok=True)
        probe = SAIMemoryAdapter(
            "probe", persona_dir=probe_dir, resource_id="probe",
        )
        try:
            labels, results = part_b_recall_filters(probe.conn)
            order = list(labels.items())
            for name, passed in results.items():
                print(f"\n--- {name} ---")
                for mid, label in order:
                    mark = "通る" if mid in passed else "落ちる"
                    print(f"  [{mark}] {label}")
            print()
            print("=" * 78)
            print("PART C: ユーザーの Memopedia 操作を拾う差分通知 (backstop)")
            print("=" * 78)
            notices = part_c_memopedia_diff_notice(probe)
            if not notices:
                print("(ラベルなし)")
            for kind, text in notices:
                print(f"\n--- {kind} ---")
                print(text)
        finally:
            probe.close()

        adapter.close()
        engine.dispose()

    try:
        tmp.cleanup()
    except OSError:
        pass          # Windows の sqlite ハンドル解放待ち。後始末は best-effort
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
