"""実験4 — 取り込みの入力がおかしいとき、途中で失敗したときに何が残るか。

観測表の 4 行目 (不正な入力・途中の失敗) に対応する。

**確かめること** (それぞれ別の合成ペルソナで、置き換え取り込み ``clear=True`` を使う)

1. ``pages`` の項目そのものが無い入力 — 先行削除が走るか、応答は何か。
2. ``pages`` が空配列の入力 — 同上。
3. 取り込める行が 1 つも無い入力 (中身が全部 Chronicle) — 先行削除だけ走らないか。
4. 一部の行が作成に失敗する入力 — 失敗した行が応答の件数と画面にどう現れるか。
5. 既にある識別子と同じ行を含む入力 (``clear=False``) — 衝突した行の扱い。

**確かめないこと**

- 実際の HTTP と、FastAPI のリクエスト検証 (``body: dict`` なので JSON であれば通る)。
  壊れた JSON ファイルを選んだときの挙動は画面側の話で、ここでは見ていない。
- 途中でプロセスが落ちた場合や、DB ファイルが壊れている場合。
- バックエンドの起動、ペルソナの発話、LLM の呼び出し。

実行: ``<repo>/.venv/Scripts/python.exe exp4_invalid_and_partial_failure.py``
(日本語が化けるときは ``PYTHONIOENCODING=utf-8`` を付ける)
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import memopedia_fixture as fx  # noqa: E402

HOME = fx.new_world("exp4_invalid_and_partial_failure")

from api.routes.people.memopedia import import_memopedia  # noqa: E402


def _titles(observation: dict) -> list:
    """生きているページの表題 (カテゴリ付き) を並べる。"""
    return [
        f"[{r['category']}] {r['title']}"
        for r in observation["保存表の行"] if not r["is_deleted"]
    ]


def _scene(name: str, persona_id: str, body, *, clear: bool, note: str) -> dict:
    persona = fx.ProbePersona(persona_id)
    persona.seed(
        trashed_core_memory=None, with_fragment=False, with_desk=False,
        with_theme=False,
    )
    manager = fx.make_manager(persona)

    before = persona.observe()
    result = fx.capture(
        lambda: import_memopedia(persona_id, body=body, clear=clear, manager=manager)
    )
    after = persona.observe()

    out = {
        "場面": name,
        "この入力で見たいこと": note,
        "渡した入力": body,
        "clear": clear,
        "API の応答 (原文)": result,
        "生きているページ (前)": _titles(before),
        "生きているページ (後)": _titles(after),
        "コア記憶の一覧 (前)": before["正規の読み取り"]["コア記憶の一覧 (list_core_memories)"],
        "コア記憶の一覧 (後)": after["正規の読み取り"]["コア記憶の一覧 (list_core_memories)"],
        "あらすじの一覧 (後)": after["正規の読み取り"]["あらすじの一覧 (get_all_entries_ordered)"],
    }
    persona.close()
    return out


def main() -> dict:
    out: dict = {
        "実験": "exp4_invalid_and_partial_failure — 不正な入力と途中の失敗",
        "隔離ホーム": str(HOME),
        "呼んだもの": (
            "api.routes.people.memopedia.import_memopedia"
            "(<ペルソナ>, body=<各場面の入力>, clear=<各場面>, manager=<偽 manager>)"
        ),
        "場面": [],
    }

    out["場面"].append(_scene(
        "1. pages の項目が無い入力", "probe_bad_nopages",
        {"version": 1},
        clear=True,
        note="削除だけ走って何も入らない形にならないか",
    ))
    out["場面"].append(_scene(
        "2. pages が空配列の入力", "probe_bad_empty",
        {"version": 1, "pages": []},
        clear=True,
        note="同上",
    ))
    out["場面"].append(_scene(
        "3. 取り込める行が 1 つも無い入力 (全部 Chronicle)", "probe_bad_allchronicle",
        {"version": 1, "pages": [{
            "id": "legacy-ch-1", "parent_id": None, "title": "旧書き出しの残骸",
            "summary": "", "content": "x", "category": "chronicle",
        }]},
        clear=True,
        note="除外される行しか無いのに、先行削除だけ走らないか",
    ))
    out["場面"].append(_scene(
        "4. 一部の行が作成に失敗する入力", "probe_bad_partial",
        {"version": 1, "pages": [
            {"id": "ok-1", "parent_id": None, "title": "入る行",
             "summary": "", "content": "本文", "category": "terms"},
            # title に文字列でない値を置くと、行の作成が SQLite の束縛で失敗する。
            {"id": "ng-1", "parent_id": None, "title": {"壊れた": "表題"},
             "summary": "", "content": "本文", "category": "terms"},
            {"id": "ok-2", "parent_id": None, "title": "これも入る行",
             "summary": "", "content": "本文", "category": "terms"},
        ]},
        clear=True,
        note="失敗した行が応答の件数と利用者への表示にどう現れるか",
    ))
    out["場面"].append(_scene(
        "5. 既にある識別子と同じ行を含む入力", "probe_bad_conflict",
        {"version": 1, "pages": [
            {"id": "root_terms", "parent_id": None, "title": "用語を上書きしたい",
             "summary": "", "content": "上書き本文", "category": "terms"},
            {"id": "new-1", "parent_id": None, "title": "新しい行",
             "summary": "", "content": "本文", "category": "terms"},
        ]},
        clear=False,
        note="衝突した行が黙って落ちるか、上書きされるか",
    ))
    return out


if __name__ == "__main__":
    result = main()
    path = fx.write_result(__file__, result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"\n結果を書きました: {path}")
