"""実験1 — 画面の「Memopedia 全削除」を押したとき、実際に何が消えるか。

観測表の 1 行目 (Memopedia 全削除) に対応する。

**確かめること**

- ``DELETE /people/{id}/memopedia/pages`` の実装 (``delete_all_memopedia_pages``) を
  呼んだあと、手帳の通常ページ・コア記憶 (生存)・コア記憶 (ごみ箱)・Chronicle・
  テーマページのそれぞれが残るか消えるか。
- 利用者が画面で見る数字 (``deleted_count``) と応答の原文。
- 消えたあと、正規の読み取り (``list_core_memories`` / ``get_all_entries_ordered``) と
  ペルソナの頭のコア記憶の章が何を返すか。
- ページに紐づく付随データ (Fragment・ページの埋め込み・編集来歴・机に開いた参照) の
  どれが道連れになり、どれが残るか。

**確かめないこと**

- 実際の HTTP リクエスト。ルート関数を Python から直接呼んでいるので、FastAPI の
  ルーティング・依存解決・レスポンスのシリアライズは通っていない。
- 画面の操作 (確認ボタンの 2 段階クリック) と、画面が出す文言。
- バックエンドの起動、ペルソナの発話、LLM の呼び出し。
- 埋め込み検索の結果 (埋め込みは 0 ベクトルの偽物に差し替えてある)。

実行: ``<repo>/.venv/Scripts/python.exe exp1_delete_all.py``
(日本語が化けるときは ``PYTHONIOENCODING=utf-8`` を付ける)
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import memopedia_fixture as fx  # noqa: E402

HOME = fx.new_world("exp1_delete_all")

from api.routes.people.memopedia import delete_all_memopedia_pages  # noqa: E402


def main() -> dict:
    persona = fx.ProbePersona("probe_delete_all")
    made = persona.seed()

    out: dict = {
        "実験": "exp1_delete_all — 手帳の全削除が何を巻き込むか",
        "隔離ホーム": str(HOME),
        "記憶 DB": persona.db_path,
        "用意したもの": made,
    }
    out["操作前"] = persona.observe()
    out["呼んだもの"] = (
        "api.routes.people.memopedia.delete_all_memopedia_pages"
        '("probe_delete_all", manager=<偽 manager>)'
    )
    out["API の応答 (原文)"] = delete_all_memopedia_pages(
        persona.persona_id, manager=fx.make_manager(persona),
    )
    out["操作後"] = persona.observe()

    before = out["操作前"]["正規の読み取り"]
    after = out["操作後"]["正規の読み取り"]
    out["読み取りの前後"] = {
        "コア記憶の一覧": {
            "前": before["コア記憶の一覧 (list_core_memories)"],
            "後": after["コア記憶の一覧 (list_core_memories)"],
        },
        "コア記憶のごみ箱": {
            "前": before["コア記憶のごみ箱 (list_deleted_core_memories)"],
            "後": after["コア記憶のごみ箱 (list_deleted_core_memories)"],
        },
        "あらすじの一覧": {
            "前": before["あらすじの一覧 (get_all_entries_ordered)"],
            "後": after["あらすじの一覧 (get_all_entries_ordered)"],
        },
    }
    persona.close()
    return out


if __name__ == "__main__":
    result = main()
    path = fx.write_result(__file__, result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"\n結果を書きました: {path}")
