"""実験3 — 「インポート前に既存ページを削除」にチェックを入れて取り込むと何が起きるか。

観測表の 3 行目 (既存ページを置き換えて取り込み) に対応する。

**確かめること**

- ``POST .../memopedia/import?clear=true`` の実装 (``import_memopedia`` の ``clear=True``) が、
  取り込みの前に何を消すか — 画面のラベル「インポート前に既存ページを削除」が指す範囲と、
  実際に消える範囲の差。
- 取り込みから除外される記憶 (Chronicle) が、先行削除でも除外されるか。
- 取り込みに巻き込まれる記憶 (コア記憶) が、先行削除でどうなるか。
- 一連のあと、正規の読み取りとペルソナの頭のコア記憶の章が何を返すか。

**確かめないこと**

- 実際の HTTP と、画面のチェックボックス 2 段 (``importClear`` と ``confirmImportClear``) の操作。
  ここではルート関数に ``clear=True`` を直接渡している。
- バックエンドの起動、ペルソナの発話、LLM の呼び出し。
- 埋め込み検索の結果 (埋め込みは 0 ベクトルの偽物に差し替えてある)。

実行: ``<repo>/.venv/Scripts/python.exe exp3_replace_import.py``
(日本語が化けるときは ``PYTHONIOENCODING=utf-8`` を付ける)
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import memopedia_fixture as fx  # noqa: E402

HOME = fx.new_world("exp3_replace_import")

from api.routes.people.memopedia import export_memopedia, import_memopedia  # noqa: E402


def main() -> dict:
    source = fx.ProbePersona("probe_replace_source")
    target = fx.ProbePersona("probe_replace_target")
    manager = fx.make_manager(source, target)

    out: dict = {
        "実験": "exp3_replace_import — 置き換え取り込みが何を先に消すか",
        "隔離ホーム": str(HOME),
        "取り込み先の記憶 DB": target.db_path,
    }
    source.seed(
        core_memories=("持ち出し元のコア記憶",),
        trashed_core_memory=None,
        knowledge_title="持ち出し元の知識ページ",
        with_fragment=False, with_desk=False, with_chronicle=False, with_theme=False,
    )
    out["取り込み先に用意したもの"] = target.seed()

    exported = export_memopedia(source.persona_id, manager=manager)
    out["取り込むファイルの中身"] = [
        {"title": p.get("title"), "category": p.get("category")}
        for p in exported.get("pages", [])
    ]

    out["取り込み前"] = target.observe()
    out["呼んだもの"] = (
        "api.routes.people.memopedia.import_memopedia"
        '("probe_replace_target", body=<書き出しそのもの>, clear=True,'
        " manager=<偽 manager>)"
    )
    out["取り込みの応答 (原文)"] = import_memopedia(
        target.persona_id, body=exported, clear=True, manager=manager,
    )
    out["取り込み後"] = target.observe()

    def titles(observation: dict, category: str) -> list:
        return [
            r["title"] for r in observation["保存表の行"]
            if r["category"] == category
        ]

    out["先行削除で消えたもの・残ったもの"] = {
        category: {
            "前": titles(out["取り込み前"], category),
            "後": titles(out["取り込み後"], category),
        }
        for category in ("terms", "theme", "core", "chronicle")
    }
    out["読み取りの前後"] = {
        "コア記憶の一覧": {
            "前": out["取り込み前"]["正規の読み取り"]["コア記憶の一覧 (list_core_memories)"],
            "後": out["取り込み後"]["正規の読み取り"]["コア記憶の一覧 (list_core_memories)"],
        },
        "コア記憶のごみ箱": {
            "前": out["取り込み前"]["正規の読み取り"]["コア記憶のごみ箱 (list_deleted_core_memories)"],
            "後": out["取り込み後"]["正規の読み取り"]["コア記憶のごみ箱 (list_deleted_core_memories)"],
        },
        "あらすじの一覧": {
            "前": out["取り込み前"]["正規の読み取り"]["あらすじの一覧 (get_all_entries_ordered)"],
            "後": out["取り込み後"]["正規の読み取り"]["あらすじの一覧 (get_all_entries_ordered)"],
        },
        "頭のコア記憶の章": {
            "前": out["取り込み前"]["頭のコア記憶の章"],
            "後": out["取り込み後"]["頭のコア記憶の章"],
        },
    }

    source.close()
    target.close()
    return out


if __name__ == "__main__":
    result = main()
    path = fx.write_result(__file__, result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"\n結果を書きました: {path}")
