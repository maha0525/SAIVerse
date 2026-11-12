"""実験6 — 壊れたあと、利用者は自力で元に戻せるか。

依頼書の「既に壊れた記憶は、復旧可能な根拠と不可能な範囲を分ける」に対応する。

**確かめること** — 書き出し→取り込みで壊れた状態を作り、そこから 4 通りの手当てを試す

1. 壊れた行に何が残っているか (本文が残っていれば、復旧の材料はある)。
2. 手帳の木を返す API (``GET .../memopedia/tree``) に、その行が見えるか。
   見えなければ、利用者は画面からその行を選べない。
3. 手帳の 1 件削除 (``DELETE .../memopedia/pages/{page_id}``) で取り除けるか。
   取り除いたあと、コア記憶の一覧が読めるように戻るか。
4. コア記憶の 1 件削除 (``DELETE .../core-memory/{memory_id}``) で取り除けるか。
5. もう一度「Memopedia 全削除」を押すと、壊れた行ごと消えるか
   (そのとき、壊れていない本物のコア記憶も道連れになるか)。

手当てはそれぞれ別の合成ペルソナで試す (前の手当てが次の観測を汚さないように)。

**確かめないこと**

- 画面の操作と表示。ここで呼んでいるのは API のルート関数で、
  「まだコア記憶はありません。」という表示に至る画面側の分岐は追っていない。
- バックアップからの復元。利用者が別に控えを持っている場合の話は見ていない。
- ペルソナの発話、LLM の呼び出し、バックエンドの起動。

実行: ``<repo>/.venv/Scripts/python.exe exp6_recovery.py``
(日本語が化けるときは ``PYTHONIOENCODING=utf-8`` を付ける)
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import memopedia_fixture as fx  # noqa: E402

HOME = fx.new_world("exp6_recovery")

from api.routes.people.core_memory import delete_core_memory_item  # noqa: E402
from api.routes.people.memopedia import (  # noqa: E402
    delete_all_memopedia_pages,
    delete_memopedia_page,
    export_memopedia,
    get_memopedia_tree,
    import_memopedia,
)

SOURCE_ID = "probe_recovery_source"


def build_source() -> fx.ProbePersona:
    source = fx.ProbePersona(SOURCE_ID)
    source.seed(
        core_memories=("持ち出し元のコア記憶 A", "持ち出し元のコア記憶 B"),
        trashed_core_memory=None, knowledge_title="持ち出し元の知識ページ",
        with_fragment=False, with_desk=False, with_chronicle=False, with_theme=False,
    )
    return source


def make_broken(persona_id: str, source: fx.ProbePersona):
    """書き出し→取り込みで、metadata を失ったコア記憶の行がある状態を作る。"""
    target = fx.ProbePersona(persona_id)
    target.seed(
        core_memories=("取り込み先にもとからあった記憶",), trashed_core_memory=None,
        knowledge_title="取り込み先の知識ページ", with_fragment=False,
        with_desk=False, with_theme=False,
    )
    manager = fx.make_manager(source, target)
    exported = export_memopedia(source.persona_id, manager=manager)
    import_memopedia(persona_id, body=exported, clear=False, manager=manager)

    with target.adapter._db_lock:
        broken = [
            {"id": r[0], "title": r[1], "content": r[2], "metadata": r[3]}
            for r in target.adapter.conn.execute(
                "SELECT id, title, content, metadata FROM memopedia_pages "
                "WHERE parent_id = 'root_core' AND category = 'core' "
                "AND metadata IS NULL ORDER BY title"
            ).fetchall()
        ]
    return target, manager, broken


def main() -> dict:
    source = build_source()
    out: dict = {
        "実験": "exp6_recovery — 壊れたあと自力で戻せるか",
        "隔離ホーム": str(HOME),
        "壊し方": (
            "健全なペルソナから export_memopedia で書き出し、別のペルソナへ "
            "import_memopedia(clear=False) で取り込む"
        ),
        "手当て": [],
    }

    # --- 0. 壊れた行に何が残っているか ---
    target, manager, broken = make_broken("probe_recovery_look", source)
    out["壊れた行の中身"] = broken
    out["壊れた直後の読み取り"] = target.reads()
    out["壊れた直後の頭のコア記憶の章"] = target.head_core_memory()
    target.close()

    # --- 1. 手帳の木に見えるか ---
    target, manager, broken = make_broken("probe_recovery_tree", source)
    tree = fx.capture(lambda: get_memopedia_tree(target.persona_id, manager=manager))
    visible: dict = {}
    if isinstance(tree, dict):
        visible = {
            "画面に出るカテゴリ": [c["key"] for c in tree.get("categories", [])],
            "木に出るページの表題": {
                key: [p.get("title") for p in pages]
                for key, pages in tree.items()
                if key != "categories" and isinstance(pages, list)
            },
        }
    out["手当て"].append({
        "手当て": "1. 手帳の木 (GET /people/{id}/memopedia/tree) に壊れた行は見えるか",
        "結果": visible or str(tree)[:400],
    })
    target.close()

    # --- 2. 手帳の 1 件削除で取り除けるか ---
    target, manager, broken = make_broken("probe_recovery_single", source)
    deletes = []
    for row in broken:
        deletes.append({
            "消そうとした行": row["title"],
            "応答": fx.capture(
                lambda rid=row["id"]: delete_memopedia_page(
                    target.persona_id, rid, manager=manager,
                )
            ),
        })
    out["手当て"].append({
        "手当て": "2. 手帳の 1 件削除 (DELETE .../memopedia/pages/{page_id}) で取り除けるか",
        "結果": deletes,
        "そのあとの読み取り": target.reads(),
        "そのあとの頭のコア記憶の章": target.head_core_memory(),
    })
    target.close()

    # --- 3. コア記憶の 1 件削除で取り除けるか ---
    target, manager, broken = make_broken("probe_recovery_coreapi", source)
    out["手当て"].append({
        "手当て": "3. コア記憶の 1 件削除 (DELETE .../core-memory/{memory_id}) で取り除けるか",
        "core:1 を指定した応答": fx.capture(
            lambda: delete_core_memory_item(
                target.persona_id, 1, manager=manager,
            ).model_dump()
        ),
        "core:2 を指定した応答": fx.capture(
            lambda: delete_core_memory_item(
                target.persona_id, 2, manager=manager,
            ).model_dump()
        ),
        "そのあとの読み取り": target.reads(),
    })
    target.close()

    # --- 4. もう一度全削除を押すとどうなるか ---
    target, manager, broken = make_broken("probe_recovery_clear", source)
    out["手当て"].append({
        "手当て": "4. もう一度「Memopedia 全削除」を押す",
        "API の応答 (原文)": delete_all_memopedia_pages(
            target.persona_id, manager=manager,
        ),
        "そのあとの読み取り": target.reads(),
        "そのあとの頭のコア記憶の章": target.head_core_memory(),
        "そのあとに残った core の行": [
            {"title": r["title"], "metadata": r["metadata"], "is_deleted": r["is_deleted"]}
            for r in target.rows() if r["category"] == "core"
        ],
    })
    target.close()

    source.close()
    return out


if __name__ == "__main__":
    result = main()
    path = fx.write_result(__file__, result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"\n結果を書きました: {path}")
